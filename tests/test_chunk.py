"""Rule-level tests for ra_chunk (no embedding model needed)."""
from pathlib import Path

from ra_chunk.chunker import ChunkConfig, chunk_document, guidance_section_id, row_lines, table_grid
from ra_chunk.query import Router

ROOT = Path(__file__).resolve().parents[1]


def _t(text, path, speaker="regulator", page=1, label="text", ref=None):
    return {"type": "text", "label": label, "text": text, "page": page, "section_path": path,
            "speaker": speaker, "furniture": False, "ref": ref or f"#/texts/{abs(hash(text)) % 10**6}"}


def _cell(r, c, text, header=False, rs=1, cs=1):
    return {"r": r, "c": c, "rs": rs, "cs": cs, "text": text, "header": header}


PK_TABLE = {
    "type": "table", "ref": "#/tables/1", "page": 2, "caption": "Table 5. Primary PK parameters",
    "section_path": ["2.6.2 Pharmacokinetics"], "speaker": "applicant_data", "furniture": False,
    "study_ids": ["CT-P43 1.1"], "n_rows": 5, "n_cols": 3,
    "cells": [
        _cell(0, 0, "Parameter", True), _cell(0, 1, "CT-P43", True), _cell(0, 2, "EU-Stelara", True),
        _cell(1, 0, ""), _cell(1, 1, "(N=80)"), _cell(1, 2, "(N=81)"),
        _cell(2, 0, "Cmax (ug/mL)"),
        _cell(3, 0, "Mean"), _cell(3, 1, "5.35"), _cell(3, 2, "4.57"),
        _cell(4, 0, "SD"), _cell(4, 1, "1.456"), _cell(4, 2, "1.368"),
    ],
}


def test_table_header_rows_inferred_and_group_labels():
    grid, n_head = table_grid(PK_TABLE)
    assert n_head == 2  # "(N=80)" row is a header even though Docling flagged only row 0
    cols = [" / ".join(x for x in (grid[0][k], grid[1][k]) if x) for k in range(3)]
    lines = [t for _, t in row_lines(PK_TABLE["caption"], grid, n_head, cols)]
    assert lines == [
        "Table 5. Primary PK parameters / Cmax (ug/mL) / Mean: CT-P43 / (N=80)=5.35; EU-Stelara / (N=81)=4.57",
        "Table 5. Primary PK parameters / Cmax (ug/mL) / SD: CT-P43 / (N=80)=1.456; EU-Stelara / (N=81)=1.368",
    ]


def test_narrative_chunks_break_at_section_and_speaker():
    doc = {"document_id": 1, "doc_type": "assessment_report", "blocks": [
        _t("Study design was adequate.", ["2.6 Clinical", "Discussion"]),
        _t("Results were comparable.", ["2.6 Clinical", "Discussion"]),
        _t("The applicant states the margin is justified.", ["2.6 Clinical", "Discussion"], speaker="applicant"),
        _t("PK parameters were similar.", ["2.6.2 PK"]),
        PK_TABLE,
        {**_t("a Geometric mean", ["2.6.2 PK"], page=2, label="footnote")},
    ]}
    chunks, parents = chunk_document(doc, ChunkConfig(min_words=1))
    narr = [c for c in chunks if c.kind == "narrative"]
    assert [c.text for c in narr] == [
        "Study design was adequate.\nResults were comparable.",
        "The applicant states the margin is justified.",
        "PK parameters were similar.",
    ]
    assert narr[0].parent_id == narr[1].parent_id and narr[0].parent_id in parents
    table = [c for c in chunks if c.kind == "table"][0]
    assert table.text.endswith("a Geometric mean")  # footnote under the table goes with it
    assert len([c for c in chunks if c.kind == "row"]) == 2
    assert all(c.parent_id == table.parent_id for c in chunks if c.kind == "row")


def test_block_variant_and_skipped_sections():
    doc = {"document_id": 2, "doc_type": "review", "blocks": [
        _t("Table of Contents", ["Table of Contents"]),
        _t("One.", ["1. Intro"]), _t("Two.", ["1. Intro"]),
    ]}
    chunks, _ = chunk_document(doc, ChunkConfig(narrative="block", rows=False, min_words=1))
    assert [c.text for c in chunks] == ["One.", "Two."]
    assert all(c.parent_id is None for c in chunks)


def test_guidance_section_ids():
    assert guidance_section_id(["II. DISCUSSION", "A.I.8.a Reference Product"]) == "A.I.8.a"
    assert guidance_section_id(["5.3.2 Comparative clinical efficacy studies"]) == "5.3.2"
    assert guidance_section_id(["IV. SPECIFIC RECOMMENDATIONS"]) == "IV"


def test_router():
    r = Router(ROOT / "config" / "products.yaml")
    x = r.route("Wezlana(ABP654)는 어떤 근거로 인터체인저블로 지정됐나?")
    assert x.products == ["ABP654"] and x.agency is None
    x = r.route("FDA는 비교 임상 효능 시험 요구를 2015 가이던스와 지금 사이에 어떻게 바꿨나?")
    assert x.agency == "fda" and x.guidance and not x.products
    # comparator mentions of the reference product do not filter to it
    assert r.route("PK 시험에서 EU-Stelara와 US-Stelara를 어떻게 썼나?").products == []
    assert r.route("Eksunbi의 PK 동등성 결과는?").products == ["SB17"]


def test_tiny_chunks_dropped():
    doc = {"document_id": 3, "doc_type": "review", "blocks": [
        _t("xiv", ["Glossary"]), _t("BLA 761373", ["1. Title"]),
        _t("The applicant submitted a single PK study in healthy subjects.", ["2. PK"])]}
    chunks, _ = chunk_document(doc, ChunkConfig())
    assert [c.text for c in chunks] == ["The applicant submitted a single PK study in healthy subjects."]
