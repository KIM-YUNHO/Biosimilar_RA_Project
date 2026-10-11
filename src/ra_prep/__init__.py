"""Preprocessing for stored RA documents: page routing, extraction, RA post-processing.

Deterministic by design: Docling layout/table models, RapidOCR and rules only.
No LLM is called anywhere in this package.
"""

ENGINE_VERSION = "prep-1.2"  # 1.1: text-layer repairs (textfix); 1.2: speaker rule for verdicts
# Earlier versions whose extraction stage (Docling, OCR, re-reads) is the same as now: their
# stored parses are upgraded by replaying textfix + structure ("refix") instead of re-parsing.
REFIXABLE_FROM = ("prep-1", "prep-1.1")
