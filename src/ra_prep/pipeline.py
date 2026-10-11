"""Preprocessing of stored documents: sanitize -> page signals -> routes -> extraction ->
RA post-processing -> QA -> data/parsed/<sha256>.json + database rows.

Deterministic: Docling layout/table models, RapidOCR and rules. No LLM is called.
"""
from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .extract import Extractor
from .qa import document_qa
from .route import route_document
from .sanitize import remove_watermarks
from .select import Selection
from .signals import document_signals
from .store import PageRouteRow, PrepDocumentRow
from .structure import structure

_log = logging.getLogger(__name__)


def process_pdf(path: str, doc_type: str | None, extractor: Extractor,
                workdir: str | None = None) -> dict:
    """Extraction only (no post-processing). Returns {"routes", "blocks", "engine",
    "timing", "watermarks_removed"}."""
    t0 = time.time()
    tmpdir = None
    if workdir is None:
        tmpdir = tempfile.TemporaryDirectory(prefix="ra-prep-")
        workdir = tmpdir.name
    try:
        clean = str(Path(workdir) / (Path(path).stem + ".clean.pdf"))
        removed = remove_watermarks(path, clean)
        src = clean if removed else path
        routes = route_document(document_signals(src), doc_type)
        res = extractor.extract(src, routes)
        res["routes"] = routes
        res["watermarks_removed"] = removed
        res["timing"]["total"] = time.time() - t0
        return res
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()


def process_document(sel: Selection, extractor: Extractor) -> dict:
    """Extraction + RA post-processing + QA for one selected document."""
    t0 = time.time()
    if sel.path.lower().endswith((".html", ".htm")):
        res = extractor.extract_html(sel.path)
        res["routes"] = [{"page": 1, "route": "HTML", "reason": "html document", "signals": {}}]
        res["watermarks_removed"] = 0
        sizes: dict = {}
    else:
        res = process_pdf(sel.path, sel.doc_type, extractor)
        sizes = {r["page"]: (r["signals"]["width"], r["signals"]["height"]) for r in res["routes"]}
    blocks = structure(res["blocks"], sel.agency, sel.doc_type, sizes)
    return {
        "document_id": sel.document_id, "sha256": sel.sha256, "agency": sel.agency,
        "doc_type": sel.doc_type, "program_id": sel.program_id, "engine": res["engine"],
        "watermarks_removed": res["watermarks_removed"],
        "routes": [{k: r[k] for k in ("page", "route", "reason")} for r in res["routes"]],
        "blocks": blocks,
        "qa": document_qa(blocks, res["routes"]),
        "timing": {**res["timing"], "total": time.time() - t0},
        "_signals": {r["page"]: r["signals"] for r in res["routes"]},
    }


_WORKER_EXTRACTOR: Extractor | None = None


def _init_worker(cfg: dict) -> None:
    global _WORKER_EXTRACTOR
    logging.getLogger("docling").setLevel(logging.WARNING)
    _WORKER_EXTRACTOR = Extractor(**cfg)


def _work(sel: Selection) -> tuple[Selection, dict | None, str | None, float]:
    t = time.time()
    try:
        return sel, process_document(sel, _WORKER_EXTRACTOR), None, time.time() - t
    except Exception as e:  # recorded by the parent
        return sel, None, f"{type(e).__name__}: {e}", time.time() - t


