"""RA post-processing of extracted blocks (P4). Rules only.

Per document:
  1. furniture   page headers/footers, page numbers, watermark text, margin line numbers
                 -> block["furniture"] = True (kept for provenance, skipped by chunking)
  2. sections    section_path from numbered/unnumbered headings
  3. speaker     who is talking: regulator | applicant | applicant_data | label | unknown
  4. redaction   (b)(4) / (b)(6) / CCI marks -> block["redacted"]; marks stripped from
                 numeric table cells
  5. cells       wrap artefacts and dash variants in table cells
  6. ids         study / trial identifiers mentioned in the block
"""
from __future__ import annotations

import re
from collections import defaultdict

# -- 1. furniture ------------------------------------------------------------------------
_FURNITURE_PATTERNS = [re.compile(p, re.I) for p in (
    r"^reference id:?\s*\d+$",
    r"^page \d+\s*(of|/)\s*\d+$",
    r"^\d{1,4}$",                       # bare page number
    r"^[ivxlc]{1,6}$",                  # roman page number
    r"^assessment report$",
    r"^assessment report\s+ema/\d+/\d{4}$",
    r"^ema/\d+/\d{4}$",
    r"^biosimilar multidisciplinary evaluation and review \(bmer\)$",
    r"^medicinal product no longer authori[sz]ed$",
    r"^contains nonbinding recommendations$",
    r"^draft\s*[-—–]\s*not for implementation$",
    r"^unclassified\s*/\s*non classifi[ée]$",
)]
_DIGITS = re.compile(r"\d+")


def _norm_key(text: str) -> str:
    return _DIGITS.sub("#", " ".join(text.lower().split()))


def mark_furniture(blocks: list[dict], page_sizes: dict[int, tuple[float, float]]) -> None:
    n_pages = len({b["page"] for b in blocks}) or 1
    # repetition: same text (digits ignored) near the top or bottom on many pages
    seen = defaultdict(set)
    for b in blocks:
        if b["type"] != "text" or not b.get("bbox"):
            continue
        w, h = page_sizes.get(b["page"], (612.0, 792.0))
        top, bottom = b["bbox"][3], b["bbox"][1]
        if top > 0.9 * h or bottom < 0.1 * h:
            seen[_norm_key(b["text"])].add(b["page"])
    repeated = {k for k, pages in seen.items() if len(pages) >= max(3, 0.3 * n_pages)}
    for b in blocks:
        if b["type"] != "text":
            continue
        t = " ".join(b["text"].split())
        w, h = page_sizes.get(b["page"], (612.0, 792.0))
        margin_number = bool(b.get("bbox")) and t.isdigit() and len(t) <= 4 and b["bbox"][2] < 0.14 * w
        b["furniture"] = bool(
            b.get("layer") == "furniture"
            or b.get("label") in ("page_header", "page_footer")
            or margin_number
            or any(p.match(t) for p in _FURNITURE_PATTERNS)
            or _norm_key(t) in repeated
        )


# -- 2. sections ---------------------------------------------------------------------------
_NUMBERED = re.compile(r"^((?:\d+\.)*\d+)\.?\s+\S")
_ROMAN = re.compile(r"^([IVX]+)\.\s+\S")
_LETTER = re.compile(r"^([A-H])\.\s+\S")


def _heading_level(text: str) -> int | None:
    m = _NUMBERED.match(text)
    if m:
        return m.group(1).count(".") + 1
    if _ROMAN.match(text):
        return 1
    if _LETTER.match(text):
        return 2
    return None


def assign_sections(blocks: list[dict]) -> None:
    stack: list[tuple[int, str]] = []
    for b in blocks:
        if b.get("furniture"):
            continue
        if b["type"] == "text" and b.get("label") in ("section_header", "title"):
            text = " ".join(b["text"].split())
            lvl = b.get("level") or _heading_level(text)  # HTML headings carry their tag level
            if lvl is None:  # unnumbered heading: one level below the last numbered one
                numbered = [lv for lv, t in stack if _heading_level(t) is not None]
                lvl = (max(numbered) + 1) if numbered else 1
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            stack.append((lvl, text))
        b["section_path"] = [t for _, t in stack]


# -- 3. speaker ----------------------------------------------------------------------------
_APPLICANT_SAYS = re.compile(
    r"^(the\s+)?(applicant|sponsor|mah|company)('s)?\s+(state[sd]?|propos\w*|submit\w*|claim\w*|"
    r"report\w*|assert\w*|provid\w*|conclud\w*|consider\w*|argu\w*|position)", re.I)
