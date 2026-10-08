"""Mocked agency endpoints. Formats follow the published descriptions (see
docs/ra-document-sources.md); identifiers here are fake test values."""
from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import date
from pathlib import Path

import pytest
import responses

from ra_ingest.connectors.ema import DOCUMENTS_JSON, MEDICINES_JSON
from ra_ingest.connectors.fda import DRUGSATFDA_ZIP, purple_book_candidates
from ra_ingest.connectors.hc import DPD_API
from ra_ingest.http import Fetcher

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 8)

EMA_MEDICINES = {"meta": {"generated": "2026-10-08T06:00:00"}, "data": [
    {"Category": "Human", "Name of medicine": "Pyzchiva", "EMA product number": "EMEA/H/C/900001",
     "Medicine status": "Authorised", "International non-proprietary name (INN) / common name": "ustekinumab",
     "Biosimilar": "Yes", "Marketing authorisation date": "20/04/2024",
     "Marketing authorisation developer / applicant / holder": "Samsung Bioepis NL B.V."},
    {"Category": "Human", "Name of medicine": "Wezenla", "EMA product number": "EMEA/H/C/900002",
     "Medicine status": "Authorised", "International non-proprietary name (INN) / common name": "ustekinumab",
     "Biosimilar": "Yes", "Marketing authorisation date": "20/06/2024"},
    {"Category": "Human", "Name of medicine": "Usymro", "EMA product number": "EMEA/H/C/900005",
     "Medicine status": "Authorised", "International non-proprietary name (INN) / common name": "ustekinumab",
     "Biosimilar": "Yes", "Marketing authorisation date": "26/08/2025"},
    {"Category": "Human", "Name of medicine": "Otulfi", "EMA product number": "EMEA/H/C/900009",
     "Medicine status": "Authorised", "International non-proprietary name (INN) / common name": "ustekinumab",
     "Biosimilar": "Yes"},
    {"Category": "Human", "Name of medicine": "Humira", "EMA product number": "EMEA/H/C/000481",
     "International non-proprietary name (INN) / common name": "adalimumab", "Biosimilar": "No"},
    {"Category": "Veterinary", "Name of medicine": "Ustekinumab-vet", "EMA product number": "EMEA/V/C/1",
     "International non-proprietary name (INN) / common name": "ustekinumab"},
]}

EMA_AR = "https://www.ema.europa.eu/en/documents/assessment-report/pyzchiva-epar-public-assessment-report_en.pdf"
EMA_DOCS = {"data": [
    {"Name": "Pyzchiva : EPAR - Public assessment report", "Type": "assessment-report",
     "EMA product number": "EMEA/H/C/900001", "Language": "en", "URL": EMA_AR,
     "First published date": "10/05/2024", "Last updated date": "10/05/2024"},
    {"Name": "Pyzchiva : EPAR - Public assessment report (BG)", "Type": "assessment-report",
     "EMA product number": "EMEA/H/C/900001", "Language": "bg",
     "URL": EMA_AR.replace("_en.pdf", "_bg.pdf")},
    {"Name": "Pyzchiva : EPAR - Product information", "Type": "product-information",
     "EMA product number": "EMEA/H/C/900001", "Language": "en",
     "URL": "https://www.ema.europa.eu/en/documents/product-information/pyzchiva-epar-product-information_en.pdf"},
    {"Name": "Wezenla : EPAR - Public assessment report", "Type": "assessment-report",
     "EMA product number": "EMEA/H/C/900002", "Language": "en",
     "URL": "https://www.ema.europa.eu/en/documents/assessment-report/wezenla-epar-public-assessment-report_en.pdf"},
]}

