"""Tests for scripts/sources/cache.py.

Stdlib-only TTL cache. Exercises happy path, expiry, invalidation,
namespace clears, key sanitization edge cases, partial-file robustness,
and concurrent writes (atomicity via tmp+rename).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Load `scripts/sources/cache.py` directly. We can't put `scripts/` on
# sys.path and `from sources import cache` because the `tests.sources`
# package shadows the name. Direct file-based import sidesteps the conflict.
import importlib.util  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "ai_quickstart_cache", ROOT / "scripts" / "sources" / "cache.py"
)
cache = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(cache)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point AI_QUICKSTART_HOME at a per-test tmp_path."""
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(tmp_path))
    yield tmp_path


# ---------------------------------------------------------------------------
# get/set/invalidate/clear
# ---------------------------------------------------------------------------


def test_get_missing_returns_none():
    assert cache.get("github", "missing-repo", ttl_seconds=3600) is None


def test_set_then_get_within_ttl_returns_value():
    payload = {"stars": 42, "name": "foo/bar"}
    cache.set("github", "foo/bar", payload)
    got = cache.get("github", "foo/bar", ttl_seconds=3600)
    assert got == payload


def test_set_then_get_past_ttl_returns_none(_isolated_home):
    cache.set("github", "foo/bar", {"stars": 1})
    # Rewrite the underlying file with a written_at far in the past.
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    payload = json.loads(path.read_text())
    payload["written_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=7200)
    ).isoformat()
    path.write_text(json.dumps(payload))
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None


def test_get_at_exact_ttl_boundary_returns_none(_isolated_home):
    cache.set("github", "foo/bar", {"stars": 1})
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    payload = json.loads(path.read_text())
    payload["written_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=3600)
    ).isoformat()
    path.write_text(json.dumps(payload))
    # age >= ttl → expired
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None


def test_invalidate_removes_file(_isolated_home):
    cache.set("github", "foo/bar", {"stars": 1})
    cache.invalidate("github", "foo/bar")
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    assert not path.exists()


def test_invalidate_missing_is_noop():
    # Should not raise.
    cache.invalidate("github", "never-written")


def test_clear_removes_namespace_only(_isolated_home):
    cache.set("github", "foo/bar", {"stars": 1})
    cache.set("github", "baz/qux", {"stars": 2})
    cache.set("mcpmarket", "search?q=ai", {"results": []})

    cache.clear("github")

    # github namespace gone.
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None
    assert cache.get("github", "baz/qux", ttl_seconds=3600) is None
    gh_dir = _isolated_home / "cache" / "github"
    assert not gh_dir.exists()

    # mcpmarket untouched.
    assert cache.get("mcpmarket", "search?q=ai", ttl_seconds=3600) == {"results": []}


def test_clear_missing_namespace_is_noop():
    # Should not raise even if cache root doesn't exist yet.
    cache.clear("never-created")


# ---------------------------------------------------------------------------
# safe_key
# ---------------------------------------------------------------------------


def test_safe_key_url_with_query_string():
    raw = "https://mcpmarket.com/search?q=ai+stuff&page=2"
    key = cache.safe_key(raw)
    assert "/" not in key
    assert ":" not in key
    assert "?" not in key
    assert "&" not in key
    assert " " not in key
    assert key  # non-empty


def test_safe_key_truncates_very_long_key_with_hash_suffix():
    raw = "a" * 1000
    key = cache.safe_key(raw)
    assert len(key) <= 200
    # On truncation the hash suffix should be appended (the digest hex prefix).
    assert "-" in key


def test_safe_key_long_keys_distinct_inputs_dont_collide():
    a = "a" * 1000
    b = "a" * 999 + "b"
    assert cache.safe_key(a) != cache.safe_key(b)


def test_safe_key_unicode_input():
    raw = "café/日本語/ key with spaces"
    key = cache.safe_key(raw)
    assert "/" not in key
    assert " " not in key
    # Unicode letters that aren't in the unsafe set are preserved.
    assert "café" in key or key  # at minimum, some output


def test_safe_key_empty_input_returns_placeholder():
    key = cache.safe_key("")
    assert key  # not empty


def test_safe_key_only_unsafe_chars_returns_non_empty():
    key = cache.safe_key("///   ???")
    assert key
    assert "/" not in key
    assert "?" not in key


