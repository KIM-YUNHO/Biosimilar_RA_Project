"""Object store keys, path migration and SQLite -> PostgreSQL copy."""
from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import func, select

from ra_ingest.db import DocumentRow, DocumentVersionRow, Store
from ra_ingest.migrate import copy_database, migrate_paths
from ra_ingest.objstore import LocalStore, S3Store, open_store
from ra_ingest.pipeline import IngestConfig, run_ingest
from ra_ingest.storage import RawStore


def test_local_store_keys(tmp_path):
    st = LocalStore(tmp_path)
    st.put_bytes("raw/ema/ab/abc.pdf", b"%PDF-1.7 x")
    assert st.exists("raw/ema/ab/abc.pdf") and st.get_bytes("raw/ema/ab/abc.pdf") == b"%PDF-1.7 x"
    assert st.local_path("raw/ema/ab/abc.pdf") == str(tmp_path / "raw/ema/ab/abc.pdf")
    # legacy absolute paths still resolve, and map back to keys inside the root
    legacy = str(tmp_path / "raw/ema/ab/abc.pdf")
    assert st.resolve(legacy) == legacy and st.key_for(legacy) == "raw/ema/ab/abc.pdf"
    assert st.key_for("/elsewhere/x.pdf") is None
    for bad in ("../x", "/abs/x", ""):
        with pytest.raises(ValueError):
            st.put_bytes(bad, b"")
    assert isinstance(open_store(str(tmp_path)), LocalStore)
    assert isinstance(open_store(f"file://{tmp_path}"), LocalStore)


def test_raw_store_is_content_addressed(tmp_path):
    raw = RawStore(LocalStore(tmp_path))
    sha, key = raw.put("fda", b"%PDF-1.4 same", ".pdf")
    assert key == f"raw/fda/{sha[:2]}/{sha}.pdf"
    assert raw.put("fda", b"%PDF-1.4 same", ".pdf") == (sha, key)


def test_s3_store_roundtrip(tmp_path, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="ra-docs")
        st = S3Store("s3://ra-docs/prod", cache_dir=tmp_path / "cache", client=client)
        assert not st.exists("raw/hc/00/x.html")
        st.put_bytes("raw/hc/00/x.html", b"<main>x</main>")
        assert st.exists("raw/hc/00/x.html")
        assert client.get_object(Bucket="ra-docs", Key="prod/raw/hc/00/x.html")["Body"].read() == b"<main>x</main>"
        local = st.local_path("raw/hc/00/x.html")
        assert open(local, "rb").read() == b"<main>x</main>"
        st.write_text("parsed/x.json", '{"a": 1}')
        assert json.loads(st.read_text("parsed/x.json")) == {"a": 1}


def test_paths_from_another_machine_become_keys(tmp_path):
    from ra_ingest.migrate import _to_key

    st = LocalStore(tmp_path)
    sha = "ab" + "0" * 62
    assert _to_key(f"/home/user/Biosimilar_RA_Project/data/raw/fda/ab/{sha}.pdf", st) == f"raw/fda/ab/{sha}.pdf"
    assert _to_key(f"C:\\work\\data\\parsed\\{sha}.json", st) == f"parsed/{sha}.json"
    assert _to_key(f"raw/fda/ab/{sha}.pdf", st) == f"raw/fda/ab/{sha}.pdf"        # already a key
    assert _to_key("/somewhere/else/old.pdf", st) == "/somewhere/else/old.pdf"    # unknown layout kept
    assert _to_key(None, st) is None


def _ingest(mocked, fetcher, tmp_path, db_url):
    cfg = IngestConfig(ingredient="ustekinumab", agencies=["ema", "fda"], db_url=db_url,
                       data_dir=tmp_path / "store")
    run_ingest(cfg, fetcher=fetcher)
    return cfg


