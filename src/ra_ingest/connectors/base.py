from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Iterable

from ..http import Fetcher, FetchResult
from ..models import DocumentRecord, RegistrationRecord
from ..registry import Target

# (level, kind, message, detail) -> None ; connectors report partial failures here
# instead of raising, so one broken source does not stop the other sources of an agency.
Reporter = Callable[..., None]


class Connector(ABC):
    """Agency connector contract.

    discover_registrations: catalog/API lookup for the target -> RegistrationRecord
    discover_documents:     documents of one registration       -> DocumentRecord
    expand_index:           links inside a fetched index page    -> DocumentRecord (optional)
    """

    agency: str

    def __init__(self, fetcher: Fetcher, report: Reporter):
        self.fetcher = fetcher
        self.report = report

    @abstractmethod
    def discover_registrations(self, target: Target) -> Iterable[RegistrationRecord]: ...

    @abstractmethod
    def discover_documents(self, reg: RegistrationRecord, target: Target) -> Iterable[DocumentRecord]: ...

    def expand_index(self, doc: DocumentRecord, page: FetchResult) -> Iterable[DocumentRecord]:
        return []

    def keep(self, target: Target, names: list[str], ingredient_text: str | None) -> bool:
        """Selection rule shared by connectors.
        product-scoped run: keep only alias matches.
        ingredient run: keep anything whose ingredient field matches (unmapped products
        are kept too and flagged later, so the catalog shows every same-ingredient product)."""
        from ..util import contains_any, norm_name

        alias_hit = bool({norm_name(n) for n in names if n} &
                         {norm_name(a) for a in target.search_names(self.agency)})
        if target.product_scoped:
            return alias_hit
        return alias_hit or contains_any(ingredient_text, target.ingredient_synonyms)
