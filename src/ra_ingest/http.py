from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

# FDA (Akamai) answers non-browser user agents with an "abuse detection" redirect, so a
# browser-like agent is the default. Requests stay polite: one host at a time with a delay.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36 ra-ingest/0.1")

# A blocked host is not asked again for this long (asking again only prolongs a block).
BLOCK_COOLDOWN = 600.0
_BLOCK_MARKERS = ("apology", "abuse-detection")


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: int | None = None, blocked: bool = False):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason
        self.status = status
        self.blocked = blocked      # the host answered with a bot-detection page, not the document


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


def is_block_url(url: str) -> bool:
    """A redirect target that is a bot-detection page (FDA: /apology_objects/abuse-detection-apology.html)."""
    path = urlparse(url).path.lower()
    return any(m in path for m in _BLOCK_MARKERS)


class Fetcher:
    """GET with retries, per-host politeness delay and a size cap. Proxy and CA
    settings come from the environment (HTTPS_PROXY, REQUESTS_CA_BUNDLE).

    - A bot-detection page is recognised whatever its status code (FDA serves it with 404)
      and reported as `FetchError.blocked`; the host is then skipped for BLOCK_COOLDOWN.
    - `cache=True` keeps the response of a catalog file for the life of the fetcher, so one
      run that needs the same file twice downloads it once."""

    def __init__(self, timeout: float = 90, retries: int = 4, host_delay: float = 1.0,
                 max_bytes: int = 200 * 1024 * 1024, user_agent: str = USER_AGENT,
                 block_cooldown: float = BLOCK_COOLDOWN):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.retries = retries
        self.host_delay = host_delay
        self.max_bytes = max_bytes
        self.block_cooldown = block_cooldown
        self._last_hit: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self._cache: dict[tuple, FetchResult] = {}

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < self.host_delay:
                time.sleep(self.host_delay - gap)
        self._last_hit[host] = time.monotonic()

    def is_blocked(self, url: str) -> bool:
        return self._blocked_until.get(urlparse(url).netloc, 0.0) > time.monotonic()

    def blocked_hosts(self) -> list[str]:
        """Hosts that answered with a bot-detection page and are still being left alone."""
        now = time.monotonic()
        return sorted(h for h, until in self._blocked_until.items() if until > now)

    def clear_cache(self) -> None:
        """Forget cached catalog files. A long-lived fetcher must not serve one run's catalog to the next."""
        self._cache.clear()

    def _mark_blocked(self, url: str) -> None:
        self._blocked_until[urlparse(url).netloc] = time.monotonic() + self.block_cooldown

    def get(self, url: str, params: dict | None = None, cache: bool = False) -> FetchResult:
        ckey = (url, tuple(sorted((params or {}).items())))
        if cache and ckey in self._cache:
            return self._cache[ckey]
        if self.is_blocked(url):
            raise FetchError(url, "host is blocking this client (bot detection); skipped", 429, blocked=True)
        last_err: FetchError | None = None
        for attempt in range(self.retries):
            self._wait(url)
            try:
                r = self.session.get(url, params=params, timeout=(20, self.timeout), stream=True)
            except requests.RequestException as e:
                last_err = FetchError(url, f"{type(e).__name__}: {e}")
            else:
                if is_block_url(r.url):
                    self._mark_blocked(url)
                    raise FetchError(url, "blocked by bot detection (redirected to an apology page)",
                                     429, blocked=True)
                if r.status_code == 200:
                    chunks, size = [], 0
                    try:
                        for chunk in r.iter_content(1 << 16):
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise FetchError(url, f"size exceeds {self.max_bytes} bytes")
                            chunks.append(chunk)
                    except requests.RequestException as e:  # connection dropped mid-body: retry
                        last_err = FetchError(url, f"{type(e).__name__}: {e}")
                    else:
                        res = FetchResult(
                            url=r.url, status=200, content=b"".join(chunks),
                            content_type=r.headers.get("Content-Type"),
                            last_modified=r.headers.get("Last-Modified"),
                            etag=r.headers.get("ETag"),
                        )
                        if cache:
                            self._cache[ckey] = res
                        return res
                else:
                    last_err = FetchError(url, f"HTTP {r.status_code}", r.status_code)
                    if r.status_code in (400, 401, 403, 404, 410):
                        break  # not transient
            if attempt + 1 < self.retries:
                time.sleep(min(2 ** (attempt + 1), 20))
        assert last_err is not None
        raise last_err
