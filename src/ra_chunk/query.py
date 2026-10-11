"""Rule-based query routing (no LLM): which products, agency and document kind a question is
about, from names found in the question text.

  products  development codes and brand names from config/products.yaml (all agencies)
  agency    "EMA", "FDA", "Health Canada"/"HC"
  guidance  "가이던스", "가이드라인", "guidance", "guideline", "기준"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ASCII-only boundaries: Korean particles follow names directly ("FDA는")
_AGENCY = {"ema": re.compile(r"(?<![A-Za-z])EMA(?![A-Za-z])|유럽"),
           "fda": re.compile(r"(?<![A-Za-z])FDA(?![A-Za-z])|미국 ?식약"),
           "hc": re.compile(r"Health Canada|(?<![A-Za-z])HC(?![A-Za-z])|캐나다")}
# "each product", "the four products": every biosimilar program in the registry (holdouts excluded)
_ALL_PRODUCTS = re.compile(r"각 제품|제품별|모든 제품|\d+개 제품|바이오시밀러 \d+개")
_GUIDANCE = re.compile(r"가이던스|가이드라인|guidance|guideline|기준", re.I)


@dataclass
class Route:
    products: list[str] = field(default_factory=list)
    agency: str | None = None
    guidance: bool = False


class Router:
    def __init__(self, products_yaml: str | Path):
        cfg = yaml.safe_load(Path(products_yaml).read_text(encoding="utf-8"))
        self.names: list[tuple[re.Pattern, str]] = []
        self.display: dict[str, list[str]] = {}
        self.biosimilars = [p["id"] for p in cfg["programs"]
                            if p.get("role") == "biosimilar" and not p.get("holdout")]
        for p in cfg["programs"]:
            brands = sorted({n for v in (p.get("aliases") or {}).values() for n in v
                             if not n.lower().startswith(p.get("ingredient", "~"))})
            self.display[p["id"]] = [p["id"], *brands]
            names = {p["id"], *p.get("codes", []),
                     *[n for v in (p.get("aliases") or {}).values() for n in v]}
            for n in names:
                if n.endswith("-"):
                    continue
                # "ABP654" also matches "ABP 654"; brand names match whole words
                pat = re.escape(n).replace(r"\ ", r"\s?").replace(r"\-", r"[-\s]?")
                if re.fullmatch(r"[A-Z]+\d+", n):
                    pat = re.sub(r"([A-Z])(\d)", r"\1\\s?\2", n, count=1)
                # the reference product as comparator ("EU-Stelara", "US-Stelara") is not a filter
                lead = r"(?<!EU-)(?<!US-)(?<!EU )(?<!US )" if p.get("role") == "reference" else ""
                self.names.append((re.compile(rf"{lead}(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])", re.I),
                                   p["id"]))

    def route(self, question: str) -> Route:
        prods: list[str] = []
        for pat, pid in self.names:
            if pid not in prods and pat.search(question):
                prods.append(pid)
        if _ALL_PRODUCTS.search(question):
            prods += [p for p in self.biosimilars if p not in prods]
        agencies = [a for a, pat in _AGENCY.items() if pat.search(question)]
        return Route(products=prods, agency=agencies[0] if len(agencies) == 1 else None,
                     guidance=bool(_GUIDANCE.search(question)))


class Glossary:
    """Append the English source-document terms for Korean RA terms found in a question
    (config/query_glossary.yaml). Longest terms first, each English term once."""

    def __init__(self, path: str | Path):
        terms = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["terms"]
        self.terms = sorted(terms.items(), key=lambda kv: -len(kv[0]))

    def expand(self, question: str) -> str:
        rest, extra = question, []
        for ko, en in self.terms:
            if ko in rest:
                rest = rest.replace(ko, " ")
                extra += [e for e in en if e not in extra]
        return f"{question}\n{'; '.join(extra)}" if extra else question


def product_subquery(question: str, router: "Router", product: str, names: dict[str, list[str]]) -> str:
    """For a question naming several products, the query for one of them: the other products'
    names removed and this product's names put in front ("Wezlana (ABP654): ...")."""
    q = question
    for pat, pid in router.names:
        if pid != product:
            q = pat.sub("", q)
    q = re.sub(r"(\s*,)+", ",", q)                 # "SB17, , ," -> "SB17,"
    q = re.sub(r"\(\s*,\s*|\s*,\s*\)", lambda m: "(" if "(" in m.group() else ")", q)
    q = re.sub(r"^\s*,\s*|\(\s*\)", "", q)
    q = re_space.sub(" ", q)
    return f"{' / '.join(names.get(product, [product]))}: {q.strip()}"


re_space = re.compile(r"[ \t]{2,}")
