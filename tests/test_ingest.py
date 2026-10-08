from __future__ import annotations

import json
import os

import pytest
import responses
from sqlalchemy import select

from conftest import ROOT, TODAY, PURPLE_BOOK_CSV
from ra_ingest.cli import main as cli_main
from ra_ingest.connectors.fda import FdaConnector, parse_purple_book
from ra_ingest.db import (DocumentRow, DocumentVersionRow, IngestEventRow, RegistrationRow, Store,
                          TrialRow)
from ra_ingest.metadata import build_document_metadata, context_header
from ra_ingest.pipeline import IngestConfig, run_ingest
from ra_ingest.registry import Registry
from ra_ingest.util import to_iso_date

REGISTRY = Registry.load(ROOT / "config" / "products.yaml")


# --- registry / target resolution ---------------------------------------------------
def test_resolve_by_ingredient_excludes_holdout():
    t = REGISTRY.resolve(ingredient="ustekinumab")
    ids = {p.id for p in t.programs}
    assert ids == {"stelara", "SB17", "ABP654", "CT-P43", "BMAB1200"}
    assert "BAT2206" in {p.id for p in REGISTRY.resolve("ustekinumab", include_holdout=True).programs}


@pytest.mark.parametrize("name,expected", [
    ("SB17", "SB17"), ("ct p43", "CT-P43"), ("Wezenla", "ABP654"), ("ustekinumab-auub", "ABP654"),
    ("Usymro", "BAT2206"), ("Bmab 1200", "BMAB1200"),
])
def test_resolve_product_aliases(name, expected):
    t = REGISTRY.resolve(products=[name])
    assert [p.id for p in t.programs] == [expected]
    assert t.product_scoped and t.ingredient == "ustekinumab"


def test_unknown_product_raises():
    with pytest.raises(ValueError, match="레지스트리에 없는 제품"):
        REGISTRY.resolve(products=["NOPE-1"])


# --- parsers -------------------------------------------------------------------------
def test_purple_book_takes_full_list_block():
    rows = parse_purple_book(PURPLE_BOOK_CSV)
    assert len(rows) == 6
    assert rows[0]["Proprietary Name"] == "STELARA"


def test_dates():
    assert to_iso_date("20/04/2024", dayfirst=True) == "2024-04-20"
    assert to_iso_date("06/28/2024") == "2024-06-28"
    assert to_iso_date("2024-06-28 00:00:00") == "2024-06-28"
    assert to_iso_date("") is None


# --- end to end (mocked agencies) --------------------------------------------------
def _cfg(tmp_path, db_url, **kw):
    base = dict(ingredient="ustekinumab", agencies=["ema", "fda", "hc", "guidance", "ctgov"],
                db_url=db_url, data_dir=tmp_path)
    base.update(kw)
    return IngestConfig(**base)



