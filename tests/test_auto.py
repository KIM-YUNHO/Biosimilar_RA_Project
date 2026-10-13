"""Requests for products and ingredients the registry does not list, blocked hosts, refill and
the HTML fingerprint. Agencies are the mocked endpoints of conftest.py."""
from __future__ import annotations

import json
import re

import pytest
import responses
from sqlalchemy import select

from conftest import FDA_LTR, FDA_TOC
from ra_ingest.autoprogram import build_programs, holder_key
from ra_ingest.connectors.ema import MEDICINES_JSON
from ra_ingest.connectors.fda import DAF_OVERVIEW, DRUGSATFDA_ZIP, parse_daf_overview
from ra_ingest.db import (DocumentRow, DocumentVersionRow, IngestEventRow, IngestRunRow, ProgramRow,
                          RegistrationRow, Store)
from ra_ingest.http import FetchError, Fetcher
from ra_ingest.migrate import load_all_tables, rekey_html_versions
from ra_ingest.models import RegistrationRecord
from ra_ingest.objstore import LocalStore
from ra_ingest.pipeline import IngestConfig, run_ingest
from ra_ingest.storage import RawStore, fingerprint

APOLOGY = "https://www.fda.gov/apology_objects/abuse-detection-apology.html"
EMA_AR = ("https://www.ema.europa.eu/en/documents/assessment-report/"
          "pyzchiva-epar-public-assessment-report_en.pdf")


@pytest.fixture
def no_registry(tmp_path):
    p = tmp_path / "empty_registry.yaml"
    p.write_text("ingredients: {}\nprograms: []\n", encoding="utf-8")
    return p


def _repeatable(mock):
    """`responses` drops the first of several matching answers after it was used once. The
    fixture's catch-all 404 for fda/ema URLs would make every exact answer one-shot, and
    these tests fetch the same URL in several runs."""
    registered = mock.registered()
    registered[:] = [r for r in registered
                     if not (hasattr(r.url, "pattern") and r.url.pattern.startswith(r"https://www\.(fda|ema)"))]


def _swap(mock, url, **response):
    """Answer `url` differently from now on (the new answer goes first)."""
    new = mock.get(url, **response)
    registered = mock.registered()
    registered[:] = [new] + [r for r in registered if r is not new]


def _cfg(tmp_path, db_url, **kw):
    base = dict(ingredient="ustekinumab", agencies=["ema", "fda", "hc"], db_url=db_url, data_dir=tmp_path)
    base.update(kw)
    return IngestConfig(**base)


# --- a request is enough: no registry entry --------------------------------------------
def test_ingredient_without_registry_entry_builds_programs_from_catalogs(mocked, fetcher, tmp_path, db_url,
                                                                         no_registry):
    cfg = _cfg(tmp_path, db_url, registry_path=no_registry)
    res = run_ingest(cfg, fetcher=fetcher)
    assert res.stats["auto_programs"] == 7 and res.stats["unmapped_registrations"] == 0
    with Store(db_url).session() as s:
        programs = {p.id: p for p in s.scalars(select(ProgramRow))}
        assert set(programs) == {"Stelara", "Pyzchiva", "Wezlana", "Starjemza", "Usymro", "Otulfi", "Jamteki"}
        assert programs["Stelara"].role == "reference" and programs["Pyzchiva"].role == "biosimilar"
        # EU and US/CA brands of one programme end up together (similar_name), EMA duplicates too
        assert programs["Wezlana"].aliases["ema"] == ["Wezenla"]
        assert set(programs["Pyzchiva"].aliases) == {"fda", "ema", "hc"}
        regs = {(r.agency, r.brand_name): r.program_id for r in s.scalars(select(RegistrationRow))}
        assert regs[("ema", "Wezenla")] == regs[("fda", "Wezlana")] == regs[("hc", "Wezlana")] == "Wezlana"
        assert regs[("hc", "Pyzchiva")] == regs[("fda", "Pyzchiva")] == "Pyzchiva"
        docs = s.scalars(select(DocumentRow)).all()
        by_prog = {(d.program_id, d.agency) for d in docs}
        assert {("Pyzchiva", "ema"), ("Pyzchiva", "fda"), ("Pyzchiva", "hc"), ("Wezlana", "ema")} <= by_prog
        assert any(d.program_id == "Jamteki" and d.agency == "hc" for d in docs)
        events = [e for e in s.scalars(select(IngestEventRow)) if e.kind == "auto_program"]
        assert len(events) == 7
        wez = next(e for e in events if e.program_id == "Wezlana")
        assert "similar_name" in wez.message and "ema=Wezenla" in wez.message