PURPLE_BOOK_CSV = """Purple Book Data Download - changes this month
N/R/U,Applicant,BLA Number,Proprietary Name,Proper Name,BLA Type,Strength,Dosage Form,Approval Date
N,Bio-Thera,999905,STARJEMZA,ustekinumab-hmny,351(k) Biosimilar,45 mg/0.5 mL,Injection,05/22/2025

All products
Applicant,BLA Number,Proprietary Name,Proper Name,BLA Type,Strength,Dosage Form,Route of Administration,Product Number,Approval Date,Ref. Product Proprietary Name,Ref. Product Proper Name,Marketing Status
Janssen,125261,STELARA,ustekinumab,351(a),45 mg/0.5 mL,Injection,Subcutaneous,001,09/25/2009,,,Rx
Samsung Bioepis,999901,PYZCHIVA,ustekinumab-ttwe,351(k) Biosimilar,45 mg/0.5 mL,Injection,Subcutaneous,001,06/28/2024,STELARA,ustekinumab,Rx
Samsung Bioepis,999901,PYZCHIVA,ustekinumab-ttwe,351(k) Biosimilar,90 mg/mL,Injection,Subcutaneous,002,06/28/2024,STELARA,ustekinumab,Rx
Amgen,999902,WEZLANA,ustekinumab-auub,351(k) Interchangeable,45 mg/0.5 mL,Injection,Subcutaneous,001,10/31/2023,STELARA,ustekinumab,Rx
Bio-Thera,999905,STARJEMZA,ustekinumab-hmny,351(k) Biosimilar,45 mg/0.5 mL,Injection,Subcutaneous,001,05/22/2025,STELARA,ustekinumab,Rx
AbbVie,125057,HUMIRA,adalimumab,351(a),40 mg/0.8 mL,Injection,Subcutaneous,001,12/31/2002,,,Rx
"""

FDA_TOC = "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2025/999901Orig1s000TOC.cfm"
FDA_LTR = "https://www.accessdata.fda.gov/drugsatfda_docs/appletter/2024/999901Orig1s000ltr.pdf"
FDA_REVIEW = "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2025/999901Orig1s000MultidisciplineR.pdf"


