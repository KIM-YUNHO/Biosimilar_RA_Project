"""Chunking of preprocessed RA documents (data/parsed/*.json) for retrieval.

Deterministic: rules over the blocks ra_prep produced (sections, speaker, tables). No LLM.
See docs/chunking-strategy.md.
"""

CHUNKER_VERSION = "chunk-1"
