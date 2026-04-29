"""Tests for scripts/sources/mcpmarket.py.

Covers cache hit (no HTTP), cache miss → scrape, robots.txt allow/disallow,
HTML parse failure (empty results + warning), User-Agent header set on
requests, throttle delay between consecutive requests in the same session,
and confirmation that malformed HTML doesn't crash. urllib is patched —
the suite never touches the network.
"""

from __future__ import annotations

import urllib.error
from unittest import mock

import pytest

from ._loader import load

mcpmarket = load("mcpmarket")
cache = load("cache")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(tmp_path))
    # Reset in-memory session state so each test starts fresh.
    mcpmarket._reset_session_state_for_tests()
    yield tmp_path


def _http_response(body: str):
    resp = mock.MagicMock()
    resp.read.return_value = body.encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


# Minimal HTML mimicking mcpmarket-like markup. The parser groups headings
# with anchors that look like server-detail links.
SAMPLE_HTML = """
<html><body>
  <div class="listing">
    <h2>Filesystem</h2>
    <p>Read and write local files.</p>
    <a href="/server/filesystem">Filesystem</a>
  </div>
  <div class="listing">
    <h2>Git</h2>
    <p>Inspect git repos.</p>
    <a href="/server/git">Git</a>
  </div>
</body></html>
"""

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_DISALLOW_SEARCH = "User-agent: *\nDisallow: /search\n"
HTML_NO_LISTINGS = "<html><body><h1>oops</h1><p>nothing here</p></body></html>"


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_cache_hit_skips_network():
    """If a result is already cached, no urlopen call is made."""
    cached_payload = {
        "results": [{"title": "x", "url": "https://x", "description": "", "raw_html_anchor": "<a>x</a>"}],
        "source": "mcpmarket",
        "warnings": [],
        "source_tier": "mcpmarket-scrape",
    }
    cache.set("mcpmarket", "q=foo|n=20", cached_payload)
    with mock.patch.object(mcpmarket.urllib.request, "urlopen") as urlopen:
        out = mcpmarket.search("foo")
    assert urlopen.call_count == 0
    assert out["results"] == cached_payload["results"]
    assert out["source_tier"] == "mcpmarket-cache"


def test_cache_miss_triggers_scrape():
    responses = [_http_response(ROBOTS_ALLOW), _http_response(SAMPLE_HTML)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ) as urlopen, mock.patch.object(mcpmarket.time, "sleep"):
        out = mcpmarket.search("foo")
    assert urlopen.call_count == 2  # robots + search
    assert out["source_tier"] == "mcpmarket-scrape"
    assert out["warnings"] == []
    assert len(out["results"]) == 2
    titles = [r["title"] for r in out["results"]]
    assert "Filesystem" in titles and "Git" in titles


def test_force_refresh_bypasses_cache():
    cached = {
        "results": [{"title": "stale", "url": "https://stale", "description": "", "raw_html_anchor": ""}],
        "source": "mcpmarket",
        "warnings": [],
        "source_tier": "mcpmarket-scrape",
    }
    cache.set("mcpmarket", "q=foo|n=20", cached)
    responses = [_http_response(ROBOTS_ALLOW), _http_response(SAMPLE_HTML)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ), mock.patch.object(mcpmarket.time, "sleep"):
        out = mcpmarket.search("foo", force_refresh=True)
    assert any(r["title"] != "stale" for r in out["results"])


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


def test_robots_disallow_returns_empty_with_warning():
    """When robots.txt disallows the search path, we don't scrape."""
    responses = [_http_response(ROBOTS_DISALLOW_SEARCH)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ) as urlopen, mock.patch.object(mcpmarket.time, "sleep"):
        out = mcpmarket.search("foo")
    # Only the robots fetch happened; no search request.
    assert urlopen.call_count == 1
    assert out["results"] == []
    assert any("robots" in w.lower() for w in out["warnings"])


