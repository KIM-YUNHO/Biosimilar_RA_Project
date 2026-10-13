"""Evidence for the answer step: retrieved hits -> source blocks (E1..) -> citable segments (S1..).

  block    one parent of one or more hits (whole leaf section or whole table, ra_chunk.chunker),
           with the document fields the answer needs. Hits sharing a parent become one block.
  segment  one line of a block: a paragraph of a section, or one row of a Markdown table. The
           model cites segment IDs; the quotes are cut from the source by code, so a citation
           never carries text the source does not have. Table rules ("| --- |") and blank lines
           stay in the reading text but cannot be cited.

Blocks are admitted whole (never clipped), round-robin over the routed products so that every
product's best passage is read before a second passage of any one product, until the prompt no
longer fits the model's input budget.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Callable, Iterable

_TABLE_RULE = re.compile(r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$")


@dataclass
class Segment:
    id: str | None          # "S12"; None: layout only, shown for reading
    text: str
    start: int              # offsets in the block text: block.text[start:end] == text
    end: int


@dataclass
class Block:
    key: tuple              # (index document, parent key), or ("chunk", id) without a parent
    document_id: int
    agency: str | None
    program_id: str | None
    doc_kind: str | None
    doc_type: str | None
    kind: str               # "table" | "text"
    pages: list[int]
    section_path: list[str]
    text: str
    speakers: list[str] = field(default_factory=list)
    needs_review: bool = False
    title: str | None = None
    table_caption: str | None = None
    decision_date: str | None = None
    guidance_status: str | None = None
    guidance_effective_from: str | None = None
    guidance_effective_to: str | None = None
    product: str | None = None       # the routed product whose search found it
    chunk_ids: list[int] = field(default_factory=list)
    score: float = 0.0
    id: str = ""                     # "E3", set by number()
    segments: list[Segment] = field(default_factory=list)


def from_hits(hits: Iterable, titles: dict[int, str] | None = None) -> list[Block]:
    """ra_chunk.retrieve.Retrieved hits -> blocks in hit order, one per parent."""
    titles = titles or {}
    out: dict[tuple, Block] = {}
    for h in hits:
        c = h.chunk
        key = (c.index_document_id, c.parent_key) if c.parent_key else ("chunk", c.id)
        b = out.get(key)
        if b is None:
            b = out[key] = Block(
                key=key, document_id=c.document_id, agency=c.agency, program_id=c.program_id,
                doc_kind=c.doc_kind, doc_type=c.doc_type, kind="table" if c.kind in ("table", "row") else "text",
                pages=list(getattr(h, "parent_pages", None) or c.pages or []), section_path=list(c.section_path or []),
                text=h.parent_text, title=titles.get(c.document_id), table_caption=c.table_caption,
                decision_date=c.decision_date, guidance_status=c.guidance_status,
                guidance_effective_from=c.guidance_effective_from, guidance_effective_to=c.guidance_effective_to,
                product=h.product, score=h.score)
        b.chunk_ids.append(c.id)
        b.score = max(b.score, h.score)
        b.needs_review = b.needs_review or bool(c.needs_review)
        if c.speaker and c.speaker not in b.speakers:
            b.speakers.append(c.speaker)
    return list(out.values())


def number(blocks: list[Block]) -> list[Block]:
    """Block IDs E1.. and segment IDs S1.. in reading order."""
    n = 0
    for i, b in enumerate(blocks, 1):
        b.id, b.segments, pos = f"E{i}", [], 0
        for line in b.text.split("\n"):
            start, end, pos = pos, pos + len(line), pos + len(line) + 1
            if not line.strip():
                continue
            sid = None
            if not _TABLE_RULE.match(line):
                n += 1
                sid = f"S{n}"
            b.segments.append(Segment(sid, line, start, end))
    return blocks


def pack(blocks: list[Block], fits: Callable[[list[Block]], bool]) -> tuple[list[Block], list[Block]]:
    """Admit whole blocks round-robin over products while fits(numbered candidate list) holds.
    Returns (admitted, omitted)."""
    groups: dict[str | None, list[Block]] = {}
    for b in blocks:
        groups.setdefault(b.product, []).append(b)
    admitted: list[Block] = []
    omitted: list[Block] = []
    for row in zip_longest(*groups.values()):
        for b in row:
            if b is None:
                continue
            if fits(number(admitted + [b])):
                admitted.append(b)
            else:
                omitted.append(b)
    return number(admitted), omitted


def spans(blocks: list[Block]) -> dict[str, tuple[Block, Segment]]:
    return {s.id: (b, s) for b in blocks for s in b.segments if s.id}
