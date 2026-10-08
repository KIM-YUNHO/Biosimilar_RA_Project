"""Product registry and target resolution.

A run targets an ingredient ("ustekinumab") and/or products named by development code,
brand name or FDA suffix name ("SB17", "Wezenla", "ustekinumab-auub"). Resolution
returns the programs to ingest plus the names each agency connector should search for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import AGENCIES, RegistrationRecord
from .util import norm_name


@dataclass
class Program:
    id: str
    role: str
    ingredient: str
    developer: str | None = None
    codes: list[str] = field(default_factory=list)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    identifiers: dict[str, Any] = field(default_factory=dict)
    agency_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    seed_documents: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    holdout: bool = False

    def all_names(self) -> list[str]:
        names = [self.id, *self.codes]
        for v in self.aliases.values():
            names.extend(v)
        return names

    def agency_names(self, agency: str) -> list[str]:
        return list(self.aliases.get(agency, []))

    def expected_at(self, agency: str) -> bool:
        return self.agency_status.get(agency, {}).get("approved", True)


@dataclass
class Target:
    """What one ingest run is about."""

    ingredient: str | None
    programs: list[Program]
    ingredient_synonyms: list[str]
    product_scoped: bool  # True when the user named products: connectors keep only alias matches

    def search_names(self, agency: str) -> list[str]:
        names: list[str] = []
        for p in self.programs:
            names.extend(p.agency_names(agency))
        return names

    def match_program(self, agency: str, reg: RegistrationRecord) -> str | None:
        """Map a registration to a program by agency alias, then by any program name."""
        reg_names = {norm_name(n) for n in reg.names()}
        for p in self.programs:
            if reg_names & {norm_name(a) for a in p.agency_names(agency)}:
                return p.id
        for p in self.programs:
            if reg_names & {norm_name(a) for a in p.all_names()}:
                return p.id
        return None


class Registry:
    def __init__(self, data: dict[str, Any]):
        self.ingredients: dict[str, dict[str, Any]] = data.get("ingredients", {})
        self.programs: list[Program] = [
            Program(
                id=str(p["id"]),
                role=p.get("role", "biosimilar"),
                ingredient=p["ingredient"],
                developer=p.get("developer"),
                codes=[str(c) for c in p.get("codes", [])],
                aliases={k: list(v or []) for k, v in (p.get("aliases") or {}).items()},
                identifiers=p.get("identifiers") or {},
                agency_status=p.get("agency_status") or {},
                seed_documents=p.get("seed_documents") or {},
                holdout=bool(p.get("holdout", False)),
            )
            for p in data.get("programs", [])
        ]

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {})

    def find_program(self, name: str) -> Program | None:
        key = norm_name(name)
        for p in self.programs:
            if key in {norm_name(n) for n in p.all_names()}:
                return p
        return None

    def resolve(
        self,
        ingredient: str | None = None,
        products: list[str] | None = None,
        include_holdout: bool = False,
    ) -> Target:
        if not ingredient and not products:
            raise ValueError("ingredient 또는 product 중 하나는 지정해야 합니다")
        selected: list[Program] = []
        if products:
            unknown = []
            for name in products:
                p = self.find_program(name)
                if p is None:
                    unknown.append(name)
                elif p not in selected:
                    selected.append(p)
            if unknown:
                raise ValueError(f"레지스트리에 없는 제품: {', '.join(unknown)} (config/products.yaml에 추가)")
            ingredient = ingredient or selected[0].ingredient
        else:
            selected = [
                p for p in self.programs
                if p.ingredient == ingredient and (include_holdout or not p.holdout)
            ]
        ing_cfg = self.ingredients.get(ingredient or "", {})
        synonyms = list(ing_cfg.get("synonyms") or ([ingredient] if ingredient else []))
        return Target(
            ingredient=ingredient,
            programs=selected,
            ingredient_synonyms=synonyms,
            product_scoped=bool(products),
        )


def validate_agencies(agencies: list[str]) -> list[str]:
    allowed = set(AGENCIES) | {"guidance", "ctgov"}
    bad = [a for a in agencies if a not in allowed]
    if bad:
        raise ValueError(f"지원하지 않는 기관: {bad} (허용: {sorted(allowed)})")
    return agencies
