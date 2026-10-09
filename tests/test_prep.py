"""Rule-level tests for ra_prep (no Docling / OCR models needed). Page-level tests use
single-page PDFs from the gold set in eval/gold/pages."""
from pathlib import Path

import pytest

pytest.importorskip("pypdfium2")

from ra_prep.grid import build, looks_tabular  # noqa: E402
from ra_prep.reocr import decide  # noqa: E402
from ra_prep.route import route_page  # noqa: E402
from ra_prep.sanitize import remove_watermarks  # noqa: E402
from ra_prep.select import jaccard  # noqa: E402
from ra_prep.signals import PageSignals, document_signals  # noqa: E402
from ra_prep.structure import structure, study_ids  # noqa: E402
from ra_prep.textquality import garbled_spans  # noqa: E402

GOLD = Path(__file__).resolve().parents[1] / "eval" / "gold" / "pages"


# -- garbled-line detector -----------------------------------------------------------------
# "Tfhosyiir ss t y ts e rosi ..." is not caught by the fragment test on its own; the
# two-reader comparison catches it (test_decide_takes_clean_second_reading).
@pytest.mark.parametrize("text", [
    "the format and content of a complete rpora  n o ror ri r ro r ro oa submitted under",
    "and in 41.9% ys  t ,  vi u -   ts  rt (rvns   e  os SAEs were similar",
    "te o e e oe se",
])
def test_garbled_lines_detected(text):
    assert garbled_spans(text)


@pytest.mark.parametrize("text", [
    "This reviewer agrees with the investigator and the Applicant that possible causality",
    "nebivolol and zofenopril for hypertension (Grade 2), metformin for glucose tolerance",
    "ALT (U/L) (RR: 10 to 33) AST (U/L) GGT ALP",
    "BM12H-NHV-01-G-01 CT-P43 3.1 SB17 ABP 654 q12w IV SC US-Stelara EU-Stelara",
    "a p < 0.001 b p < 0.05 c Nominally significant",
])
def test_clean_lines_not_flagged(text):
    assert not garbled_spans(text)


# -- router ----------------------------------------------------------------------------------
def _sig(**kw):
    base = dict(page=1, width=612, height=792, chars=2000, img_cov=0.0, bare_img_cov=0.0,
                invisible_text=0.0, n_text_objs=100, n_paths=10, garble=0.0, bare_boxes=[])
    base.update(kw)
    return PageSignals(**base)


@pytest.mark.parametrize("sig,doc_type,route", [
    (_sig(), "review", "DIGITAL"),
    (_sig(chars=0, img_cov=1.0, bare_img_cov=1.0), "review", "IMAGE"),
    (_sig(chars=0, img_cov=1.0, bare_img_cov=1.0), "label", "ARTWORK"),
    (_sig(img_cov=1.0, invisible_text=1.0), "review", "LEGACY_OCR"),
    (_sig(img_cov=0.3, bare_img_cov=0.3), "assessment_report", "MIXED"),
    (_sig(garble=0.8), "review", "BROKEN_TEXT"),
    (_sig(chars=0), "review", "BLANK"),
])
def test_route_rules(sig, doc_type, route):
    assert route_page(sig, doc_type)[0] == route


@pytest.mark.parametrize("gid,route", [
    ("g01", "DIGITAL"), ("g04", "MIXED"), ("g08", "IMAGE"), ("g21", "LEGACY_OCR"),
    ("g24", "BROKEN_TEXT"),
])
def test_route_gold_pages(gid, route):
    sig = document_signals(str(GOLD / f"{gid}.pdf"))[0]
    assert route_page(sig, "review")[0] == route


def test_watermark_removed_and_hidden_table_image_found(tmp_path):
    src = GOLD / "g03.pdf"
    assert route_page(document_signals(str(src))[0], "assessment_report")[0] == "DIGITAL"
    out = tmp_path / "clean.pdf"
    assert remove_watermarks(str(src), str(out)) == 1
    # with the diagonal watermark gone the pasted table image is seen as needing OCR
    assert route_page(document_signals(str(out))[0], "assessment_report")[0] == "MIXED"
    assert remove_watermarks(str(GOLD / "g01.pdf"), str(tmp_path / "x.pdf")) == 0