def test_end_to_end(mocked, fetcher, tmp_path, db_url):
    cfg = _cfg(tmp_path, db_url, include_holdout=True)
    res = run_ingest(cfg, fetcher=fetcher, connectors=None)
    store = Store(cfg.db_url)
    with store.session() as s:
        regs = s.scalars(select(RegistrationRow)).all()
        by = {(r.agency, r.program_id): r for r in regs}
        # EMA: alias mapping incl. EU-only names, unmapped same-ingredient product kept
        assert by[("ema", "ABP654")].brand_name == "Wezenla"
        assert by[("ema", "BAT2206")].brand_name == "Usymro"
        assert by[("ema", "SB17")].first_approval_date == "2024-04-20"
        assert ("ema", None) in by and by[("ema", None)].brand_name == "Otulfi"
        assert not any(r.ingredient == "adalimumab" for r in regs)
        # FDA: Purple Book last block, presentations grouped per BLA, interchangeable type
        wez = by[("fda", "ABP654")]
        assert wez.licence_type == "351(k) Interchangeable" and wez.first_approval_date == "2023-10-31"
        assert len(by[("fda", "SB17")].identifiers["presentations"]) == 2
        assert by[("fda", "stelara")].licence_type == "351(a)"
        # HC: DINs grouped per brand+company, earliest status date, unmapped ingredient match kept
        pyz = by[("hc", "SB17")]
        assert pyz.identifiers["din"] == ["09990011", "09990012"] and pyz.first_approval_date == "2024-08-15"
        assert any(r.agency == "hc" and r.brand_name == "Jamteki" and r.program_id is None for r in regs)

        docs = s.scalars(select(DocumentRow)).all()
        urls = {d.source_url: d for d in docs}
        # EMA: English only
        assert not any(u.endswith("_bg.pdf") for u in urls)
        assert urls["https://www.ema.europa.eu/en/documents/assessment-report/"
                    "pyzchiva-epar-public-assessment-report_en.pdf"].doc_type == "assessment_report"
        # FDA: TOC page expanded into its PDFs, http->https, letter de-duplicated by URL
        review = urls["https://www.accessdata.fda.gov/drugsatfda_docs/nda/2025/999901Orig1s000MultidisciplineR.pdf"]
        assert review.doc_type == "review" and review.parent_document_id is not None
        assert review.decision_date == "2024-06-28" and review.procedure_id == "ORIG-1"
        assert sum(1 for u in urls if u.endswith("999901Orig1s000ltr.pdf")) == 1
        # guidance axis
        g = [d for d in docs if d.doc_kind == "guidance"]
        assert len(g) >= 1 and all(d.guidance.get("status") for d in g)

        assert {t.nct_id for t in s.scalars(select(TrialRow))} == {"NCT00000001"}

        events = {(e.kind, e.agency, e.program_id) for e in s.scalars(select(IngestEventRow))}
        assert ("expected_absent", "hc", "BAT2206") in events
        assert ("manual_seed_needed", "hc", "SB17") in events
        assert ("missing_registration", "hc", "CT-P43") in events  # not in the DPD fixture

        # metadata contract
        ar = urls["https://www.ema.europa.eu/en/documents/assessment-report/pyzchiva-epar-public-assessment-report_en.pdf"]
        meta = build_document_metadata(s, ar)
        assert meta["jurisdiction"] == "EU" and meta["program_id"] == "SB17" and meta["brand_name"] == "Pyzchiva"
        assert meta["doc_version_sha256"] and os.path.exists(meta["storage_path"])
        assert "SB-17" in meta["name_variants"] and meta["default_speaker"] == "mixed"
        hdr = context_header(meta, {"section_path": ["2.6 Clinical", "2.6.2 PK", "Discussion"], "speaker": "regulator"})
        assert hdr.startswith("[EU | EMA | assessment_report | Pyzchiva (SB17, ustekinumab)") and "regulator" in hdr
    assert res.stats["fetch_failed"] > 0  # guidance URLs not mocked -> 404, recorded not fatal
    assert res.status == "partial"


def test_rerun_is_idempotent(mocked, fetcher, tmp_path, db_url):
    cfg = _cfg(tmp_path, db_url, agencies=["ema"])
    first = run_ingest(cfg, fetcher=fetcher)
    second = run_ingest(cfg, fetcher=fetcher)
    store = Store(cfg.db_url)
    with store.session() as s:
        assert len(s.scalars(select(DocumentRow)).all()) == first.stats["documents"]
        assert len(s.scalars(select(DocumentVersionRow)).all()) == first.stats["new_versions"]
    assert second.stats["new_versions"] == 0


def test_product_scoped_run_only_touches_that_product(mocked, fetcher, tmp_path, db_url):
    cfg = _cfg(tmp_path, db_url, ingredient=None, products=["Wezenla"], agencies=["ema", "hc"])
    run_ingest(cfg, fetcher=fetcher)
    with Store(cfg.db_url).session() as s:
        regs = s.scalars(select(RegistrationRow)).all()
    assert {(r.agency, r.program_id) for r in regs} == {("ema", "ABP654"), ("hc", "ABP654")}


def test_network_down_is_recorded_not_raised(fetcher, tmp_path, db_url):
    cfg = _cfg(tmp_path, db_url, agencies=["ema", "fda", "hc"])
    with responses.RequestsMock(assert_all_requests_are_fired=False):  # every request -> ConnectionError
        res = run_ingest(cfg, fetcher=fetcher)
    assert res.status == "failed" and res.stats["errors"] >= 3
    with Store(cfg.db_url).session() as s:
        kinds = {e.kind for e in s.scalars(select(IngestEventRow))}
    assert "source_failed" in kinds and "missing_registration" in kinds


def test_cli_status_and_manifest(mocked, fetcher, tmp_path, db_url, capsys, monkeypatch):
    cfg = _cfg(tmp_path, db_url, agencies=["ema"])
    run_ingest(cfg, fetcher=fetcher)
    assert cli_main(["--db", cfg.db_url, "status"]) == 0
    out = capsys.readouterr().out
    assert "SB17" in out and "Pyzchiva" in out
    mf = tmp_path / "manifest.jsonl"
    assert cli_main(["--db", cfg.db_url, "manifest", "--out", str(mf)]) == 0
    lines = [json.loads(l) for l in mf.read_text().splitlines()]
    assert lines and all(l["schema_version"] == "1" and l["agency"] == "ema" for l in lines)
