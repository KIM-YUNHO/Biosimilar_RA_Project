"""ra-index: chunk, embed and index preprocessed documents; search the index.

  ra-index run    [--product X ...] [--agencies ema,fda,hc] [--doc-id N ...] [--guidance] [--force]
  ra-index status                                       indexed documents and chunks per model
  ra-index search "질문" [--k 10] [--rerank] [--mode routed|open] [--no-glossary]

Database and object store as for ra-ingest / ra-prep (--db / RA_DB, --store / RA_STORE).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from ra_ingest.settings import ROOT, default_db, default_store


def _store(url: str):
    from ra_prep import store as _prep  # noqa: F401  (prep tables)

    from . import store as _index  # noqa: F401  (index tables)
    from ra_ingest.db import Store

    return Store(url)


def _device(name: str) -> str:
    if name != "auto":
        return name
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _embedder(a):
    from .embed import BgeM3

    return BgeM3(device=_device(a.device), threads=a.threads)


def _program_ids(products: list[str] | None) -> list[str] | None:
    if not products:
        return None
    from ra_ingest.registry import Registry

    reg = Registry.load(ROOT / "config" / "products.yaml")
    ids = []
    for p in products:
        prog = reg.find_program(p)
        if prog is None:
            raise SystemExit(f"unknown product: {p}")
        ids.append(prog.id)
    return ids


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ra-index")
    ap.add_argument("--db", default=default_db(), help="SQLAlchemy URL (default: RA_DB or SQLite data/ra.db)")
    ap.add_argument("--store", default=default_store(),
                    help="object store: directory or s3://bucket/prefix (default: RA_STORE or data/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="index the latest parse of each selected document")
    r.add_argument("--product", action="append", help="dev code or brand name (repeatable)")
    r.add_argument("--agencies", help="comma list: ema,fda,hc,guidance")
    r.add_argument("--doc-id", type=int, action="append")
    r.add_argument("--guidance", action="store_true", help="guidance documents only")
    r.add_argument("--force", action="store_true", help="index again even if unchanged")
    sub.add_parser("status")
    q = sub.add_parser("search", help="retrieve chunks for a question")
    q.add_argument("question")
    q.add_argument("--k", type=int, default=10)
    q.add_argument("--mode", default="routed", choices=["routed", "open"])
    q.add_argument("--rerank", action="store_true", help="bge-reranker-v2-m3 over --depth candidates")
    q.add_argument("--depth", type=int, default=30)
    q.add_argument("--no-glossary", action="store_true")
    q.add_argument("--width", type=int, default=300, help="characters of each hit to print")
    for p in (r, q):
        p.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
        p.add_argument("--threads", type=int, default=None)
    a = ap.parse_args(argv)

    st = _store(a.db)
    with st.session() as s:
        if a.cmd == "run":
            from ra_ingest.objstore import open_store

            from .index import candidates, run

            cands = candidates(s, program_ids=_program_ids(a.product),
                               agencies=a.agencies.split(",") if a.agencies else None,
                               document_ids=a.doc_id, guidance=a.guidance)
            c = Counter(x.action for x in cands)
            print(f"candidates: {dict(c)}")
            for x in cands:
                if x.action == "not_parsed":
                    print(f"  not parsed: doc {x.document_id} ({x.reason})")
            res = run(s, cands, _embedder(a), open_store(a.store), force=a.force)
            print(f"done: {res}")
            return 1 if res.failed else 0
        if a.cmd == "status":
            from sqlalchemy import func, select

            from .store import IndexChunkRow, IndexDocumentRow

            rows = s.execute(select(IndexDocumentRow.embed_model, IndexDocumentRow.chunker,
                                    IndexDocumentRow.status, IndexDocumentRow.retired_at.is_(None),
                                    func.count()).group_by(IndexDocumentRow.embed_model,
                                                           IndexDocumentRow.chunker,
                                                           IndexDocumentRow.status,
                                                           IndexDocumentRow.retired_at.is_(None))).all()
            for model, chunker, status, active, n in sorted(rows, key=lambda r: tuple(map(str, r))):
                print(f"{model:24} {chunker:14} {status:7} {'active' if active else 'retired':8} {n} docs")
            for model, n in s.execute(select(IndexChunkRow.embed_model, func.count())
                                      .where(IndexChunkRow.active.is_(True))
                                      .group_by(IndexChunkRow.embed_model)):
                print(f"{model}: {n} active chunks")
            return 0
        if a.cmd == "search":
            from .query import Glossary, Router
            from .retrieve import Retriever

            rr = None
            if a.rerank:
                from .rerank import Reranker

                rr = Reranker(device=_device(a.device), threads=a.threads)
            ret = Retriever(s, _embedder(a), Router(ROOT / "config" / "products.yaml"),
                            None if a.no_glossary else Glossary(ROOT / "config" / "query_glossary.yaml"),
                            reranker=rr, depth=a.depth)
            for i, h in enumerate(ret.retrieve(a.question, k=a.k, mode=a.mode), 1):
                c = h.chunk
                where = f"doc {c.document_id} p.{','.join(map(str, c.pages))}"
                print(f"{i:>2}. [{h.score:.3f}] {c.agency}/{c.program_id or '-'}/{c.doc_type} {where} "
                      f"{' > '.join(c.section_path[-2:])}{' [needs review]' if c.needs_review else ''}")
                print("    " + " ".join(c.text.split())[: a.width])
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