def test_unknown_product_request_ingests_only_its_program(mocked, fetcher, tmp_path, db_url, no_registry):
    cfg = _cfg(tmp_path, db_url, ingredient=None, products=["Wezlana"], registry_path=no_registry)
    res = run_ingest(cfg, fetcher=fetcher)
    assert res.status in ("ok", "partial")
    with Store(db_url).session() as s:
        assert [p.id for p in s.scalars(select(ProgramRow))] == ["Wezlana"]
        regs = {(r.agency, r.brand_name) for r in s.scalars(select(RegistrationRow))}
        assert regs == {("fda", "Wezlana"), ("hc", "Wezlana"), ("ema", "Wezenla")}   # not Pyzchiva, Otulfi, ...
        urls = {d.source_url for d in s.scalars(select(DocumentRow))}
        assert any("wezenla-epar" in u for u in urls) and not any("pyzchiva" in u for u in urls)


def test_unknown_product_found_nowhere_is_reported(mocked, fetcher, tmp_path, db_url, no_registry):
    cfg = _cfg(tmp_path, db_url, ingredient=None, products=["Nonexistentia"], registry_path=no_registry)
    res = run_ingest(cfg, fetcher=fetcher)
    assert res.status == "failed"
    with Store(db_url).session() as s:
        assert {e.kind for e in s.scalars(select(IngestEventRow))} >= {"no_match"}
        assert s.scalars(select(ProgramRow)).all() == [] and s.scalars(select(DocumentRow)).all() == []


def test_registry_ingredient_run_maps_the_rest_but_not_the_holdout(mocked, fetcher, tmp_path, db_url):
    cfg = _cfg(tmp_path, db_url, agencies=["ema", "hc"])             # config/products.yaml, BAT2206 held out
    run_ingest(cfg, fetcher=fetcher)
    with Store(db_url).session() as s:
        ids = {p.id for p in s.scalars(select(ProgramRow))}
        assert {"SB17", "ABP654", "Otulfi", "Jamteki"} <= ids          # curated + derived
        assert "Usymro" not in ids and "BAT2206" not in ids            # holdout: neither mapped nor derived
        usymro = s.scalar(select(RegistrationRow).where(RegistrationRow.brand_name == "Usymro"))
        assert usymro is not None and usymro.program_id is None


def test_no_auto_programs_keeps_the_registry_only_behaviour(mocked, fetcher, tmp_path, db_url):
    cfg = _cfg(tmp_path, db_url, agencies=["ema"], auto_programs=False)
    res = run_ingest(cfg, fetcher=fetcher)
    assert res.stats["auto_programs"] == 0 and res.stats["unmapped_registrations"] >= 1


# --- FDA bot detection ---------------------------------------------------------------------
def test_bot_detection_page_is_blocked_not_404_and_host_is_left_alone():
    with responses.RequestsMock() as rs:
        rs.get("https://www.fda.gov/media/1/download", status=302, headers={"Location": APOLOGY})
        rs.get(APOLOGY, status=404, body="x")
        f = Fetcher(retries=3, host_delay=0)
        with pytest.raises(FetchError) as e:
            f.get("https://www.fda.gov/media/1/download")
        assert e.value.blocked and "bot detection" in e.value.reason
        with pytest.raises(FetchError, match="skipped"):    # no request: nothing registered for it
            f.get("https://www.fda.gov/media/2/download")
        assert f.blocked_hosts() == ["www.fda.gov"]


