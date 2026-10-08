"""Health Canada connector.

Sources (docs/ra-document-sources.md H1/H3/H4), verified against the live services 2026-10-08:
- Drug Product Database (DPD) REST API
    drugproduct/?brandname=X   partial brand match ("Pyzchiva" also returns "PYZCHIVA I.V.")
    activeingredient/?ingredientname=X  -> drug_codes (used for ingredient runs)
    drugproduct/?id=<drug_code>, status/?id=..., activeingredient/?id=...
  One registration = (base brand, company); IV/SC brand variants and their DINs are grouped.
- Drug and Health Products Portal (DHPP) review documents: the listing page accepts
  ?search=<term> and returns SBD / RDS / SSR links whose titles name the product.
  Titles are matched against the program's HC aliases (the full-text search also returns
  other ustekinumab products).
- Product monographs: not reachable from the DPD API; seed URLs via config if needed.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from ..http import FetchError
from ..models import DocumentRecord, RegistrationRecord
from ..registry import Target
from ..util import norm_name, pick, to_iso_date
from .base import Connector

DPD_API = "https://health-products.canada.ca/api/drug"
DHPP_REVIEW_DOCS = "https://dhpp.hpfb-dgpsa.ca/review-documents"

_DOC_PREFIX = {"SBD": "sbd", "RDS": "rds", "SSR": "other"}
_BRAND_SUFFIX = re.compile(r"\s*(I\.?\s?V\.?|IV|SC|S\.C\.|AUTOINJECTOR)\s*$", re.I)


def base_brand(brand: str) -> str:
    return _BRAND_SUFFIX.sub("", brand.strip()).strip()


def parse_review_listing(html: str) -> list[tuple[str, str, str]]:
    """-> [(url, doc_id, title)] from a DHPP review-documents listing page."""
    out = []
    for path, doc_id, title in re.findall(
            r'href="(?:https://dhpp\.hpfb-dgpsa\.ca)?/?(review-documents/resource/([A-Z]+\d+))"[^>]*>([^<]+)</a>',
            html):
        out.append((f"https://dhpp.hpfb-dgpsa.ca/{path}", doc_id, re.sub(r"\s+", " ", title).strip()))
    return out


class HcConnector(Connector):
    agency = "hc"

    def __init__(self, *a, api_base: str = DPD_API, review_docs_url: str = DHPP_REVIEW_DOCS, **kw):
        super().__init__(*a, **kw)
        self.api = api_base.rstrip("/")
        self.review_docs_url = review_docs_url
        self._seeds: dict[str, list[dict[str, Any]]] = {}

    def _get(self, resource: str, **params: Any) -> list[dict[str, Any]]:
        res = self.fetcher.get(f"{self.api}/{resource}/", params={"lang": "en", "type": "json", **params})
        data = res.json()
        return data if isinstance(data, list) else [data] if isinstance(data, dict) and data else []

    # -- registrations ---------------------------------------------------------
    def _candidate_products(self, target: Target) -> list[dict[str, Any]]:
        products: dict[Any, dict[str, Any]] = {}
        for term in dict.fromkeys(target.search_names("hc")):
            try:
                for p in self._get("drugproduct", brandname=term):
                    products[pick(p, "drug_code")] = p
            except FetchError as e:
                self.report("error", "source_failed", f"DPD 브랜드 검색 실패({term}): {e.reason}", url=e.url)
        if not target.product_scoped and target.ingredient:
            try:
                codes = {pick(r, "drug_code") for r in self._get("activeingredient", ingredientname=target.ingredient)}
            except FetchError as e:
                self.report("error", "source_failed", f"DPD 성분 검색 실패: {e.reason}", url=e.url)
                codes = set()
            for code in codes - set(products):
                try:
                    rows = self._get("drugproduct", id=code)
                except FetchError:
                    continue
                for p in rows:
                    products[pick(p, "drug_code")] = p
        return list(products.values())

    def discover_registrations(self, target: Target) -> Iterable[RegistrationRecord]:
        groups: dict[str, RegistrationRecord] = {}
        for p in self._candidate_products(target):
            brand = str(pick(p, "brand_name", default="")).strip()
            drug_code = pick(p, "drug_code")
            if not brand or drug_code is None:
                continue
            ingredients = self._ingredients(drug_code)
            if not self.keep(target, [brand, base_brand(brand)], " ".join(ingredients)):
                continue
            company = pick(p, "company_name") or ""
            brand0 = base_brand(brand)
            key = f"DPD:{norm_name(brand0)}:{norm_name(company)}"
            reg = groups.get(key)
            if reg is None:
                reg = groups[key] = RegistrationRecord(
                    agency="hc", native_key=key, brand_name=brand0.title() if brand0.isupper() else brand0,
                    ingredient=", ".join(sorted(set(ingredients))) or None,
                    identifiers={"din": [], "drug_code": [], "other_names": []}, holder=company or None,
                    source="hc.dpd_api", raw={"products": []})
            reg.identifiers["din"].append(pick(p, "drug_identification_number"))
            reg.identifiers["drug_code"].append(drug_code)
            if brand != brand0 and brand not in reg.identifiers["other_names"]:
                reg.identifiers["other_names"].append(brand)
            status = self._status(drug_code)
            p = {**p, "status": status}
            reg.raw["products"].append(p)
            if status:
                reg.status = status.get("status") or reg.status
                first = to_iso_date(status.get("original_market_date") or status.get("history_date"))
                cur = reg.identifiers.get("first_market_date")
                if first and (cur is None or first < cur):
                    # DPD has no approval date: this is a market/status date. The approval (NOC)
                    # date is filled from the SBD page in after_fetch().
                    reg.identifiers["first_market_date"] = first
        yield from groups.values()

    def _ingredients(self, drug_code: Any) -> list[str]:
        try:
            rows = self._get("activeingredient", id=drug_code)
        except FetchError:
            return []
        return [str(pick(r, "ingredient_name")) for r in rows if pick(r, "ingredient_name")]

    def _status(self, drug_code: Any) -> dict[str, Any] | None:
        try:
            rows = self._get("status", id=drug_code)
        except FetchError:
            return None
        return rows[0] if rows else None

    # -- dates from fetched pages ----------------------------------------------
    def after_fetch(self, doc: DocumentRecord, page) -> dict:
        if doc.doc_type not in ("sbd", "rds"):
            return {}
        import html as _html
        text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", page.text())))
        out: dict = {"document": {}, "registration": {}}
        m = re.search(r"Date of [Dd]ecision:?\s*(\d{4}-\d{2}-\d{2})", text)
        if m:
            out["document"]["decision_date"] = m.group(1)
        m = re.search(r"Date (?:SBD|RDS) issued:?\s*(\d{4}-\d{2}-\d{2})", text)
        if m:
            out["document"]["doc_date"] = m.group(1)
        if doc.doc_type == "sbd":
            # SBD templates differ by year; first match wins
            noc = None
            for pat in (r"Authori[sz]ation Date:?\s*(\d{4}-\d{2}-\d{2})",
                        r"(\d{4}-\d{2}-\d{2})\s*NOC issued for the New Drug Submission",
                        r"On (\w+ \d{1,2}, \d{4}), Health Canada issued a Notice of Compliance",
                        r"NOC issued by Director General:?\s*(\d{4}-\d{2}-\d{2})",
                        r"Notice of Compliance issued by Director General[^0-9]{0,120}(\d{4}-\d{2}-\d{2})"):
                m = re.search(pat, text)
                if m:
                    noc = to_iso_date(m.group(1))
                    break
            if noc:
                out["document"]["decision_date"] = noc
                out["registration"] = {"first_approval_date": noc, "identifiers.noc_date": noc}
        return out

    # -- documents -------------------------------------------------------------
    def set_seeds(self, seeds_by_program: dict[str, list[dict[str, Any]]]) -> None:
        self._seeds = seeds_by_program

    def _search_review_docs(self, term: str) -> list[tuple[str, str, str]]:
        out, page = [], 0
        while page < 10:
            res = self.fetcher.get(self.review_docs_url, params={"search": term, "page": page})
            items = parse_review_listing(res.text())
            if not items:
                break
            out.extend(items)
            if f"page={page + 1}" not in res.text():
                break
            page += 1
        return out

    def discover_documents(self, reg: RegistrationRecord, target: Target) -> Iterable[DocumentRecord]:
        names = [reg.brand_name] + [a for p in target.programs if p.id == reg.program_id
                                    for a in p.agency_names("hc")]
        names = list(dict.fromkeys(n for n in names if n))
        seen: set[str] = set()
        for term in names:
            try:
                items = self._search_review_docs(term)
            except FetchError as e:
                self.report("error", "source_failed", f"DHPP 검색 실패({term}): {e.reason}", url=e.url,
                            program_id=reg.program_id)
                continue
            pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.I)
            for url, doc_id, title in items:
                if url in seen or not pattern.search(title):
                    continue
                seen.add(url)
                prefix = re.match(r"[A-Z]+", doc_id).group(0)
                yield DocumentRecord(
                    agency="hc", doc_kind="product", doc_type=_DOC_PREFIX.get(prefix, "other"),
                    native_doc_type=prefix, title=title, source_url=url, registration_key=reg.native_key,
                    program_id=reg.program_id, procedure_id=doc_id, language="en",
                    extra={"dhpp_id": doc_id},
                )
        if not seen:
            self.report("warning", "no_review_documents", f"{reg.brand_name}: DHPP에서 SBD/RDS를 찾지 못함",
                        program_id=reg.program_id)
        for s in self._seeds.get(reg.program_id or "", []):
            yield DocumentRecord(
                agency="hc", doc_kind="product", doc_type=s.get("doc_type", "other"),
                native_doc_type=s.get("doc_type"), title=s.get("title") or s["url"],
                source_url=s["url"], registration_key=reg.native_key, program_id=reg.program_id,
                decision_date=to_iso_date(s.get("decision_date")), doc_date=to_iso_date(s.get("doc_date")),
                language="en", extra={"seeded": True},
            )
