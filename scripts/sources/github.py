"""GitHub repo metadata fetcher with 3-tier auth fallback.

Tiers (in order):
  1. ``gh api repos/{owner}/{repo}`` via the local ``gh`` CLI (subprocess).
  2. Authenticated REST call to ``https://api.github.com/repos/{owner}/{repo}``
     using ``GITHUB_TOKEN`` from env.
  3. Unauthenticated REST call to the same endpoint.

Successful results are cached under namespace ``github`` (key
``{owner}/{repo}``) with a 6-hour TTL via :mod:`sources.cache`. The
unauthenticated tier benefits most from the cache (60 req/h global limit) but
all tiers consult and write the cache so repeated lookups within the TTL
window stay snappy.

Failure modes (401 / 403 / 404 / network / timeout / unparseable JSON) never
crash. Instead, ``fetch_repo`` returns a dict containing an ``error`` field
and an ``error_kind`` discriminator. The caller is expected to surface this
to the user and continue with degraded suggestions.

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Optional

from . import cache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_NAMESPACE = "github"
CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
LOW_QUALITY_STAR_THRESHOLD = 100
USER_AGENT = "ai-quickstart/0.1.0 (+https://github.com/ai-quickstart/ai-quickstart)"
REQUEST_TIMEOUT = 10  # seconds
GH_CLI_TIMEOUT = 15  # seconds — gh cli can be slow on cold start

API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_repo(owner: str, repo: str, force_refresh: bool = False) -> dict:
    """Fetch metadata for ``owner/repo``, trying each tier in order.

    Returns a dict with keys:
      - ``stars`` (int)
      - ``forks`` (int)
      - ``contributors`` (int or None — populated best-effort)
      - ``last_commit_iso`` (str ISO-8601 timestamp or None)
      - ``watchers`` (int)
      - ``warning_low_quality`` (bool — True if stars < 100)
      - ``source_tier`` (one of ``"gh-cli"``, ``"github-token"``,
        ``"unauth"``, ``"cache"``)

    On any error:
      - ``error`` (str — human-readable message)
      - ``error_kind`` (one of ``"auth"``, ``"not_found"``, ``"rate_limit"``,
        ``"network"``, ``"parse"``, ``"unknown"``)
    """
    cache_key = f"{owner}/{repo}"

    if not force_refresh:
        cached = cache.get(CACHE_NAMESPACE, cache_key, ttl_seconds=CACHE_TTL_SECONDS)
        if cached is not None:
            # Mark this hit as coming from cache while preserving original tier.
            out = dict(cached)
            out["source_tier"] = "cache"
            return out

    # Try each tier in order. Each returns either a normalized dict or an
    # error dict. We stop at the first non-error result.
    last_error: Optional[dict] = None

    for tier_fn, tier_name in (
        (_try_gh_cli, "gh-cli"),
        (_try_github_token, "github-token"),
        (_try_unauth, "unauth"),
    ):
        result = tier_fn(owner, repo)
        if result is None:
            # Tier was not applicable (e.g. gh cli not installed, token unset).
            continue
        if "error" in result:
            last_error = result
            # 404 short-circuits — the repo definitively doesn't exist.
            if result.get("error_kind") == "not_found":
                return result
            continue
        # Success.
        normalized = _normalize_repo(result, tier_name)
        try:
            cache.set(CACHE_NAMESPACE, cache_key, normalized)
        except Exception:
            # Caching is best-effort; never let a cache write break the call.
            pass
        return normalized

    if last_error is not None:
        return last_error
    return {
        "error": "no GitHub source tier was usable (gh CLI, GITHUB_TOKEN, and "
                 "unauthenticated request all unavailable)",
        "error_kind": "unknown",
    }


# ---------------------------------------------------------------------------
# Tier 1: gh CLI
# ---------------------------------------------------------------------------


def _try_gh_cli(owner: str, repo: str) -> Optional[dict]:
    """Try ``gh api repos/{owner}/{repo}``.

    Returns the parsed JSON dict on success, an error dict on failure, or
    ``None`` if the gh CLI is not installed.
    """
    try:
        completed = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}"],
            capture_output=True,
            text=True,
            timeout=GH_CLI_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        # gh CLI not installed → this tier is not applicable.
        return None
    except subprocess.TimeoutExpired:
        return {"error": "gh CLI timed out", "error_kind": "network"}
    except OSError as exc:
        return {"error": f"gh CLI failed to start: {exc}", "error_kind": "unknown"}

    if completed.returncode != 0:
        stderr = (completed.stderr or "").lower()
        if "404" in stderr or "not found" in stderr:
            return {"error": f"repo {owner}/{repo} not found", "error_kind": "not_found"}
        if "401" in stderr or "authentication" in stderr or "not logged" in stderr:
            return {"error": "gh CLI not authenticated", "error_kind": "auth"}
        if "403" in stderr or "rate limit" in stderr:
            return {"error": "gh CLI hit rate limit / forbidden", "error_kind": "rate_limit"}
        return {
            "error": f"gh CLI returned non-zero ({completed.returncode}): "
                     f"{(completed.stderr or '').strip()[:200]}",
            "error_kind": "unknown",
        }

    try:
        parsed = json.loads(completed.stdout or "")
    except (ValueError, TypeError):
        return {"error": "gh CLI returned unparseable JSON", "error_kind": "parse"}
    if not isinstance(parsed, dict):
        return {"error": "gh CLI returned non-object payload", "error_kind": "parse"}
    return parsed


# ---------------------------------------------------------------------------
# Tier 2: authenticated REST via GITHUB_TOKEN
# ---------------------------------------------------------------------------


def _try_github_token(owner: str, repo: str) -> Optional[dict]:
    """Try authenticated REST using ``GITHUB_TOKEN`` from env."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }
    return _do_rest_request(owner, repo, headers)


