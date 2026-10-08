from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

# FDA (Akamai) answers non-browser user agents with an "abuse detection" redirect, so a
# browser-like agent is the default. Requests stay polite: one host at a time with a delay.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36 ra-ingest/0.1")


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason
        self.status = status


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    content_type: str | None
    last_modified: str | None
    etag: str | None

    def json(self):
        import json
        return json.loads(self.content.decode("utf-8-sig"))

    def text(self, encoding: str = "utf-8") -> str:
        try:
            return self.content.decode(encoding)
        except UnicodeDecodeError:
            return self.content.decode("latin-1")


class Fetcher:
    """GET with retries, per-host politeness delay and a size cap. Proxy and CA
    settings come from the environment (HTTPS_PROXY, REQUESTS_CA_BUNDLE)."""

    def __init__(self, timeout: float = 60, retries: int = 3, host_delay: float = 1.0,
                 max_bytes: int = 200 * 1024 * 1024, user_agent: str = USER_AGENT):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.retries = retries
        self.host_delay = host_delay
        self.max_bytes = max_bytes
        self._last_hit: dict[str, float] = {}

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.host_delay:
                time.sleep(self.host_delay - gap)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None) -> FetchResult:
        last_err: FetchError | None = None
        for attempt in range(self.retries):
            self._wait(url)
            try:
                r = self.session.get(url, params=params, timeout=self.timeout, stream=True)
            except requests.RequestException as e:
                last_err = FetchError(url, f"{type(e).__name__}: {e}")
            else:
                if r.status_code == 200 and "apology" in r.url:
                    raise FetchError(url, "bot-detection redirect (apology page)", 429)
                if r.status_code == 200:
                    chunks, size = [], 0
                    for chunk in r.iter_content(1 << 16):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise FetchError(url, f"size exceeds {self.max_bytes} bytes")
                        chunks.append(chunk)
                    return FetchResult(
                        url=r.url, status=200, content=b"".join(chunks),
                        content_type=r.headers.get("Content-Type"),
                        last_modified=r.headers.get("Last-Modified"),
                        etag=r.headers.get("ETag"),
                    )
                last_err = FetchError(url, f"HTTP {r.status_code}", r.status_code)
                if r.status_code in (400, 401, 403, 404, 410):
                    break  # not transient
            if attempt + 1 < self.retries:
                time.sleep(2 ** attempt)
        assert last_err is not None
        raise last_err
