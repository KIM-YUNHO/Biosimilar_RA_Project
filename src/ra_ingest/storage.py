from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .objstore import ObjectStore


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
    """Content-addressed raw store: key raw/<agency>/<sha[:2]>/<sha><ext> in an ObjectStore.
    Identical bytes are stored once; versions are tracked in the database (by key)."""

    def __init__(self, store: "ObjectStore | str | Path"):
        from .objstore import ObjectStore, open_store

        self.store = store if isinstance(store, ObjectStore) else open_store(str(store))

    @staticmethod
    def key(agency: str, sha: str, ext: str) -> str:
        return f"raw/{agency}/{sha[:2]}/{sha}{ext}"

    def put(self, agency: str, content: bytes, ext: str) -> tuple[str, str]:
        """Returns (sha256, key)."""
        sha = fingerprint(content, ext)
        key = self.key(agency, sha, ext)
        if not self.store.exists(key):
            self.store.put_bytes(key, content)
        return sha, key
