"""EMA connector.

Sources (see docs/ra-document-sources.md E1/E2):
- medicines JSON  : centrally authorised medicines, one row per EMA product
- EPAR documents JSON: every EPAR document with type, product number, dates and URL
Both files are refreshed twice a day. Field names are matched tolerantly (util.pick)
because the exact key spelling was not verifiable when this was written.
"""
from __future__ import annotations

from typing import Any, Iterable

from ..http import FetchError
from ..models import DocumentRecord, RegistrationRecord
from ..registry import Target
from ..util import norm_name, pick, to_iso_date
from .base import Connector

MEDICINES_JSON = "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines_json-report_en.json"
DOCUMENTS_JSON = "https://www.ema.europa.eu/en/documents/report/documents-output-epar_documents_json-report_en.json"

# EMA native document type (normalized) -> shared doc type
_TYPE_MAP = {
    "assessment_report": "assessment_report",
    "public_assessment_report": "assessment_report",
    "epar_public_assessment_report": "assessment_report",
    "scientific_discussion": "assessment_report",
    "procedural_steps_taken_and_scientific_information_after_authorisation": "procedural_steps",
    "procedural_steps_after": "procedural_steps",
    "procedural_steps": "procedural_steps",
    "product_information": "product_information",
    "epar_product_information": "product_information",
    "overview": "epar_overview",
    "medicine_overview": "epar_overview",
    "epar_medicine_overview": "epar_overview",
    "summary_for_the_public": "epar_overview",
    "risk_management_plan_summary": "risk_management",
    "rmp_summary": "risk_management",
    "rmp": "risk_management",
    "all_authorised_presentations": "other",
}


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def map_doc_type(native: str | None, url: str) -> str:
    from ..util import norm_key

    key = norm_key(native or "")
    if key in _TYPE_MAP:
        return _TYPE_MAP[key]
    for k, v in _TYPE_MAP.items():
        if k and k in key:
            return v
    u = url.lower()
    for marker, v in (("assessment-report", "assessment_report"), ("procedural-steps", "procedural_steps"),
                      ("product-information", "product_information"), ("medicine-overview", "epar_overview"),
                      ("risk-management", "risk_management")):
        if marker in u:
            return v
    return "other"


class EmaConnector(Connector):
    agency = "ema"

    def __init__(self, *a, medicines_url: str = MEDICINES_JSON, documents_url: str = DOCUMENTS_JSON, **kw):
        super().__init__(*a, **kw)
        self.medicines_url = medicines_url
        self.documents_url = documents_url
        self._docs_by_product: dict[str, list[dict[str, Any]]] | None = None
        self._docs_by_name: dict[str, list[dict[str, Any]]] = {}

    # -- registrations ---------------------------------------------------------
    def discover_registrations(self, target: Target) -> Iterable[RegistrationRecord]:
        res = self.fetcher.get(self.medicines_url)
        rows = _rows(res.json())
        snapshot = res.last_modified
        if not rows:
            self.report("error", "source_format", "EMA medicines JSON에서 행을 찾지 못함", url=self.medicines_url)
        for row in rows:
            category = pick(row, "category")
            if category and str(category).lower() != "human":
                continue
            name = pick(row, "name_of_medicine", "medicine_name", "name")
            inn = pick(row, "international_non_proprietary_name_common_name",
                       "international_non_proprietary_name_inn_common_name", "inn_common_name", "inn")
            substance = pick(row, "active_substance")
            ingredient_text = f"{inn or ''} {substance or ''}".strip()
            if not ingredient_text:
                # column names unknown: fall back to any field whose key mentions the ingredient/INN
                ingredient_text = " ".join(str(v) for k, v in row.items()
                                           if any(w in k.lower() for w in ("inn", "substance", "common name")))
            if not name or not self.keep(target, [name], ingredient_text):
                continue
            product_no = pick(row, "ema_product_number", "product_number")
            biosimilar = str(pick(row, "biosimilar", default="")).lower() in ("yes", "true", "1")
            yield RegistrationRecord(
                agency="ema",
                native_key=str(product_no or f"name:{norm_name(name)}"),
                brand_name=str(name),
                ingredient=inn or substance,
                identifiers={"ema_product_number": product_no,
                             "medicine_url": pick(row, "medicine_url", "url")},
                holder=pick(row, "marketing_authorisation_developer_applicant_holder",
                            "marketing_authorisation_holder", "mah"),
                licence_type="biosimilar" if biosimilar else "centrally_authorised",
                first_approval_date=to_iso_date(pick(row, "marketing_authorisation_date",
                                                     "european_commission_decision_date"), dayfirst=True),
                status=pick(row, "medicine_status", "authorisation_status", "status"),
                indications=pick(row, "therapeutic_indication", "condition_indication"),
                source="ema.medicines_json",
                source_snapshot=snapshot,
                raw=row,
            )

    # -- documents -------------------------------------------------------------
    def _load_documents(self) -> None:
        if self._docs_by_product is not None:
            return
        res = self.fetcher.get(self.documents_url)
        self._docs_by_product = {}
        for row in _rows(res.json()):
            pn = pick(row, "ema_product_number", "product_number")
            if pn:
                self._docs_by_product.setdefault(str(pn), []).append(row)
            med = pick(row, "name_of_medicine", "medicine_name")
            if med:
                self._docs_by_name.setdefault(norm_name(str(med)), []).append(row)

    def discover_documents(self, reg: RegistrationRecord, target: Target) -> Iterable[DocumentRecord]:
        try:
            self._load_documents()
        except FetchError as e:
            self.report("error", "source_failed", f"EMA 문서 JSON 수집 실패: {e.reason}", url=e.url)
            return
        assert self._docs_by_product is not None
        rows = self._docs_by_product.get(reg.native_key) or self._docs_by_name.get(norm_name(reg.brand_name), [])
        for row in rows:
            url = pick(row, "url", "document_url", "link")
            if not url:
                continue
            lang = pick(row, "language", "language_code")
            if lang and str(lang).lower() not in ("en", "english"):
                continue
            if not lang and "_en." not in url and not url.lower().endswith("_en"):
                # EMA translations share names except the language suffix; keep English only
                if any(f"_{c}." in url for c in ("bg", "cs", "da", "de", "el", "es", "et", "fi", "fr",
                                                  "hr", "hu", "it", "lt", "lv", "mt", "nl", "pl", "pt",
                                                  "ro", "sk", "sl", "sv", "is", "no")):
                    continue
            native = pick(row, "type", "document_type")
            yield DocumentRecord(
                agency="ema",
                doc_kind="product",
                doc_type=map_doc_type(native, url),
                native_doc_type=native,
                title=str(pick(row, "name", "title", "document_name", default=url)),
                source_url=url,
                registration_key=reg.native_key,
                program_id=reg.program_id,
                procedure_id=pick(row, "reference_number"),
                source_published_at=to_iso_date(pick(row, "first_published_date", "publish_date"), dayfirst=True),
                source_updated_at=to_iso_date(pick(row, "last_updated_date", "revision_date"), dayfirst=True),
                language="en",
            )
