"""Metadata contract between ingestion and the later stages (parsing, chunking, embedding,
retrieval). See docs/metadata-strategy.md.

- Document-level metadata is produced here, at ingestion time, from the database.
  Every chunk of a document inherits it unchanged.
- Chunk-level fields (ChunkMetadata) are filled by the preprocessing stage.
- context_header() builds the short text prefix embedded with each chunk (bge-m3 dense
  + sparse). Only a few discriminating fields go into the text; everything else stays a
  structured filter so it does not dilute the embedding.
"""
from __future__ import annotations

from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import DocumentRow, DocumentVersionRow, ProgramRow, RegistrationRow

SCHEMA_VERSION = "1"

JURISDICTION = {"ema": "EU", "fda": "US", "hc": "CA"}

# Per document type: who is speaking by default, and how the preprocessing stage should cut it.
DOC_TYPE_PROFILE: dict[str, dict[str, str]] = {
    "assessment_report": {"default_speaker": "mixed", "chunking": "section_tree+table_rows",
                          "speaker_rule": "Discussion/Conclusions sections -> regulator; results text -> applicant_data"},
    "review":            {"default_speaker": "mixed", "chunking": "section_tree+table_rows",
                          "speaker_rule": "'Applicant's Position' -> applicant; 'FDA's Assessment' -> regulator"},
    "sbd":               {"default_speaker": "regulator", "chunking": "html_sections+table_rows",
                          "speaker_rule": "whole document is Health Canada's summary"},
    "rds":               {"default_speaker": "regulator", "chunking": "html_sections"},
    "procedural_steps":  {"default_speaker": "regulator", "chunking": "table_rows",
                          "speaker_rule": "one row per post-authorisation procedure"},
    "approval_letter":   {"default_speaker": "regulator", "chunking": "paragraphs"},
    "product_information": {"default_speaker": "label", "chunking": "label_sections"},
    "label":             {"default_speaker": "label", "chunking": "label_sections"},
    "product_monograph": {"default_speaker": "label", "chunking": "label_sections"},
    "epar_overview":     {"default_speaker": "regulator", "chunking": "paragraphs"},
    "risk_management":   {"default_speaker": "applicant", "chunking": "section_tree"},
    "guidance":          {"default_speaker": "regulator", "chunking": "numbered_sections",
                          "speaker_rule": "section numbers carry status (partial supersession)"},
    "review_index":      {"default_speaker": "none", "chunking": "none"},
    "other":             {"default_speaker": "unknown", "chunking": "paragraphs"},
}


class ChunkMetadata(TypedDict, total=False):
    """Fields the preprocessing stage adds per chunk (document-level fields are inherited)."""
    chunk_id: str
    chunk_type: str            # text | table_row | table | footnote | list | heading
    section_path: list[str]    # ["2.6 Clinical aspects", "2.6.2 Pharmacokinetics", "Discussion"]
    section_role: str          # results | discussion | conclusion | background | label_section
    speaker: str               # applicant | applicant_data | regulator | label | unknown
    page_pdf: int
    page_printed: str | None
    table_id: str | None
    table_caption: str | None
    table_headers: list[str] | None   # repeated on every table_row chunk
    footnotes: list[str] | None       # footnotes attached to the table/row
    study_ids: list[str]              # e.g. ["SB17-3001", "NCT04967508"]
    populations: list[str]            # e.g. ["PK set", "FAS", "safety set"]
    topics: list[str]                 # shared issue vocabulary (see docs/metadata-strategy.md)
    redacted: bool
    guidance_section_id: str | None   # e.g. "I.8" for Q&A documents
    guidance_section_status: str | None