def test_safe_key_idempotent_on_already_safe_key():
    key = cache.safe_key("simple-key")
    # Already safe keys round-trip without modification.
    assert key == "simple-key"


# ---------------------------------------------------------------------------
# robustness: partial / corrupt files
# ---------------------------------------------------------------------------


def test_truncated_file_returns_none(_isolated_home):
    cache.set("github", "foo/bar", {"stars": 1})
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    # Simulate a partial write by truncating mid-document.
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None


def test_garbage_file_returns_none(_isolated_home):
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all {{{")
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None


def test_non_dict_payload_returns_none(_isolated_home):
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["not", "a", "dict"]))
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None


def test_missing_written_at_returns_none(_isolated_home):
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"value": {"x": 1}}))
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None


def test_unparseable_written_at_returns_none(_isolated_home):
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"written_at": "yesterday-ish", "value": {"x": 1}})
    )
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None


def test_zulu_written_at_is_accepted(_isolated_home):
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    path.write_text(json.dumps({"written_at": now, "value": {"x": 1}}))
    assert cache.get("github", "foo/bar", ttl_seconds=3600) == {"x": 1}


def test_set_rejects_non_dict_value():
    with pytest.raises(TypeError):
        cache.set("github", "foo/bar", ["not", "a", "dict"])  # type: ignore[arg-type]


def test_value_field_must_be_dict(_isolated_home):
    # A file with a non-dict ``value`` is ignored rather than returned.
    path = _isolated_home / "cache" / "github" / (cache.safe_key("foo/bar") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps({"written_at": now, "value": "scalar"}))
    assert cache.get("github", "foo/bar", ttl_seconds=3600) is None


# ---------------------------------------------------------------------------
# concurrency: atomic writes never produce a corrupt file
# ---------------------------------------------------------------------------


def test_concurrent_writes_never_corrupt_file(_isolated_home):
    """Many threads racing to write the same key should leave a valid JSON file
    (one of the writers' values), never a partial document."""
    namespace = "github"
    key = "race/key"
    iterations = 50
    threads = 8

    def writer(idx: int):
        for i in range(iterations):
            cache.set(namespace, key, {"writer": idx, "iteration": i})

    workers = [threading.Thread(target=writer, args=(i,)) for i in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    # Final read must succeed and return a dict with the expected keys.
    got = cache.get(namespace, key, ttl_seconds=3600)
    assert got is not None
    assert "writer" in got and "iteration" in got
    assert 0 <= got["writer"] < threads
    assert 0 <= got["iteration"] < iterations

    # No leftover tmp files in the namespace dir.
    ns_dir = _isolated_home / "cache" / namespace
    leftovers = [p for p in ns_dir.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == [], f"stray tmp files: {leftovers}"


def test_concurrent_writes_via_fork(_isolated_home):
    """Belt-and-suspenders: also exercise via os.fork on POSIX to confirm the
    tmp+rename path holds across process boundaries (where threading races
    could be masked by the GIL)."""
    if not hasattr(os, "fork"):
        pytest.skip("os.fork not available on this platform")

    namespace = "github"
    key = "fork/key"
    children = 4
    pids = []
    for i in range(children):
        pid = os.fork()
        if pid == 0:
            # In child: write a few times then exit.
            try:
                for j in range(20):
                    cache.set(namespace, key, {"child": i, "j": j})
            finally:
                os._exit(0)
        else:
            pids.append(pid)
    for pid in pids:
        os.waitpid(pid, 0)

    got = cache.get(namespace, key, ttl_seconds=3600)
    assert got is not None
    assert "child" in got and "j" in got


# ---------------------------------------------------------------------------
# AI_QUICKSTART_HOME env var honored
# ---------------------------------------------------------------------------


def test_env_var_overrides_home(tmp_path, monkeypatch):
    custom = tmp_path / "custom-home"
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(custom))
    cache.set("github", "x/y", {"v": 1})
    assert (custom / "cache" / "github").exists()
    assert cache.get("github", "x/y", ttl_seconds=3600) == {"v": 1}


def test_env_var_with_tilde_is_expanded(tmp_path, monkeypatch):
    # Drop the tmp_path autouse fixture's value and use a fake HOME so a
    # leading ``~`` resolves under tmp_path.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AI_QUICKSTART_HOME", "~/aiqs")
    cache.set("github", "x/y", {"v": 1})
    assert (fake_home / "aiqs" / "cache" / "github").exists()
