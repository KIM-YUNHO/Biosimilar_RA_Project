"""HTML documents (HC Summary Basis of Decision, Regulatory Decision Summary) -> blocks.

These pages are already structured, so no layout model is needed: walk the DOM of the
page's <main> element. Headings (h1-h6 and <details><summary>) become section headers with
their tag level, paragraphs and list items become text blocks, tables keep row/col spans.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

_MAIN = re.compile(r"<main\b.*?</main>", re.S | re.I)
_SKIP = {"script", "style", "nav", "button", "form", "noscript", "svg"}
_TEXT = {"p", "li", "dd", "dt", "figcaption", "blockquote"}
_CONTAINERS = {"div", "section", "article", "details", "ul", "ol", "dl", "main", "body", "html",
               "header", "footer", "aside", "span", "tbody", "thead", "figure"}


def _text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _table(node: Tag) -> dict:
    cells, occupied = [], set()
    for r, tr in enumerate(node.find_all("tr")):
        c = 0
        for td in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in occupied:
                c += 1
            rs = int(td.get("rowspan", 1) or 1)
            cs = int(td.get("colspan", 1) or 1)
            for dr in range(rs):
                for dc in range(cs):
                    occupied.add((r + dr, c + dc))
            cells.append({"r": r, "c": c, "rs": rs, "cs": cs, "text": _text(td),
                          "header": td.name == "th", "bbox": None})
            c += cs
    cap = node.find("caption")
    return {"type": "table", "caption": _text(cap) if cap else "", "cells": cells,
            "n_rows": 1 + max((x["r"] + x["rs"] - 1 for x in cells), default=-1),
            "n_cols": 1 + max((x["c"] + x["cs"] - 1 for x in cells), default=-1)}


def html_blocks(raw: str) -> list[dict]:
    m = _MAIN.search(raw)
    soup = BeautifulSoup(m.group(0) if m else raw, "lxml")
    out: list[dict] = []
    base = {"page": 1, "bbox": None, "layer": "body", "route": "HTML", "source": "html",
            "status": "kept", "parent": None}

    def emit_text(text: str, label: str, level: int | None = None):
        if text:
            b = {"type": "text", **base, "label": label, "text": text}
            if level is not None:
                b["level"] = level
            out.append(b)

    def walk(node):
        for child in node.children:
            if isinstance(child, NavigableString):
                t = " ".join(str(child).split())
                if len(t) > 2 and node.name in _CONTAINERS:
                    emit_text(t, "text")
                continue
            if not isinstance(child, Tag) or child.name in _SKIP:
                continue
            name = child.name
            if re.fullmatch(r"h[1-6]", name):
                emit_text(_text(child), "section_header", int(name[1]))
            elif name == "summary":
                emit_text(_text(child), "section_header", 3)
            elif name == "table":
                out.append({**base, **_table(child)})
            elif name in _TEXT:
                # a list item holding nested lists/tables: take its own text, then recurse
                if child.find(["ul", "ol", "table", "p"]):
                    own = " ".join(str(s).strip() for s in child.find_all(string=True, recursive=False))
                    emit_text(" ".join(own.split()), "list_item" if name == "li" else "text")
                    walk(child)
                else:
                    emit_text(_text(child), "list_item" if name == "li" else "text")
            else:
                walk(child)

    walk(soup)
    for i, b in enumerate(out):
        b["ref"] = f"#/html/{i}"
    return out
