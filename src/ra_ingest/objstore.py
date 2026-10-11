"""Object store for raw documents and parse outputs, addressed by keys, not file paths.

The database stores keys such as ``raw/fda/ab/<sha256>.pdf`` and ``parsed/<sha256>.json``.
Where the bytes live is a deployment choice:

  LocalStore  a directory (default ``data/``; a shared volume works the same way)
  S3Store     an S3-compatible bucket (AWS S3, MinIO, ...), ``s3://bucket/prefix``

Raw objects are content-addressed (the key carries the sha256): they never change once
written, so local copies of remote raw files never go stale. Parse outputs keep one key per
document version and are rewritten by ``ra-prep refix``; read them with ``read_text`` (always
from the store), not ``local_path``.

Rows written before keys were introduced hold absolute paths; ``resolve`` still accepts
them (``ra-ingest migrate-paths`` rewrites them to keys).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath

class ObjectStore:
    """Interface. Keys are relative POSIX paths ("raw/ema/0f/0f3c....pdf")."""

    url: str

    def put_bytes(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def local_path(self, key: str) -> str:
        """A readable local file with the object's bytes (PDF libraries need a path)."""
        raise NotImplementedError

    def resolve(self, key_or_path: str) -> str:
        """Local path for a stored key, or for a legacy absolute path written before keys."""
        if os.path.isabs(key_or_path) and os.path.exists(key_or_path):
            return key_or_path
        return self.local_path(key_or_path)

    def read_bytes(self, key_or_path: str) -> bytes:
        if os.path.isabs(key_or_path) and os.path.exists(key_or_path):
            return Path(key_or_path).read_bytes()
        return self.get_bytes(key_or_path)

    def read_text(self, key_or_path: str) -> str:
        return self.read_bytes(key_or_path).decode("utf-8")

    def write_text(self, key_or_path: str, text: str) -> None:
        if os.path.isabs(key_or_path):  # legacy row: keep writing where it points
            _atomic_write(Path(key_or_path), text.encode("utf-8"))
        else:
            self.put_bytes(key_or_path, text.encode("utf-8"))

    def key_for(self, path: str) -> str | None:
        """Key of a legacy absolute path inside this store's root (None if outside)."""
        return None


def _check_key(key: str) -> str:
    p = PurePosixPath(key)
    if p.is_absolute() or ".." in p.parts or not key:
        raise ValueError(f"bad object key: {key!r}")
    return key


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


class LocalStore(ObjectStore):
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.url = str(self.root)

    def path(self, key: str) -> Path:
        return self.root / _check_key(key)

    def put_bytes(self, key: str, data: bytes) -> None:
        _atomic_write(self.path(key), data)

    def get_bytes(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self.path(key).exists()

    def local_path(self, key: str) -> str:
        return str(self.path(key))

    def key_for(self, path: str) -> str | None:
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None


class S3Store(ObjectStore):
    """S3-compatible bucket. Endpoint and credentials come from the usual AWS settings
    (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL for MinIO and the like).
    Objects read through local_path are cached under `cache_dir`; use it for raw files only
    (content-addressed, so the cache needs no invalidation)."""

    def __init__(self, url: str, cache_dir: str | Path | None = None, client=None):
        rest = url[len("s3://"):]
        self.bucket, _, prefix = rest.partition("/")
        self.prefix = prefix.strip("/")
        self.url = url
        if client is None:
            import boto3  # optional dependency: pip install ".[s3]"

            client = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None)
        self.client = client
        self.cache = Path(cache_dir or os.environ.get("RA_STORE_CACHE")
                          or Path(tempfile.gettempdir()) / "ra-store-cache")

    def _k(self, key: str) -> str:
        key = _check_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_bytes(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._k(key), Body=data)
        cached = self.cache / key
        if cached.exists():
            _atomic_write(cached, data)

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def local_path(self, key: str) -> str:
        path = self.cache / _check_key(key)
        if not path.exists():
            _atomic_write(path, self.get_bytes(key))
        return str(path)


def open_store(url: str | None = None) -> ObjectStore:
    """``s3://bucket/prefix`` -> S3Store; anything else is a local directory."""
    from .settings import default_store

    url = url or default_store()
    if url.startswith("s3://"):
        return S3Store(url)
    if url.startswith("file://"):
        url = url[len("file://"):]
    return LocalStore(url)