# ---------------------------------------------------------------------------
# Tier 3: unauthenticated REST
# ---------------------------------------------------------------------------


def _try_unauth(owner: str, repo: str) -> Optional[dict]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    return _do_rest_request(owner, repo, headers)


def _do_rest_request(owner: str, repo: str, headers: dict) -> dict:
    url = f"{API_BASE}/repos/{owner}/{repo}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"error": f"repo {owner}/{repo} not found", "error_kind": "not_found"}
        if exc.code == 401:
            return {"error": "GitHub auth rejected (401)", "error_kind": "auth"}
        if exc.code == 403:
            return {"error": "GitHub forbidden / rate limit (403)", "error_kind": "rate_limit"}
        return {"error": f"GitHub HTTP {exc.code}", "error_kind": "unknown"}
    except urllib.error.URLError as exc:
        return {"error": f"network error reaching GitHub: {exc.reason}", "error_kind": "network"}
    except Exception as exc:  # noqa: BLE001 - last-resort guardrail
        return {"error": f"unexpected error: {exc}", "error_kind": "unknown"}

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"error": "GitHub returned unparseable JSON", "error_kind": "parse"}
    if not isinstance(parsed, dict):
        return {"error": "GitHub returned non-object payload", "error_kind": "parse"}
    return parsed


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_repo(raw: dict, source_tier: str) -> dict:
    """Project the GitHub REST response into our public schema.

    All numeric fields default to 0 if missing or non-numeric; ``last_commit_iso``
    is taken from ``pushed_at`` (the most reliable proxy for recent activity)
    and falls back to ``updated_at`` if absent.
    """
    stars = _coerce_int(raw.get("stargazers_count"))
    forks = _coerce_int(raw.get("forks_count"))
    watchers = _coerce_int(raw.get("watchers_count"))
    last_commit_iso = raw.get("pushed_at") or raw.get("updated_at") or None
    if not isinstance(last_commit_iso, str):
        last_commit_iso = None

    # Contributors is not in the basic /repos response. We don't make a second
    # call here (it would defeat caching); leave as None and let callers fetch
    # on demand if they need it. Tests treat None as acceptable.
    contributors = None

    return {
        "stars": stars,
        "forks": forks,
        "contributors": contributors,
        "last_commit_iso": last_commit_iso,
        "watchers": watchers,
        "warning_low_quality": stars < LOW_QUALITY_STAR_THRESHOLD,
        "source_tier": source_tier,
    }


def _coerce_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
