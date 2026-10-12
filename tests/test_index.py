"""Indexing stage (ra_chunk.index) and search over the index (ra_chunk.search / retrieve).

A deterministic fake embedder stands in for bge-m3, so these run without models. Set
RA_TEST_DB_URL to a PostgreSQL database with pgvector to run them on halfvec / sparsevec.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

import numpy as np
import pytest
from sqlalchemy import func, select

from conftest import ROOT
from ra_chunk.chunker import ChunkConfig
from ra_chunk.index import INDEX_CHUNKING, build_chunks, candidates, embed_text, run
from ra_chunk.query import Router
from ra_chunk.retrieve import Retriever
from ra_chunk.search import ChunkIndex, Filters, hybrid_search
from ra_chunk.store import IndexChunkRow, IndexDocumentRow
from ra_ingest.db import DocumentRow, DocumentVersionRow, ProgramRow, Store
from ra_ingest.metadata import build_document_metadata
from ra_ingest.objstore import LocalStore
from ra_prep.store import PrepDocumentRow

DIM = 32
_WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Dense: random unit vector seeded by the text plus one direction per word, so texts
    sharing words score higher. Sparse: word hash -> weight."""
    name = "fake-embedder"

    def __init__(self):
        self.calls = 0

    def count_tokens(self, text):
        return max(1, len(text) // 4)

    def _word(self, w):
        return int(hashlib.md5(w.encode()).hexdigest()[:6], 16)

    def encode(self, texts):
        out = []
        for t in texts:
            self.calls += 1
            words = _WORD.findall(t.lower())
            rng = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16))
            v = rng.normal(size=DIM) * 0.3
            for w in words:
                v[self._word(w) % DIM] += 1.0
            v = (v / np.linalg.norm(v)).astype(np.float16)
            sp: dict[int, float] = {}
            for w in words:
                tid = self._word(w) % 50000
                sp[tid] = round(min(0.4, sp.get(tid, 0.0) + 0.1), 3)
            out.append((v, sp))
        return out


def _t(text, path, speaker="regulator", page=1):
    return {"type": "text", "label": "text", "text": text, "page": page, "section_path": path,
            "speaker": speaker, "furniture": False, "ref": f"#/texts/{abs(hash(text)) % 10**6}"}


