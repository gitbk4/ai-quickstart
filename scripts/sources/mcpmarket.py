"""mcpmarket.com search scraper (stdlib only).

Politeness rules baked in:

  - Identifying ``User-Agent`` on every request.
  - Consults ``robots.txt`` once per session (in-memory cache) before any
    scrape; if the search path is disallowed we return an empty result set
    plus a warning rather than scraping anyway.
  - Throttles repeat requests in the same session to ~1 req/sec via
    ``time.sleep`` between consecutive ``urlopen`` calls.
  - Caches successful searches under namespace ``mcpmarket`` with 24h TTL.

The HTML parser is intentionally permissive: if the selectors don't match
(mcpmarket changed their markup, common per the Accepted Risks section of
PLAN.md), we return ``results=[]`` plus a warning rather than crashing.

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import List, Optional, Tuple
from urllib.robotparser import RobotFileParser

from . import cache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_NAMESPACE = "mcpmarket"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

USER_AGENT = "ai-quickstart/0.1.0 (+https://github.com/ai-quickstart/ai-quickstart)"
BASE_URL = "https://mcpmarket.com"
SEARCH_PATH = "/search"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
THROTTLE_SECONDS = 1.0
REQUEST_TIMEOUT = 10  # seconds
SOURCE_TIER = "mcpmarket-scrape"

# In-memory per-session caches. Reset on process exit. Tests poke these
# directly to simulate fresh sessions.
_robots_decision_cache: dict = {}  # path -> bool (allowed?)
_last_request_at: List[float] = [0.0]  # mutable singleton so tests can reset it


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search(query: str, limit: int = 20, force_refresh: bool = False) -> dict:
    """Search mcpmarket.com for ``query``.

    Returns a dict shaped::

        {
          "results":  [ {"title", "url", "description", "raw_html_anchor"}, ... ],
          "source":   "mcpmarket",
          "warnings": [ ... ],
          "source_tier": "mcpmarket-scrape" | "mcpmarket-cache",
        }

    Never raises. Failure modes (network error, parse failure, robots
    disallow) all yield empty results plus a warning.
    """
    query = (query or "").strip()
    if not query:
        return _empty(["empty query"])

    cache_key = f"q={query}|n={limit}"
    if not force_refresh:
        cached = cache.get(CACHE_NAMESPACE, cache_key, ttl_seconds=CACHE_TTL_SECONDS)
        if cached is not None:
            out = dict(cached)
            out["source_tier"] = "mcpmarket-cache"
            return out

    target_url = _build_search_url(query)
    target_path = urllib.parse.urlsplit(target_url).path or "/"

    if not _robots_allows(target_path):
        return _empty([f"robots.txt disallows {target_path}; not scraping"])

    fetched = _http_get(target_url)
    if "error" in fetched:
        return _empty([fetched["error"]])

    try:
        results = _parse_search_html(fetched["body"], limit)
    except Exception as exc:  # noqa: BLE001 - parser must never crash callers
        return _empty([
            f"mcpmarket parser raised {type(exc).__name__}; site may have changed"
        ])

    if not results:
        # Couldn't extract anything but the page loaded. Treat as a soft
        # failure: empty + warn so suggest.py keeps moving.
        out = {
            "results": [],
            "source": "mcpmarket",
            "warnings": [
                "mcpmarket parser couldn't extract listings; site may have changed"
            ],
            "source_tier": SOURCE_TIER,
        }
        # Cache the empty result so we don't hammer the site after a layout
        # change. 24h TTL bounds the misery if upstream restores their HTML.
        try:
            cache.set(CACHE_NAMESPACE, cache_key, out)
        except Exception:
            pass
        return out

    out = {
        "results": results,
        "source": "mcpmarket",
        "warnings": [],
        "source_tier": SOURCE_TIER,
    }
    try:
        cache.set(CACHE_NAMESPACE, cache_key, out)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def _robots_allows(path: str) -> bool:
    """Return whether ``path`` is allowed by mcpmarket's robots.txt.

    Decision is cached in-memory per-process. On any error fetching robots
    we conservatively allow the request (mirrors what most polite scrapers
    do) — robots.txt should not be load-bearing for correctness.
    """
    if path in _robots_decision_cache:
        return _robots_decision_cache[path]

    rp = RobotFileParser()
    rp.set_url(ROBOTS_URL)
    try:
        # Fetch via our identifying UA. RobotFileParser.read() uses urllib but
        # doesn't let us inject headers easily; we replicate the fetch here.
        req = urllib.request.Request(ROBOTS_URL, headers={"User-Agent": USER_AGENT})
        _throttle()
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        rp.parse(raw.splitlines())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        # Treat fetch failure as "allowed" — we tried.
        _robots_decision_cache[path] = True
        return True

    allowed = rp.can_fetch(USER_AGENT, BASE_URL + path)
    _robots_decision_cache[path] = allowed
    return allowed


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _build_search_url(query: str) -> str:
    qs = urllib.parse.urlencode({"q": query})
    return f"{BASE_URL}{SEARCH_PATH}?{qs}"


def _http_get(url: str) -> dict:
    """Fetch ``url`` with the identifying UA. Returns ``{"body": str}`` or
    ``{"error": str}``."""
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        return {"error": f"mcpmarket HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"error": f"network error reaching mcpmarket: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"unexpected error fetching mcpmarket: {exc}"}
    try:
        body = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - decode shouldn't fail with errors=replace
        return {"error": "could not decode mcpmarket response body"}
    return {"body": body}


def _throttle() -> None:
    """Sleep just long enough to keep us under 1 req/sec in this session."""
    now = time.monotonic()
    elapsed = now - _last_request_at[0]
    if elapsed < THROTTLE_SECONDS and _last_request_at[0] > 0:
        time.sleep(THROTTLE_SECONDS - elapsed)
    _last_request_at[0] = time.monotonic()


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


class _ListingParser(HTMLParser):
    """Best-effort extractor for mcpmarket search listings.

    Strategy: collect every ``<a>`` whose ``href`` looks like a server detail
    page (``/server/...`` or ``/mcp/...``) and capture the visible anchor
    text as the title. We greedily group by the closest preceding ``<h2>`` /
    ``<h3>`` text and the next paragraph for the description, but if the
    DOM doesn't yield those we still emit the link.

    This is intentionally loose. mcpmarket's HTML is undocumented and
    changes; the test suite only requires that *something* extractable like
    a server-detail link produces a result, and that nothing extractable
    yields an empty list (not a crash).
    """

    LINK_PREFIXES = ("/server/", "/mcp/", "/servers/", "/listing/")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._results: List[dict] = []
        # State for the in-flight anchor.
        self._in_anchor = False
        self._cur_href: Optional[str] = None
        self._cur_title_chunks: List[str] = []
        self._cur_anchor_html: List[str] = []
        # Track the nearest preceding heading / paragraph for context.
        self._last_heading: str = ""
        self._last_paragraph: str = ""
        self._in_heading = False
        self._heading_chunks: List[str] = []
        self._in_paragraph = False
        self._paragraph_chunks: List[str] = []

    # -- handlers ----------------------------------------------------------

    def handle_starttag(self, tag: str, attrs):
        if tag == "a":
            href = self._attr(attrs, "href")
            if href and any(href.startswith(p) for p in self.LINK_PREFIXES):
                self._in_anchor = True
                self._cur_href = href
                self._cur_title_chunks = []
                self._cur_anchor_html = [self._reconstruct_starttag(tag, attrs)]
        elif tag in ("h1", "h2", "h3", "h4"):
            self._in_heading = True
            self._heading_chunks = []
        elif tag == "p":
            self._in_paragraph = True
            self._paragraph_chunks = []
        if self._in_anchor and tag != "a":
            self._cur_anchor_html.append(self._reconstruct_starttag(tag, attrs))

    def handle_endtag(self, tag: str):
        if self._in_anchor and tag != "a":
            self._cur_anchor_html.append(f"</{tag}>")
        if tag == "a" and self._in_anchor:
            self._cur_anchor_html.append("</a>")
            title = " ".join(
                chunk.strip() for chunk in self._cur_title_chunks if chunk.strip()
            ).strip()
            href = self._cur_href or ""
            if href and (title or self._last_heading):
                self._results.append({
                    "title": title or self._last_heading,
                    "url": _absolutize(href),
                    "description": self._last_paragraph,
                    "raw_html_anchor": "".join(self._cur_anchor_html),
                })
            self._in_anchor = False
            self._cur_href = None
            self._cur_title_chunks = []
            self._cur_anchor_html = []
        elif tag in ("h1", "h2", "h3", "h4") and self._in_heading:
            self._last_heading = " ".join(
                c.strip() for c in self._heading_chunks if c.strip()
            ).strip()
            self._in_heading = False
        elif tag == "p" and self._in_paragraph:
            self._last_paragraph = " ".join(
                c.strip() for c in self._paragraph_chunks if c.strip()
            ).strip()
            self._in_paragraph = False

    def handle_data(self, data: str):
        if self._in_anchor:
            self._cur_title_chunks.append(data)
            self._cur_anchor_html.append(data)
        if self._in_heading:
            self._heading_chunks.append(data)
        if self._in_paragraph:
            self._paragraph_chunks.append(data)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _attr(attrs, name: str) -> Optional[str]:
        for k, v in attrs:
            if k == name:
                return v
        return None

    @staticmethod
    def _reconstruct_starttag(tag: str, attrs: List[Tuple[str, Optional[str]]]) -> str:
        parts = [tag]
        for k, v in attrs:
            if v is None:
                parts.append(k)
            else:
                # html.parser doesn't escape; this is a coarse echo only
                # used for the raw_html_anchor field, not for re-rendering.
                parts.append(f'{k}="{v}"')
        return "<" + " ".join(parts) + ">"

    # -- result accessor ---------------------------------------------------

    @property
    def results(self) -> List[dict]:
        return self._results


def _absolutize(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("/"):
        return BASE_URL + href
    return f"{BASE_URL}/{href.lstrip('/')}"


def _parse_search_html(body: str, limit: int) -> List[dict]:
    parser = _ListingParser()
    parser.feed(body)
    parser.close()
    return parser.results[:limit]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty(warnings: List[str]) -> dict:
    return {
        "results": [],
        "source": "mcpmarket",
        "warnings": list(warnings),
        "source_tier": SOURCE_TIER,
    }


def _reset_session_state_for_tests() -> None:
    """Reset in-memory throttle + robots cache. Used by tests."""
    _robots_decision_cache.clear()
    _last_request_at[0] = 0.0