def run(s: Session, selections: list[Selection], out_dir: str | Path,
        extractor: Extractor | None = None, force: bool = False, log=print,
        workers: int = 1) -> dict:
    """Process selected documents, skipping versions already parsed by this engine.

    workers > 1 runs documents in parallel processes (each with its own Docling models,
    num_threads split between them). Results are written by this process only, so the
    database never sees concurrent writers (safe for SQLite)."""
    extractor = extractor or Extractor()
    engine = extractor.version()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"ok": 0, "failed": 0, "skipped": 0, "cached": 0, "refixed": 0, "pages": 0,
               "seconds": 0.0}
    t_run = time.time()

    todo = []
    for sel in selections:
        existing = s.scalar(select(PrepDocumentRow).where(
            PrepDocumentRow.sha256 == sel.sha256, PrepDocumentRow.engine == engine))
        if sel.action != "process":
            if existing is None:
                s.add(PrepDocumentRow(document_id=sel.document_id, sha256=sel.sha256, engine=engine,
                                      status="skipped", reason=sel.reason, duplicate_of=sel.duplicate_of))
                s.commit()
            summary["skipped"] += 1
            continue
        if existing is not None and existing.status == "ok" and not force:
            summary["cached"] += 1
            continue
        if existing is None and not force:
            old = refixable_row(s, sel.sha256, engine)
            if old is not None:  # same extraction stage: replay the cheap stages only
                refix_row(s, old, engine)
                summary["refixed"] += 1
                continue
        if existing is not None:
            s.delete(existing)
            s.commit()
        todo.append(sel)
    log(f"to process: {len(todo)} documents (cached {summary['cached']}, "
        f"refixed {summary['refixed']}, skipped {summary['skipped']})")

    def record(sel: Selection, doc: dict | None, err: str | None, el: float) -> None:
        if doc is None:
            s.add(PrepDocumentRow(document_id=sel.document_id, sha256=sel.sha256, engine=engine,
                                  status="failed", reason=(err or "")[:2000], seconds=el))
            s.commit()
            summary["failed"] += 1
            log(f"FAILED doc {sel.document_id}: {err}")
            return
        signals = doc.pop("_signals")
        path = out_dir / f"{sel.sha256}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        s.execute(delete(PageRouteRow).where(PageRouteRow.sha256 == sel.sha256))
        for r in doc["routes"]:
            s.add(PageRouteRow(document_id=sel.document_id, sha256=sel.sha256, page=r["page"],
                               route=r["route"], reason=r["reason"][:256],
                               signals=signals.get(r["page"], {})))
        s.add(PrepDocumentRow(document_id=sel.document_id, sha256=sel.sha256, engine=engine,
                              status="ok", output_path=str(path), pages=doc["qa"]["pages"],
                              seconds=el, qa=doc["qa"]))
        s.commit()
        summary["ok"] += 1
        summary["pages"] += doc["qa"]["pages"]
        summary["seconds"] += el
        q = doc["qa"]
        log(f"doc {sel.document_id:>4} {sel.agency}/{sel.doc_type:<18} {q['pages']:>4} p "
            f"{el:6.1f}s routes={q['routes']} review={q['needs_review_blocks']}")

    if workers <= 1 or len(todo) <= 1:
        for sel in todo:
            t = time.time()
            try:
                doc, err = process_document(sel, extractor), None
            except Exception as e:  # keep going; the failure is recorded
                _log.exception("preprocessing failed: %s", sel.path)
                doc, err = None, f"{type(e).__name__}: {e}"
            record(sel, doc, err, time.time() - t)
    else:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor, as_completed

        cfg = dict(extractor.config(), num_threads=max(1, extractor.num_threads // workers))
        # largest documents first so the pool does not end on one long straggler
        todo.sort(key=lambda x: -_page_count(x.path))
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"),
                                 initializer=_init_worker, initargs=(cfg,)) as pool:
            for fut in as_completed([pool.submit(_work, sel) for sel in todo]):
                record(*fut.result())
    summary["wall_seconds"] = time.time() - t_run
    return summary


def _page_count(path: str) -> int:
    if not path.lower().endswith(".pdf"):
        return 0
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(path)
        n = len(pdf)
        pdf.close()
        return n
    except Exception:
        return 0


def refixable_row(s: Session, sha256: str, engine: str):
    """An ok parse of this version by an earlier engine that differs from `engine` only in a
    version listed in REFIXABLE_FROM (same Docling version and settings)."""
    from . import REFIXABLE_FROM

    _, _, suffix = engine.partition("+")
    for old in REFIXABLE_FROM:
        row = s.scalar(select(PrepDocumentRow).where(
            PrepDocumentRow.sha256 == sha256, PrepDocumentRow.engine == f"{old}+{suffix}",
            PrepDocumentRow.status == "ok"))
        if row is not None:
            return row
    return None


def refix_row(s: Session, row: PrepDocumentRow, engine_to: str) -> dict:
    """Upgrade one stored parse in place without re-running Docling: re-apply the text-layer
    repairs (textfix) and RA post-processing (structure), recompute QA, relabel the engine."""
    from ra_ingest.db import DocumentRow, DocumentVersionRow

    from .textfix import fix_blocks

    doc = json.loads(Path(row.output_path).read_text(encoding="utf-8"))
    d = s.get(DocumentRow, row.document_id)
    src = s.scalar(select(DocumentVersionRow.storage_path).where(
        DocumentVersionRow.sha256 == row.sha256))
    fixes = {}
    if src and src.lower().endswith(".pdf"):
        fixes = fix_blocks(src, doc["blocks"])
    sizes = {r.page: (r.signals.get("width", 612.0), r.signals.get("height", 792.0))
             for r in s.scalars(select(PageRouteRow).where(PageRouteRow.sha256 == row.sha256))}
    doc["blocks"] = structure(doc["blocks"], d.agency, d.doc_type, sizes)
    doc["qa"] = document_qa(doc["blocks"], doc["routes"])
    doc["engine"] = engine_to
    doc["text_fixes"] = fixes
    Path(row.output_path).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    row.engine = engine_to
    row.qa = doc["qa"]
    s.commit()
    return fixes


def refix(s: Session, engine_from: str, engine_to: str, log=print) -> dict:
    """Upgrade every ok parse made by `engine_from` to `engine_to` (see refix_row)."""
    rows = list(s.scalars(select(PrepDocumentRow).where(
        PrepDocumentRow.engine == engine_from, PrepDocumentRow.status == "ok")))
    done = 0
    for row in rows:
        if s.scalar(select(PrepDocumentRow).where(PrepDocumentRow.sha256 == row.sha256,
                                                  PrepDocumentRow.engine == engine_to)):
            continue
        fixes = refix_row(s, row, engine_to)
        done += 1
        if any(fixes.values()):
            log(f"doc {row.document_id:>4}: {fixes}")
    return {"updated": done, "candidates": len(rows)}