# -- reading choice ----------------------------------------------------------------------------
def test_decide_keeps_agreeing_readings():
    t = "The study drug was permanently discontinued because of the SAEs."
    assert decide(t, t) == (t, "kept")


def test_decide_takes_clean_second_reading():
    bad = "This reviewer agrees with the Tfhosyiir ss t y ts e rosi t it sii reis sitr the SAEs"
    good = "This reviewer agrees with the investigator and the Applicant that possible the SAEs"
    text, status = decide(bad, good)
    assert text == good and status == "reread"


def test_decide_flags_number_disagreement():
    _, status = decide("Overall, 7 subjects (1.8%) had 9 events", "Overall, 7 subjects (1.6%) had 9 events")
    assert status == "needs_review"


# -- picture-to-table rebuild -------------------------------------------------------------------
def _line(x0, y, x1, text):
    return ([x0, y, x1, y + 8], text)


def test_grid_rebuild():
    items = [_line(10, 100, 40, "n"), _line(100, 100, 115, "67"), _line(150, 100, 165, "13"),
             _line(10, 88, 40, "Mean"), _line(98, 88, 118, "5.37"), _line(148, 88, 168, "5.29"),
             _line(10, 76, 40, "Min"), _line(100, 76, 115, "2.1"), _line(150, 76, 165, "2.5")]
    assert looks_tabular(items)
    rows = {}
    for c in build(items):
        rows.setdefault(c["r"], {})[c["c"]] = c["text"]
    assert rows[0] == {0: "n", 1: "67", 2: "13"}
    assert rows[1] == {0: "Mean", 1: "5.37", 2: "5.29"}


# -- RA post-processing --------------------------------------------------------------------------
def _t(text, label="text", page=1, bbox=(72, 400, 540, 420), **kw):
    return {"type": "text", "label": label, "text": text, "page": page, "bbox": list(bbox),
            "layer": "body", **kw}


def test_structure_furniture_sections_speaker_cells():
    blocks = [
        _t("Biosimilar Multidisciplinary Evaluation and Review (BMER)", bbox=(72, 760, 400, 772)),
        _t("2.2. Studies Submitted by the Applicant", label="section_header"),
        _t("The Applicant states that the PK study met its endpoints."),
        _t("Clinical Reviewer's Comment: the demographics were similar."),
        _t("17", bbox=(30, 500, 40, 510)),  # margin line number
        {"type": "table", "page": 1, "bbox": [72, 100, 540, 300], "layer": "body", "caption": "",
         "cells": [{"r": 0, "c": 0, "text": "(b) (4) 31"}, {"r": 0, "c": 1, "text": "一"},
                   {"r": 0, "c": 2, "text": "SB17- 1001"}]},
        _t("Reference ID: 5454548", bbox=(20, 10, 200, 20)),
    ]
    out = structure(blocks, "fda", "review", {1: (612.0, 792.0)})
    assert out[0]["furniture"] and out[4]["furniture"] and out[6]["furniture"]
    assert out[2]["section_path"] == ["2.2. Studies Submitted by the Applicant"]
    assert out[2]["speaker"] == "applicant" and out[3]["speaker"] == "regulator"
    cells = [c["text"] for c in out[5]["cells"]]
    assert cells == ["31", "—", "SB17-1001"] and out[5]["redacted"]


def test_study_ids():
    ids = study_ids("Study BM12H-PSO-03-G-02 (NCT04967508) and EudraCT No. 2020-004447-88")
    assert "NCT04967508" in ids and "2020-004447-88" in ids and "BM12H-PSO-03-G-02" in ids


def test_jaccard():
    assert jaccard({1, 2, 3}, {1, 2, 3}) == 1.0 and jaccard(set(), {1}) == 0.0
