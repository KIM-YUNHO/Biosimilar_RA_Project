"""ra-answer: answer a question from the index, with citations.

  ra-answer "질문" [--k 10] [--mode routed|open] [--rerank] [--depth 30] [--no-glossary]
                   [--out report.json] [--dry-run]

Retrieval is that of `ra-index search` (ra_chunk.retrieve); generation is one model call
(ra_answer.generate). The model and its budget come from the environment or .env (ra_answer.llm).
--dry-run builds the prompt and reports the evidence and token counts without calling the model.
The full report (evidence, claims with quotes, rejected claims, gaps, usage) is written as JSON
to --out (default data/answers/<time>.json).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ra_ingest.settings import ROOT, default_db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ra-answer")
    ap.add_argument("question")
    ap.add_argument("--db", default=default_db(), help="SQLAlchemy URL (default: RA_DB or SQLite data/ra.db)")
    ap.add_argument("--k", type=int, default=10, help="hits per planned search")
    ap.add_argument("--mode", default="routed", choices=["routed", "open"])
    ap.add_argument("--rerank", action="store_true", help="bge-reranker-v2-m3 over --depth candidates")
    ap.add_argument("--depth", type=int, default=30)
    ap.add_argument("--no-glossary", action="store_true")
    ap.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--max-claims", type=int, default=8)
    ap.add_argument("--out", type=Path, help="report JSON (default data/answers/<time>.json)")
    ap.add_argument("--dry-run", action="store_true", help="build the prompt only; no model call")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from sqlalchemy import select

    from ra_chunk.cli import _device, _embedder, _store
    from ra_chunk.query import Glossary, Router
    from ra_chunk.retrieve import Retriever
    from ra_ingest.db import DocumentRow

    from .generate import answer
    from .llm import OpenAIChat

    client = OpenAIChat()
    router = Router(ROOT / "config" / "products.yaml")
    st = _store(a.db)
    with st.session() as s:
        rr = None
        if a.rerank:
            from ra_chunk.rerank import Reranker

            rr = Reranker(device=_device(a.device), threads=a.threads)
        ret = Retriever(s, _embedder(a), router,
                        None if a.no_glossary else Glossary(ROOT / "config" / "query_glossary.yaml"),
                        reranker=rr, depth=a.depth)
        hits = ret.retrieve(a.question, k=a.k, mode=a.mode)
        ids = {h.chunk.document_id for h in hits}
        titles = dict(s.execute(select(DocumentRow.id, DocumentRow.title).where(DocumentRow.id.in_(ids))).all()) \
            if ids else {}
    route = asdict(router.route(a.question))
    names = {p: router.display.get(p, [p]) for p in router.biosimilars}
    report = answer(a.question, hits, client, titles=titles, names=names, route=route,
                    max_claims=a.max_claims, dry_run=a.dry_run)
    report["retrieval"] = {"k": a.k, "mode": a.mode, "rerank": a.rerank, "depth": a.depth,
                           "glossary": not a.no_glossary, "hits": len(hits)}

    out = a.out or ROOT / "data" / "answers" / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if report["status"] == "dry_run":
        print(f"hits {len(hits)} -> evidence blocks {len(report['evidence'])} "
              f"(omitted {len(report['omitted'])}), estimated prompt {report['estimated_prompt_tokens']} "
              f"of {report['input_token_budget']} tokens, model {report['model']}")
        for e in report["evidence"]:
            print(f"  {e['id']} doc {e['document_id']} {e['agency']}/{e['program_id'] or '-'}/{e['doc_type']} "
                  f"p.{','.join(map(str, e['pages']))} {' > '.join(e['section_path'][-2:])}")
    elif report["status"] == "error":
        print(f"error: {report['error']}", file=sys.stderr)
    else:
        print(report["answer_ko"])
    print(f"\nreport: {out}")
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
