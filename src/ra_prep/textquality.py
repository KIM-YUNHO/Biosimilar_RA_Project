"""Cheap, model-free text quality checks.

Used twice: by the router (is a text layer usable?) and after OCR (is a line garbled?).
A garbled OCR line looks like "rpora  n o ror ri r ro r ro oa" or
"Tfhosyiir ss t y ts e rosi": many 1-2 letter lowercase fragments and odd letter runs.
Acronyms (ALT, GGT, SC) and tokens with digits (BM12H, q12w) are ignored, since RA text
is full of them.
"""
from __future__ import annotations

import re

_VOWEL = re.compile(r"[aeiouy]", re.I)
_STRIP = re.compile(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$")
# short lowercase words that are legitimately common in RA prose
_SHORT_OK = {
    "a", "i", "an", "as", "at", "be", "by", "do", "if", "in", "is", "it", "no", "of", "on",
    "or", "to", "up", "us", "we", "mg", "ml", "kg", "dl", "vs", "eg", "ie", "per", "and",
    "the", "for", "not", "was", "are", "who", "all", "any", "may", "has", "had", "its", "one",
    "two", "day", "use", "via", "nm", "ng", "µg", "ug", "iu", "h", "x", "e", "g", "n",
}
_CONSONANT_RUN = re.compile(r"[^aeiouy\W\d_]{5,}", re.I)


def _words(text: str) -> list[str]:
    out = []
    for raw in text.split():
        w = _STRIP.sub("", raw)
        if not w or any(ch.isdigit() for ch in w):
            continue  # numbers, ids, doses
        for part in w.split("-"):  # "US-Stelara", "post-stenotic"
            part = part.strip("'")
            if not part.isalpha():
                continue
            if part.isupper() and len(part) <= 8:
                continue  # acronyms
            out.append(part)
    return out


def _bad(w: str) -> bool:
    lw = w.lower()
    if lw in _SHORT_OK or len(lw) == 1:  # single letters: footnote marks, bullets
        return False
    if len(lw) <= 2:
        return True
    return not _VOWEL.search(lw) or bool(_CONSONANT_RUN.search(lw))


def bad_token_count(text: str) -> int:
    return sum(_bad(w) for w in _words(text))


def garble_score(text: str) -> float:
    """0 = clean, 1 = garbage. 0 when there are too few judgeable words."""
    ws = _words(text)
    if len(ws) < 3:
        return 0.0
    return sum(_bad(w) for w in ws) / len(ws)


def is_garbled(text: str, threshold: float = 0.4) -> bool:
    return garble_score(text) >= threshold


def garbled_spans(text: str, window: int = 6, threshold: float = 0.5) -> bool:
    """True if some run of `window` consecutive words is mostly garbage. Catches one broken
    line merged into an otherwise clean paragraph."""
    ws = _words(text)
    if len(ws) < window:
        return is_garbled(text)
    for i in range(len(ws) - window + 1):
        if sum(_bad(w) for w in ws[i:i + window]) / window >= threshold:
            return True
    return False


# -- letter-trigram model ------------------------------------------------------------
# Counts trained on clean text layers of this corpus (EMA/FDA/HC, 2026-10-09). No neural
# model: P(c3 | c1 c2) from counts with add-k smoothing, over a-z and space.
_LM = None


def _lm():
    global _LM
    if _LM is None:
        import json
        import math
        from collections import Counter
        from pathlib import Path

        tri = json.loads((Path(__file__).parent / "data" / "trigram_en.json").read_text())["trigrams"]
        ctx: Counter = Counter()
        for k, v in tri.items():
            ctx[k[:2]] += v
        _LM = (tri, ctx, math.log)
    return _LM


_NONLETTER = re.compile(r"[^a-z]+")


def _logprobs(text: str) -> list[float]:
    tri, ctx, log = _lm()
    t = " " + _NONLETTER.sub(" ", text.lower()).strip() + " "
    return [log((tri.get(t[i:i + 3], 0) + 0.1) / (ctx.get(t[i:i + 2], 0) + 2.7))
            for i in range(len(t) - 2)]


def lm_score(text: str) -> float:
    """Mean log-probability per character. Clean English prose is around -2.0 to -2.6."""
    lp = _logprobs(text)
    return sum(lp) / len(lp) if lp else 0.0


def lm_worst_window(text: str, window: int = 20) -> float:
    lp = _logprobs(text)
    if len(lp) < window:
        return sum(lp) / len(lp) if lp else 0.0
    run = sum(lp[:window])
    worst = run
    for i in range(window, len(lp)):
        run += lp[i] - lp[i - window]
        worst = min(worst, run)
    return worst / window


def looks_garbled(text: str) -> bool:
    """Either signal fires: odd letter sequences, or a run of broken short fragments."""
    if sum(ch.isalpha() for ch in text) < 12:
        return False
    return lm_worst_window(text) < -3.7 or garbled_spans(text)
