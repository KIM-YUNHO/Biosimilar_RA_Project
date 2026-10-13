"""Load an index export (scripts/export_index.py) into the index tables of a database, without the
embedding model.

  python scripts/import_index.py --export path/to/export            # RA_DB / RA_STORE as for ra-index
  python scripts/import_index.py --export path/to/export --db postgresql+psycopg://ra:ra@localhost:5434/ra

What it does
  1. Vectors of the export are looked up by the hash of the embedded text (the same key ra-index
     uses to reuse vectors), so the normal indexing stage runs unchanged: the chunker rebuilds
     the chunks from the parse output in the object store, the export supplies the vectors.
     Rows therefore carry every chunk field (speaker, table caption, study ids, review flag)
     and get the same index key (version sha256, engine, chunker, model, metadata hash) that
     `ra-index run` expects, so a later `ra-index run` skips them.
  2. A document whose parse output is not in the store is taken from the export rows as they
     are (fields the export lacks stay empty: speaker, table_caption, study_ids, needs_review).
  3. Legacy `prep_documents.output_path` values that are not store keys (e.g. a scratch folder)
     are rewritten to `parsed/<sha256>.json`.
  4. Compares the stored chunks of every document with the export (count and text hashes).

Needs the bge-m3 tokenizer (a few MB from huggingface.co) to count tokens when chunking; the
model itself is not loaded.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ra_chunk import index as ix  # noqa: E402
from ra_chunk.store import IndexChunkRow, IndexDocumentRow, IndexParentRow  # noqa: E402
from ra_ingest.db import DocumentRow, Store  # noqa: E402
from ra_ingest.metadata import build_document_metadata  # noqa: E402
from ra_ingest.objstore import open_store  # noqa: E402
from ra_ingest.settings import default_db, default_store  # noqa: E402
from ra_prep import store as _prep_tables  # noqa: E402,F401
from ra_prep.store import PrepDocumentRow  # noqa: E402


class ExportEmbedder:
    """Vectors from an export, keyed by the hash of the embedded text. Counts tokens with the
    model's tokenizer; it never computes a vector."""

    def __init__(self, export: Path, tokenizer: str):
        from transformers import AutoTokenizer

        manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
        self.name = manifest["embedding_model"]
        self.tok = AutoTokenizer.from_pretrained(tokenizer or self.name)
        self.chunks = [json.loads(line) for line in (export / "chunks.jsonl").open(encoding="utf-8")]
        dense = np.load(export / "dense_fp16.npy")
        sparse = [json.loads(line) for line in (export / "sparse.jsonl").open(encoding="utf-8")]
        if not (len(self.chunks) == len(dense) == len(sparse)):
            raise SystemExit(f"export is inconsistent: {len(self.chunks)} chunks, {len(dense)} dense, "
                             f"{len(sparse)} sparse")
        self.vectors: dict[str, tuple[np.ndarray, dict[int, float]]] = {}
        for c, d, sp in zip(self.chunks, dense, sparse):
            c["hash"] = ix.text_hash(c["embed_text"])
            self.vectors[c["hash"]] = (d, {int(k): v for k, v in sp.items()})
        self.manifest = manifest

    def count_tokens(self, text: str) -> int:
        return len(self.tok(text, add_special_tokens=False)["input_ids"])

    def encode(self, texts: list[str]):
        out = []
        for t in texts:
            h = ix.text_hash(t)
            if h not in self.vectors:
                raise KeyError("no vector in the export for a chunk text (chunker or metadata "
                               f"differ from the export): {t[:80]!r}")
            out.append(self.vectors[h])
        return out


def rewrite_output_paths(s, store) -> int:
    """prep_documents.output_path that is not a store key -> parsed/<sha256>.json."""
    n = 0
    for r in s.scalars(select(PrepDocumentRow).where(PrepDocumentRow.output_path.is_not(None))):
        if r.output_path.startswith("parsed/"):
            continue
        r.output_path = f"parsed/{r.sha256}.json"
        n += 1
    s.commit()
    return n


