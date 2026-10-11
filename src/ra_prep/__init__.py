"""Preprocessing for stored RA documents: page routing, extraction, RA post-processing.

Deterministic by design: Docling layout/table models, RapidOCR and rules only.
No LLM is called anywhere in this package.
"""

ENGINE_VERSION = "prep-1.4"  # 1.1 textfix; 1.2 verdict speaker; 1.3 dropped shifted codes; 1.4 page-numbered headers
# Earlier versions whose extraction stage (Docling, OCR, re-reads) is the same as now: their
# stored parses are upgraded by replaying textfix + structure ("refix") instead of re-parsing.
REFIXABLE_FROM = ("prep-1", "prep-1.1", "prep-1.2", "prep-1.3")