def test_migrate_paths_rewrites_legacy_rows(mocked, fetcher, tmp_path, db_url):
    cfg = _ingest(mocked, fetcher, tmp_path, db_url)
    store = LocalStore(cfg.data_dir)
    st = Store(db_url)
    with st.session() as s:  # make two rows look like the pre-key layout
        rows = s.scalars(select(DocumentVersionRow).order_by(DocumentVersionRow.id)).all()
        keys = {r.id: r.storage_path for r in rows}
        rows[0].storage_path = str(cfg.data_dir / rows[0].storage_path)
        rows[1].storage_path = "/somewhere/else/old.pdf"
        s.commit()
    stats = migrate_paths(db_url, store)
    assert stats["document_versions.storage_path"] == {"rewritten": 1, "outside_store": 1}
    with st.session() as s:
        got = {r.id: r.storage_path for r in s.scalars(select(DocumentVersionRow))}
    assert got[rows[0].id] == keys[rows[0].id]
    assert got[rows[1].id] == "/somewhere/else/old.pdf"


def test_copy_database_sqlite_to_target(mocked, fetcher, tmp_path, db_url):
    src_url = f"sqlite:///{tmp_path / 'src.db'}"
    cfg = _ingest(mocked, fetcher, tmp_path, src_url)
    with Store(src_url).session() as s:  # one legacy absolute path rides along
        v = s.scalars(select(DocumentVersionRow).order_by(DocumentVersionRow.id)).first()
        key = v.storage_path
        v.storage_path = str(cfg.data_dir / key)
        s.commit()
        n_docs = s.scalar(select(func.count()).select_from(DocumentRow))
        parents = {d.id: d.parent_document_id for d in s.scalars(select(DocumentRow))}
    dst_url = db_url if db_url != src_url else f"sqlite:///{tmp_path / 'dst.db'}"
    counts = copy_database(src_url, dst_url, LocalStore(cfg.data_dir), log=lambda *_: None)
    assert counts["documents"] == n_docs
    dst = Store(dst_url)
    with dst.session() as s:
        assert {d.id: d.parent_document_id for d in s.scalars(select(DocumentRow))} == parents
        assert s.get(DocumentVersionRow, v.id).storage_path == key
        if dst.engine.dialect.name == "postgresql":  # SQLite keeps no zone; PostgreSQL gets UTC
            assert s.scalars(select(DocumentRow)).first().first_seen_at.tzinfo is not None
    # sequences continue after the copied ids (PostgreSQL), so later ingests can insert
    res = run_ingest(IngestConfig(ingredient="ustekinumab", agencies=["ema", "fda", "hc"],
                                  db_url=dst_url, data_dir=cfg.data_dir), fetcher=fetcher)
    assert res.status in ("ok", "partial")
    with pytest.raises(SystemExit):  # never copies into a database that already has rows
        copy_database(src_url, dst_url, log=lambda *_: None)


def test_prep_run_writes_parse_to_store(mocked, fetcher, tmp_path, db_url, monkeypatch):
    pytest.importorskip("pypdfium2")
    from conftest import ROOT
    from ra_prep import pipeline as P
    from ra_prep.select import select_documents
    from ra_prep.store import PrepDocumentRow

    store = LocalStore(tmp_path / "store")
    st = Store(db_url)
    pdf = (ROOT / "eval" / "gold" / "pages" / "g01.pdf").read_bytes()
    sha, key = RawStore(store).put("ema", pdf, ".pdf")
    with st.session() as s:
        d = DocumentRow(agency="ema", doc_kind="product", doc_type="assessment_report", title="t",
                        source_url="https://x/ar.pdf", program_id=None, fetch_status="fetched")
        s.add(d)
        s.flush()
        st.add_version(s, d, sha, key, len(pdf), "application/pdf", None, None)
        s.commit()

    class FakeExtractor:
        def version(self):
            return "prep-test+docling"

    def fake_process(sel, extractor):
        assert os.path.exists(sel.path) and sel.path.endswith(key)  # read through the store
        return {"document_id": sel.document_id, "sha256": sel.sha256, "routes": [
            {"page": 1, "route": "DIGITAL", "reason": "r"}], "blocks": [],
            "qa": {"pages": 1, "needs_review_blocks": 0, "routes": {"DIGITAL": 1}},
            "_signals": {1: {}}}

    monkeypatch.setattr(P, "process_document", fake_process)
    with st.session() as s:
        sels = select_documents(s, store=store)
        res = P.run(s, sels, store, FakeExtractor(), log=lambda *_: None)
        assert res["ok"] == 1
        row = s.scalar(select(PrepDocumentRow))
        assert row.output_path == f"parsed/{sha}.json"
        assert json.loads(store.read_text(row.output_path))["sha256"] == sha
