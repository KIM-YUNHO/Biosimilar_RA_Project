"""Build retrieval chunks from one parsed document.

Chunk kinds
  narrative  consecutive body blocks of one leaf section and one speaker, up to `target` tokens
  table      a whole table as Markdown (caption, header, rows, footnotes); long tables are split
             into row groups with the header repeated
  row        one table row as a line: "caption / group / row label: column=value; ..."
             (only tables with numbers or study IDs)
  block      one parsed block as it is (baseline variant)

Every narrative chunk has a parent (its whole leaf section), every row chunk the whole table;
retrieval finds the small chunk and the answer step reads the parent.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

# sections that are kept in the parse but not worth retrieving
_SKIP_SECTION = re.compile(
    r"table of contents|^contents$|list of (abbreviations|tables|figures)|^abbreviations$|"
    r"signature|financial disclosure|reviewers? (list|team)|^review team|^glossary$|"
    r"application (information|data sheet)", re.I)
_TOC_LEADER = re.compile(r"\.{6,}|…{3,}")
_NUM = re.compile(r"\d")
_SENT = re.compile(r"(?<=[.;:])\s+(?=[A-Z(\[])")
_GUIDANCE_ID = re.compile(r"^((?:[A-Z]{1,2}\.)?(?:[IVXL]+\.)?(?:[A-Z]\.)?(?:\d+\.)*\d*[a-z]?\.?)\s")

TEXT_LABELS = {"text", "list_item", "section_header", "caption", "footnote", None, "paragraph",
               "formula", "code", "title"}


def approx_tokens(text: str) -> int:
    """XLM-R (bge-m3) tokens for English RA prose: about one per 4 characters."""
    return max(1, (len(text) + 3) // 4)


@dataclass
class Chunk:
    id: str
    kind: str
    doc_id: int
    text: str
    pages: list[int]
    section_path: list[str]
    speaker: str | None
    parent_id: str | None = None
    table_caption: str | None = None
    study_ids: list[str] = field(default_factory=list)
    needs_review: bool = False
    guidance_section_id: str | None = None

    def meta(self) -> dict:
        return {"section_path": self.section_path, "speaker": self.speaker,
                "table_caption": self.table_caption, "study_ids": self.study_ids,
                "guidance_section_id": self.guidance_section_id}


@dataclass
class Parent:
    id: str
    doc_id: int
    text: str
    pages: list[int]


@dataclass
class ChunkConfig:
    narrative: str = "section"      # "section" | "block"
    target: int = 400                # narrative target tokens
    max_tokens: int = 600            # hard limit for one narrative or table chunk
    rows: bool = True                # row chunks for numeric / study tables
    parent_max: int = 2000           # tokens of a parent passed to the answer step
    min_words: int = 5               # narrative chunks shorter than this are dropped
    min_section: int = 0             # leaf sections under this many tokens join their parent section
    count: Callable[[str], int] = approx_tokens


def _cid(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def _skip_section(path: list[str]) -> bool:
    return any(_SKIP_SECTION.search(p.strip()) for p in path)


def guidance_section_id(path: list[str]) -> str | None:
    for p in reversed(path):
        m = _GUIDANCE_ID.match(p.strip() + " ")
        if m and any(ch.isalnum() for ch in m.group(1)):
            return m.group(1).rstrip(".")
    return None


# -- tables --------------------------------------------------------------------------------
def table_grid(block: dict) -> tuple[list[list[str]], int]:
    """Cells to a full grid (spanned cells filled with the value) and the number of header rows."""
    n_rows = block.get("n_rows") or (max((c["r"] + c.get("rs", 1) for c in block["cells"]), default=0))
    n_cols = block.get("n_cols") or (max((c["c"] + c.get("cs", 1) for c in block["cells"]), default=0))
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    header_rows: set[int] = set()
    for c in block["cells"]:
        for r in range(c["r"], min(c["r"] + c.get("rs", 1), n_rows)):
            for k in range(c["c"], min(c["c"] + c.get("cs", 1), n_cols)):
                if not grid[r][k]:
                    grid[r][k] = " ".join(c["text"].split())
        if c.get("header"):
            header_rows.add(c["r"])
    n_head = 0
    while n_head in header_rows and n_head < n_rows - 1:
        n_head += 1
    n_head = max(n_head, _infer_header_rows(grid))  # Docling often flags only the first row
    for r in range(n_head):  # a header cell spanning columns often comes out as value + blanks
        for k in range(1, n_cols):
            if not grid[r][k] and grid[r][k - 1] and k - 1 > 0:
                grid[r][k] = grid[r][k - 1]
    return grid, n_head


_VALUE = re.compile(r"^[-+<>≤≥~]?\s*\d[\d.,]*\s*(%|\(.*\)|\[.*\])?\s*$|^(NA|NC|ND|-|–|—)$")


def _infer_header_rows(grid: list[list[str]], limit: int = 6) -> int:
    """Without header flags from Docling: the rows above the first data row (one whose
    filled cells after the first column are mostly plain values) are headers. Rows with only
    the first cell filled are group labels, not headers."""
    last_head = -1
    for r, row in enumerate(grid[:limit + 1]):
        rest = [v for v in row[1:] if v]
        if not rest:
            continue
        if sum(bool(_VALUE.match(v)) for v in rest) >= 0.5 * len(rest):
            return last_head + 1
        last_head = r
    return 0


def _column_headers(grid: list[list[str]], n_head: int) -> list[str]:
    cols = []
    for k in range(len(grid[0]) if grid else 0):
        parts: list[str] = []
        for r in range(n_head):
            v = grid[r][k]
            if v and v not in parts:
                parts.append(v)
        cols.append(" / ".join(parts))
    return cols


def _is_toc_or_abbrev(block: dict, grid: list[list[str]], cols: list[str]) -> bool:
    flat = [v for row in grid for v in row if v]
    if flat and sum(bool(_TOC_LEADER.search(v)) for v in flat) >= max(2, len(flat) // 5):
        return True
    head = " ".join(cols).lower()
    return bool(re.search(r"abbreviation|acronym", head)) or (
        len(cols) == 2 and re.fullmatch(r"(term|abbreviation)s? / ?|term", (cols[0] or "").lower()) is not None)


def _md_row(vals: list[str]) -> str:
    return "| " + " | ".join(v.replace("|", "/") for v in vals) + " |"


def table_markdown(caption: str, cols: list[str], rows: list[list[str]], notes: list[str]) -> str:
    out = [caption] if caption else []
    if any(cols):
        out += [_md_row(cols), _md_row(["---"] * len(cols))]
    out += [_md_row(r) for r in rows]
    out += notes
    return "\n".join(out)


def row_lines(caption: str, grid: list[list[str]], n_head: int, cols: list[str]) -> list[tuple[int, str]]:
    """One line per data row. A row with only its first cell filled is a group label for the
    rows below it ("Cmax (ug/mL)", "PK Similarity Study")."""
    out, group = [], ""
    for r in range(n_head, len(grid)):
        row = grid[r]
        if not any(row):
            continue
        label, rest = row[0], row[1:]
        if label and not any(v and v != label for v in rest):
            group = label
            continue
        pairs = [f"{cols[k + 1] or f'col{k + 2}'}={v}" for k, v in enumerate(rest)
                 if v and k + 1 < len(cols)]
        if not pairs:
            continue
        lead = " / ".join(x for x in (caption, group, label) if x)
        out.append((r, f"{lead}: " + "; ".join(pairs)))
    return out


# -- document ------------------------------------------------------------------------------
def chunk_document(doc: dict, cfg: ChunkConfig) -> tuple[list[Chunk], dict[str, Parent]]:
    did = doc["document_id"]
    blocks = [b for b in doc["blocks"]
              if not b.get("furniture") and b.get("layer", "body") == "body"]
    is_guidance = doc.get("doc_type") == "guidance"
    chunks: list[Chunk] = []
    parents: dict[str, Parent] = {}
    captions = {(b.get("caption") or "").strip() for b in blocks if b["type"] == "table"} - {""}

    # footnotes directly below a table belong to the table
    table_notes: dict[int, list[str]] = {}
    last_table = None
    for i, b in enumerate(blocks):
        if b["type"] == "table":
            last_table = i
        elif b["type"] == "text" and b.get("label") == "footnote" and last_table is not None \
                and b["page"] == blocks[last_table]["page"]:
            table_notes.setdefault(last_table, []).append(b["text"])
            b["_attached"] = True
        elif b["type"] == "text" and b.get("label") not in ("footnote", "caption"):
            last_table = None

    def gid(path):
        return guidance_section_id(path) if is_guidance else None

    # short leaf sections (one-sentence label indications, short Q&A items) join their siblings
    # under the parent section; each keeps its heading as a lead-in so the text stays specific
    if cfg.min_section and cfg.narrative == "section":
        def eligible(b):
            return (b["type"] == "text" and b.get("label") != "section_header" and not b.get("_attached")
                    and b.get("label") in TEXT_LABELS and b["text"].strip())
        size: dict[tuple, int] = {}
        for b in blocks:
            if eligible(b):
                key = tuple(b.get("section_path", []))
                size[key] = size.get(key, 0) + cfg.count(b["text"])
        last_leaf = None
        for i, b in enumerate(blocks):
            if not eligible(b):
                continue
            path = list(b.get("section_path", []))
            if len(path) > 1 and size.get(tuple(path), 0) < cfg.min_section:
                lead = f"{path[-1]}: " if tuple(path) != last_leaf else ""
                blocks[i] = {**b, "section_path": path[:-1], "text": lead + b["text"]}
                last_leaf = tuple(path)
            else:
                last_leaf = None

    # sections: parents of narrative chunks
    sec_text: dict[tuple, list[tuple[int, str]]] = {}
    for b in blocks:
        if b["type"] == "text" and b.get("label") != "section_header" and not b.get("_attached") \
                and b.get("label") in TEXT_LABELS and b["text"].strip():
            sec_text.setdefault(tuple(b.get("section_path", [])), []).append((b["page"], b["text"]))
    for path, items in sec_text.items():
        if _skip_section(list(path)):
            continue
        pid = _cid(did, "sec", *path)
        text = "\n".join(t for _, t in items)
        parents[pid] = Parent(pid, did, text, sorted({p for p, _ in items}))

    # narrative
    def flush(buf: list[dict]):
        if not buf:
            return
        path = buf[0].get("section_path", [])
        text = "\n".join(b["text"] for b in buf)
        if len(text.split()) < cfg.min_words:  # stray titles, page labels: nothing to retrieve
            return
        pid = _cid(did, "sec", *path)
        chunks.append(Chunk(
            id=_cid(did, "n", buf[0]["ref"] if "ref" in buf[0] else buf[0]["page"], len(chunks)),
            kind="narrative" if cfg.narrative == "section" else "block", doc_id=did, text=text,
            pages=sorted({b["page"] for b in buf}), section_path=path, speaker=buf[0].get("speaker"),
            parent_id=pid if pid in parents and cfg.narrative == "section" else None,
            study_ids=sorted({s for b in buf for s in b.get("study_ids", [])}),
            needs_review=any(b.get("status") == "needs_review" for b in buf), guidance_section_id=gid(path)))

    buf: list[dict] = []
    size = 0
    for i, b in enumerate(blocks):
        if b["type"] == "table":
            flush(buf)
            buf, size = [], 0
            _table_chunks(did, i, b, table_notes.get(i, []), cfg, chunks, parents, gid)
            continue
        if b["type"] != "text" or b.get("_attached") or b.get("label") not in TEXT_LABELS:
            continue
        text = b["text"].strip()
        if not text or b.get("label") == "section_header" or _skip_section(b.get("section_path", [])):
            continue
        if b.get("label") == "caption" and text in captions:
            continue
        if cfg.narrative == "block":
            flush([b])
            continue
        key = (tuple(b.get("section_path", [])), b.get("speaker"))
        n = cfg.count(text)
        if buf and (key != (tuple(buf[0].get("section_path", [])), buf[0].get("speaker"))
                    or size + n > cfg.target):
            flush(buf)
            buf, size = [], 0
        if n > cfg.max_tokens:  # one long block: split at sentence ends
            for piece in _split(text, cfg.target, cfg.count):
                flush([{**b, "text": piece}])
            continue
        buf.append(b)
        size += n
    flush(buf)
    return chunks, parents


def _split(text: str, target: int, count) -> Iterable[str]:
    cur, n = [], 0
    for s in _SENT.split(text):
        k = count(s)
        if cur and n + k > target:
            yield " ".join(cur)
            cur, n = [], 0
        cur.append(s)
        n += k
    if cur:
        yield " ".join(cur)


def _table_chunks(did, i, b, notes, cfg: ChunkConfig, chunks, parents, gid):
    path = b.get("section_path", [])
    if _skip_section(path) or not b.get("cells"):
        return
    grid, n_head = table_grid(b)
    if not grid:
        return
    cols = _column_headers(grid, n_head)
    if _is_toc_or_abbrev(b, grid, cols):
        return
    caption = (b.get("caption") or "").strip()
    rows = grid[n_head:]
    review = any(c.get("status") == "needs_review" for c in b["cells"])
    tid = _cid(did, "t", b.get("ref", i))
    full = table_markdown(caption, cols, rows, notes)
    parents[tid] = Parent(tid, did, full, [b["page"]])
    common = dict(doc_id=did, pages=[b["page"]], section_path=path, speaker=b.get("speaker"),
                  table_caption=caption or None, study_ids=b.get("study_ids", []), needs_review=review,
                  guidance_section_id=gid(path))
    # whole table, split into row groups with the header repeated when long
    group: list[list[str]] = []
    parts: list[list[list[str]]] = []
    for r in rows:
        if group and cfg.count(table_markdown(caption, cols, group + [r], [])) > cfg.max_tokens:
            parts.append(group)
            group = []
        group.append(r)
    parts.append(group)
    for k, g in enumerate(parts):
        body = table_markdown(caption, cols, g, notes if k == len(parts) - 1 else [])
        chunks.append(Chunk(id=_cid(tid, "part", k), kind="table", text=body,
                            parent_id=tid if cfg.narrative == "section" else None, **common))
    if not cfg.rows:
        return
    flat = [v for row in rows for v in row[1:] if v]
    numeric = flat and sum(bool(_NUM.search(v)) for v in flat) >= 0.3 * len(flat)
    if not (numeric or b.get("study_ids")):
        return
    for r, line in row_lines(caption, grid, n_head, cols):
        chunks.append(Chunk(id=_cid(tid, "row", r), kind="row", text=line, parent_id=tid, **common))