OVERVIEW = f"""<html><table id="exampleApplOrig"><tbody>
<tr><td>06/28/2024</td><td>ORIG-1</td><td>Approval</td><td></td><td>N/A</td><td>
<a href="{FDA_LTR}" title="Links to Letter">Letter (PDF)</a><br/>
<a href="{FDA_TOC}" title="Links to Review">Review</a></td><td></td><td>{FDA_LTR}</td></tr>
</tbody></table><table id="exampleApplSuppl"><tbody>
<tr><td>06/27/2025</td><td>SUPPL-4</td><td>Labeling</td><td>
<a href="https://www.accessdata.fda.gov/drugsatfda_docs/label/2025/999901s004lbl.pdf#page=3">Label (PDF)</a></td>
<td></td></tr></tbody></table></html>"""


def test_parse_overview_rows_and_links():
    rows = parse_daf_overview(OVERVIEW)
    assert [(r["date"], r["submission"], [l for _, l in r["links"]]) for r in rows] == [
        ("2024-06-28", "ORIG-1", ["Letter (PDF)", "Review"]), ("2025-06-27", "SUPPL-4", ["Label (PDF)"])]


def test_fda_zip_blocked_documents_come_from_the_overview_page(mocked, fetcher, tmp_path, db_url):
    _swap(mocked, DRUGSATFDA_ZIP, status=302, headers={"Location": APOLOGY})
    mocked.get(APOLOGY, status=404, body="blocked")
    mocked.get(re.compile(re.escape(DAF_OVERVIEW) + r"\?.*ApplNo=999901.*"), body=OVERVIEW, content_type="text/html")
    mocked.get("https://www.accessdata.fda.gov/drugsatfda_docs/label/2025/999901s004lbl.pdf",
               body=b"%PDF-1.4 label", content_type="application/pdf")
    cfg = _cfg(tmp_path, db_url, agencies=["fda"], auto_programs=False, products=["Pyzchiva"], ingredient=None)
    res = run_ingest(cfg, fetcher=fetcher)
    with Store(db_url).session() as s:
        docs = {d.source_url: d for d in s.scalars(select(DocumentRow))}
        assert docs[FDA_LTR].fetch_status == "fetched" and docs[FDA_LTR].procedure_id == "ORIG-1"
        label = docs["https://www.accessdata.fda.gov/drugsatfda_docs/label/2025/999901s004lbl.pdf"]
        assert label.doc_type == "label" and label.fetch_status == "fetched"
        assert label.extra.get("page_fragment") == "page=3"
        assert any(d.parent_document_id for d in docs.values())          # TOC expanded as before
        kinds = {e.kind for e in s.scalars(select(IngestEventRow))}
        assert "source_blocked" in kinds and "host_blocked" in kinds
    assert res.status in ("ok", "partial")


