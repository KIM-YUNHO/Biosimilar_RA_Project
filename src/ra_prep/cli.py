"""ra-prep: preprocessing of documents stored by ra-ingest.

  ra-prep select  [--product X ...] [--agencies ema,fda,hc]     what would be processed
  ra-prep run     [--product X ...] [--agencies ...] [--doc-id N ...] [--force]
  ra-prep status                                                   counts and QA summary
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = f"sqlite:///{ROOT / 'data' / 'ra.db'}"


def _store(url: str):
    from . import store as _tables  # noqa: F401  (registers the prep tables)
    from ra_ingest.db import Store

    return Store(url)


def _programs(s, products: list[str] | None) -> list[str] | None:
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


def _selections(s, a):
    from .select import select_documents

    sels = select_documents(s, program_ids=_programs(s, a.product),
                            agencies=a.agencies.split(",") if a.agencies else None,
                            include_superseded_labels=a.all_labels)
    if a.doc_id:
        keep = set(a.doc_id)
        sels = [x for x in sels if x.document_id in keep]
    return sels


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ra-prep")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("select", "run"):
        p = sub.add_parser(name)
        p.add_argument("--product", action="append", help="dev code or brand name (repeatable)")
        p.add_argument("--agencies", help="comma list: ema,fda,hc,guidance")
        p.add_argument("--doc-id", type=int, action="append")
        p.add_argument("--all-labels", action="store_true", help="also older label versions")
        if name == "run":
            p.add_argument("--out", default=str(ROOT / "data" / "parsed"))
            p.add_argument("--force", action="store_true")
            p.add_argument("--threads", type=int, default=4)
            p.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
            p.add_argument("--artifacts", help="offline model cache (docling-tools models download)")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING)

    st = _store(a.db)
    with st.session() as s:
        if a.cmd == "select":
            sels = _selections(s, a)
            c = Counter((x.action, x.reason.split(" (")[0] or "-") for x in sels)
            for (act, why), n in sorted(c.items()):
                print(f"{act:8} {why:22} {n}")
            return 0
        if a.cmd == "run":
            from .extract import Extractor
            from .pipeline import run

            ex = Extractor(num_threads=a.threads, device=a.device, artifacts_path=a.artifacts)
            res = run(s, _selections(s, a), a.out, ex, force=a.force)
            rate = res["seconds"] / res["pages"] if res["pages"] else 0
            print(f"done: {res} ({rate:.2f} s/page)")
            return 1 if res["failed"] else 0
        if a.cmd == "status":
            from sqlalchemy import select

            from .store import PrepDocumentRow

            rows = list(s.scalars(select(PrepDocumentRow)))
            c = Counter((r.engine, r.status) for r in rows)
            for (eng, stt), n in sorted(c.items()):
                print(f"{eng:60} {stt:8} {n}")
            ok = [r for r in rows if r.status == "ok"]
            if ok:
                pages = sum(r.pages or 0 for r in ok)
                secs = sum(r.seconds or 0 for r in ok)
                rv = sum(r.qa.get("needs_review_blocks", 0) for r in ok)
                print(f"parsed: {len(ok)} docs, {pages} pages, {secs / max(pages, 1):.2f} s/page, "
                      f"needs_review blocks {rv}")
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
