"""Tests for scripts/sources/github.py.

Covers the 3-tier auth fallback (gh CLI → GITHUB_TOKEN env → unauth),
graceful 401/403/404/network handling, the low-quality star warning, the
6h cache TTL, and force_refresh bypass. All external calls (subprocess,
urllib) are mocked — the suite never touches the network.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from unittest import mock

import pytest

from ._loader import load

github = load("github")
cache = load("cache")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(tmp_path))
    # Wipe any GITHUB_TOKEN by default; tests opt in.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield tmp_path


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Build a minimal subprocess.CompletedProcess stand-in."""
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _gh_repo_payload(stars: int = 1500, forks: int = 200, watchers: int = 1500):
    return {
        "stargazers_count": stars,
        "forks_count": forks,
        "watchers_count": watchers,
        "pushed_at": "2026-04-20T10:00:00Z",
        "updated_at": "2026-04-20T10:00:00Z",
    }


def _http_response(payload: dict, status: int = 200):
    body = json.dumps(payload).encode("utf-8")
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.status = status
    return resp


# ---------------------------------------------------------------------------
# Tier 1: gh CLI
# ---------------------------------------------------------------------------


def test_tier_gh_cli_happy_path():
    payload = _gh_repo_payload(stars=2500)
    with mock.patch.object(
        github.subprocess,
        "run",
        return_value=_completed(stdout=json.dumps(payload)),
    ) as run:
        out = github.fetch_repo("octocat", "hello-world")
    assert out["source_tier"] == "gh-cli"
    assert out["stars"] == 2500
    assert out["forks"] == 200
    assert out["warning_low_quality"] is False
    assert out["last_commit_iso"] == "2026-04-20T10:00:00Z"
    # Confirm we used the gh CLI command shape we promised.
    args = run.call_args[0][0]
    assert args[0] == "gh"
    assert args[1] == "api"
    assert args[2] == "repos/octocat/hello-world"


def test_tier_gh_cli_low_quality_warning_triggers_under_100_stars():
    payload = _gh_repo_payload(stars=42)
    with mock.patch.object(
        github.subprocess, "run",
        return_value=_completed(stdout=json.dumps(payload)),
    ):
        out = github.fetch_repo("tiny", "repo")
    assert out["warning_low_quality"] is True


def test_tier_gh_cli_low_quality_off_at_exactly_100_stars():
    payload = _gh_repo_payload(stars=100)
    with mock.patch.object(
        github.subprocess, "run",
        return_value=_completed(stdout=json.dumps(payload)),
    ):
        out = github.fetch_repo("med", "repo")
    assert out["warning_low_quality"] is False


def test_tier_gh_cli_404_short_circuits_to_not_found():
    """A 404 from gh CLI is definitive — don't bother trying lower tiers."""
    with mock.patch.object(
        github.subprocess,
        "run",
        return_value=_completed(returncode=1, stderr="HTTP 404: Not Found"),
    ) as run, mock.patch.object(github.urllib.request, "urlopen") as urlopen:
        out = github.fetch_repo("ghost", "repo")
    assert "error" in out
    assert out["error_kind"] == "not_found"
    # Lower tiers must NOT have been tried.
    assert urlopen.call_count == 0
    assert run.call_count == 1


def test_tier_gh_cli_unparseable_json_falls_through():
    """Malformed gh output should not crash; we fall through to the next tier."""
    payload = _gh_repo_payload(stars=500)
    # gh returns junk, then unauth tier returns success.
    with mock.patch.object(
        github.subprocess,
        "run",
        return_value=_completed(stdout="this is { not json"),
    ), mock.patch.object(
        github.urllib.request,
        "urlopen",
        return_value=_http_response(payload),
    ):
        out = github.fetch_repo("a", "b")
    assert "error" not in out
    assert out["source_tier"] == "unauth"
    assert out["stars"] == 500


def test_tier_gh_cli_not_installed_falls_through_to_unauth():
    payload = _gh_repo_payload(stars=300)
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError("gh not found")
    ), mock.patch.object(
        github.urllib.request, "urlopen", return_value=_http_response(payload)
    ):
        out = github.fetch_repo("a", "b")
    assert out["source_tier"] == "unauth"
    assert out["stars"] == 300


def test_tier_gh_cli_timeout_falls_through():
    payload = _gh_repo_payload(stars=300)
    with mock.patch.object(
        github.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=1),
    ), mock.patch.object(
        github.urllib.request, "urlopen", return_value=_http_response(payload)
    ):
        out = github.fetch_repo("a", "b")
    assert out["source_tier"] == "unauth"


def test_tier_gh_cli_auth_error_falls_through_to_token():
    payload = _gh_repo_payload(stars=999)
    monkey = mock.patch.dict("os.environ", {"GITHUB_TOKEN": "secret"})
    with mock.patch.object(
        github.subprocess,
        "run",
        return_value=_completed(returncode=1, stderr="HTTP 401: not logged in"),
    ), monkey, mock.patch.object(
        github.urllib.request, "urlopen", return_value=_http_response(payload)
    ) as urlopen:
        out = github.fetch_repo("a", "b")
    assert out["source_tier"] == "github-token"
    assert out["stars"] == 999
    # The Authorization header should be present on the token call.
    req = urlopen.call_args[0][0]
    assert req.headers.get("Authorization") == "Bearer secret"