# --- a failed fetch does not undo an earlier one; known documents are refilled ----------------
def test_failed_refetch_keeps_the_version_and_refill_restores_a_missing_raw_file(mocked, fetcher, tmp_path, db_url):
    _repeatable(mocked)
    cfg = _cfg(tmp_path, db_url, agencies=["ema"], auto_programs=False)
    run_ingest(cfg, fetcher=fetcher)
    with Store(db_url).session() as s:
        row = s.scalar(select(DocumentRow).where(DocumentRow.source_url == EMA_AR))
        assert row.fetch_status == "fetched"
        key = s.scalar(select(DocumentVersionRow).where(DocumentVersionRow.document_id == row.id)).storage_path

    _swap(mocked, EMA_AR, status=500)                                     # 1: the source breaks
    res = run_ingest(cfg, fetcher=fetcher)
    with Store(db_url).session() as s:
        row = s.scalar(select(DocumentRow).where(DocumentRow.source_url == EMA_AR))
        assert row.fetch_status == "fetched" and row.fetch_error == "HTTP 500"
        assert len(s.scalars(select(DocumentVersionRow).where(DocumentVersionRow.document_id == row.id)).all()) == 1
    assert res.stats["fetch_failed"] >= 1

    (tmp_path / key).unlink()                                             # 2: raw file lost, catalog down
    _swap(mocked, EMA_AR, body=b"%PDF-1.7 " + EMA_AR.encode(), content_type="application/pdf")
    _swap(mocked, MEDICINES_JSON, status=500)
    res = run_ingest(cfg, fetcher=fetcher)
    assert (tmp_path / key).exists() and res.stats["refilled"] >= 1
    with Store(db_url).session() as s:
        row = s.scalar(select(DocumentRow).where(DocumentRow.source_url == EMA_AR))
        assert len(s.scalars(select(DocumentVersionRow).where(DocumentVersionRow.document_id == row.id)).all()) == 1


def test_crash_is_recorded_not_left_running(mocked, fetcher, tmp_path, db_url, monkeypatch):
    def boom(self, target, agencies):
        raise RuntimeError("boom")
    monkeypatch.setattr("ra_ingest.pipeline._Run._collect", boom)
    with pytest.raises(RuntimeError):
        run_ingest(_cfg(tmp_path, db_url, agencies=["ema"]), fetcher=fetcher)
    with Store(db_url).session() as s:
        run = s.scalars(select(IngestRunRow)).one()
        assert run.status == "failed"


# --- HTML fingerprint -------------------------------------------------------------------------
def _hc_page(day: str, body: str = "text") -> bytes:
    return (f'<html><body><main><h1>RDS</h1><p>{body}</p><dl id="wb-dtmd"><dt>Date modified:</dt>'
            f'<dd><time property="dateModified">{day}</time></dd></dl></main></body></html>').encode()


def _toc_page(token: str, body: str = "toc") -> bytes:
    return (f'<html><body><p>{body}</p><script type="text/javascript"  '
            f'src="/{token}/5Jn7Qwg8t/VJsfnkjO0/p3OfLV6hbh6DrtbOYY/YhJzb2w0Ag/cw/lCEkJ7Kgs"></script></body></html>').encode()


def test_fingerprint_ignores_the_request_day_and_the_bot_manager_script():
    assert fingerprint(_hc_page("2026-10-08"), ".html") == fingerprint(_hc_page("2026-10-12"), ".html")
    assert fingerprint(_hc_page("2026-10-08"), ".html") != fingerprint(_hc_page("2026-10-08", "new text"), ".html")
    assert fingerprint(_toc_page("Y9jQ9Po1k"), ".html") == fingerprint(_toc_page("Zq1w2e3r4"), ".html")
    assert fingerprint(_toc_page("Y9jQ9Po1k"), ".html") != fingerprint(_toc_page("Y9jQ9Po1k", "other"), ".html")