def _parsed(doc_id, sha, topic):
    blocks = []
    for i in range(6):
        blocks.append(_t(f"The {topic} study {i} compared pharmacokinetics in healthy subjects and "
                         f"the equivalence margin was met for AUC and Cmax in part {i}.",
                         ["2.6 Clinical aspects", f"2.6.{i % 3} Section {i % 3}"], page=1 + i // 2))
    blocks.append(_t(f"The committee considered the {topic} immunogenicity data acceptable.",
                     ["2.6 Clinical aspects", "Discussion"], page=4))
    return {"document_id": doc_id, "sha256": sha, "doc_type": "assessment_report", "blocks": blocks}


@pytest.fixture
def env(tmp_path, db_url):
    """Four parsed documents: SB17 (EMA), CT-P43 (FDA), one FDA guidance, a Stelara label."""
    st = Store(db_url)
    store = LocalStore(tmp_path / "store")
    with st.session() as s:
        s.add_all([ProgramRow(id="SB17", role="biosimilar", ingredient="ustekinumab", codes=["SB17"],
                              aliases={"ema": ["Pyzchiva"]}),
                   ProgramRow(id="CT-P43", role="biosimilar", ingredient="ustekinumab", codes=["CT-P43"],
                              aliases={"fda": ["Steqeyma"]}),
                   ProgramRow(id="stelara", role="reference", ingredient="ustekinumab", codes=[],
                              aliases={"ema": ["Stelara"]})])
        s.flush()
        docs = [("ema", "product", "assessment_report", "SB17", "pyzchiva switching"),
                ("fda", "product", "review", "CT-P43", "steqeyma interchangeability"),
                ("fda", "guidance", "guidance", None, "comparative efficacy guidance"),
                ("ema", "product", "product_information", "stelara", "stelara label indications")]
        ids = []
        for agency, kind, dtype, prog, topic in docs:
            d = DocumentRow(agency=agency, doc_kind=kind, doc_type=dtype, title=topic, program_id=prog,
                            source_url=f"https://x/{topic.replace(' ', '-')}.pdf", fetch_status="fetched",
                            guidance={"status": "draft"} if kind == "guidance" else None)
            s.add(d)
            s.flush()
            _add_version(s, st, store, d, topic, "prep-1.4+docling")
            ids.append(d.id)
        s.commit()
    return st, store, ids


def _add_version(s, st, store, d, topic, engine, status="ok"):
    sha = hashlib.sha256(f"{d.id}-{topic}-{engine}".encode()).hexdigest()
    st.add_version(s, d, sha, f"raw/{d.agency}/{sha[:2]}/{sha}.pdf", 1, "application/pdf", None, None)
    key = f"parsed/{sha}.json"
    store.put_bytes(key, json.dumps(_parsed(d.id, sha, topic)).encode())
    s.add(PrepDocumentRow(document_id=d.id, sha256=sha, engine=engine, status=status,
                          output_path=key if status == "ok" else None,
                          reason="superseded_label (latest: 9)" if status == "skipped" else None))
    s.flush()
    return sha


def _active(s):
    return s.scalar(select(func.count()).select_from(IndexChunkRow).where(IndexChunkRow.active.is_(True)))


def test_index_is_incremental(env):
    st, store, ids = env
    emb = FakeEmbedder()
    with st.session() as s:
        res = run(s, candidates(s), emb, store, log=lambda *_: None)
        assert (res.indexed, res.failed) == (4, 0) and res.chunks == _active(s) > 0
        first_calls = emb.calls
        res = run(s, candidates(s), emb, store, log=lambda *_: None)
        assert (res.indexed, res.cached, emb.calls) == (0, 4, first_calls)  # nothing to redo

        # guidance status goes into the context header -> that document is indexed again
        g = s.get(DocumentRow, ids[2])
        g.guidance = {"status": "final"}
        s.commit()
        res = run(s, candidates(s), emb, store, log=lambda *_: None)
        assert (res.indexed, res.cached) == (1, 3) and res.embedded > 0
        olds = s.scalars(select(IndexDocumentRow).where(IndexDocumentRow.document_id == ids[2])).all()
        assert sorted(o.retired_at is None for o in olds) == [False, True]
        assert all("status=final" in c.embed_text for c in s.scalars(
            select(IndexChunkRow).where(IndexChunkRow.document_id == ids[2], IndexChunkRow.active.is_(True))))

        # a new version of the SB17 report replaces the old one
        d = s.get(DocumentRow, ids[0])
        _add_version(s, st, store, d, "pyzchiva switching v2", "prep-1.4+docling")
        s.commit()
        res = run(s, candidates(s), emb, store, log=lambda *_: None)
        assert res.indexed == 1
        active = s.scalars(select(IndexChunkRow).where(IndexChunkRow.document_id == ids[0],
                                                       IndexChunkRow.active.is_(True))).all()
        assert active and all("v2" in c.text for c in active)

        # forced re-index of unchanged text copies vectors instead of computing them
        before = emb.calls
        res = run(s, candidates(s, document_ids=[ids[1]]), emb, store, force=True, log=lambda *_: None)
        assert res.indexed == 1 and res.embedded == 0 and emb.calls == before


def test_skipped_latest_version_retires_index(env):
    st, store, ids = env
    emb = FakeEmbedder()
    with st.session() as s:
        run(s, candidates(s), emb, store, log=lambda *_: None)
        d = s.get(DocumentRow, ids[1])
        _add_version(s, st, store, d, "older label", "prep-1.4+docling", status="skipped")
        s.commit()
        c = [x for x in candidates(s) if x.document_id == ids[1]][0]
        assert c.action == "retire" and "superseded_label" in c.reason
        res = run(s, candidates(s), emb, store, log=lambda *_: None)
        assert res.retired == 1
        n = s.scalar(select(func.count()).select_from(IndexChunkRow).where(
            IndexChunkRow.document_id == ids[1], IndexChunkRow.active.is_(True)))
        assert n == 0


def test_index_search_matches_in_memory_evaluation(env):
    """The index returns the same chunks, in the same order, as the in-memory search the
    retrieval evaluation measured (eval/retrieval/compare_chunking.py)."""
    st, store, ids = env
    emb = FakeEmbedder()
    with st.session() as s:
        run(s, candidates(s), emb, store, log=lambda *_: None)
        # the evaluation's way: chunks built in memory from the same parses
        sys.path.insert(0, str(ROOT / "eval" / "retrieval"))
        import compare_chunking as C

        docs = []
        for i in ids:
            d = s.get(DocumentRow, i)
            v = s.scalars(select(DocumentVersionRow).where(DocumentVersionRow.document_id == i)
                          .order_by(DocumentVersionRow.id.desc())).first()
            docs.append((json.loads(store.read_text(f"parsed/{v.sha256}.json")), build_document_metadata(s, d)))
        chunks, _ = C.build("V2", docs, emb.count_tokens)
        enc = emb.encode([c["embed"] for c in chunks])
        dense = np.stack([e[0] for e in enc]).astype(np.float32)
        sparse = [e[1] for e in enc]
        idx = ChunkIndex(s, emb.name)
        rows = s.scalars(select(IndexChunkRow).where(IndexChunkRow.active.is_(True))).all()
        assert sorted(r.embed_text for r in rows) == sorted(c["embed"] for c in chunks)
        by_id = {r.id: (r.document_id, r.chunk_key) for r in rows}
        prog = np.array([c["program"] or "" for c in chunks])
        for qtext, f, mask in [
            ("pharmacokinetics equivalence margin", Filters(), np.ones(len(chunks), bool)),
            ("immunogenicity committee", Filters(program_id="SB17"), prog == "SB17"),
            ("comparative efficacy", Filters(agency="fda"), np.array([c["agency"] == "fda" for c in chunks])),
        ]:
            q = emb.encode([qtext])[0]
            for k in (3, 10):
                want = [(chunks[i]["doc_id"], chunks[i]["id"]) for i in hybrid_search(q, dense, sparse, mask, k)]
                got = [by_id[h.chunk_id] for h in idx.search(q, f, k)]
                assert got == want, (qtext, k)


def test_retriever_routes_by_product(env):
    st, store, ids = env
    emb = FakeEmbedder()
    with st.session() as s:
        run(s, candidates(s), emb, store, log=lambda *_: None)
        ret = Retriever(s, emb, Router(ROOT / "config" / "products.yaml"))
        hits = ret.retrieve("SB17의 PK 시험 동등성 한계는?", k=5)
        assert hits and {h.chunk.program_id for h in hits} == {"SB17"}
        assert all(h.parent_text for h in hits)
        both = ret.retrieve("SB17과 CT-P43의 면역원성 비교", k=3)
        assert {h.product for h in both} == {"SB17", "CT-P43"} and len(both) == 6
        guid = ret.retrieve("FDA 가이던스에서 비교 효능 시험", k=5)
        assert guid and all(h.chunk.doc_kind == "guidance" for h in guid)
        # the originator's documents only when the question asks for them
        ema = ret.retrieve("EMA 위원회의 면역원성 판단", k=20)
        assert ema and "stelara" not in {h.chunk.program_id for h in ema}
        ref = ret.retrieve("SB17 시험의 원 시험 근거", k=3)
        assert {h.product for h in ref} == {"SB17", "stelara"}
        # a document type named in the question adds a search within that type
        lab = ret.retrieve("Stelara 제품 정보의 적응증", k=2)
        assert {h.chunk.doc_type for h in lab} == {"product_information"}


def test_v2_config_is_the_evaluated_one():
    from ra_chunk.chunker import ChunkConfig as CC
    sys.path.insert(0, str(ROOT / "eval" / "retrieval"))
    import compare_chunking as C

    assert CC(**INDEX_CHUNKING) == CC(**C.VARIANTS["V2"])
    doc = _parsed(1, "x", "a")
    ch, _ = build_chunks(doc, ChunkConfig(**INDEX_CHUNKING))
    assert doc["blocks"][0].get("_attached") is None  # input left untouched
    assert all(embed_text(c, {"agency": "ema", "doc_type": "x"}).startswith("[") for c in ch)


def test_parity_script_on_synthetic_corpus(env):
    st, store, ids = env
    emb = FakeEmbedder()
    sys.path.insert(0, str(ROOT / "eval" / "retrieval"))
    import check_index_parity as P
    from ra_chunk.query import Glossary

    gold = {"questions": [{"id": "Q1", "question": "SB17 PK 동등성 한계"},
                          {"id": "Q2", "question": "각 제품의 면역원성"},
                          {"id": "Q3", "question": "FDA 가이던스 비교 효능 시험"},
                          {"id": "Q4", "question": "healthy subjects pharmacokinetics"},
                          {"id": "Q5", "question": "EMA 위원회 판단"},
                          {"id": "Q6", "question": "SB17 원 시험과 라벨"},
                          {"id": "Q7", "question": "각 제품은 무엇이 필요한가"}]}
    with st.session() as s:
        run(s, candidates(s), emb, store, log=lambda *_: None)
        docs = [(json.loads(store.read_text(r.output_path)), build_document_metadata(s, s.get(DocumentRow, r.document_id)))
                for r in s.scalars(select(PrepDocumentRow).order_by(PrepDocumentRow.document_id))]
        res = P.compare(s, gold, Router(ROOT / "config" / "products.yaml"),
                        Glossary(ROOT / "config" / "query_glossary.yaml"), emb, docs, 10, log=lambda *_: None)
    assert res["mismatch"] == 0 and res["chunks_not_indexed"] == 0 and res["match"] >= 10


def test_cli_run_status_search(env, db_url, monkeypatch, capsys):
    st, store, ids = env
    import ra_chunk.cli as cli

    monkeypatch.setattr(cli, "_embedder", lambda a: FakeEmbedder())
    base = ["--db", db_url, "--store", store.url]
    assert cli.main([*base, "run"]) == 0
    assert "indexed=4" in capsys.readouterr().out
    assert cli.main([*base, "run", "--product", "SB17"]) == 0
    assert "cached=1" in capsys.readouterr().out
    assert cli.main([*base, "status"]) == 0
    assert "fake-embedder" in capsys.readouterr().out
    assert cli.main([*base, "search", "SB17 면역원성", "--k", "2"]) == 0
    out = capsys.readouterr().out
    assert out.count("ema/SB17/assessment_report") == 2


def test_retriever_rerank_reorders_candidates(env):
    st, store, ids = env
    emb = FakeEmbedder()

    class ByLength:  # stand-in cross-encoder: prefers the shortest passage
        def score(self, q, passages):
            return [-len(p) for p in passages]

    with st.session() as s:
        run(s, candidates(s), emb, store, log=lambda *_: None)
        ret = Retriever(s, emb, Router(ROOT / "config" / "products.yaml"), reranker=ByLength(), depth=7)
        hits = ret.retrieve("SB17 study", k=2)
        lens = sorted(len(c.embed_text) for c in s.scalars(select(IndexChunkRow).where(
            IndexChunkRow.program_id == "SB17", IndexChunkRow.active.is_(True))))
        assert [len(h.chunk.embed_text) for h in hits] == lens[:2]
