"""Programs for products the registry (config/products.yaml) does not list.

A request names an ingredient or a product that nobody has written a registry entry for.
The agency catalogs still list every authorisation of the ingredient, so the programs can
be derived from them:

  1. role     FDA: 351(a) = reference, 351(k) / reference-product column = biosimilar.
              EMA: "Biosimilar" flag. HC: not in the DPD, decided by the cluster.
              An EMA product that is merely "not a biosimilar" is a reference only when the
              FDA shows no 351(a) product of the ingredient (a withdrawn application or a
              hybrid is not the originator).
  2. cluster  registrations that are one development programme are joined by
                same_brand     same brand name (IV / SC variants removed) at any agency
                same_holder    EMA only: the same marketing holder, both biosimilars (the EMA
                               grants duplicate authorisations: Eksunbi = Pyzchiva)
                similar_name   other agencies, 7+ letters, one edit (Amjevita / Amgevita) or
                               same length, same first three letters, three edits at most
                               (Wezlana / Wezenla)
                same_holder_cross
                               the same company markets one programme under different brands
                               in different regions (Imraldi EU = Hadlima US). Only when the
                               company has a single programme of the ingredient in every
                               agency it appears in.
  3. program  one per cluster: id = brand name (FDA, else EMA, else HC), aliases per agency
              = every brand name and the FDA non-proprietary name of its members.

The rules are heuristics. Every program carries the rule that joined its members (`basis`),
and the pipeline writes an `auto_program` event, so a person can check or correct the
mapping by adding the programme to config/products.yaml (curated entries win next time).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .connectors.hc import base_brand
from .models import RegistrationRecord
from .registry import Program
from .util import norm_name

_LEGAL = {"co", "ltd", "limited", "inc", "incorporated", "llc", "corp", "corporation", "bv", "nv", "nl",
          "gmbh", "ag", "kg", "kft", "sa", "ab", "as", "plc", "lp", "the", "canada", "europe", "usa", "us",
          "uk", "international", "holding", "holdings"}
_FDA_SUFFIX = re.compile(r"-[a-z]{4}$")


def lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def holder_key(holder: str | None) -> str:
    """First significant word of a company name: 'Samsung Bioepis NL B.V.' -> 'samsung'."""
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", (holder or "").lower()).split() if w not in _LEGAL]
    return words[0] if words else ""


def brand_key(reg: RegistrationRecord) -> str:
    return norm_name(base_brand(reg.brand_name))


def base_ingredient(reg: RegistrationRecord) -> str:
    """'ustekinumab-ttwe' / 'USTEKINUMAB' -> 'ustekinumab' (first ingredient only)."""
    text = (reg.identifiers.get("proper_name") if isinstance(reg.identifiers.get("proper_name"), str) else None) \
        or reg.ingredient or ""
    first = re.split(r"[,;/]", text.lower())[0].strip()
    return _FDA_SUFFIX.sub("", first)


def is_reference(reg: RegistrationRecord) -> bool | None:
    """True / False when the catalog says so, None when it does not (Health Canada)."""
    lic = (reg.licence_type or "").lower()
    if reg.agency == "fda":
        if any(w in lic for w in ("351(k)", "biosimilar", "interchangeable")):
            return False
        if "351(a)" in lic:
            return True
        return False if reg.raw.get("reference_product") else None
    if reg.agency == "ema":
        return lic != "biosimilar"
    return None


@dataclass
class AutoResult:
    programs: list[Program] = field(default_factory=list)
    assignment: dict[tuple[str, str], str] = field(default_factory=dict)   # (agency, native_key) -> program id
    basis: dict[str, list[str]] = field(default_factory=dict)              # program id -> rules that joined it


class _Union:
    def __init__(self, n: int):
        self.p = list(range(n))
        self.why: dict[int, set[str]] = {}

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def join(self, a: int, b: int, rule: str) -> None:
        ra, rb = self.find(a), self.find(b)
        why = self.why.pop(ra, set()) | self.why.pop(rb, set()) | {rule}
        self.p[ra] = rb
        self.why[rb] = why


def _title(brand: str) -> str:
    return brand.title() if brand.isupper() else brand


def build_programs(regs: list[RegistrationRecord], taken: set[str] | None = None,
                   has_reference: bool = False) -> AutoResult:
    """Group registrations (same ingredient) into programs. `taken`: ids that must not be
    reused. `has_reference`: the ingredient already has a reference program (curated), so an
    EMA "not a biosimilar" flag alone does not make another one."""
    taken = {t.lower() for t in (taken or set())}
    regs = [r for r in regs if r.brand_name]
    u = _Union(len(regs))
    ref = [is_reference(r) for r in regs]
    keys = [brand_key(r) for r in regs]
    holders = [holder_key(r.holder) for r in regs]

    for i in range(len(regs)):
        for j in range(i + 1, len(regs)):
            if keys[i] and keys[i] == keys[j]:
                u.join(i, j, "same_brand")
            elif ref[i] is not True and ref[j] is not True:
                if regs[i].agency == regs[j].agency:
                    if regs[i].agency == "ema" and holders[i] and holders[i] == holders[j]:
                        u.join(i, j, "same_holder")
                elif len(keys[i]) >= 7 and len(keys[i]) == len(keys[j]):
                    d = lev(keys[i], keys[j])
                    if d <= 1 or (d <= 3 and keys[i][:3] == keys[j][:3]):
                        u.join(i, j, "similar_name")

    by_holder: dict[str, list[int]] = {}
    for i, h in enumerate(holders):
        if h and ref[i] is not True:
            by_holder.setdefault(h, []).append(i)
    for idx in by_holder.values():
        roots: dict[str, set[int]] = {}
        for i in idx:
            roots.setdefault(regs[i].agency, set()).add(u.find(i))
        if len({r for rs in roots.values() for r in rs}) > 1 and all(len(rs) == 1 for rs in roots.values()):
            for i in idx[1:]:
                u.join(idx[0], i, "same_holder_cross")

    clusters: dict[int, list[int]] = {}
    for i in range(len(regs)):
        clusters.setdefault(u.find(i), []).append(i)

    fda_reference = has_reference or any(r.agency == "fda" and is_reference(r) is True for r in regs)
    out = AutoResult()
    order = sorted(clusters.items(), key=lambda kv: min(regs[i].first_approval_date or "9999" for i in kv[1]))
    for root, members in order:
        rs = [regs[i] for i in members]
        flags = [ref[i] for i in members]
        if any(f is True and r.agency == "fda" for f, r in zip(flags, rs)):
            role = "reference"
        elif any(f is True for f in flags) and not fda_reference:
            role = "reference"
        else:
            role = "biosimilar"
        lead = sorted(rs, key=lambda r: ({"fda": 0, "ema": 1}.get(r.agency, 2), r.first_approval_date or "9999"))[0]
        pid = base = _title(base_brand(lead.brand_name))
        n = 2
        while pid.lower() in taken:
            pid, n = f"{base}-{n}", n + 1
        taken.add(pid.lower())

        aliases: dict[str, list[str]] = {}
        for r in rs:
            names = [_title(base_brand(r.brand_name))]
            if isinstance(r.identifiers.get("proper_name"), str) and r.agency == "fda":
                names.append(r.identifiers["proper_name"])
            names += [n_ for n_ in r.identifiers.get("other_names", []) if isinstance(n_, str)]
            lst = aliases.setdefault(r.agency, [])
            lst.extend(n_ for n_ in names if n_ and n_ not in lst)
        holder = next((r.holder for r in (lead, *rs) if r.holder), None)
        out.programs.append(Program(id=pid, role=role, ingredient=base_ingredient(lead),
                                    developer=holder, aliases=aliases))
        out.basis[pid] = sorted(u.why.get(root, set())) or ["single_registration"]
        for r in rs:
            out.assignment[(r.agency, r.native_key)] = pid
    return out
