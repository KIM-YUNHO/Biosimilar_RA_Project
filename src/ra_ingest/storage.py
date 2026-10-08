from __future__ import annotations

import hashlib
import mimetypes
import re
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


# Tokens that change on every request without the document changing (Cloudflare email
# obfuscation, Drupal form ids, nonces). Seen on dhpp.hpfb-dgpsa.ca SBD/RDS pages.
_VOLATILE = [
    re.compile(rb'/cdn-cgi/l/email-protection#[0-9a-f]+'),
    re.compile(rb'data-cfemail="[0-9a-f]+"'),
    re.compile(rb'name="form_build_id" value="[^"]*"'),
    re.compile(rb'(nonce|data-drupal-selector-hash)="[^"]*"'),
    re.compile(rb'data-drupal-selector="form-[A-Za-z0-9_-]+"'),
    re.compile(rb'value="form-[A-Za-z0-9_-]+"'),
]
_MAIN = re.compile(rb"<main\b.*?</main>", re.S | re.I)


def fingerprint(content: bytes, ext: str) -> str:
    """Version identity. HTML is hashed with volatile tokens removed; other files as-is."""
    if ext in (".html", ".htm"):
        # only the page body matters (headers/footers/forms carry per-request tokens)
        m = _MAIN.search(content)
        if m:
            content = m.group(0)
        for pat in _VOLATILE:
            content = pat.sub(b"", content)
    return hashlib.sha256(content).hexdigest()


class RawStore:
    """Content-addressed raw store: data/raw/<agency>/<sha[:2]>/<sha><ext>.
    Identical bytes are stored once; versions are tracked in the database."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, agency: str, content: bytes, ext: str) -> tuple[str, Path]:
        sha = fingerprint(content, ext)
        path = self.root / "raw" / agency / sha[:2] / f"{sha}{ext}"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(content)
            tmp.replace(path)
        return sha, path
