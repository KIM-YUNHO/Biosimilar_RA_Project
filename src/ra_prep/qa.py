"""Per-document quality metrics (P5). Stored with every parsed document so weak documents
can be found without opening them."""
from __future__ import annotations

import re
from collections import Counter

from .textquality import garbled_spans

_NUMBER_CELL = re.compile(
    r"^[<>≤≥~±]?\s*-?\d[\d,]*(\.\d+)?\s*%?"            # 12 / 1,234 / 0.95 / 12%
    r"(\s*\(\s*-?\d[\d,]*(\.\d+)?\s*%?\s*\))?"           # 5 (52) / 80 (31.6%)
    r"(\s*[,;]\s*-?\d[\d,]*(\.\d+)?%?)?$"                # 90.5, 363.6 / 0.90; 1.08
    r"|^\d+\s*/\s*\d+$"                                  # 14/151
    r"|^\(\s*-?\d[\d.]*%?\s*[,;]\s*-?\d[\d.]*%?\s*\)$"   # (115.58, 133.82)
    r"|^\d{4}-\d{2}-\d{2}$"                             # 2024-10-01
)


def document_qa(blocks: list[dict], routes: list[dict]) -> dict:
    body = [b for b in blocks if not b.get("furniture")]
    text_blocks = [b for b in body if b["type"] == "text"]
    tables = [b for b in body if b["type"] == "table"]
    chars = sum(len(b["text"]) for b in text_blocks)
    ocr_chars = sum(len(b["text"]) for b in text_blocks if b.get("source") == "ocr")
    num_cells = parsed = 0
    for t in tables:
        for c in t["cells"]:
            if any(ch.isdigit() for ch in c["text"]):
                num_cells += 1
                parsed += bool(_NUMBER_CELL.match(c["text"].strip()))
    return {
        "pages": len(routes),
        "routes": dict(Counter(r["route"] for r in routes)),
        "text_blocks": len(text_blocks),
        "text_chars": chars,
        "ocr_char_share": round(ocr_chars / chars, 3) if chars else 0.0,
        "tables": len(tables),
        "tables_ocr_grid": sum(1 for t in tables if t.get("structure") == "ocr_grid"),
        "numeric_cells": num_cells,
        "numeric_cell_parse_rate": round(parsed / num_cells, 3) if num_cells else None,
        "needs_review_blocks": sum(1 for b in body if b.get("status") == "needs_review"),
        "reread_blocks": sum(1 for b in body if b.get("status") == "reread"),
        "garbled_residual": sum(1 for b in text_blocks if garbled_spans(b["text"])),
        "furniture_blocks": sum(1 for b in blocks if b.get("furniture")),
        "redacted_blocks": sum(1 for b in body if b.get("redacted")),
        "sections": len({tuple(b.get("section_path", [])) for b in body if b.get("section_path")}),
    }
