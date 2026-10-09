"""Score candidate outputs (eval/out/<cand>/<gid>.json) against the gold set.

Metrics per page:
  text_recall      share of gold text items found in the candidate output (fuzzy >= 95,
                   searched over all candidate content: text blocks and table cells)
  text_score       mean fuzzy score of gold text items (0-100)
  cell_recall      share of gold non-empty table cells present as a cell in the
                   candidate's best-matching table (exact after normalisation)
  num_cell_recall  same, cells containing a digit only
  row_exact        share of gold table rows reproduced exactly (same non-empty cells, order)
  num_in_text      share of gold numeric cells whose value appears anywhere in the output
                   (what retrieval would still find even without table structure)
  garbled          candidate text blocks with a run of broken fragments (textquality.garbled_spans)
  furniture_leak   gold header/footer/page-number strings that remain as text items
  seconds          extraction time for the page

Normalisation: Unicode NFKC, lower case, all whitespace removed, dash variants -> "-",
checkbox glyphs -> [x] / [ ], (R)/(TM) marks dropped.
Usage: python eval/evaluate.py A0 A1 B C [--md report.md]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))
from ra_prep.textquality import garbled_spans  # noqa: E402

import yaml  # noqa: E402

_DASH = re.compile(r"[‐-―−一ー]")
_CHECKED = re.compile(r"[✓✔√☑☒⊠]")
_UNCHECKED = re.compile(r"[☐□❑]")
_WS = re.compile(r"\s+")


def norm(s: str, keep_space: bool = False) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = _DASH.sub("-", s)
    s = _CHECKED.sub("[x]", s)
    s = _UNCHECKED.sub("[ ]", s)
    s = s.replace("®", "").replace("™", "").replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    return _WS.sub(" ", s).strip() if keep_space else _WS.sub("", s)


def has_digit(s: str) -> bool:
    return any(ch.isdigit() for ch in s)


def cells(table) -> list[str]:
    return [c for row in table for c in row]


def best_table(gold_t, cand_tables):
    g = Counter(norm(c) for c in cells(gold_t) if norm(c))
    best, best_ov = None, 0
    for t in cand_tables:
        ov = sum((g & Counter(norm(c) for c in cells(t) if norm(c))).values())
        if ov > best_ov:
            best, best_ov = t, ov
    return best


def score_page(gold: dict, cand: dict) -> dict:
    cand_text = cand.get("text", [])
    cand_tables = cand.get("tables", [])
    everything = " ".join(cand_text + [c for t in cand_tables for c in cells(t)])
    hay = norm(everything, keep_space=True)
    hay_compact = norm(everything)

    ts = [fuzz.partial_ratio(norm(t, keep_space=True), hay) if hay else 0.0 for t in gold["text"]]
    m: dict = {
        "text_n": len(ts),
        "text_hit": sum(1 for s in ts if s >= 95),
        "text_score_sum": sum(ts),
    }
    gc = gn = hc = hn = rows = rows_hit = nit = 0
    used_tables = set()
    for gt in gold["tables"]:
        ct = best_table(gt, [t for i, t in enumerate(cand_tables) if i not in used_tables])
        if ct is not None:
            used_tables.add(next(i for i, t in enumerate(cand_tables) if t is ct))
        cand_cells = Counter(norm(c) for c in cells(ct) if norm(c)) if ct else Counter()
        cand_rows = Counter(tuple(norm(c) for c in r if norm(c)) for r in ct) if ct else Counter()
        for row in gt:
            key = tuple(norm(c) for c in row if norm(c))
            if not key:
                continue
            rows += 1
            if cand_rows[key] > 0:
                rows_hit += 1
                cand_rows[key] -= 1
            for c in row:
                n = norm(c)
                if not n:
                    continue
                gc += 1
                if has_digit(n):
                    gn += 1
                    if n in hay_compact:
                        nit += 1
                if cand_cells[n] > 0:
                    cand_cells[n] -= 1
                    hc += 1
                    if has_digit(n):
                        hn += 1
    m.update(cell_n=gc, cell_hit=hc, num_n=gn, num_hit=hn, row_n=rows, row_hit=rows_hit, num_in_text=nit)
    m["garbled"] = sum(1 for t in cand_text if garbled_spans(t))
    cand_items = {norm(t) for t in cand_text}
    m["furniture_leak"] = sum(1 for f in gold.get("furniture", []) if norm(f) in cand_items)
    m["seconds"] = cand.get("seconds")
    return m


def aggregate(ms: list[dict]) -> dict:
    s = defaultdict(float)
    for m in ms:
        for k, v in m.items():
            if isinstance(v, (int, float)) and v is not None:
                s[k] += v
    secs = [m["seconds"] for m in ms if m.get("seconds") is not None]

    def r(a, b):
        return round(100 * s[a] / s[b], 1) if s[b] else None

    return {
        "pages": len(ms),
        "text_recall": r("text_hit", "text_n"),
        "text_score": round(s["text_score_sum"] / s["text_n"], 1) if s["text_n"] else None,
        "cell_recall": r("cell_hit", "cell_n"),
        "num_cell_recall": r("num_hit", "num_n"),
        "row_exact": r("row_hit", "row_n"),
        "num_in_text": r("num_in_text", "num_n"),
        "garbled": int(s["garbled"]),
        "furniture_leak": int(s["furniture_leak"]),
        "sec_per_page": round(sum(secs) / len(secs), 1) if secs else None,
        "cells": int(s["cell_n"]), "num_cells": int(s["num_n"]),
    }


GROUPS = {
    "digital": ("DIGITAL",),
    "mixed (EMA image tables)": ("MIXED",),
    "image (FDA flattened)": ("IMAGE",),
    "legacy scan": ("LEGACY_OCR",),
    "broken text / artwork": ("BROKEN_TEXT", "ARTWORK"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates", nargs="+")
    ap.add_argument("--md")
    ap.add_argument("--json")
    a = ap.parse_args()
    manifest = {p["id"]: p for p in yaml.safe_load((ROOT / "gold" / "manifest.yaml").read_text())["pages"]}
    route_of = {}
    a1 = ROOT / "out" / "A1"
    for gid in manifest:
        f = a1 / f"{gid}.json"
        route_of[gid] = json.loads(f.read_text())["flags"]["route"] if f.exists() else "?"
    results: dict = {}
    per_page: dict = {}
    for cand in a.candidates:
        rows = {}
        for gid in manifest:
            gf = ROOT / "gold" / "annotations" / f"{gid}.json"
            cf = ROOT / "out" / cand / f"{gid}.json"
            if not gf.exists() or not cf.exists():
                continue
            rows[gid] = score_page(json.loads(gf.read_text()), json.loads(cf.read_text()))
        per_page[cand] = rows
        res = {"all": aggregate(list(rows.values()))}
        for gname, routes in GROUPS.items():
            sel = [m for gid, m in rows.items() if route_of[gid] in routes]
            if sel:
                res[gname] = aggregate(sel)
        results[cand] = res

    cols = ["pages", "text_recall", "text_score", "cell_recall", "num_cell_recall", "row_exact",
            "num_in_text", "garbled", "furniture_leak", "sec_per_page"]
    lines = []
    for group in ["all"] + list(GROUPS):
        lines.append(f"\n### {group}\n")
        lines.append("| cand | " + " | ".join(cols) + " |")
        lines.append("|---" * (len(cols) + 1) + "|")
        for cand in a.candidates:
            r = results[cand].get(group)
            if r:
                lines.append(f"| {cand} | " + " | ".join(str(r.get(c)) for c in cols) + " |")
    out = "\n".join(lines)
    print(out)
    if a.md:
        Path(a.md).write_text(out + "\n")
    if a.json:
        Path(a.json).write_text(json.dumps({"summary": results, "pages": per_page}, indent=1))


if __name__ == "__main__":
    main()