def build_document_metadata(s: Session, doc: DocumentRow) -> dict[str, Any]:
    reg = s.get(RegistrationRow, doc.registration_id) if doc.registration_id else None
    prog = s.get(ProgramRow, doc.program_id) if doc.program_id else None
    latest = s.scalar(select(DocumentVersionRow).where(DocumentVersionRow.document_id == doc.id)
                      .order_by(DocumentVersionRow.retrieved_at.desc()))
    profile = DOC_TYPE_PROFILE.get(doc.doc_type, DOC_TYPE_PROFILE["other"])
    g = doc.guidance or {}
    names = []
    if prog:
        names = [prog.id, *prog.codes, *[n for v in (prog.aliases or {}).values() for n in v]]
    return {
        "schema_version": SCHEMA_VERSION,
        # identity
        "doc_id": doc.id,
        "doc_version_sha256": latest.sha256 if latest else None,
        "storage_path": latest.storage_path if latest else None,
        "content_type": latest.content_type if latest else None,
        "source_url": doc.source_url,
        "title": doc.title,
        # where it comes from
        "agency": doc.agency,
        "jurisdiction": g.get("jurisdiction") or JURISDICTION.get(doc.agency),
        "doc_kind": doc.doc_kind,
        "doc_type": doc.doc_type,
        "native_doc_type": doc.native_doc_type,
        # what it is about
        "ingredient": prog.ingredient if prog else None,
        "program_id": doc.program_id,
        "program_role": prog.role if prog else None,
        "brand_name": reg.brand_name if reg else None,
        "registration_key": reg.native_key if reg else None,
        "name_variants": sorted(set(names)),
        "procedure_id": doc.procedure_id,
        # time
        "decision_date": doc.decision_date,
        "doc_date": doc.doc_date,
        "source_published_at": doc.source_published_at,
        "source_updated_at": doc.source_updated_at,
        "retrieved_at": latest.retrieved_at.isoformat() if latest else None,
        # guidance axis
        "guidance_status": g.get("status"),
        "guidance_effective_from": g.get("effective_from"),
        "guidance_effective_to": g.get("effective_to"),
        "guidance_reference_no": g.get("reference_no"),
        "guidance_scope_products": g.get("scope_products"),
        "guidance_scope_exceptions": g.get("scope_exceptions"),
        "guidance_partial_supersession": g.get("partial_supersession"),
        "topics": g.get("topics", []),
        # preprocessing hints
        "default_speaker": profile["default_speaker"],
        "chunking": profile["chunking"],
        "speaker_rule": profile.get("speaker_rule"),
        "is_index_page": bool((doc.extra or {}).get("is_index_page")),
        "parent_document_id": doc.parent_document_id,
    }


def context_header(doc_meta: dict[str, Any], chunk: ChunkMetadata | None = None) -> str:
    """Short English prefix embedded with each chunk. Example:
    [EU | EMA | assessment_report | Pyzchiva (SB17, ustekinumab) | 2024-04-22 | 2.6.2 Pharmacokinetics > Discussion | regulator]"""
    chunk = chunk or {}
    parts = [doc_meta.get("jurisdiction"), (doc_meta.get("agency") or "").upper(), doc_meta.get("doc_type")]
    if doc_meta.get("doc_kind") == "guidance":
        parts.append(doc_meta.get("title"))
        parts.append(f"status={doc_meta.get('guidance_status')}")
        if chunk.get("guidance_section_id"):
            parts.append(f"section {chunk['guidance_section_id']} ({chunk.get('guidance_section_status')})")
    else:
        prod = doc_meta.get("brand_name") or doc_meta.get("program_id")
        extra = ", ".join(x for x in (doc_meta.get("program_id"), doc_meta.get("ingredient")) if x and x != prod)
        parts.append(f"{prod} ({extra})" if extra else prod)
    parts.append(doc_meta.get("decision_date") or doc_meta.get("doc_date") or doc_meta.get("source_published_at"))
    if chunk.get("section_path"):
        parts.append(" > ".join(chunk["section_path"][-2:]))
    if chunk.get("table_caption"):
        parts.append(f"table: {chunk['table_caption']}")
    if chunk.get("study_ids"):
        parts.append("study " + "/".join(chunk["study_ids"]))
    if chunk.get("speaker"):
        parts.append(chunk["speaker"])
    return "[" + " | ".join(str(p) for p in parts if p) + "]"
