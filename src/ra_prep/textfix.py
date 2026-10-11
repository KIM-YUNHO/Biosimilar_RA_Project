"""Text-layer repairs that need no OCR (applied to text_layer blocks after extraction).

1. Replacement characters. docling-parse returns U+FFFD for glyphs of some embedded
   fonts that pdfium decodes fine (FDA guidance and BMER pages: "stu�y", "Gui�ance").
   The block's text is re-read from the PDF with pdfium inside the block's box and used
   when it matches the original everywhere except at those characters.

2. Shifted font encoding. Some FDA PDFs embed fonts whose character codes are the
   glyph codes, which sit 29 below ASCII: "LV D UHFRPELQDQW" = "is a recombinant",
   "\x14\x13" = "10". Whole pages like that are routed to OCR (BROKEN_TEXT); runs of
   such words inside otherwise good text are decoded here by adding 29 back, but only
   when the decoded run reads clearly better as English (letter-trigram score).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

import pypdfium2 as pdfium

from .textquality import lm_score

SHIFT = 29
_TOKEN = re.compile(r"\S+")


def _shift_char(ch: str) -> str:
    o = ord(ch)
    # shifted codes for printable ASCII 33..126 live at 4..97
    return chr(o + SHIFT) if 4 <= o <= 97 else ch


def _shiftable(word: str, inner: bool = False) -> bool:
    """A word can be shifted text: no lowercase letters (shifted lowercase comes out as
    upper case and '[\\]') and every character in the shifted code range. A run must start
    with a word that has a letter; inside a run, letter-less tokens such as '*' (= 'G')
    are allowed."""
    w = word.replace("\ufffd", "")
    if not w or any("a" <= ch <= "z" for ch in w) or any(not 4 <= ord(ch) <= 97 for ch in w):
        return False
    return inner or any(ch.isalpha() for ch in w)


_CONTROL = {i for i in range(4, 32)} - {9, 10, 13}


# C1 controls seen in the same fonts, read from context: "BSA \x95 10%", "PASI \x95 12",
# "body weight \x94100 kg vs >100 kg"
_C1 = {"\x94": "\u2264", "\x95": "\u2265"}


def decode_controls(text: str) -> str:
    """Control characters never occur in real text; in these fonts they are shifted digits
    and punctuation ("\x14\x1a\x1b" = "178", "\x11" = ".", "\x08" = "%")."""
    return "".join(chr(ord(c) + SHIFT) if ord(c) in _CONTROL else _C1.get(c, c) for c in text)


def _decode_token(tok: str) -> str:
    return "".join(_shift_char(c) for c in tok.replace("\ufffd", ""))


_EDGE = re.compile(r"^([(\[]*)(.*?)([.,;:)\]]*)$")


def _variants(tok: str):
    """Ways a token may be shifted: whole; with real punctuation at its edges ("JURXSV."); or
    real parts joined by a hyphen or slash ("EU-6WHODUD")."""
    w = tok.replace("\ufffd", "")
    yield _decode_token(w)
    lead, core, trail = _EDGE.match(w).groups()
    if core and (lead or trail):
        yield lead + _decode_token(core) + trail
    if "-" in core or "/" in core:
        parts = re.split(r"([-/])", core)
        yield lead + "".join(
            p if p in "-/" or _gain(p, _decode_token(p)) <= 0 else _decode_token(p) for p in parts
        ) + trail


def _gain(orig: str, decoded: str) -> float:
    if not any(ch.isalpha() for ch in orig):
        return 0.0
    return lm_score(decoded) - lm_score(orig)


def _best_decode(tok: str) -> tuple[float, str]:
    w = tok.replace("\ufffd", "")
    return max(((_gain(w, v), v) for v in _variants(tok)), key=lambda x: x[0])


# short function words: a lone shifted "DQG" has too few letters for the score to decide
_STOP = {"and", "the", "of", "in", "to", "was", "were", "for", "with", "is", "are", "by", "at",
         "on", "or", "as", "be", "from", "that", "than", "this", "not", "an", "between"}


_SHIFTED_STOP = {"".join(chr(ord(c) - SHIFT) for c in w) for w in _STOP}


def _has_shift_evidence(text: str) -> bool:
    """Abbreviations (HPLC, SAF, NYHA) also decode to letter strings the score may prefer, so
    decoding needs proof that the text uses the shifted font: a shifted function word of three
    or more letters ("DQG", "WKH", "ZDV"), or two shorter ones ("LQ", "RI"; "LV" alone is
    also left ventricle)."""
    words = [w.strip(".,;:()[]") for w in text.split()]
    hits = [w for w in words if w in _SHIFTED_STOP]
    return any(len(w) >= 3 for w in hits) or len(hits) >= 2


def decode_shifted(text: str, min_gain: float = 0.3) -> str:
    """Decode the shifted words inside runs of shiftable tokens.

    Docling often mixes the two encodings inside one line ("ZHUH 26. %, 34.3%, DQG 34.3% LQ
    WKH"), so the decision is per token: a token is decoded when it reads better as English
    decoded (letter-trigram score gain > min_gain). Tokens that read better as they are
    (PK, AUC, CD4+, percentages) stay. A run whose only decodable token is short must decode
    to a common function word. Nothing is decoded without shifted function words in the text."""
    if not text or not _has_shift_evidence(text):
        return text
    tokens = list(_TOKEN.finditer(text))
    out, pos, i = [], 0, 0
    while i < len(tokens):
        if not _shiftable(tokens[i].group()):
            i += 1
            continue
        j = i
        while j + 1 < len(tokens) and _shiftable(tokens[j + 1].group(), inner=True):
            j += 1
        dec = {}
        for k in range(i, j + 1):
            tok = tokens[k].group()
            if _shiftable(tok):
                g, d = _best_decode(tok)
                if g > min_gain:
                    dec[k] = d
        if len(dec) == 1:
            (k, d), = dec.items()
            core = d.strip(".,;:()[]").lower()
            if core not in _STOP and sum(ch.isalpha() for ch in d) < 5:
                dec = {}
        for k, d in sorted(dec.items()):
            out.append(text[pos:tokens[k].start()])
            out.append(d)
            pos = tokens[k].end()
        i = j + 1
    out.append(text[pos:])
    return "".join(out)


# Shifted codes that Python treats as whitespace: "\x0b" "\x0c" = "(" ")", "\x1c".."\x1f" =
# "9" ":" ";" "<". Whitespace normalisation (ours and Docling's) silently drops them, so
# "\x1c\x13\x08 confidence interval \x0b&,\x0c" ends up as "0% confidence interval CI".
_HIDDEN = {chr(c) for c in (11, 12, 28, 29, 30, 31)}
_HIDDEN_DECODED = {chr(ord(c) + SHIFT) for c in _HIDDEN}


def _pdfium_raw(tp: pdfium.PdfTextPage, bbox, pad: float = 1.0) -> str:
    l, b, r, t = bbox
    return tp.get_text_bounded(left=l - pad, bottom=b - pad, right=r + pad, top=t + pad)


def _normalise(raw: str) -> str:
    """Control characters decoded before whitespace is collapsed, so none are lost."""
    return " ".join(decode_controls(raw).split())


def repair_replacement_chars(original: str, alt: str) -> str | None:
    """Fill U+FFFD runs in `original` with the characters pdfium read at the same place, and
    put back shifted characters that were dropped as whitespace ("0%" -> "90%").

    The two strings are aligned; only aligned segments where the original is nothing but
    U+FFFD (and spaces), or where pdfium has extra characters that are all such dropped
    characters, are changed, and only when both neighbours of the segment match, so
    differences elsewhere (hyphenation, reading order) do not leak in."""
    if not alt:
        return None
    a = " ".join(original.split())
    m = SequenceMatcher(None, a, alt, autojunk=False)
    if m.ratio() < 0.6:
        return None
    ops = m.get_opcodes()
    out, changed = [], False
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        seg, new = a[i1:i2], alt[j1:j2]
        anchored = 0 < k < len(ops) - 1 and ops[k - 1][0] == "equal" and ops[k + 1][0] == "equal"
        if (tag == "replace" and anchored and "\ufffd" in seg and not set(seg) - {"\ufffd", " "}
                and (j2 - j1) <= 3 * (i2 - i1) + 2):
            out.append(new)
            changed = True
        elif tag == "insert" and anchored and not set(new) - _HIDDEN_DECODED - {" "}:
            out.append(new)
            changed = True
        elif tag == "insert":
            continue  # other text only pdfium has: not ours to add
        else:
            out.append(seg)
    return "".join(out) if changed else None


def fix_blocks(path: str, blocks: list[dict]) -> dict:
    """Repair text_layer blocks and table cells in place. Returns counts."""
    stats = {"fffd_fixed": 0, "fffd_left": 0, "shift_decoded": 0, "hidden_restored": 0}
    pdf = None
    pages: dict[int, tuple] = {}

    def page(n):
        """(textpage, page has dropped-as-whitespace shifted codes)"""
        nonlocal pdf
        if pdf is None:
            pdf = pdfium.PdfDocument(path)
        if n not in pages:
            pg = pdf[n - 1]
            tp = pg.get_textpage()
            pages[n] = (pg, tp, bool(_HIDDEN & set(tp.get_text_range())))
        return pages[n][1], pages[n][2]

    def fix(obj: dict, key: str, bbox, page_no: int):
        t = obj[key]
        if not t:
            return
        has_fffd = "\ufffd" in t
        tp, hidden = page(page_no) if bbox else (None, False)  # one pdfium text page per page
        d = decode_shifted(decode_controls(t))
        if d != t:
            stats["shift_decoded"] += 1
        if tp is not None and (has_fffd or hidden):
            alt = decode_shifted(_normalise(_pdfium_raw(tp, bbox)))
            readable = d.replace("\ufffd", "").strip(" .,;:()-%")
            if has_fffd and not readable and alt and "\ufffd" not in alt:
                new = alt  # nothing usable in Docling's text: take pdfium's reading
            else:
                new = repair_replacement_chars(d, alt)
            if new is not None and new != d:
                stats["fffd_fixed" if has_fffd else "hidden_restored"] += 1
                d = new
            elif has_fffd:
                stats["fffd_left"] += 1
        obj[key] = d

    try:
        for blk in blocks:
            if blk.get("source") != "text_layer":
                continue
            if blk["type"] == "text":
                fix(blk, "text", blk.get("bbox"), blk["page"])
            elif blk["type"] == "table":
                for c in blk["cells"]:
                    fix(c, "text", c.get("bbox"), blk["page"])
    finally:
        for pg, tp, _ in pages.values():
            tp.close()
            pg.close()
        if pdf is not None:
            pdf.close()
    return stats