def test_robots_decision_cached_in_memory_per_session():
    """Multiple searches in one session fetch robots.txt at most once."""
    # Three responses queued: robots, search1, search2. If robots was fetched
    # twice, side_effect would raise StopIteration.
    responses = [
        _http_response(ROBOTS_ALLOW),
        _http_response(SAMPLE_HTML),
        _http_response(SAMPLE_HTML),
    ]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ) as urlopen, mock.patch.object(mcpmarket.time, "sleep"):
        mcpmarket.search("foo")
        mcpmarket.search("bar")
    # 1 robots + 2 searches = 3 calls.
    assert urlopen.call_count == 3
    urls = [
        (call.args[0].full_url if hasattr(call.args[0], "full_url") else call.args[0])
        for call in urlopen.call_args_list
    ]
    # Exactly one robots.txt fetch.
    assert sum(1 for u in urls if "robots.txt" in str(u)) == 1


def test_robots_fetch_failure_treated_as_allow():
    """If robots.txt is unreachable, we conservatively allow the request."""
    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "robots.txt" in url:
            raise urllib.error.URLError("robots boom")
        return _http_response(SAMPLE_HTML)

    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=fake_urlopen
    ), mock.patch.object(mcpmarket.time, "sleep"):
        out = mcpmarket.search("foo")
    # Search proceeded despite robots fetch failure.
    assert len(out["results"]) >= 1


# ---------------------------------------------------------------------------
# Parse failure handling
# ---------------------------------------------------------------------------


def test_html_with_no_listings_returns_empty_with_warning():
    responses = [_http_response(ROBOTS_ALLOW), _http_response(HTML_NO_LISTINGS)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ), mock.patch.object(mcpmarket.time, "sleep"):
        out = mcpmarket.search("foo")
    assert out["results"] == []
    assert any("couldn't extract" in w.lower() or "site may have changed" in w.lower()
               for w in out["warnings"])


def test_malformed_html_does_not_crash():
    """Pathological HTML (mismatched tags, incomplete) must not raise."""
    bad = "<html><body><a href='/server/x'>x<a href='/server/y'>y</body>"
    responses = [_http_response(ROBOTS_ALLOW), _http_response(bad)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ), mock.patch.object(mcpmarket.time, "sleep"):
        out = mcpmarket.search("foo")
    # Whatever the result, we must not crash and must conform to the schema.
    assert "results" in out
    assert "warnings" in out
    assert "source" in out


def test_parser_exception_caught_and_yields_warning():
    """Worst-case: feed() itself raises. The caller still gets an empty result."""
    responses = [_http_response(ROBOTS_ALLOW), _http_response(SAMPLE_HTML)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ), mock.patch.object(mcpmarket.time, "sleep"), mock.patch.object(
        mcpmarket, "_parse_search_html", side_effect=RuntimeError("synthetic")
    ):
        out = mcpmarket.search("foo")
    assert out["results"] == []
    assert any("parser raised" in w.lower() or "site may have changed" in w.lower()
               for w in out["warnings"])


def test_network_error_returns_empty_with_warning():
    """A urllib URLError on the search request should not crash."""
    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "robots.txt" in url:
            return _http_response(ROBOTS_ALLOW)
        raise urllib.error.URLError("dns boom")

    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=fake_urlopen
    ), mock.patch.object(mcpmarket.time, "sleep"):
        out = mcpmarket.search("foo")
    assert out["results"] == []
    assert any("network" in w.lower() or "boom" in w.lower() for w in out["warnings"])


# ---------------------------------------------------------------------------
# User-Agent header
# ---------------------------------------------------------------------------


def test_user_agent_set_on_search_request():
    responses = [_http_response(ROBOTS_ALLOW), _http_response(SAMPLE_HTML)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ) as urlopen, mock.patch.object(mcpmarket.time, "sleep"):
        mcpmarket.search("foo")
    # Find the search request (not the robots one) and inspect its UA.
    for call in urlopen.call_args_list:
        req = call.args[0]
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "robots.txt" in url:
            continue
        ua = req.headers.get("User-agent")  # urllib normalizes to title-case
        assert ua is not None
        assert "ai-quickstart" in ua.lower()
        return
    pytest.fail("never inspected the search request")


