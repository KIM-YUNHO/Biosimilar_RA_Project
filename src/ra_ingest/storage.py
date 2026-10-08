from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlparse


def guess_ext(url: str, content_type: str | None) -> str:
    path = urlparse(url).path.lower()
    for ext in (".pdf", ".json", ".csv", ".xlsx", ".zip", ".xml", ".txt", ".html", ".htm"):
        if path.endswith(ext):
            return ext
    if content_type:
        ct = content_type.split(";")[0].strip()
        if ct == "application/pdf":
            return ".pdf"
        if ct in ("text/html", "application/xhtml+xml"):
            return ".html"
        ext = mimetypes.guess_extension(ct)
        if ext:
            return ext
    return ".bin"


class RawStore:
    """Content-addressed raw store: data/raw/<agency>/<sha[:2]>/<sha><ext>.
    Identical bytes are stored once; versions are tracked in the database."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, agency: str, content: bytes, ext: str) -> tuple[str, Path]:
        sha = hashlib.sha256(content).hexdigest()
        path = self.root / "raw" / agency / sha[:2] / f"{sha}{ext}"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(content)
            tmp.replace(path)
        return sha, path
