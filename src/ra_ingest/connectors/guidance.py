"""Guidance axis.

- FDA: the guidance search page is backed by a public JSON file listing every guidance with
  status (Draft/Final), issue date, docket number and comment close date. Every entry whose
  product topic is "Biosimilars" (or whose title names biosimilars/BPCI/BsUFA/interchangeability)
  is ingested automatically; the PDF link is the document, the landing page is kept as metadata.
- EMA / Health Canada: no machine-readable list -> curated config/guidances.yaml.
- Curated entries also enrich FDA catalog entries (topics, scope, partial supersession) when
  their url is the FDA landing page of the same guidance.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..http import FetchError, Fetcher
from ..models import DocumentRecord
from ..util import to_iso_date

FDA_GUIDANCE_JSON = "https://www.fda.gov/files/api/datatables/static/search-for-guidance.json"
FDA_BASE = "https://www.fda.gov"

_GUIDANCE_FIELDS = ("jurisdiction", "reference_no", "status", "published_at", "adopted_at",
                    "effective_from", "effective_to", "consultation_end", "supersedes",
                    "superseded_note", "partial_supersession", "scope_products", "scope_exceptions",
                    "topics", "url_verified")

_TOPIC_RULES = [
    (r"interchangeab", "interchangeability"), (r"comparative analytical|quality", "analytical"),
    (r"clinical pharmacology|pharmacokinetic", "pk"), (r"efficacy stud", "ces"),
    (r"immunogenicity", "immunogenicity"), (r"label", "labeling"), (r"meeting", "meetings"),
    (r"fewer than all conditions|extrapolat", "extrapolation"), (r"q&a|questions and answers", "general"),
]


def load_guidances(path: str | Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("guidances", [])


def _href(cell: str) -> str | None:
    m = re.search(r'href="([^"]+)"', cell or "")
    if not m:
        return None
    url = html.unescape(m.group(1))
    return url if url.startswith("http") else FDA_BASE + url


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell or ""))).strip()


def infer_topics(title: str) -> list[str]:
    t = title.lower()
    return sorted({topic for pat, topic in _TOPIC_RULES if re.search(pat, t)}) or ["general"]


def parse_fda_catalog(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        title = _text(r.get("title", ""))
        # title filter only: FDA also tags unrelated guidances (oncology trial design, BIMO)
        # with the "Biosimilars" product topic
        if not re.search(r"biosimilar|interchangeab|bpci|bsufa|351\([ak]\)", title, re.I):
            continue
        out.append({
            "title": title,
            "landing_url": _href(r.get("title", "")),
            "pdf_url": _href(r.get("field_associated_media_2", "")),
            "status": _text(r.get("field_final_guidance_1", "")).lower() or None,
            "published_at": to_iso_date(_text(r.get("field_issue_datetime", ""))),
            "consultation_end": to_iso_date(_text(r.get("field_comment_close_date", ""))),
            "docket": _text(r.get("field_docket_number", "")).rstrip(". ") or None,
            "issuing_office": _text(r.get("field_issuing_office_taxonomy", "")),
            "page_changed": to_iso_date(re.sub(r".*datetime=\"([^\"]+)\".*", r"\1", r.get("changed", ""), flags=re.S)),
        })
    return out


def _curated_doc(g: dict[str, Any]) -> DocumentRecord:
    meta = {k: g[k] for k in _GUIDANCE_FIELDS if k in g}
    meta["guidance_id"] = g["id"]
    meta["source"] = "curated"
    url = g["url"]
    return DocumentRecord(
        agency=g["agency"], doc_kind="guidance", doc_type="guidance", native_doc_type=g.get("type"),
        title=g["title"], source_url=url, procedure_id=g.get("reference_no"),
        doc_date=str(g.get("published_at")) if g.get("published_at") else None,
        language="en", guidance=meta,
        extra={"landing_page": not url.lower().endswith(".pdf") and "/download" not in url},
    )


def guidance_documents(entries: list[dict[str, Any]], agencies: list[str], report,
                       fetcher: Fetcher | None = None) -> Iterable[DocumentRecord]:
    curated_by_url = {e["url"]: e for e in entries if e.get("url")}
    used: set[str] = set()

    if "fda" in agencies and fetcher is not None:
        try:
            catalog = parse_fda_catalog(fetcher.get(FDA_GUIDANCE_JSON).json())
        except (FetchError, ValueError) as e:
            report("error", "source_failed", f"FDA 가이던스 목록 수집 실패: {e}", agency="fda")
            catalog = []
        for c in catalog:
            if not c["pdf_url"]:
                continue
            cur = curated_by_url.get(c["landing_url"]) or curated_by_url.get(c["pdf_url"])
            meta: dict[str, Any] = {"jurisdiction": "US", "source": "fda.guidance_catalog",
                                    "topics": infer_topics(c["title"])}
            if cur:
                used.add(cur["url"])
                meta.update({k: cur[k] for k in _GUIDANCE_FIELDS if k in cur})
                meta["guidance_id"] = cur["id"]
            # live catalog values win over curated ones for status and dates
            meta.update({"status": c["status"], "published_at": c["published_at"],
                         "consultation_end": c["consultation_end"], "reference_no": c["docket"],
                         "landing_url": c["landing_url"], "catalog_page_changed": c["page_changed"],
                         "issuing_office": c["issuing_office"]})
            yield DocumentRecord(
                agency="fda", doc_kind="guidance", doc_type="guidance", native_doc_type="fda_guidance",
                title=c["title"], source_url=c["pdf_url"], procedure_id=c["docket"],
                doc_date=c["published_at"], source_updated_at=c["page_changed"], language="en",
                guidance=meta,
            )
        if catalog:
            for e in entries:
                if e.get("agency") == "fda" and e.get("url") and e["url"] not in used:
                    report("warning", "guidance_not_in_catalog",
                           f"큐레이션 목록의 FDA 가이던스가 FDA 현재 목록에 없음: {e['id']} (철회·대체 여부 확인)",
                           agency="fda")
                    used.add(e["url"])

    for g in entries:
        if g.get("agency") not in agencies or (g.get("url") in used):
            continue
        if not g.get("url"):
            report("warning", "guidance_url_missing", f"가이던스 URL 미기재: {g['id']}", agency=g.get("agency"))
            continue
        yield _curated_doc(g)
