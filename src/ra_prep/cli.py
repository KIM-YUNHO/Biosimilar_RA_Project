"""ra-prep: preprocessing of documents stored by ra-ingest.

  ra-prep select  [--product X ...] [--agencies ema,fda,hc]     what would be processed
  ra-prep run     [--product X ...] [--agencies ...] [--doc-types ...] [--guidance]
                  [--doc-id N ...] [--force]
  ra-prep status                                                   counts and QA summary
  ra-prep warmup  [--artifacts DIR]                                model download + smoke test
  ra-prep refix                                                    upgrade stored parses (no re-OCR)
"""
from __future__ import annotations

import argparse
import logging
import os
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
                            include_superseded_labels=a.all_labels,
                            doc_kind="guidance" if a.guidance else None)
    if a.doc_types:
        keep_types = set(a.doc_types.split(","))
        sels = [x for x in sels if x.doc_type in keep_types]
    if a.doc_id:
        keep = set(a.doc_id)
        sels = [x for x in sels if x.document_id in keep]
    return sels


def _warmup(a) -> int:
    """Fetch every model the pipeline uses and run one digital and one image page from the
    gold set, so network or install problems show up in a minute, not hours into a run."""
    import time

    if a.artifacts:
        from docling.utils.model_downloader import download_models

        print(f"downloading Docling models to {a.artifacts} ...")
        download_models(Path(a.artifacts), progress=True, with_code_formula=False,
                        with_picture_classifier=False)
    from .extract import Extractor
    from .pipeline import process_pdf

    ex = Extractor(device=a.device, artifacts_path=a.artifacts)
    gold = ROOT / "eval" / "gold" / "pages"
    for gid, doc_type in (("g01", "assessment_report"), ("g08", "review")):
        t = time.time()
        res = process_pdf(str(gold / f"{gid}.pdf"), doc_type, ex)
        n_tables = sum(1 for b in res["blocks"] if b["type"] == "table")
        print(f"{gid}: route={res['routes'][0]['route']} blocks={len(res['blocks'])} "
              f"tables={n_tables} {time.time() - t:.1f}s")
    print(f"ok - engine {ex.version()}")
    return 0


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
        p.add_argument("--doc-types", help="comma list, e.g. assessment_report,review,sbd")
        p.add_argument("--guidance", action="store_true", help="guidance documents only")
        if name == "run":
            p.add_argument("--out", default=str(ROOT / "data" / "parsed"))
            p.add_argument("--force", action="store_true")
            p.add_argument("--threads", type=int, default=os.cpu_count() or 4,
                           help="CPU threads in total (split across workers)")
            p.add_argument("--workers", type=int, default=1,
                           help="documents processed in parallel (each loads its own models, ~2-3 GB RAM)")
            p.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
            p.add_argument("--artifacts", help="offline model cache (docling-tools models download)")
    sub.add_parser("status")
    rf = sub.add_parser("refix", help="apply newer text repairs/post-processing to stored parses")
    rf.add_argument("--from-engine", default="prep-1+")
    w = sub.add_parser("warmup", help="download models and run two gold pages (check before a long run)")
    w.add_argument("--artifacts", help="also save Docling/RapidOCR models here for offline runs")
    w.add_argument("--device", default="auto")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING)

    if a.cmd == "warmup":
        return _warmup(a)

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
            res = run(s, _selections(s, a), a.out, ex, force=a.force, workers=a.workers)
            rate = res["wall_seconds"] / res["pages"] if res["pages"] else 0
            print(f"done: {res} ({rate:.2f} s/page wall clock)")
            return 1 if res["failed"] else 0
        if a.cmd == "refix":
            from sqlalchemy import select

            from . import ENGINE_VERSION
            from .pipeline import refix
            from .store import PrepDocumentRow

            engines = {r for r in s.scalars(select(PrepDocumentRow.engine))
                       if r.startswith(a.from_engine) and not r.startswith(ENGINE_VERSION + "+")}
            for eng in sorted(engines):
                old = eng.split("+", 1)[0]
                print(eng, "->", ENGINE_VERSION, refix(s, eng, eng.replace(old, ENGINE_VERSION, 1)))
            return 0
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
