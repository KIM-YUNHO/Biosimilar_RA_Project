"""Mocked agency endpoints. Formats follow the published descriptions (see
docs/ra-document-sources.md); identifiers here are fake test values."""
from __future__ import annotations

import io
import json
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

PURPLE_BOOK_CSV = """Purple Book Monthly Historical Data Changes Report - September 2026,,,,
,,,,
Newly Approved Products (N)  / Products Added in Current Release (R) / Updated Products (U),,,,
N/R/U,Applicant,BLA Number,Proprietary Name,Proper Name,License Type,Strength,Dosage Form,Route of Administration,Product Presentation,Marketing Status,Licensure,Approval Date,Inter. Approval Date,Ref. Product Proper Name,Ref. Product Proprietary Name,Supplement Number,Submission Type,Product Number
N,Bio-Thera,999905,Starjemza,ustekinumab-hmny,351(k) Biosimilar,45MG/0.5ML,Injection ,Subcutaneous,Pre-Filled Syringe,Rx,Licensed,"May 22, 2025",,ustekinumab,Stelara,,Original,001
,,,,
Purple Book Database,,,,
N/R/U,Applicant,BLA Number,Proprietary Name,Proper Name,License Type,Strength,Dosage Form,Route of Administration,Product Presentation,Marketing Status,Licensure,Approval Date,Inter. Approval Date,Ref. Product Proper Name,Ref. Product Proprietary Name,Supplement Number,Submission Type,Product Number
,"Janssen Biotech, Inc.",125261,Stelara,ustekinumab,351(a),45MG/0.5ML,Injection ,Subcutaneous,Single-Dose Vial,Rx,Licensed,"September 25, 2009",,,,,Original,001
,"Samsung Bioepis Co., Ltd.",999901,Pyzchiva,ustekinumab-ttwe,351(k) Interchangeable,45MG/0.5ML,Injection ,Subcutaneous,Pre-Filled Syringe,Rx,Licensed,"June 28, 2024","April 30, 2025",ustekinumab,Stelara,,Original,001
,"Samsung Bioepis Co., Ltd.",999901,Pyzchiva,ustekinumab-ttwe,351(k) Interchangeable,45MG/0.5ML,Injection ,Subcutaneous,Autoinjector,Rx,Licensed,"June 27, 2025","April 30, 2025",ustekinumab,Stelara,1,Supplement,004
,Amgen Inc.,999902,Wezlana,ustekinumab-auub,351(k) Interchangeable,45MG/0.5ML,Injection ,Subcutaneous,Pre-Filled Syringe,Rx,Licensed,"October 31, 2023","October 31, 2023",ustekinumab,Stelara,,Original,001
,Amgen Inc.,999902,Wezlana,ustekinumab-auub,351(k) Biosimilar,45MG/0.5ML,Injection ,Subcutaneous,Autoinjector,Rx,Licensed,"December 26, 2024",,ustekinumab,Stelara,1,Supplement,004
,"Bio-Thera Solutions, Ltd.",999905,Starjemza,ustekinumab-hmny,351(k) Interchangeable,45MG/0.5ML,Injection ,Subcutaneous,Pre-Filled Syringe,Rx,Licensed,"May 22, 2025","May 22, 2025",ustekinumab,Stelara,,Original,001
,AbbVie Inc.,125057,Humira,adalimumab,351(a),40MG/0.8ML,Injection ,Subcutaneous,Kit,Rx,Licensed,"December 31, 2002",,,,,Original,001
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


TOC_HTML = """<html><head><script>
var pdfBaseName = "999901Orig1s000";
var pdfFiles = {
    approv: 1,
    lbl: 0,
    chemR: 0,
    multidisciplineR: 1,
};
function populatePageContent() {
    var approvalHtml = '';
    if (pdfFiles.approv == 1) {
        approvalHtml += '<li><a href="' + pdfBaseName + 'Approv.pdf" title="Go to approval letter" target="_blank">Approval Letter(s)</a> (PDF)</li>';
    }
    if (pdfFiles.lbl == 1) {
        approvalHtml += '<li><a href="' + pdfBaseName + 'Lbl.pdf" title="Go to printed labeling" target="_blank">Printed Labeling</a> (PDF)</li>';
    }
    var reviewHtml = '';
    if (pdfFiles.chemR == 1) {
        reviewHtml += '<li><a href="' + pdfBaseName + 'ChemR.pdf" title="Go to product quality review(s)" target="_blank">Product Quality Review(s)</a> (PDF)</li>';
    }
    if (pdfFiles.multidisciplineR == 1) {
        reviewHtml += '<li><a href="' + pdfBaseName + 'MultidisciplineR.pdf" title="Go to multi-discipline review" target="_blank">Multi-Discipline Review</a> (PDF)</li>';
    }
}
</script></head><body></body></html>"""
FDA_APPROV = "https://www.accessdata.fda.gov/drugsatfda_docs/nda/2025/999901Orig1s000Approv.pdf"

DPD_PRODUCTS = {
    "Pyzchiva": [{"drug_code": 11, "brand_name": "PYZCHIVA", "drug_identification_number": "09990011",
                  "company_name": "SAMSUNG BIOEPIS CO., LTD."},
                 {"drug_code": 12, "brand_name": "PYZCHIVA", "drug_identification_number": "09990012",
                  "company_name": "SAMSUNG BIOEPIS CO., LTD."}],
    "Wezlana": [{"drug_code": 21, "brand_name": "WEZLANA", "drug_identification_number": "09990021",
                 "company_name": "AMGEN CANADA INC"}],
}
DPD_BY_ID = {31: {"drug_code": 31, "brand_name": "JAMTEKI", "drug_identification_number": "09990031",
                  "company_name": "JAMP"}}
DPD_PRODUCTS["Pyzchiva"].append({"drug_code": 13, "brand_name": "PYZCHIVA I.V.",
                                 "drug_identification_number": "09990013",
                                 "company_name": "SAMSUNG BIOEPIS CO., LTD."})
DPD_INGREDIENT = {13: "USTEKINUMAB", 11: "USTEKINUMAB", 12: "USTEKINUMAB", 21: "USTEKINUMAB", 31: "USTEKINUMAB"}
DPD_STATUS = {13: "2024-09-01", 11: "2024-08-15", 12: "2024-08-20", 21: "2023-12-27", 31: "2023-11-30"}

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
        rs.get(FDA_APPROV, body=b"%PDF-1.4 approv", content_type="application/pdf")

        def dpd(request):
            q = dict(re.findall(r"[?&]([^=&]+)=([^&]*)", request.url))
            resource = request.url.split("/api/drug/")[1].split("/")[0]
            if resource == "drugproduct":
                if "id" in q:
                    return 200, {}, json.dumps([DPD_BY_ID[int(q["id"])]] if int(q["id"]) in DPD_BY_ID else [])
                return 200, {}, json.dumps(DPD_PRODUCTS.get(q.get("brandname", ""), []))
            if resource == "activeingredient" and "ingredientname" in q:
                return 200, {}, json.dumps([{"drug_code": c, "ingredient_name": i} for c, i in DPD_INGREDIENT.items()])
            code = int(q["id"])
            if resource == "activeingredient":
                return 200, {}, json.dumps([{"ingredient_name": DPD_INGREDIENT[code]}])
            if resource == "status":
                return 200, {}, json.dumps([{"status": "MARKETED", "history_date": DPD_STATUS[code]}])
            return 404, {}, "[]"
        rs.add_callback(responses.GET, re.compile(re.escape(DPD_API) + r"/.*"), callback=dpd)
        def dhpp(request):
            q = dict(re.findall(r"[?&]([^=&]+)=([^&]*)", request.url))
            term, page = q.get("search", "").lower(), q.get("page", "0")
            items = [(f"SBD000{i}", t) for i, t in enumerate([
                "Summary Basis of Decision for Pyzchiva and Pyzchiva I.V.",
                "Regulatory Decision Summary for Pyzchiva / Pyzchiva I.V. (ustekinumab)",
                "Summary Basis of Decision for Wezlana/Wezlana I.V.",
                "Regulatory Decision Summary for Jamteki / Jamteki I.V."])]
            items = [(("RDS" if t.startswith("Reg") else "SBD") + d[3:], t) for d, t in items]
            hits = [(d, t) for d, t in items if term in t.lower() or term == "ustekinumab"] if page == "0" else []
            body = "".join(f'<a href="/review-documents/resource/{d}">{t}</a>' for d, t in hits)
            return 200, {"Content-Type": "text/html"}, f"<html>{body}</html>"
        rs.add_callback(responses.GET, re.compile(r"https://dhpp\.hpfb-dgpsa\.ca/review-documents\?.*"), callback=dhpp)
        rs.get(re.compile(r"https://dhpp\.hpfb-dgpsa\.ca/review-documents/resource/SBD.*"),
               body="<html><p>Date SBD issued: 2025-02-18</p><table><tr><td>NOC: 2024-07-29</td>"
                    "<td>NOC issued for the New Drug Submission</td></tr></table></html>", content_type="text/html")
        rs.get(re.compile(r"https://dhpp\.hpfb-dgpsa\.ca/review-documents/resource/RDS.*"),
               body="<html><p>Date of decision: 2025-11-21</p></html>", content_type="text/html")
        def dpd_page(request):
            code = dict(re.findall(r"[?&]([^=&]+)=([^&]*)", request.url)).get("code")
            pm = {"11": "00099911", "12": "00099911", "13": "00099911", "21": "00099921"}.get(code)
            body = ("<html><p>Product Monograph/Veterinary Labelling:</p><p>Date: 2026-07-27</p>"
                    f'<a href="https://pdf.hres.ca/dpd_pm/{pm}.PDF">Product monograph (PDF)</a></html>') if pm else "<html></html>"
            return 200, {"Content-Type": "text/html"}, body
        rs.add_callback(responses.GET, re.compile(r"https://health-products\.canada\.ca/dpd-bdpp/info.*"), callback=dpd_page)
        rs.get(re.compile(r"https://pdf\.hres\.ca/dpd_pm/.*"), body=b"%PDF-1.6 pm", content_type="application/pdf")
        rs.get(re.compile(r"https://clinicaltrials\.gov/api/v2/studies.*"), json=CTGOV)
        rs.get(GUIDANCE_URL, body="<html>summary of changes</html>", content_type="text/html")
        rs.get(re.compile(r"https://www\.(fda|ema)\.(gov|europa\.eu)/.*"), status=404)
        yield rs
