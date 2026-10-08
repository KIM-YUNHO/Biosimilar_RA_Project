"""Connector output records. Every agency connector emits these two shapes, so the
pipeline, the database layer and later stages (parsing, chunking) never depend on
agency-specific formats."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

AGENCIES = ("ema", "fda", "hc")

# Normalized document types shared across agencies. Agency-native type strings are
# kept in DocumentRecord.native_doc_type.
DOC_TYPES = (
    "assessment_report",      # EMA EPAR public assessment report / scientific discussion
    "procedural_steps",       # EMA procedural steps taken after authorisation
    "product_information",    # EMA SmPC / annexes
    "epar_overview",          # EMA medicine overview / summary for the public
    "risk_management",        # RMP summaries
    "review",                 # FDA review documents (multidiscipline, chemistry, ...)
    "review_index",           # FDA approval package table-of-contents page
    "approval_letter",
    "label",                  # FDA USPI / DailyMed SPL
    "sbd",                    # HC Summary Basis of Decision
    "rds",                    # HC Regulatory Decision Summary
    "product_monograph",      # HC product monograph
    "guidance",
    "other",
)


@dataclass
class RegistrationRecord:
    """One marketing authorisation / licence at one agency."""

    agency: str
    native_key: str                 # unique within the agency (EMA product number, BLA, DPD brand key)
    brand_name: str
    ingredient: str | None = None
    identifiers: dict[str, Any] = field(default_factory=dict)
    holder: str | None = None
    licence_type: str | None = None  # e.g. "biosimilar", "351(k) Interchangeable", "reference"
    first_approval_date: str | None = None  # ISO date
    status: str | None = None
    indications: str | None = None
    source: str = ""                # connector source id, e.g. "ema.medicines_json"
    source_snapshot: str | None = None  # date/version of the catalog file the row came from
    raw: dict[str, Any] = field(default_factory=dict)
    program_id: str | None = None   # filled by the alias matcher

    def names(self) -> list[str]:
        out = [self.brand_name]
        for key in ("proper_name", "other_names"):
            v = self.identifiers.get(key)
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, list):
                out.extend(v)
        return [n for n in out if n]


@dataclass
class DocumentRecord:
    """One public document (or index page) to fetch and version."""

    agency: str                     # ema | fda | hc | guidance agencies use their own code too
    doc_kind: str                   # "product" | "guidance"
    doc_type: str                   # one of DOC_TYPES
    title: str
    source_url: str
    native_doc_type: str | None = None
    registration_key: str | None = None   # RegistrationRecord.native_key
    program_id: str | None = None
    procedure_id: str | None = None       # EMEA/H/C/.../II/..., ORIG-1, SUPPL-5, ...
    decision_date: str | None = None
    doc_date: str | None = None
    source_published_at: str | None = None
    source_updated_at: str | None = None
    language: str | None = None
    guidance: dict[str, Any] | None = None  # jurisdiction, status, effective_from, topics, ...
    is_index_page: bool = False            # HTML page whose links should be expanded
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrialRecord:
    nct_id: str
    program_id: str | None
    title: str | None
    phase: str | None
    conditions: list[str]
    primary_outcomes: list[str]
    sponsor: str | None
    raw: dict[str, Any] = field(default_factory=dict)
