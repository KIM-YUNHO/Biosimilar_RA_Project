"""Health Canada connector.

Sources (docs/ra-document-sources.md H1/H3/H4/H5):
- Drug Product Database (DPD) REST API: products by brand name, active ingredients, status.
  One registration = one (brand, company) pair; its DINs are collected in identifiers.
- SBD / RDS / Product Monograph: no API. URLs come from config/products.yaml
  `seed_documents.hc` until a discovery method for the Drug and Health Product Portal is added.

Limitation: the DPD API searches by brand name, so an ingredient run only finds products
whose brand is in the registry or whose brand contains the ingredient name.
"""
from __future__ import annotations

from typing import Any, Iterable

from ..http import FetchError
from ..models import DocumentRecord, RegistrationRecord
from ..registry import Target
from ..util import norm_name, pick, to_iso_date
from .base import Connector

DPD_API = "https://health-products.canada.ca/api/drug"


class HcConnector(Connector):
    agency = "hc"

    def __init__(self, *a, api_base: str = DPD_API, **kw):
        super().__init__(*a, **kw)
        self.api = api_base.rstrip("/")
        self._seeds: dict[str, list[dict[str, Any]]] = {}

    def _get(self, resource: str, **params: Any) -> list[dict[str, Any]]:
        res = self.fetcher.get(f"{self.api}/{resource}/", params={"lang": "en", "type": "json", **params})
        data = res.json()
        return data if isinstance(data, list) else [data] if isinstance(data, dict) and data else []

    def discover_registrations(self, target: Target) -> Iterable[RegistrationRecord]:
        terms = list(dict.fromkeys(target.search_names("hc") +
                                   ([] if target.product_scoped else [target.ingredient or ""])))
        groups: dict[str, RegistrationRecord] = {}
        for term in filter(None, terms):
            try:
                products = self._get("drugproduct", brandname=term)
            except FetchError as e:
                self.report("error", "source_failed", f"DPD 검색 실패({term}): {e.reason}", url=e.url)
                continue
            for p in products:
                brand = str(pick(p, "brand_name", default="")).strip()
                drug_code = pick(p, "drug_code")
                if not brand or drug_code is None:
                    continue
                ingredients = self._ingredients(drug_code)
                if not self.keep(target, [brand], " ".join(ingredients)):
                    continue
                company = pick(p, "company_name") or ""
                key = f"DPD:{norm_name(brand)}:{norm_name(company)}"
                reg = groups.get(key)
                if reg is None:
                    reg = groups[key] = RegistrationRecord(
                        agency="hc", native_key=key, brand_name=brand.title() if brand.isupper() else brand,
                        ingredient=", ".join(ingredients) or None,
                        identifiers={"din": [], "drug_code": []}, holder=company or None,
                        source="hc.dpd_api", raw={"products": []})
                reg.identifiers["din"].append(pick(p, "drug_identification_number"))
                reg.identifiers["drug_code"].append(drug_code)
                reg.raw["products"].append(p)
                status = self._status(drug_code)
                if status:
                    reg.status = status.get("status") or reg.status
                    first = to_iso_date(status.get("original_market_date") or status.get("history_date"))
                    if first and (reg.first_approval_date is None or first < reg.first_approval_date):
                        reg.first_approval_date = first
        yield from groups.values()

    def _ingredients(self, drug_code: Any) -> list[str]:
        try:
            rows = self._get("activeingredient", id=drug_code)
        except FetchError:
            return []
        return [str(pick(r, "ingredient_name", default="")) for r in rows if pick(r, "ingredient_name")]

    def _status(self, drug_code: Any) -> dict[str, Any] | None:
        try:
            rows = self._get("status", id=drug_code)
        except FetchError:
            return None
        return rows[0] if rows else None

    def set_seeds(self, seeds_by_program: dict[str, list[dict[str, Any]]]) -> None:
        self._seeds = seeds_by_program

    def discover_documents(self, reg: RegistrationRecord, target: Target) -> Iterable[DocumentRecord]:
        seeds = self._seeds.get(reg.program_id or "", [])
        if not seeds:
            self.report("warning", "manual_seed_needed",
                        f"{reg.brand_name}: HC SBD/RDS/PM URL이 레지스트리에 없음 (seed_documents.hc 추가 필요)",
                        program_id=reg.program_id)
        for s in seeds:
            yield DocumentRecord(
                agency="hc", doc_kind="product", doc_type=s.get("doc_type", "other"),
                native_doc_type=s.get("doc_type"), title=s.get("title") or s["url"],
                source_url=s["url"], registration_key=reg.native_key, program_id=reg.program_id,
                decision_date=to_iso_date(s.get("decision_date")), doc_date=to_iso_date(s.get("doc_date")),
                language="en", extra={"seeded": True},
            )
