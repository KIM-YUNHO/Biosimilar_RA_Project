"""FDA connector.

Sources (docs/ra-document-sources.md F1/F2/F3):
- Purple Book monthly CSV: licensure catalog (BLA, proper name with suffix, 351(k) type,
  reference product, approval date). The file has a "changes this month" block on top and
  the full list below; both blocks start with their own header row.
- Drugs@FDA data files (ZIP of tab-delimited tables): Products/Applications/Submissions/
  ApplicationDocs -> document URLs (letters, labels, reviews) with dates.
- Approval package TOC pages: HTML index pages whose PDF links are expanded.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date
from typing import Any, Iterable
from urllib.parse import urljoin

from ..http import FetchError, FetchResult
from ..models import DocumentRecord, RegistrationRecord
from ..registry import Target
from ..util import contains_any, norm_key, norm_name, pick, to_iso_date
from .base import Connector

PURPLE_BOOK_URL = ("https://www.accessdata.fda.gov/drugsatfda_docs/PurpleBook/{year}/"
                   "purplebook-search-{month}-data-download.csv")
DRUGSATFDA_ZIP = "https://www.fda.gov/media/89850/download?attachment"

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]


def purple_book_candidates(today: date, months_back: int = 6) -> list[tuple[str, str]]:
    out, y, m = [], today.year, today.month
    for _ in range(months_back):
        out.append((f"{y}-{m:02d}", PURPLE_BOOK_URL.format(year=y, month=_MONTHS[m - 1])))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def parse_purple_book(text: str) -> list[dict[str, str]]:
    """Return rows of the LAST block (full product list). A header row is any row that
    contains both a BLA-number column and a proper-name column."""
    reader = csv.reader(io.StringIO(text))
    blocks: list[list[dict[str, str]]] = []
    header: list[str] | None = None
    for cells in reader:
        keys = [norm_key(c) for c in cells]
        if "bla_number" in keys and any(k.startswith("proper_name") for k in keys):
            header = cells
            blocks.append([])
            continue
        if header is None or not any(c.strip() for c in cells):
            continue
        blocks[-1].append({h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header)})
    return blocks[-1] if blocks else []


def read_tsv_table(z: zipfile.ZipFile, name_hint: str) -> list[dict[str, str]]:
    for info in z.infolist():
        if norm_key(info.filename.rsplit("/", 1)[-1]).startswith(norm_key(name_hint)):
            raw = z.read(info)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            return list(csv.DictReader(io.StringIO(text), delimiter="\t"))
    return []


def map_fda_doc_type(desc: str | None, url: str) -> tuple[str, bool]:
    d = (desc or "").lower()
    u = url.lower()
    if re.search(r"toc\.(cfm|htm|html)$", u) or u.endswith(".cfm"):
        return "review_index", True
    if "letter" in d or "ltr" in u:
        return "approval_letter", False
    if "label" in d or "lbl" in u:
        return "label", False
    if "review" in d or u.endswith("r.pdf"):
        return "review", False
    return "other", False


class FdaConnector(Connector):
    agency = "fda"

    def __init__(self, *a, today: date | None = None, drugsatfda_url: str = DRUGSATFDA_ZIP, **kw):
        super().__init__(*a, **kw)
        self.today = today or date.today()
        self.drugsatfda_url = drugsatfda_url
        self._dafda: dict[str, list[dict[str, str]]] | None = None

    # -- sources ---------------------------------------------------------------
    def _purple_book(self) -> tuple[str | None, list[dict[str, str]]]:
        last: FetchError | None = None
        for month, url in purple_book_candidates(self.today):
            try:
                res = self.fetcher.get(url)
            except FetchError as e:
                last = e
                if e.status is None:   # network-level failure: other months will fail the same way
                    break
                continue
            rows = parse_purple_book(res.text())
            if rows:
                return month, rows
            self.report("warning", "source_format", "Purple Book CSV에서 헤더/행을 찾지 못함", url=url)
        if last is not None:
            self.report("error", "source_failed", f"Purple Book 최근 월 파일 수집 실패: {last.reason}", url=last.url)
        return None, []

    def _drugsatfda(self) -> dict[str, list[dict[str, str]]]:
        if self._dafda is None:
            try:
                res = self.fetcher.get(self.drugsatfda_url)
                z = zipfile.ZipFile(io.BytesIO(res.content))
                self._dafda = {t: read_tsv_table(z, t) for t in
                               ("Products", "Applications", "Submissions", "ApplicationDocs",
                                "ApplicationsDocsType_Lookup")}
            except (FetchError, zipfile.BadZipFile) as e:
                self.report("error", "source_failed", f"Drugs@FDA ZIP 수집 실패: {e}", url=self.drugsatfda_url)
                self._dafda = {}
        return self._dafda

    # -- registrations ---------------------------------------------------------
    def discover_registrations(self, target: Target) -> Iterable[RegistrationRecord]:
        regs: dict[str, RegistrationRecord] = {}

        month, rows = self._purple_book()
        for row in rows:
            prop = pick(row, "proprietary_name") or ""
            proper = pick(row, "proper_name") or ""
            if not self.keep(target, [prop, proper], proper):
                continue
            bla = re.sub(r"\D", "", str(pick(row, "bla_number", default="")))
            if not bla:
                continue
            key = f"BLA{bla}"
            reg = regs.get(key)
            if reg is None:
                bla_type = pick(row, "bla_type", "license_type") or ""
                reg = regs[key] = RegistrationRecord(
                    agency="fda", native_key=key, brand_name=prop or proper, ingredient=proper,
                    identifiers={"fda_bla": bla, "proper_name": proper, "presentations": []},
                    holder=pick(row, "applicant"), licence_type=bla_type or None,
                    first_approval_date=to_iso_date(pick(row, "approval_date", "date_of_first_licensure")),
                    status=pick(row, "marketing_status", "licensure"),
                    source="fda.purple_book", source_snapshot=month,
                    raw={"reference_product": pick(row, "ref_product_proprietary_name"),
                         "reference_proper_name": pick(row, "ref_product_proper_name")},
                )
            reg.identifiers["presentations"].append({
                k: pick(row, k) for k in ("product_number", "strength", "dosage_form",
                                          "route_of_administration", "product_presentation")})

        # Drugs@FDA adds BLAs the Purple Book file missed (or covers for it when it failed)
        t = self._drugsatfda()
        apps = {a.get("ApplNo", "").lstrip("0"): a for a in t.get("Applications", [])}
        for p in t.get("Products", []):
            name, ingr = p.get("DrugName", ""), p.get("ActiveIngredient", "")
            if not self.keep(target, [name], ingr):
                continue
            appl = p.get("ApplNo", "").lstrip("0")
            app = apps.get(appl, {})
            if app and app.get("ApplType", "").upper() != "BLA":
                continue
            key = f"BLA{appl}"
            if key not in regs:
                regs[key] = RegistrationRecord(
                    agency="fda", native_key=key, brand_name=name.title() if name.isupper() else name,
                    ingredient=ingr, identifiers={"fda_bla": appl},
                    holder=app.get("SponsorName"), source="fda.drugsatfda",
                    raw={"product": p})
        yield from regs.values()

    # -- documents -------------------------------------------------------------
    def discover_documents(self, reg: RegistrationRecord, target: Target) -> Iterable[DocumentRecord]:
        t = self._drugsatfda()
        appl = reg.identifiers.get("fda_bla", "")
        lookup = {r.get("ApplicationDocsType_Lookup_ID"): r.get("ApplicationDocsType_Lookup_Description")
                  for r in t.get("ApplicationsDocsType_Lookup", [])}
        subs = {(s.get("SubmissionType"), s.get("SubmissionNo")): s
                for s in t.get("Submissions", []) if s.get("ApplNo", "").lstrip("0") == appl}
        for d in t.get("ApplicationDocs", []):
            if d.get("ApplNo", "").lstrip("0") != appl:
                continue
            url = (d.get("ApplicationDocsURL") or "").strip()
            if not url:
                continue
            url = url.replace("http://", "https://", 1)
            desc = lookup.get(d.get("ApplicationDocsTypeID"))
            doc_type, is_index = map_fda_doc_type(desc, url)
            sub = subs.get((d.get("SubmissionType"), d.get("SubmissionNo")), {})
            yield DocumentRecord(
                agency="fda", doc_kind="product", doc_type=doc_type, native_doc_type=desc,
                title=d.get("ApplicationDocsTitle") or desc or url, source_url=url,
                registration_key=reg.native_key, program_id=reg.program_id,
                procedure_id=f"{d.get('SubmissionType')}-{d.get('SubmissionNo')}",
                decision_date=to_iso_date(sub.get("SubmissionStatusDate")),
                doc_date=to_iso_date(d.get("ApplicationDocsDate")),
                language="en", is_index_page=is_index,
                extra={"submission_status": sub.get("SubmissionStatus")},
            )

    def expand_index(self, doc: DocumentRecord, page: FetchResult) -> Iterable[DocumentRecord]:
        html = page.text()
        seen = set()
        for href, label in re.findall(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>(.*?)</a>', html, flags=re.I | re.S):
            url = urljoin(page.url, href).replace("http://", "https://", 1)
            if url in seen:
                continue
            seen.add(url)
            title = re.sub(r"<[^>]+>|\s+", " ", label).strip() or url.rsplit("/", 1)[-1]
            doc_type, _ = map_fda_doc_type(title, url)
            if doc_type == "other" and contains_any(title, ["review", "summary"]):
                doc_type = "review"
            yield DocumentRecord(
                agency="fda", doc_kind="product", doc_type=doc_type, native_doc_type="approval_package_item",
                title=title, source_url=url, registration_key=doc.registration_key,
                program_id=doc.program_id, procedure_id=doc.procedure_id,
                decision_date=doc.decision_date, language="en",
                extra={"from_index": doc.source_url},
            )
