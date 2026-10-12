"""Check that the index (ra-index, SQLite or PostgreSQL + pgvector) returns what the retrieval
evaluation measured: for every gold question and every search of its plan
(ra_chunk.search.search_plan), the in-memory search of compare_chunking.py (V2, Korean +
glossary) and ChunkIndex.search must give the same chunks in the same order.

Run after indexing the fixed corpus:
  ra-index run --doc-id ...            (or: ra-index run  for everything)
  python eval/retrieval/check_index_parity.py [--db ...] [--k 10]

Exit code 0 when every list matches. Score ties between chunks can legitimately swap order;
those are reported as "tie" and not counted as mismatches.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import compare_chunking as C  # noqa: E402
from ra_chunk.query import Glossary, Router  # noqa: E402
from ra_chunk.search import ChunkIndex, search_plan, sparse_score  # noqa: E402


def compare(s, gold, router, glossary, emb, docs, k: int, log=print) -> dict:
    from sqlalchemy import select

    from ra_chunk.store import IndexChunkRow

    chunks, _ = C.build("V2", docs, emb.count_tokens)
    enc = emb.encode([c["embed"] for c in chunks])
    vecs = {"dense": np.stack([e[0] for e in enc]).astype(np.float32), "sparse": [e[1] for e in enc]}
    corpus = sorted({c["doc_id"] for c in chunks})
    idx = ChunkIndex(s, emb.name)
    keys = {r.id: (r.document_id, r.chunk_key) for r in s.scalars(
        select(IndexChunkRow).where(IndexChunkRow.active.is_(True), IndexChunkRow.embed_model == emb.name,
                                    IndexChunkRow.document_id.in_(corpus)))}
    pos = {(c["doc_id"], c["id"]): i for i, c in enumerate(chunks)}
    missing = [k_ for k_ in pos if k_ not in set(keys.values())]
    cols = C.chunk_columns(chunks)
    out = {"searches": 0, "match": 0, "tie": 0, "mismatch": 0, "chunks_not_indexed": len(missing)}
    for q in gold["questions"]:
        qv = emb.encode([glossary.expand(q["question"])])[0]
        runs = []
        for ps in search_plan(router.route(q["question"]), router.references):
            for f in (ps.filters, ps.types):
                if f is not None:
                    runs.append((ps.product, f.mask(cols), replace(f, document_ids=corpus)))
        for p, mask, f in runs:
            want = [(chunks[i]["doc_id"], chunks[i]["id"]) for i in C.search(qv, chunks, vecs, mask, k)]
            got = [keys.get(h.chunk_id) for h in idx.search(qv, f, k)]
            out["searches"] += 1
            if got == want:
                out["match"] += 1
                continue

            def score(key):
                i = pos.get(key)
                return None if i is None else round(float(vecs["dense"][i] @ qv[0].astype(np.float32))
                                                    + C.W_SPARSE * sparse_score(qv[1], vecs["sparse"][i]), 4)
            if sorted(got, key=str) == sorted(want, key=str) or \
                    [score(x) for x in got] == [score(x) for x in want]:
                out["tie"] += 1
            else:
                out["mismatch"] += 1
                log(f"{q['id']} {p or '-'}: index {got[:5]} vs eval {want[:5]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="default: RA_DB or SQLite data/ra.db")
    ap.add_argument("--store", default=None)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--corpus", default=str(ROOT / "eval" / "retrieval" / "corpus_docs.txt"))
    ap.add_argument("--threads", type=int, default=None)
    a = ap.parse_args()

    from ra_chunk.cli import _store
    from ra_chunk.embed import BgeM3
    from ra_ingest.settings import default_db

    db = a.db or default_db()
    gold = yaml.safe_load((ROOT / "eval" / "retrieval" / "questions.yaml").read_text(encoding="utf-8"))
    only = {int(x) for x in Path(a.corpus).read_text().split() if x.isdigit()}
    docs = C.load_docs(db, only, a.store)
    emb = BgeM3(cache=ROOT / "data" / "emb_cache" / "bge-m3.pkl", threads=a.threads)
    with _store(db).session() as s:
        res = compare(s, gold, Router(ROOT / "config" / "products.yaml"),
                      Glossary(ROOT / "config" / "query_glossary.yaml"), emb, docs, a.k)
    print(res)
    return 0 if res["mismatch"] == 0 and res["chunks_not_indexed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
