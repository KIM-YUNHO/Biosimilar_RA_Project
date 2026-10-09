"""One document through preprocessing: sanitize -> page signals -> routes -> extraction."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .extract import Extractor
from .route import route_document
from .sanitize import remove_watermarks
from .signals import document_signals


def process_pdf(path: str, doc_type: str | None, extractor: Extractor,
                workdir: str | None = None) -> dict:
    """Returns {"routes": [...], "blocks": [...], "engine": str, "timing": {...},
                "watermarks_removed": int}."""
    t0 = time.time()
    tmpdir = None
    if workdir is None:
        tmpdir = tempfile.TemporaryDirectory(prefix="ra-prep-")
        workdir = tmpdir.name
    try:
        clean = str(Path(workdir) / (Path(path).stem + ".clean.pdf"))
        removed = remove_watermarks(path, clean)
        src = clean if removed else path
        routes = route_document(document_signals(src), doc_type)
        res = extractor.extract(src, routes)
        res["routes"] = routes
        res["watermarks_removed"] = removed
        res["timing"]["total"] = time.time() - t0
        return res
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()
