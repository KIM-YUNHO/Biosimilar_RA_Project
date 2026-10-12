"""Export the retrieval index (chosen chunking V2m + bge-m3 vectors) and the preprocessing
outputs as plain files another program can load without this repository.

  python scripts/export_index.py --out data/export          # all parsed documents
  python scripts/export_index.py --corpus eval/retrieval/corpus_docs_v2.txt

Writes
  documents.csv        one row per document: id, agency, kind, type, program, title, date, url,
                       sha256, raw file, parsed file
  chunks.jsonl         one chunk per line, in vector order: id, doc_id, kind, text, embed_text
                       (context header + text, what was embedded), parent_id, pages,
                       section_path, speaker, program, agency, doc_type, guidance
  parents.jsonl        parent id -> full section / table text (what the answer step reads)
  dense_fp16.npy       bge-m3 dense vectors (float16, L2-normalised), row i = chunks.jsonl line i
  sparse.jsonl         bge-m3 lexical weights per chunk: {"token_id": weight}
  manifest.json        model, chunker settings, engine versions, counts
Embeddings come from the cache (data/emb_cache); texts not in it are embedded now.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval" / "retrieval"))

import compare_chunking as C  # noqa: E402
from ra_chunk import CHUNKER_VERSION  # noqa: E402
from ra_chunk.embed import MODEL, BgeM3  # noqa: E402
from ra_prep import ENGINE_VERSION  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=f"sqlite:///{ROOT / 'data' / 'ra.db'}")
    ap.add_argument("--corpus", default="all", help="file of document ids, or 'all'")
    ap.add_argument("--variant", default="V2m")
    ap.add_argument("--out", default=str(ROOT / "data" / "export"))
    ap.add_argument("--threads", type=int, default=None)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    only = None if a.corpus == "all" else {int(x) for x in Path(a.corpus).read_text().split() if x.isdigit()}
    docs = C.load_docs(a.db, only)
    model = BgeM3(cache=ROOT / "data" / "emb_cache" / "bge-m3.pkl", threads=a.threads)
    chunks, parents = C.build(a.variant, docs, model.count_tokens)
    enc = model.encode([c["embed"] for c in chunks], log=print)

    with (out / "documents.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "agency", "doc_kind", "doc_type", "program_id", "title", "decision_date",
                    "doc_date", "source_url", "sha256", "raw_file", "parsed_file"])
        for doc, meta in sorted(docs, key=lambda d: d[0]["document_id"]):
            raw = meta.get("storage_path") or ""
            w.writerow([meta["doc_id"], meta["agency"], meta["doc_kind"], meta["doc_type"],
                        meta.get("program_id") or "", meta.get("title") or "", meta.get("decision_date") or "",
                        meta.get("doc_date") or "", meta.get("source_url") or "", doc["sha256"],
                        str(Path(raw).relative_to(ROOT / "data")) if raw.startswith(str(ROOT / "data")) else raw,
                        f"parsed/{doc['sha256']}.json"])

    with (out / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps({"id": c["id"], "doc_id": c["doc_id"], "kind": c["kind"], "text": c["text"],
                                "embed_text": c["embed"], "parent_id": c["parent"], "pages": c["pages"],
                                "section_path": c.get("section_path"), "program": c["program"],
                                "agency": c["agency"], "doc_type": c.get("doc_type"),
                                "guidance": c["guidance"]}, ensure_ascii=False) + "\n")
    used = {c["parent"] for c in chunks if c["parent"]}
    with (out / "parents.jsonl").open("w", encoding="utf-8") as f:
        for pid in sorted(used):
            f.write(json.dumps({"id": pid, "text": parents[pid]}, ensure_ascii=False) + "\n")
    np.save(out / "dense_fp16.npy", np.stack([e[0] for e in enc]).astype(np.float16))
    with (out / "sparse.jsonl").open("w", encoding="utf-8") as f:
        for e in enc:
            f.write(json.dumps({str(k): round(v, 5) for k, v in e[1].items()}) + "\n")

    manifest = {
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "embedding_model": MODEL, "dense_dim": int(enc[0][0].shape[0]) if enc else 0,
        "score": "dense cosine + 0.3 * sparse dot product (see eval/retrieval/compare_chunking.py)",
        "chunker": {"version": CHUNKER_VERSION, "variant": a.variant, "settings": C.VARIANTS[a.variant]},
        "preprocessing_engine": ENGINE_VERSION,
        "documents": len(docs), "chunks": len(chunks), "parents": len(used),
        "corpus": a.corpus,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