_REGULATOR_SAYS = re.compile(
    r"(reviewer'?s?\s+comment|this reviewer|the (review )?team|fda'?s assessment|the agency|"
    r"chmp'?s? (comment|view|conclusion)|assessor'?s? comment|the committee)", re.I)
_EMA_REGULATOR_SECTIONS = re.compile(r"discussion|conclusion|benefit.risk|chmp|assessment of", re.I)
_LABEL_TYPES = {"label", "product_information", "product_monograph"}
_REGULATOR_TYPES = {"guidance", "approval_letter", "sbd", "rds", "epar_overview", "procedural_steps"}


def assign_speaker(blocks: list[dict], agency: str | None, doc_type: str | None) -> None:
    for b in blocks:
        text = b.get("text") or b.get("caption") or ""
        if doc_type in _LABEL_TYPES:
            sp = "label"
        elif doc_type in _REGULATOR_TYPES:
            sp = "regulator"
        elif doc_type == "risk_management":
            sp = "applicant"
        elif _REGULATOR_SAYS.search(text):
            sp = "regulator"
        elif _APPLICANT_SAYS.search(text):
            sp = "applicant"
        elif agency == "ema" and doc_type == "assessment_report":
            path = " / ".join(b.get("section_path", []))
            sp = "regulator" if _EMA_REGULATOR_SECTIONS.search(path) else "applicant_data"
        elif agency == "fda" and doc_type == "review":
            sp = "regulator"  # FDA reviews are written by the agency unless quoted
        else:
            sp = "unknown"
        b["speaker"] = sp


# -- 4/5. redaction and cell clean-up ------------------------------------------------------
_REDACTION = re.compile(r"\(b\)\s*\((4|6)\)|\bCCI\b|\[redacted\]", re.I)
_CELL_MARK = re.compile(r"^\s*\(b\)\s*\((?:4|6)\)\s+(?=[-<>≤≥]?\d)")
_DASH_ONLY = re.compile(r"^[\s\-–—一ー‐]+$")
_WRAP_HYPHEN = re.compile(r"(?<=[A-Za-z0-9])- (?=[A-Za-z0-9])")


def clean_cells_and_redactions(blocks: list[dict]) -> None:
    for b in blocks:
        if b["type"] == "text":
            b["redacted"] = bool(_REDACTION.search(b["text"]))
        elif b["type"] == "table":
            red = False
            for c in b["cells"]:
                t = c["text"]
                if _REDACTION.search(t):
                    red = True
                    if _CELL_MARK.match(t):
                        t = _CELL_MARK.sub("", t)
                        c["redacted_mark_removed"] = True
                if _DASH_ONLY.match(t) and t.strip():
                    t = "—"
                t = _WRAP_HYPHEN.sub("-", t)
                c["text"] = t
            b["redacted"] = red


# -- 6. identifiers ------------------------------------------------------------------------
_IDS = [
    re.compile(r"\bNCT\d{8}\b"),
    re.compile(r"\b\d{4}-\d{6}-\d{2}\b"),                    # EudraCT
    re.compile(r"\b(?:SB17|CT-P43|BM12H|ABP\s?654|BAT2206|CNTO\s?1275)[-\s]?[A-Z0-9.\-]*\d\b"),
    re.compile(r"\b[A-Z]{2,6}\d{2,5}-(?:[A-Z]{2,5}-)?\d{2,4}(?:-[A-Z0-9]{1,3})*\b"),
]


def study_ids(text: str) -> list[str]:
    out: list[str] = []
    for p in _IDS:
        for m in p.findall(text):
            m = m.strip()
            if m not in out:
                out.append(m)
    return out


def structure(blocks: list[dict], agency: str | None, doc_type: str | None,
              page_sizes: dict[int, tuple[float, float]]) -> list[dict]:
    mark_furniture(blocks, page_sizes)
    assign_sections(blocks)
    assign_speaker(blocks, agency, doc_type)
    clean_cells_and_redactions(blocks)
    for b in blocks:
        text = b.get("text", "") if b["type"] != "table" else " ".join(c["text"] for c in b["cells"])
        b["study_ids"] = study_ids(text + " " + (b.get("caption") or ""))
    return blocks
