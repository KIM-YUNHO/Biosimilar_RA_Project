"""Preprocessing for stored RA documents: page routing, extraction, RA post-processing.

Deterministic by design: Docling layout/table models, RapidOCR and rules only.
No LLM is called anywhere in this package.
"""

ENGINE_VERSION = "prep-1"
