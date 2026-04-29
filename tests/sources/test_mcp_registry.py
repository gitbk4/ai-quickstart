"""Tests for scripts/sources/mcp_registry.py.

Covers:
  - claude CLI present and returns parseable results
  - claude CLI absent → empty results + warning (graceful)
  - claude CLI returns malformed JSON → empty + warning
  - claude CLI exits non-zero → empty + warning
  - claude CLI hits timeout → empty + warning
  - cache hit on second call (no CLI invocation)
  - force_refresh bypasses cache
  - results clamped to ``limit``
"""

from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest

from ._loader import load

mcp_registry = load("mcp_registry")
cache = load("cache")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(tmp_path))
    yield tmp_path


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# Happy path: claude CLI present and returns results
# ---------------------------------------------------------------------------


def test_search_happy_path_with_claude_cli():
    fake_payload = {
        "results": [
            {"id": "fs", "title": "Filesystem MCP", "description": "..."},
            {"id": "git", "title": "Git MCP", "description": "..."},
        ]
    }
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/usr/local/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(stdout=json.dumps(fake_payload)),
    ) as run:
        out = mcp_registry.search(["filesystem", "git"], limit=10)
    assert out["source"] == "mcp-registry"
    assert out["warnings"] == []
    assert len(out["results"]) == 2
    assert out["results"][0]["id"] == "fs"
    # The CLI was called with the registry tool name.
    cmd = run.call_args[0][0]
    assert cmd[0] == "/usr/local/bin/claude"
    assert "mcp-registry" in cmd[3] or any("mcp-registry" in str(a) for a in cmd)


def test_search_happy_path_with_raw_list_payload():
    """Some CLI versions return a bare list. Handle that shape too."""
    fake_payload = [{"id": "x", "title": "X"}, {"id": "y", "title": "Y"}]
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(stdout=json.dumps(fake_payload)),
    ):
        out = mcp_registry.search(["x"])
    assert len(out["results"]) == 2
    assert out["warnings"] == []


def test_search_clamps_results_to_limit():
    fake_payload = {"results": [{"id": str(i)} for i in range(50)]}
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(stdout=json.dumps(fake_payload)),
    ):
        out = mcp_registry.search(["foo"], limit=5)
    assert len(out["results"]) == 5


# ---------------------------------------------------------------------------
# claude CLI absent
# ---------------------------------------------------------------------------


def test_search_when_claude_cli_missing_returns_empty_with_warning():
    with mock.patch.object(mcp_registry, "_find_claude_cli", return_value=None):
        out = mcp_registry.search(["foo"])
    assert out["results"] == []
    assert out["source"] == "mcp-registry"
    assert len(out["warnings"]) == 1
    assert "claude cli not found" in out["warnings"][0].lower()


# ---------------------------------------------------------------------------
# claude CLI malformed output
# ---------------------------------------------------------------------------


def test_search_with_non_json_stdout_returns_empty_with_warning():
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(stdout="not json at all {{{"),
    ):
        out = mcp_registry.search(["foo"])
    assert out["results"] == []
    assert any("non-JSON" in w or "non-json" in w.lower() for w in out["warnings"])


def test_search_with_unexpected_payload_shape_returns_empty_with_warning():
    """E.g. CLI returns ``{"foo": "bar"}`` — neither list nor results-key."""
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(stdout=json.dumps({"foo": "bar"})),
    ):
        out = mcp_registry.search(["foo"])
    assert out["results"] == []
    assert any("malformed" in w.lower() for w in out["warnings"])


def test_search_with_results_containing_non_dicts_filters_them_out():
    fake_payload = {"results": [{"id": "ok"}, "garbage", 42, {"id": "ok2"}]}
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(stdout=json.dumps(fake_payload)),
    ):
        out = mcp_registry.search(["foo"])
    ids = [r["id"] for r in out["results"]]
    assert ids == ["ok", "ok2"]


# ---------------------------------------------------------------------------
# claude CLI errors
# ---------------------------------------------------------------------------


def test_search_with_cli_nonzero_exit_returns_empty_with_warning():
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(returncode=1, stderr="boom"),
    ):
        out = mcp_registry.search(["foo"])
    assert out["results"] == []
    assert any("exited" in w.lower() or "exit" in w.lower() for w in out["warnings"])


def test_search_with_cli_timeout_returns_empty_with_warning():
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=20),
    ):
        out = mcp_registry.search(["foo"])
    assert out["results"] == []
    assert any("timed out" in w.lower() or "timeout" in w.lower() for w in out["warnings"])


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_search_cache_hit_skips_cli():
    fake_payload = {"results": [{"id": "cached"}]}
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(stdout=json.dumps(fake_payload)),
    ) as run:
        first = mcp_registry.search(["foo"], limit=10)
        second = mcp_registry.search(["foo"], limit=10)
    assert first["results"] == second["results"]
    # CLI called once; second call hit cache.
    assert run.call_count == 1


def test_search_force_refresh_bypasses_cache():
    fake_payload = {"results": [{"id": "live"}]}
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(stdout=json.dumps(fake_payload)),
    ) as run:
        mcp_registry.search(["foo"], limit=10)
        mcp_registry.search(["foo"], limit=10, force_refresh=True)
    assert run.call_count == 2


def test_search_cache_keyed_by_keywords_and_limit():
    """Different keyword sets (or limits) must not share cache entries."""
    fake_a = {"results": [{"id": "a"}]}
    fake_b = {"results": [{"id": "b"}]}
    runs = {"count": 0}

    def fake_run(*args, **kwargs):
        runs["count"] += 1
        # First call returns A, all later calls return B — proves we hit the
        # CLI a second time rather than serving stale A from cache.
        return _completed(stdout=json.dumps(fake_a if runs["count"] == 1 else fake_b))

    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(mcp_registry.subprocess, "run", side_effect=fake_run):
        a_out = mcp_registry.search(["alpha"], limit=10)
        b_out = mcp_registry.search(["beta"], limit=10)
    assert a_out["results"][0]["id"] == "a"
    assert b_out["results"][0]["id"] == "b"
    assert runs["count"] == 2


def test_search_warnings_payload_is_not_cached_as_success():
    """An empty-with-warning result should NOT be cached as if it were a hit
    (otherwise we'd hide CLI absence behind a 24h cache window forever)."""
    with mock.patch.object(mcp_registry, "_find_claude_cli", return_value=None):
        first = mcp_registry.search(["foo"])
        second = mcp_registry.search(["foo"])
    # Both calls produce the same empty-with-warning shape.
    assert first["results"] == [] and second["results"] == []
    assert first["warnings"] and second["warnings"]
    # The cache must be empty for that key (CLI absent → not cached).
    assert cache.get("mcp_registry", "q=foo|n=20", ttl_seconds=3600) is None


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def test_search_handles_string_keywords_argument():
    """Be forgiving: accept a single string by wrapping it."""
    fake_payload = {"results": [{"id": "x"}]}
    with mock.patch.object(
        mcp_registry, "_find_claude_cli", return_value="/bin/claude"
    ), mock.patch.object(
        mcp_registry.subprocess,
        "run",
        return_value=_completed(stdout=json.dumps(fake_payload)),
    ):
        out = mcp_registry.search("filesystem")  # type: ignore[arg-type]
    assert len(out["results"]) == 1