def test_rekey_moves_a_page_to_the_current_fingerprint(tmp_path, db_url):
    from ra_prep.store import PrepDocumentRow

    load_all_tables()
    store = LocalStore(tmp_path)
    st = Store(db_url)
    page = _hc_page("2026-10-12")
    new_sha = fingerprint(page, ".html")
    old_sha, mid_sha = "a" * 64, "b" * 64                       # old fingerprints (they held the date)
    new_key_v1 = RawStore.key("hc", mid_sha, ".html")
    store.put_bytes(new_key_v1, page)
    store.put_bytes(f"parsed/{old_sha}.json", json.dumps({"sha256": old_sha, "blocks": []}).encode())
    with st.session() as s:
        doc = DocumentRow(agency="hc", doc_kind="product", doc_type="rds", title="RDS", fetch_status="fetched",
                          source_url="https://dhpp.example/RDS1")
        s.add(doc)
        s.flush()
        s.add_all([
            DocumentVersionRow(document_id=doc.id, sha256=old_sha, size_bytes=len(page) - 3,   # raw not in store
                               storage_path=RawStore.key("hc", old_sha, ".html")),
            DocumentVersionRow(document_id=doc.id, sha256=mid_sha, size_bytes=len(page), storage_path=new_key_v1),
        ])
        s.add(PrepDocumentRow(document_id=doc.id, sha256=old_sha, engine="prep-x", status="ok",
                              output_path=f"parsed/{old_sha}.json"))
        s.commit()
        doc_id = doc.id

    dry = rekey_html_versions(db_url, store, dry_run=True, log=lambda *_: None)
    assert dry["rekeyed"] == 1
    stats = rekey_html_versions(db_url, store, log=lambda *_: None)
    assert stats["rekeyed"] == 1 and stats["duplicates_removed"] == 1
    with st.session() as s:
        vs = s.scalars(select(DocumentVersionRow).where(DocumentVersionRow.document_id == doc_id)).all()
        assert [(v.sha256, v.storage_path) for v in vs] == [(new_sha, RawStore.key("hc", new_sha, ".html"))]
        prep = s.scalar(select(PrepDocumentRow))
        assert prep.sha256 == new_sha and prep.output_path == f"parsed/{new_sha}.json"
    assert json.loads(store.read_text(f"parsed/{new_sha}.json"))["sha256"] == new_sha
    assert rekey_html_versions(db_url, store, log=lambda *_: None)["rekeyed"] == 0     # idempotent


# --- program derivation, without the pipeline ---------------------------------------------------
def _reg(agency, brand, holder=None, lic=None, key=None, **ident):
    return RegistrationRecord(agency=agency, native_key=key or f"{agency}:{brand}", brand_name=brand,
                              holder=holder, licence_type=lic, identifiers=ident)


def test_holder_key_drops_legal_words():
    assert holder_key("Samsung Bioepis NL B.V.") == holder_key("SAMSUNG BIOEPIS CO., LTD.") == "samsung"
    assert holder_key("Amgen Europe B.V.") == "amgen" and holder_key(None) == ""


def test_derivation_joins_duplicates_and_translated_brands_but_not_other_programmes():
    regs = [
        _reg("fda", "Stelara", "Janssen Biotech, Inc.", "351(a)"),
        _reg("ema", "Stelara", "Janssen-Cilag International NV", "centrally_authorised"),
        _reg("ema", "Qoyvolma", "Celltrion Healthcare Hungary Kft.", "biosimilar"),
        _reg("ema", "Steqeyma", "Celltrion Healthcare Hungary Kft.", "biosimilar"),        # same MAH: duplicate
        _reg("fda", "Steqeyma", "Celltrion, Inc.", "351(k) Biosimilar", proper_name="ustekinumab-stba"),
        _reg("ema", "Wezenla", "Amgen Europe B.V.", "biosimilar"),
        _reg("fda", "Wezlana", "Amgen Inc.", "351(k) Interchangeable", proper_name="ustekinumab-auub"),
        _reg("hc", "Jamteki", "JAMP PHARMA CORPORATION"),
    ]
    out = build_programs(regs)
    by_id = {p.id: p for p in out.programs}
    assert set(by_id) == {"Stelara", "Steqeyma", "Wezlana", "Jamteki"}
    assert by_id["Stelara"].role == "reference" and by_id["Steqeyma"].role == "biosimilar"
    assert by_id["Jamteki"].role == "biosimilar"                       # Health Canada flags nothing: not a reference
    assert out.assignment[("ema", "ema:Qoyvolma")] == "Steqeyma"
    assert by_id["Steqeyma"].aliases["fda"] == ["Steqeyma", "ustekinumab-stba"]
    assert by_id["Steqeyma"].aliases["ema"] == ["Qoyvolma", "Steqeyma"]
    assert "same_holder" in out.basis["Steqeyma"] and "similar_name" in out.basis["Wezlana"]
    assert out.basis["Stelara"] == ["same_brand"]