def drugsatfda_zip() -> bytes:
    tables = {
        "Applications.txt": "ApplNo\tApplType\tApplPublicNotes\tSponsorName\n"
                            "999901\tBLA\t\tSAMSUNG BIOEPIS\n999902\tBLA\t\tAMGEN\n125261\tBLA\t\tJANSSEN\n",
        "Products.txt": "ApplNo\tProductNo\tForm\tStrength\tReferenceDrug\tDrugName\tActiveIngredient\tReferenceStandard\n"
                        "999901\t001\tINJECTABLE\t45MG\t0\tPYZCHIVA\tUSTEKINUMAB-TTWE\t0\n"
                        "999902\t001\tINJECTABLE\t45MG\t0\tWEZLANA\tUSTEKINUMAB-AUUB\t0\n"
                        "125261\t001\tINJECTABLE\t45MG\t0\tSTELARA\tUSTEKINUMAB\t0\n",
        "Submissions.txt": "ApplNo\tSubmissionClassCodeID\tSubmissionType\tSubmissionNo\tSubmissionStatus\tSubmissionStatusDate\tSubmissionsPublicNotes\tReviewPriority\n"
                           "999901\t\tORIG\t1\tAP\t2024-06-28 00:00:00\t\tSTANDARD\n",
        "ApplicationDocs.txt": "ApplicationDocsID\tApplicationDocsTypeID\tApplNo\tSubmissionType\tSubmissionNo\tApplicationDocsTitle\tApplicationDocsURL\tApplicationDocsDate\n"
                               f"1\t1\t999901\tORIG\t1\t\t{FDA_LTR.replace('https://', 'http://')}\t2024-06-28 00:00:00\n"
                               f"2\t2\t999901\tORIG\t1\t\t{FDA_TOC}\t2025-02-01 00:00:00\n",
        "ApplicationsDocsType_Lookup.txt": "ApplicationDocsType_Lookup_ID\tApplicationDocsType_Lookup_Description\n"
                                           "1\tLetter\n2\tReview\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in tables.items():
            z.writestr(name, text)
    return buf.getvalue()


TOC_HTML = f"""<html><body><ul>
<li><a href="999901Orig1s000MultidisciplineR.pdf">Multi-Discipline Review</a></li>
<li><a href="/drugsatfda_docs/appletter/2024/999901Orig1s000ltr.pdf">Approval Letter</a></li>
</ul></body></html>"""

DPD_PRODUCTS = {
    "Pyzchiva": [{"drug_code": 11, "brand_name": "PYZCHIVA", "drug_identification_number": "09990011",
                  "company_name": "SAMSUNG BIOEPIS CO., LTD."},
                 {"drug_code": 12, "brand_name": "PYZCHIVA", "drug_identification_number": "09990012",
                  "company_name": "SAMSUNG BIOEPIS CO., LTD."}],
    "Wezlana": [{"drug_code": 21, "brand_name": "WEZLANA", "drug_identification_number": "09990021",
                 "company_name": "AMGEN CANADA INC"}],
    "ustekinumab": [{"drug_code": 31, "brand_name": "JAMTEKI", "drug_identification_number": "09990031",
                     "company_name": "JAMP"}],
}
DPD_INGREDIENT = {11: "USTEKINUMAB", 12: "USTEKINUMAB", 21: "USTEKINUMAB", 31: "USTEKINUMAB"}
DPD_STATUS = {11: "2024-08-15", 12: "2024-08-20", 21: "2023-12-27", 31: "2023-11-30"}

CTGOV = {"studies": [
    {"protocolSection": {
        "identificationModule": {"nctId": "NCT00000001", "briefTitle": "SB17 vs Stelara in plaque psoriasis",
                                 "orgStudyIdInfo": {"id": "SB17-3001"}},
        "designModule": {"phases": ["PHASE3"]},
        "conditionsModule": {"conditions": ["Plaque Psoriasis"]},
        "outcomesModule": {"primaryOutcomes": [{"measure": "Percent change in PASI at Week 12"}]},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Samsung Bioepis"}}}},
    {"protocolSection": {"identificationModule": {"nctId": "NCT00000099", "briefTitle": "Unrelated study"}}},
]}

GUIDANCE_URL = ("https://www.canada.ca/en/health-canada/services/drugs-health-products/biologics-radiopharmaceuticals-"
                "genetic-therapies/applications-submissions/guidance-documents/"
                "information-submission-requirements-biosimilar-biologic-drugs/summary-changes.html")


@pytest.fixture
def db_url(tmp_path) -> str:
    """SQLite per test by default; set RA_TEST_DB_URL (e.g. postgresql+psycopg://ra:ra@localhost/ra_test)
    to run the same tests on PostgreSQL. The schema is dropped before each test."""
    url = os.environ.get("RA_TEST_DB_URL")
    if not url:
        return f"sqlite:///{tmp_path / 'ra.db'}"
    from sqlalchemy import create_engine
    from ra_ingest.db import Base
    eng = create_engine(url)
    Base.metadata.drop_all(eng)
    eng.dispose()
    return url


@pytest.fixture
def fetcher() -> Fetcher:
    return Fetcher(retries=1, host_delay=0)


@pytest.fixture
def mocked():
    """All agency endpoints answered with fixture data; any other URL -> 404."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rs:
        rs.get(MEDICINES_JSON, json=EMA_MEDICINES)
        rs.get(DOCUMENTS_JSON, json=EMA_DOCS)
        for d in EMA_DOCS["data"]:
            rs.get(d["URL"], body=b"%PDF-1.7 " + d["URL"].encode(), content_type="application/pdf")
        months = purple_book_candidates(date.today())
        rs.get(months[0][1], status=404)                  # current month not published yet
        rs.get(months[1][1], body=PURPLE_BOOK_CSV, content_type="text/csv")
        rs.get(DRUGSATFDA_ZIP, body=drugsatfda_zip(), content_type="application/zip")
        rs.get(FDA_LTR, body=b"%PDF-1.4 letter", content_type="application/pdf")
        rs.get(FDA_REVIEW, body=b"%PDF-1.4 review", content_type="application/pdf")
        rs.get(FDA_TOC, body=TOC_HTML, content_type="text/html")

        def dpd(request):
            q = dict(re.findall(r"[?&]([^=&]+)=([^&]*)", request.url))
            resource = request.url.split("/api/drug/")[1].split("/")[0]
            if resource == "drugproduct":
                return 200, {}, __import__("json").dumps(DPD_PRODUCTS.get(q.get("brandname", ""), []))
            code = int(q["id"])
            if resource == "activeingredient":
                return 200, {}, __import__("json").dumps([{"ingredient_name": DPD_INGREDIENT[code]}])
            if resource == "status":
                return 200, {}, __import__("json").dumps([{"status": "MARKETED", "history_date": DPD_STATUS[code]}])
            return 404, {}, "[]"
        rs.add_callback(responses.GET, re.compile(re.escape(DPD_API) + r"/.*"), callback=dpd)
        rs.get(re.compile(r"https://clinicaltrials\.gov/api/v2/studies.*"), json=CTGOV)
        rs.get(GUIDANCE_URL, body="<html>summary of changes</html>", content_type="text/html")
        rs.get(re.compile(r"https://www\.(fda|ema)\.(gov|europa\.eu)/.*"), status=404)
        yield rs
