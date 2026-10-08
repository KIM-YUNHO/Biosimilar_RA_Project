from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

_MONTHS = "%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%d %b %Y"


def norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def norm_name(s: str) -> str:
    """Normalize a product/code name for alias matching: 'CT-P43' == 'ct p43' == 'CTP43'."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def pick(row: dict[str, Any], *candidates: str, default: Any = None) -> Any:
    """Tolerant field getter: matches keys after normalization, so 'Proper Name',
    'proper_name' and 'PROPER-NAME' are the same column."""
    if not row:
        return default
    normalized = {norm_key(k): v for k, v in row.items()}
    for c in candidates:
        v = normalized.get(norm_key(c))
        if v not in (None, ""):
            return v
    return default


def to_iso_date(value: Any, dayfirst: bool = False) -> str | None:
    """Parse the date shapes seen in agency data. dayfirst=True for EMA (dd/mm/yyyy),
    False for FDA (mm/dd/yyyy)."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        d, mo = (a, b) if dayfirst else (b, a)
        try:
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            return None
    for fmt in _MONTHS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def contains_any(text: str | None, needles: Iterable[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(n.lower() in t for n in needles if n)