# ---------------------------------------------------------------------------
# Tier 2: GITHUB_TOKEN
# ---------------------------------------------------------------------------


def test_tier_token_used_when_gh_cli_absent(monkeypatch):
    payload = _gh_repo_payload(stars=777)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request, "urlopen", return_value=_http_response(payload)
    ) as urlopen:
        out = github.fetch_repo("a", "b")
    assert out["source_tier"] == "github-token"
    req = urlopen.call_args[0][0]
    assert req.headers.get("Authorization") == "Bearer tok"
    assert req.headers.get("User-agent")  # urllib normalizes header case


def test_tier_token_skipped_if_env_unset_and_falls_to_unauth():
    payload = _gh_repo_payload(stars=300)
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request, "urlopen", return_value=_http_response(payload)
    ) as urlopen:
        out = github.fetch_repo("a", "b")
    assert out["source_tier"] == "unauth"
    req = urlopen.call_args[0][0]
    assert req.headers.get("Authorization") is None


# ---------------------------------------------------------------------------
# Tier 3: unauth + cache
# ---------------------------------------------------------------------------


def test_tier_unauth_caches_successful_fetch():
    payload = _gh_repo_payload(stars=300)
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request, "urlopen", return_value=_http_response(payload)
    ) as urlopen:
        out1 = github.fetch_repo("ca", "ched")
        out2 = github.fetch_repo("ca", "ched")
    assert out1["source_tier"] == "unauth"
    assert out2["source_tier"] == "cache"
    # urlopen called only once; the second fetch was cache.
    assert urlopen.call_count == 1


def test_force_refresh_bypasses_cache():
    payload = _gh_repo_payload(stars=300)
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request, "urlopen", return_value=_http_response(payload)
    ) as urlopen:
        github.fetch_repo("a", "b")
        out2 = github.fetch_repo("a", "b", force_refresh=True)
    assert out2["source_tier"] == "unauth"
    assert urlopen.call_count == 2


def test_cache_hit_returns_warning_low_quality_flag(tmp_path, monkeypatch):
    """If a low-star result is cached, the cache hit must preserve the warning."""
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(tmp_path))
    cache.set("github", "tiny/repo", {
        "stars": 5, "forks": 0, "contributors": None,
        "last_commit_iso": "2026-04-20T10:00:00Z", "watchers": 5,
        "warning_low_quality": True, "source_tier": "unauth",
    })
    with mock.patch.object(github.subprocess, "run") as run, \
         mock.patch.object(github.urllib.request, "urlopen") as urlopen:
        out = github.fetch_repo("tiny", "repo")
    assert out["source_tier"] == "cache"
    assert out["warning_low_quality"] is True
    # No external calls when cache hit.
    assert run.call_count == 0
    assert urlopen.call_count == 0


# ---------------------------------------------------------------------------
# Error paths: 401 / 403 / 404 / network
# ---------------------------------------------------------------------------


def _http_error(code: int):
    return urllib.error.HTTPError(
        url="https://api.github.com/x",
        code=code,
        msg="boom",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )


def test_unauth_404_returns_not_found_error():
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request, "urlopen", side_effect=_http_error(404)
    ):
        out = github.fetch_repo("nope", "nada")
    assert out["error_kind"] == "not_found"
    assert "error" in out


def test_unauth_401_returns_auth_error():
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request, "urlopen", side_effect=_http_error(401)
    ):
        out = github.fetch_repo("a", "b")
    assert out["error_kind"] == "auth"


def test_unauth_403_returns_rate_limit_error():
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request, "urlopen", side_effect=_http_error(403)
    ):
        out = github.fetch_repo("a", "b")
    assert out["error_kind"] == "rate_limit"


def test_unauth_network_error_returns_network_error():
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request,
        "urlopen",
        side_effect=urllib.error.URLError("dns boom"),
    ):
        out = github.fetch_repo("a", "b")
    assert out["error_kind"] == "network"


def test_unauth_unparseable_json_returns_parse_error():
    bad_resp = mock.MagicMock()
    bad_resp.read.return_value = b"not json {"
    bad_resp.__enter__.return_value = bad_resp
    bad_resp.__exit__.return_value = False
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(github.urllib.request, "urlopen", return_value=bad_resp):
        out = github.fetch_repo("a", "b")
    assert out["error_kind"] == "parse"


def test_404_does_not_pollute_cache():
    """A 404 is an error, not a cacheable result."""
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request, "urlopen", side_effect=_http_error(404)
    ):
        github.fetch_repo("nope", "nada")
    # The cache should be empty for that key.
    assert cache.get("github", "nope/nada", ttl_seconds=3600) is None


def test_user_agent_set_on_unauth_request():
    payload = _gh_repo_payload(stars=300)
    with mock.patch.object(
        github.subprocess, "run", side_effect=FileNotFoundError()
    ), mock.patch.object(
        github.urllib.request, "urlopen", return_value=_http_response(payload)
    ) as urlopen:
        github.fetch_repo("a", "b")
    req = urlopen.call_args[0][0]
    assert req.headers.get("User-agent")
    assert "ai-quickstart" in req.headers["User-agent"].lower()
