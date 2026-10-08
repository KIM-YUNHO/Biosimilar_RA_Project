"""Guidance axis: curated list (config/guidances.yaml) -> DocumentRecord(doc_kind="guidance")."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

from ..models import DocumentRecord

_GUIDANCE_FIELDS = ("jurisdiction", "reference_no", "status", "published_at", "adopted_at",
                    "effective_from", "effective_to", "consultation_end", "supersedes",
                    "superseded_note", "partial_supersession", "scope_products", "scope_exceptions",
                    "topics", "url_verified")


def load_guidances(path: str | Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("guidances", [])


def guidance_documents(entries: list[dict[str, Any]], agencies: list[str],
                       report) -> Iterable[DocumentRecord]:
    for g in entries:
        if g.get("agency") not in agencies:
            continue
        if not g.get("url"):
            report("warning", "guidance_url_missing", f"가이던스 URL 미기재: {g['id']}", agency=g.get("agency"))
            continue
        meta = {k: g[k] for k in _GUIDANCE_FIELDS if k in g}
        meta["guidance_id"] = g["id"]
        url = g["url"]
        yield DocumentRecord(
            agency=g["agency"], doc_kind="guidance", doc_type="guidance", native_doc_type=g.get("type"),
            title=g["title"], source_url=url, procedure_id=g.get("reference_no"),
            doc_date=str(g.get("published_at")) if g.get("published_at") else None,
            language="en", guidance=meta,
            extra={"landing_page": not url.lower().endswith(".pdf") and "/download" not in url},
        )