def test_user_agent_set_on_robots_request():
    responses = [_http_response(ROBOTS_ALLOW), _http_response(SAMPLE_HTML)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ) as urlopen, mock.patch.object(mcpmarket.time, "sleep"):
        mcpmarket.search("foo")
    robots_call = urlopen.call_args_list[0]
    req = robots_call.args[0]
    ua = req.headers.get("User-agent")
    assert ua and "ai-quickstart" in ua.lower()


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


def test_throttle_sleeps_between_consecutive_requests_in_one_session():
    """If two requests fire back-to-back, we must sleep ~1s between them."""
    responses = [
        _http_response(ROBOTS_ALLOW),
        _http_response(SAMPLE_HTML),
        _http_response(SAMPLE_HTML),
    ]
    # Each ``_throttle()`` call invokes ``time.monotonic()`` exactly twice
    # (once to compute elapsed, once to record the new last-request stamp).
    # We feed monotonic values such that the second and third throttle calls
    # see a recent prior request and therefore sleep.
    monotonic_values = iter([
        100.0, 100.0,  # robots: first call; last_request_at is still 0 → no sleep
        100.1, 101.1,  # search1: elapsed=0.1, last=100.0 (>0) → sleep ~0.9
        101.2, 102.2,  # search2: elapsed=0.1, last=101.1 (>0) → sleep ~0.9
    ])
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ), mock.patch.object(mcpmarket.time, "sleep") as sleep_mock, \
         mock.patch.object(
             mcpmarket.time, "monotonic", side_effect=lambda: next(monotonic_values)
         ):
        mcpmarket.search("foo")
        mcpmarket.search("bar")
    # At least two positive sleeps happened (one per follow-up request).
    sleep_durations = [c.args[0] for c in sleep_mock.call_args_list if c.args]
    assert sum(1 for d in sleep_durations if d > 0) >= 2, (
        f"expected ≥2 positive sleeps between consecutive requests, "
        f"got {sleep_durations}"
    )


def test_throttle_does_not_sleep_on_very_first_request():
    """A fresh session's first ``_throttle()`` call must not sleep —
    ``last_request_at`` starts at 0 and the throttle branch is gated on it.

    We exercise this by calling ``_throttle`` directly. Searching would also
    trigger a second throttle (for the search request after the robots
    fetch), which masks the property under test.
    """
    with mock.patch.object(mcpmarket.time, "sleep") as sleep_mock, \
         mock.patch.object(mcpmarket.time, "monotonic", return_value=42.0):
        mcpmarket._reset_session_state_for_tests()
        mcpmarket._throttle()
    assert sleep_mock.call_count == 0


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


def test_result_schema_has_required_fields():
    responses = [_http_response(ROBOTS_ALLOW), _http_response(SAMPLE_HTML)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ), mock.patch.object(mcpmarket.time, "sleep"):
        out = mcpmarket.search("foo")
    assert out["results"]
    for r in out["results"]:
        assert "title" in r
        assert "url" in r
        assert "description" in r
        assert "raw_html_anchor" in r
        # URL absolutized.
        assert r["url"].startswith("https://mcpmarket.com/") or r["url"].startswith("http")


def test_empty_query_returns_empty_with_warning():
    out = mcpmarket.search("")
    assert out["results"] == []
    assert out["warnings"]


def test_limit_caps_results():
    big_html = (
        "<html><body>"
        + "".join(
            f"<h2>Server {i}</h2><p>p</p><a href='/server/s{i}'>s{i}</a>"
            for i in range(50)
        )
        + "</body></html>"
    )
    responses = [_http_response(ROBOTS_ALLOW), _http_response(big_html)]
    with mock.patch.object(
        mcpmarket.urllib.request, "urlopen", side_effect=responses
    ), mock.patch.object(mcpmarket.time, "sleep"):
        out = mcpmarket.search("foo", limit=5)
    assert len(out["results"]) == 5
