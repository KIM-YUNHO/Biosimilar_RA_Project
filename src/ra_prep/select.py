"""Document selection before preprocessing (P1).

Which stored documents are worth parsing, so per-product processing time goes to content:
  - labels: only the latest version per registration is processed; older label versions
    stay in the raw store (reason "superseded_label") and can be processed on request
  - near-duplicate documents: EMA duplicate marketing authorisations (Pyzchiva / Eksunbi,
    Steqeyma / Qoyvolma, ...) carry near-identical assessment reports and product
    information (5-word shingle Jaccard 0.76-0.87 on this corpus; unrelated documents of
    the same type stay far below 0.6). One representative is processed; the others point
    to it
  - index pages (review_index) are skipped
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pypdfium2 as pdfium
from sqlalchemy import select
from sqlalchemy.orm import Session

from ra_ingest.db import DocumentRow, DocumentVersionRow

LABEL_TYPES = {"label", "product_information", "product_monograph"}
DEDUP_TYPES = {"assessment_report", "product_information", "risk_management", "epar_overview",
               "procedural_steps"}
SKIP_TYPES = {"review_index"}


@dataclass
class Selection:
    document_id: int
    sha256: str
    path: str
    agency: str
    doc_type: str
    program_id: str | None
    action: str                 # process | skip
    reason: str = ""
    duplicate_of: int | None = None


def _latest_version(s: Session, doc_id: int) -> DocumentVersionRow | None:
    return s.scalar(select(DocumentVersionRow).where(DocumentVersionRow.document_id == doc_id)
                    .order_by(DocumentVersionRow.retrieved_at.desc()))


_WORD = re.compile(r"[a-z]{3,}")


def shingles(path: str, max_pages: int = 25, k: int = 5) -> set[int]:
    """Hashed k-word shingles of the first pages' text layer."""
    try:
        pdf = pdfium.PdfDocument(path)
    except pdfium.PdfiumError:
        return set()
    try:
        words: list[str] = []
        for i in range(min(len(pdf), max_pages)):
            page = pdf[i]
            tp = page.get_textpage()
            words.extend(_WORD.findall(tp.get_text_range().lower()))
            tp.close()
            page.close()
    finally:
        pdf.close()
    return {hash(" ".join(words[i:i + k])) for i in range(max(0, len(words) - k + 1))}


def _readable(path: str) -> bool:
    try:
        pdfium.PdfDocument(path).close()
        return True
    except pdfium.PdfiumError:
        return False


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _date(d: DocumentRow) -> str:
    return d.doc_date or d.source_published_at or d.decision_date or ""


def select_documents(s: Session, program_ids: list[str] | None = None,
                     agencies: list[str] | None = None, dup_threshold: float = 0.6,
                     include_superseded_labels: bool = False) -> list[Selection]:
    q = select(DocumentRow).where(DocumentRow.fetch_status == "fetched")
    if program_ids:
        q = q.where(DocumentRow.program_id.in_(program_ids))
    if agencies:
        q = q.where(DocumentRow.agency.in_(agencies))
    docs = list(s.scalars(q.order_by(DocumentRow.id)))
    out: dict[int, Selection] = {}
    for d in docs:
        v = _latest_version(s, d.id)
        if v is None:
            continue
        sel = Selection(d.id, v.sha256, v.storage_path, d.agency, d.doc_type, d.program_id, "process")
        if d.doc_type in SKIP_TYPES:
            sel.action, sel.reason = "skip", "index_page"
        elif not v.storage_path.lower().endswith((".pdf", ".html", ".htm")):
            sel.action, sel.reason = "skip", "not_pdf_or_html"
        elif v.storage_path.lower().endswith(".pdf") and not _readable(v.storage_path):
            sel.action, sel.reason = "skip", "unreadable_pdf"
        out[d.id] = sel

    # labels: latest per registration
    if not include_superseded_labels:
        by_reg: dict = {}
        for d in docs:
            if d.doc_type in LABEL_TYPES and d.id in out and out[d.id].action == "process":
                by_reg.setdefault((d.agency, d.registration_id or f"doc{d.id}"), []).append(d)
        for group in by_reg.values():
            dated = sorted((x for x in group if _date(x)), key=_date)
            keep = dated[-1] if dated else max(group, key=lambda x: x.id)
            for x in group:
                if x is not keep:
                    out[x.id].action, out[x.id].reason = "skip", f"superseded_label (latest: {keep.id})"

    # near-duplicates within the same agency, program and document type
    groups: dict = {}
    for d in docs:
        sel = out.get(d.id)
        if sel and sel.action == "process" and d.doc_type in DEDUP_TYPES and sel.path.lower().endswith(".pdf"):
            groups.setdefault((d.agency, d.program_id, d.doc_type), []).append(d)
    for group in groups.values():
        if len(group) < 2:
            continue
        sh = {d.id: shingles(out[d.id].path) for d in group}
        reps: list[int] = []
        for d in sorted(group, key=lambda x: (_date(x), x.id)):
            dup = next((r for r in reps if jaccard(sh[d.id], sh[r]) >= dup_threshold), None)
            if dup is None:
                reps.append(d.id)
            else:
                out[d.id].action = "skip"
                out[d.id].reason = f"near_duplicate (jaccard {jaccard(sh[d.id], sh[dup]):.2f})"
                out[d.id].duplicate_of = dup
    return list(out.values())
