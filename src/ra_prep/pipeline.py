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


def run(s: Session, selections: list[Selection], out_dir: str | Path,
        extractor: Extractor | None = None, force: bool = False, log=print) -> dict:
    """Process selected documents, skipping versions already parsed by this engine."""
    extractor = extractor or Extractor()
    engine = extractor.version()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"ok": 0, "failed": 0, "skipped": 0, "cached": 0, "pages": 0, "seconds": 0.0}
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
        if existing is not None:
            s.delete(existing)
            s.commit()
        t = time.time()
        try:
            doc = process_document(sel, extractor)
        except Exception as e:  # keep going; the failure is recorded
            _log.exception("preprocessing failed: %s", sel.path)
            s.add(PrepDocumentRow(document_id=sel.document_id, sha256=sel.sha256, engine=engine,
                                  status="failed", reason=f"{type(e).__name__}: {e}"[:2000],
                                  seconds=time.time() - t))
            s.commit()
            summary["failed"] += 1
            log(f"FAILED doc {sel.document_id}: {e}")
            continue
        signals = doc.pop("_signals")
        path = out_dir / f"{sel.sha256}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False))
        s.execute(delete(PageRouteRow).where(PageRouteRow.sha256 == sel.sha256))
        for r in doc["routes"]:
            s.add(PageRouteRow(document_id=sel.document_id, sha256=sel.sha256, page=r["page"],
                               route=r["route"], reason=r["reason"][:256],
                               signals=signals.get(r["page"], {})))
        el = time.time() - t
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
    return summary