def test_derived_ids_do_not_take_registry_ids():
    out = build_programs([_reg("fda", "Pyzchiva", "Samsung Bioepis", "351(k) Biosimilar")], taken={"pyzchiva"})
    assert [p.id for p in out.programs] == ["Pyzchiva-2"]


def test_one_edit_names_and_one_company_one_programme_are_joined_across_regions():
    regs = [
        _reg("fda", "Amjevita", "Amgen Inc.", "351(k) Biosimilar", proper_name="adalimumab-atto"),
        _reg("ema", "Amgevita", "Amgen Europe B.V.", "biosimilar"),                      # one letter apart
        _reg("ema", "Imraldi", "Samsung Bioepis NL B.V.", "biosimilar"),
        _reg("fda", "Hadlima", "Samsung Bioepis Co., Ltd.", "351(k) Biosimilar"),        # other brand, same company
        _reg("hc", "Hadlima", "SAMSUNG BIOEPIS CO., LTD."),
        _reg("fda", "Humira", "AbbVie Inc.", "351(a)"),
    ]
    out = build_programs(regs)
    assert {p.id for p in out.programs} == {"Amjevita", "Hadlima", "Humira"}
    assert out.assignment[("ema", "ema:Amgevita")] == "Amjevita"
    assert out.assignment[("ema", "ema:Imraldi")] == "Hadlima"
    assert out.basis["Hadlima"] == ["same_brand", "same_holder_cross"] or "same_holder_cross" in out.basis["Hadlima"]


def test_a_company_with_two_programmes_in_one_agency_is_not_merged_across_regions():
    regs = [
        _reg("fda", "Alpha", "Acme Inc.", "351(k) Biosimilar"),
        _reg("fda", "Bravo", "Acme Inc.", "351(k) Biosimilar"),        # a second programme at the same agency
        _reg("ema", "Charlie", "Acme Europe B.V.", "biosimilar"),
    ]
    out = build_programs(regs)
    assert len(out.programs) == 3 and "same_holder_cross" not in {b for v in out.basis.values() for b in v}


def test_an_ema_product_that_is_not_a_biosimilar_is_a_reference_only_without_an_fda_reference():
    ema_only = _reg("ema", "Upstelda", "Amgen Technology (Ireland) UC", "centrally_authorised")
    with_fda = build_programs([_reg("fda", "Stelara", "Janssen Biotech, Inc.", "351(a)"), ema_only])
    assert {p.id: p.role for p in with_fda.programs} == {"Stelara": "reference", "Upstelda": "biosimilar"}
    without = build_programs([_reg("ema", "Stelara", "Janssen-Cilag", "centrally_authorised")])
    assert [p.role for p in without.programs] == ["reference"]


def test_a_curated_reference_program_counts_when_deriving_the_rest():
    out = build_programs([_reg("ema", "Upstelda", "Amgen Technology (Ireland) UC", "centrally_authorised")],
                         has_reference=True)
    assert [(p.id, p.role) for p in out.programs] == [("Upstelda", "biosimilar")]


def test_metadata_only_run_does_not_downgrade_fetched_documents(mocked, fetcher, tmp_path, db_url):
    _repeatable(mocked)
    cfg = _cfg(tmp_path, db_url, agencies=["ema"], auto_programs=False)
    run_ingest(cfg, fetcher=fetcher)
    with Store(db_url).session() as s:
        fetched = {d.id for d in s.scalars(select(DocumentRow)) if d.fetch_status == "fetched"}
    assert fetched
    res = run_ingest(_cfg(tmp_path, db_url, agencies=["ema"], auto_programs=False, download=False), fetcher=fetcher)
    assert res.stats["fetched"] == 0 and res.stats["skipped"] > 0
    with Store(db_url).session() as s:
        assert {d.id for d in s.scalars(select(DocumentRow)) if d.fetch_status == "fetched"} == fetched