def import_rows(s, cand: ix.Candidate, emb: ExportEmbedder, rows: list[dict], parents: dict[str, str]) -> int:
    """Index a document straight from its export rows (no parse output to chunk)."""
    d = s.get(DocumentRow, cand.document_id)
    meta = build_document_metadata(s, d)
    key = dict(document_id=d.id, sha256=cand.sha256, prep_engine=cand.prep_engine, chunker=ix.CHUNKER_ID,
               embed_model=emb.name, meta_hash=ix.meta_hash(meta))
    if s.scalar(select(IndexDocumentRow).filter_by(**key)) is not None:
        return 0
    row = IndexDocumentRow(**key, status="ok", n_chunks=len(rows), n_embedded=0)
    s.add(row)
    s.flush()
    filt = dict(program_id=meta.get("program_id"), agency=meta.get("agency"),
                jurisdiction=meta.get("jurisdiction"), doc_kind=meta.get("doc_kind"),
                doc_type=meta.get("doc_type"), decision_date=meta.get("decision_date"),
                guidance_status=meta.get("guidance_status"),
                guidance_effective_from=meta.get("guidance_effective_from"),
                guidance_effective_to=meta.get("guidance_effective_to"))
    s.add_all(IndexChunkRow(
        index_document_id=row.id, document_id=d.id, chunk_key=c["id"], kind=c["kind"], text=c["text"],
        embed_text=c["embed_text"], embed_hash=c["hash"], parent_key=c["parent_id"], pages=c["pages"] or [],
        section_path=c["section_path"] or [], embed_model=emb.name, active=True,
        dense=emb.vectors[c["hash"]][0], sparse=emb.vectors[c["hash"]][1], **filt) for c in rows)
    pages: dict[str, set] = defaultdict(set)
    for c in rows:
        if c["parent_id"]:
            pages[c["parent_id"]].update(c["pages"] or [])
    s.add_all(IndexParentRow(index_document_id=row.id, parent_key=pid, text=parents[pid],
                             pages=sorted(p))  # the export has no parent pages: union of its chunks'
              for pid, p in pages.items())
    ix.retire(s, d.id, emb.name, f"replaced by index_document {row.id}", keep=row.id)
    s.commit()
    return len(rows)


def verify(s, emb: ExportEmbedder, document_ids: set[int]) -> list[str]:
    """Stored active chunks vs export rows, per document: same number, same text hashes."""
    want: dict[int, Counter] = defaultdict(Counter)
    for c in emb.chunks:
        if c["doc_id"] in document_ids:
            want[c["doc_id"]][c["hash"]] += 1
    got: dict[int, Counter] = defaultdict(Counter)
    for doc_id, h in s.execute(select(IndexChunkRow.document_id, IndexChunkRow.embed_hash)
                               .where(IndexChunkRow.active.is_(True), IndexChunkRow.embed_model == emb.name)):
        if doc_id in document_ids:
            got[doc_id][h] += 1
    return [f"doc {d}: export {sum(want[d].values())} chunks, stored {sum(got[d].values())}"
            for d in sorted(document_ids) if want[d] != got[d]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export", required=True, help="folder with chunks.jsonl, dense_fp16.npy, ...")
    ap.add_argument("--db", default=default_db())
    ap.add_argument("--store", default=default_store())
    ap.add_argument("--tokenizer", default="", help="tokenizer name or folder (default: the export's model)")
    a = ap.parse_args()
    export = Path(a.export)

    emb = ExportEmbedder(export, a.tokenizer)
    print(f"export: {emb.manifest['chunks']} chunks, {emb.manifest['documents']} documents, "
          f"model {emb.name}, chunker {emb.manifest['chunker']['version']}/{emb.manifest['chunker']['variant']}, "
          f"engine {emb.manifest['preprocessing_engine']}")
    objects = open_store(a.store)
    st = Store(a.db)
    with st.session() as s:
        print(f"output_path rewritten: {rewrite_output_paths(s, objects)}")
        cands = ix.candidates(s)
        print("candidates:", dict(Counter(c.action for c in cands)))
        todo = [c for c in cands if c.action == "index"]
        from_parse = [c for c in todo if c.output_path and objects.exists(c.output_path)]
        from_rows = [c for c in todo if c not in from_parse]
        res = ix.run(s, [c for c in cands if c.action != "index"] + from_parse, emb, objects)
        print(f"indexed from parse outputs: {res.indexed} documents, {res.chunks} chunks, "
              f"failed {res.failed}, not parsed {res.not_parsed}")
        for f in res.failures:
            print("  ", f)
        if from_rows:
            parents = {p["id"]: p["text"] for p in
                       (json.loads(line) for line in (export / "parents.jsonl").open(encoding="utf-8"))}
            by_doc: dict[int, list] = defaultdict(list)
            for c in emb.chunks:
                by_doc[c["doc_id"]].append(c)
            for c in from_rows:
                rows = by_doc.get(c.document_id)
                if not rows:
                    print(f"  doc {c.document_id}: no parse output and no export rows")
                    continue
                n = import_rows(s, c, emb, rows, parents)
                print(f"  doc {c.document_id}: {n} chunks taken from the export rows (no parse output in the store)")
        bad = verify(s, emb, {c.document_id for c in todo})
        total = s.query(IndexChunkRow).filter(IndexChunkRow.active.is_(True)).count()
        print(f"active chunks: {total} (export: {emb.manifest['chunks']}); "
              f"documents differing from the export: {len(bad)}")
        for line in bad:
            print("  ", line)
        return 1 if bad or res.failed or total != emb.manifest["chunks"] else 0


if __name__ == "__main__":
    sys.exit(main())
