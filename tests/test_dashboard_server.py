"""Tests for scripts/dashboard/server.py — Wave 1B combined http.server.

Coverage map (matches the 13 GAPs in the v2-cathedral.md plan):

  1.  Port-file write on bind.
  2.  Port-file cleanup on shutdown.
  3.  Two parallel start_server() calls — only one binds, second reuses.
  4.  Stale port file (PID dead) — start_server rebinds and rewrites.
  5.  GET /persona/current happy path.
  6.  GET /persona/current during heal — includes ``stale: true``.
  7.  GET /persona/current when persona missing — 404 + helpful body.
  8.  GET /persona/p/{id} known id — returns paragraph body + provenance.
  9.  GET /persona/p/{id} unknown id — 404.
  10. GET /dashboard/ — 200 + HTML mentioning "skeleton" and 5 pane names.
  11. Request timeout — slow handler doesn't starve persona path.
  12. 404 on unknown route — JSON body.
  13. Telemetry emitted for /dashboard/ but NOT for /persona/*.

Tests bind to ephemeral ports (port=0 inside _ThreadedServer).
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

# Make scripts/ importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import persona_json  # noqa: E402  pylint: disable=wrong-import-position
from dashboard import server as dashboard_server  # noqa: E402
from dashboard.handlers import dashboard as dashboard_handlers  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """Provision an isolated AI_QUICKSTART_HOME with persona/ scaffolding."""
    h = tmp_path / "aiq-home"
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(h))
    h.mkdir(parents=True, exist_ok=True)
    (h / "persona").mkdir()
    (h / "persona" / "anecdotes").mkdir()
    (h / "run").mkdir()
    return h


@pytest.fixture
def reset_server_state():
    """Make sure each test starts and ends with a clean module-level slot."""
    # Force a fresh slate — release any held lock from a prior crashed test.
    try:
        dashboard_server.shutdown_server()
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            dashboard_server.shutdown_server()
        except Exception:
            pass
        # Aggressive cleanup of module state, just in case.
        dashboard_server._active.update(
            {
                "server": None,
                "thread": None,
                "lock_fd": None,
                "lock_path": None,
                "port_path": None,
                "port": None,
                "home": None,
                "telemetry_emitter": None,
            }
        )


def _seed_persona_json(home: Path, paragraphs: List[Dict[str, Any]]) -> None:
    """Write a minimal valid persona.json under ``home``."""
    persona_dir = home / "persona"
    persona_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": persona_json.PERSONA_JSON_SCHEMA_VERSION,
        "generated_at": "2026-05-05T00:00:00Z",
        "from_md_sha": "0" * 64,
        "structured": {
            "role": "platform engineer",
            "archetype": "job",
            "industry": "fintech",
            "skill_tolerance": "high",
            "project_style": "minimal",
            "top_projects": [],
        },
        "paragraphs": paragraphs,
        "deleted_ids": [],
    }
    (persona_dir / persona_json.PERSONA_JSON_FILE).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _http_get(host: str, port: int, path: str, timeout: float = 5.0) -> Tuple[int, Dict[str, str], bytes]:
    """Tiny synchronous GET against the bound test server."""
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = resp.status
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read()
            return status, headers, body
    except urllib.error.HTTPError as e:
        body = e.read() or b""
        headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return e.code, headers, body


@contextlib.contextmanager
def _started_server(
    home: Path,
    *,
    telemetry_emitter: Optional[Callable[[Path, str, Dict[str, Any]], None]] = None,
):
    """Start a server, yield ``(host, port)``, shut down on exit."""
    port, _thread = dashboard_server.start_server(
        home, host="127.0.0.1", telemetry_emitter=telemetry_emitter
    )
    try:
        # Tiny readiness wait: the serve_forever thread is started but the
        # accept loop may not be polling yet. _check_health is the same
        # probe start_server uses on the contended branch.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if dashboard_server._check_health("127.0.0.1", port, timeout=0.2):
                break
            time.sleep(0.02)
        yield ("127.0.0.1", port)
    finally:
        dashboard_server.shutdown_server()


# ---------------------------------------------------------------------------
# GAP 1: Port-file write on bind
# ---------------------------------------------------------------------------


def test_port_file_written_on_bind(home: Path, reset_server_state):
    with _started_server(home) as (host, port):
        port_path = home / "run" / "server.port"
        assert port_path.exists(), "server.port not created on bind"
        data = json.loads(port_path.read_text(encoding="utf-8"))
        assert data["port"] == port
        assert data["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# GAP 2: Port-file cleanup on shutdown
# ---------------------------------------------------------------------------


def test_port_file_cleaned_on_shutdown(home: Path, reset_server_state):
    port, _thread = dashboard_server.start_server(home, host="127.0.0.1")
    port_path = home / "run" / "server.port"
    assert port_path.exists()
    dashboard_server.shutdown_server()
    assert not port_path.exists(), "server.port should be removed on shutdown"


# ---------------------------------------------------------------------------
# GAP 3: Two parallel start_server() calls — only one binds
# ---------------------------------------------------------------------------


def test_parallel_start_server_calls_share_port(home: Path, reset_server_state):
    barrier = threading.Barrier(2)
    results: Dict[str, Tuple[int, threading.Thread]] = {}

    def worker(name: str) -> None:
        barrier.wait()
        results[name] = dashboard_server.start_server(home, host="127.0.0.1")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    try:
        assert "a" in results and "b" in results
        port_a, _ta = results["a"]
        port_b, _tb = results["b"]
        assert port_a == port_b, (
            f"parallel start_server should yield the same port; "
            f"got {port_a} and {port_b}"
        )
        # The on-disk port file should match.
        data = json.loads((home / "run" / "server.port").read_text(encoding="utf-8"))
        assert data["port"] == port_a
    finally:
        dashboard_server.shutdown_server()


# ---------------------------------------------------------------------------
# GAP 4: Stale port file (PID dead) — start_server rebinds and rewrites
# ---------------------------------------------------------------------------


def _find_unused_port() -> int:
    """Bind, get the OS-assigned port, close — gives an unused port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_stale_port_file_triggers_rebind(home: Path, reset_server_state):
    # Plant a port file with a definitely-dead PID and a not-listening port.
    port_path = home / "run" / "server.port"
    port_path.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = 99999  # almost certainly not running
    # Bounded search for a definitely-dead pid, so we don't loop forever
    # on an unusually populated process table.
    for candidate in range(99999, 99999 + 200):
        if not dashboard_server._pid_is_alive(candidate):
            dead_pid = candidate
            break
    else:  # pragma: no cover - extremely unlikely
        pytest.skip("could not find a dead pid in the search range")
    fake_port = _find_unused_port()
    port_path.write_text(
        json.dumps({"pid": dead_pid, "port": fake_port}), encoding="utf-8"
    )

    port, _thread = dashboard_server.start_server(home, host="127.0.0.1")
    try:
        # The new port should be different from the planted-fake port (with
        # overwhelming probability — they're both ephemeral).
        assert port != fake_port
        # And the on-disk port file should now reflect the new bind.
        data = json.loads(port_path.read_text(encoding="utf-8"))
        assert data["port"] == port
        assert data["pid"] == os.getpid()
    finally:
        dashboard_server.shutdown_server()


# ---------------------------------------------------------------------------
# GAP 5: GET /persona/current happy path
# ---------------------------------------------------------------------------


def test_persona_current_happy_path(home: Path, reset_server_state):
    _seed_persona_json(
        home,
        paragraphs=[
            {
                "id": "p:001",
                "text": "First paragraph.",
                "provenance": "heal",
                "trust_score": 3,
                "anchored_to": None,
                "locked": False,
                "merged_from": None,
            }
        ],
    )
    with _started_server(home) as (host, port):
        status, headers, body = _http_get(host, port, "/persona/current")
        assert status == 200
        assert "application/json" in headers.get("content-type", "")
        data = json.loads(body)
        assert data["schema_version"] == persona_json.PERSONA_JSON_SCHEMA_VERSION
        assert len(data["paragraphs"]) == 1
        assert data["paragraphs"][0]["id"] == "p:001"
        assert "stale" not in data, "no heal -> no stale flag"


# ---------------------------------------------------------------------------
# GAP 6: GET /persona/current during heal — includes stale: true
# ---------------------------------------------------------------------------


def test_persona_current_during_heal_marks_stale(home: Path, reset_server_state, tmp_path):
    """Heal lock semantics: hold LOCK_EX from a subprocess so the server's
    LOCK_SH|LOCK_NB probe (running in the test process) sees contention.

    flock is advisory and per-process on most BSD/macOS implementations,
    so acquiring the lock from within the same process would NOT contend
    with our own probe. We use ``subprocess`` with a tiny inline script
    to get a separate process holding the lock — works regardless of the
    multiprocessing start method.
    """
    import subprocess
    import textwrap

    _seed_persona_json(
        home,
        paragraphs=[
            {
                "id": "p:001",
                "text": "Pre-heal paragraph.",
                "provenance": "heal",
                "trust_score": 3,
                "anchored_to": None,
                "locked": False,
                "merged_from": None,
            }
        ],
    )

    heal_lock = home / "persona" / ".heal.lock"
    heal_lock.parent.mkdir(parents=True, exist_ok=True)
    heal_lock.touch()

    # Sentinel files: holder writes "ready" when it has the lock; test
    # writes "release" when it wants the holder to exit.
    ready_path = tmp_path / "holder-ready"
    release_path = tmp_path / "holder-release"

    holder_script = textwrap.dedent(
        f"""
        import fcntl, os, sys, time
        lock_path = {str(heal_lock)!r}
        ready = {str(ready_path)!r}
        release = {str(release_path)!r}
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(2)
        with open(ready, "w") as f: f.write("1")
        deadline = time.monotonic() + 30
        while not os.path.exists(release) and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        """
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait for the holder to acquire and signal ready.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if ready_path.exists():
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode("utf-8", "replace")
                raise AssertionError(
                    f"holder subprocess exited early (rc={proc.returncode}): {stderr}"
                )
            time.sleep(0.05)
        assert ready_path.exists(), "holder never signalled ready"

        with _started_server(home) as (host, port):
            status, _headers, body = _http_get(host, port, "/persona/current")
            assert status == 200
            data = json.loads(body)
            assert data.get("stale") is True, (
                "expected stale=true while heal lock is held by another process"
            )
    finally:
        # Tell the holder to exit.
        try:
            release_path.write_text("1")
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# GAP 7: GET /persona/current when persona missing -> 404
# ---------------------------------------------------------------------------


def test_persona_current_missing_returns_404(home: Path, reset_server_state):
    # No persona.json seeded.
    with _started_server(home) as (host, port):
        status, headers, body = _http_get(host, port, "/persona/current")
        assert status == 404
        assert "application/json" in headers.get("content-type", "")
        data = json.loads(body)
        assert data["error"] == "no persona"
        assert "ai-quickstart" in data["hint"]


# ---------------------------------------------------------------------------
# GAP 8: GET /persona/p/{id} known id
# ---------------------------------------------------------------------------


def test_persona_paragraph_known_id(home: Path, reset_server_state):
    _seed_persona_json(
        home,
        paragraphs=[
            {
                "id": "p:001",
                "text": "Para one.",
                "provenance": "heal",
                "trust_score": 4,
                "anchored_to": "anec-x",
                "locked": True,
                "merged_from": None,
            },
            {
                "id": "p:002",
                "text": "Para two.",
                "provenance": "anecdote",
                "trust_score": 5,
                "anchored_to": None,
                "locked": False,
                "merged_from": None,
            },
        ],
    )
    with _started_server(home) as (host, port):
        status, _headers, body = _http_get(host, port, "/persona/p/p:002")
        assert status == 200
        data = json.loads(body)
        assert data["id"] == "p:002"
        assert data["text"] == "Para two."
        assert data["provenance"] == "anecdote"
        assert data["trust_score"] == 5
        assert data["locked"] is False


# ---------------------------------------------------------------------------
# GAP 9: GET /persona/p/{id} unknown id -> 404
# ---------------------------------------------------------------------------


def test_persona_paragraph_unknown_id(home: Path, reset_server_state):
    _seed_persona_json(
        home,
        paragraphs=[
            {
                "id": "p:001",
                "text": "Only one.",
                "provenance": "heal",
                "trust_score": 3,
                "anchored_to": None,
                "locked": False,
                "merged_from": None,
            }
        ],
    )
    with _started_server(home) as (host, port):
        status, _headers, body = _http_get(host, port, "/persona/p/p:999")
        assert status == 404
        data = json.loads(body)
        assert data["error"] == "unknown paragraph"
        assert data["id"] == "p:999"


# ---------------------------------------------------------------------------
# GAP 10: GET /dashboard/ -> 200 + HTML mentioning skeleton + pane names
# ---------------------------------------------------------------------------


def test_dashboard_index_lists_skeleton_and_pane_names(home: Path, reset_server_state):
    with _started_server(home) as (host, port):
        status, headers, body = _http_get(host, port, "/dashboard/")
        assert status == 200
        ctype = headers.get("content-type", "")
        assert "text/html" in ctype
        body_text = body.decode("utf-8")
        assert "skeleton" in body_text.lower()
        for pane in dashboard_handlers.FUTURE_PANES:
            assert pane in body_text, f"pane {pane!r} missing from index HTML"


# ---------------------------------------------------------------------------
# GAP 11: Request timeout — slow handler doesn't starve persona path
# ---------------------------------------------------------------------------


def test_slow_handler_does_not_starve_persona(home: Path, reset_server_state, monkeypatch):
    """Wedge a slow handler; verify /persona/current still responds in budget.

    Strategy: monkeypatch dashboard_handlers.index to sleep longer than the
    request timeout. Fire a /dashboard/ request (which we ignore the result of)
    and concurrently fire several /persona/current requests. The persona
    requests must each complete well under the slow handler's sleep duration.
    """
    _seed_persona_json(
        home,
        paragraphs=[
            {
                "id": "p:001",
                "text": "Para.",
                "provenance": "heal",
                "trust_score": 3,
                "anchored_to": None,
                "locked": False,
                "merged_from": None,
            }
        ],
    )

    sleep_seconds = 5.0  # well under REQUEST_TIMEOUT_SECONDS (30s)

    def slow_index(_home: Path):
        time.sleep(sleep_seconds)
        return 200, "<html>slow</html>"

    monkeypatch.setattr(dashboard_handlers, "index", slow_index)

    with _started_server(home) as (host, port):
        # Kick off the slow request in the background; do NOT wait for it.
        slow_done = threading.Event()

        def _slow_call():
            try:
                _http_get(host, port, "/dashboard/", timeout=sleep_seconds + 5)
            except Exception:
                pass
            finally:
                slow_done.set()

        slow_thread = threading.Thread(target=_slow_call, daemon=True)
        slow_thread.start()

        # Give the slow handler a beat to enter its sleep — but not enough
        # time for it to finish.
        time.sleep(0.2)

        # Fire several persona requests; each should complete in well
        # under a second (budget is <100ms p99 per cathedral plan).
        for _ in range(3):
            t0 = time.monotonic()
            status, _h, body = _http_get(host, port, "/persona/current", timeout=2.0)
            elapsed = time.monotonic() - t0
            assert status == 200
            assert elapsed < 2.0, (
                f"/persona/current took {elapsed:.2f}s while a slow "
                f"/dashboard/ handler was wedged — thread cap or pool "
                f"is starving the persona path"
            )

        # Don't leave the slow thread blocking shutdown beyond its natural exit.
        slow_done.wait(timeout=sleep_seconds + 5)


# ---------------------------------------------------------------------------
# GAP 12: 404 on unknown route -> JSON body
# ---------------------------------------------------------------------------


def test_unknown_route_returns_json_404(home: Path, reset_server_state):
    with _started_server(home) as (host, port):
        status, headers, body = _http_get(host, port, "/random/path")
        assert status == 404
        assert "application/json" in headers.get("content-type", "")
        data = json.loads(body)
        assert data["error"] == "not found"
        assert data["path"] == "/random/path"


# ---------------------------------------------------------------------------
# GAP 13: Telemetry emitted for /dashboard/ but NOT for /persona/*
# ---------------------------------------------------------------------------


def test_telemetry_emitted_only_on_dashboard(home: Path, reset_server_state):
    _seed_persona_json(
        home,
        paragraphs=[
            {
                "id": "p:001",
                "text": "Para.",
                "provenance": "heal",
                "trust_score": 3,
                "anchored_to": None,
                "locked": False,
                "merged_from": None,
            }
        ],
    )
    events: List[Tuple[Path, str, Dict[str, Any]]] = []

    def emitter(h: Path, event_type: str, fields: Dict[str, Any]) -> None:
        events.append((h, event_type, fields))

    with _started_server(home, telemetry_emitter=emitter) as (host, port):
        # Persona endpoints — should NOT emit.
        _http_get(host, port, "/persona/current")
        _http_get(host, port, "/persona/p/p:001")
        _http_get(host, port, "/persona/p/p:nope")
        # Dashboard endpoint — should emit dashboard.launched.
        _http_get(host, port, "/dashboard/")
        # Pane endpoint — should emit dashboard.pane.viewed.
        _http_get(host, port, "/dashboard/persona-prose")
        # Telemetry is emitted after the response is sent — give the worker
        # threads a beat to flush their callbacks.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            types_now = {e[1] for e in events}
            if "dashboard.launched" in types_now and "dashboard.pane.viewed" in types_now:
                break
            time.sleep(0.02)

    types_seen = [e[1] for e in events]
    assert "dashboard.launched" in types_seen, (
        f"expected dashboard.launched in events, got {types_seen!r}"
    )
    assert "dashboard.pane.viewed" in types_seen, (
        f"expected dashboard.pane.viewed in events, got {types_seen!r}"
    )

    # Persona events MUST NOT appear under any name we recognize.
    persona_events = [t for t in types_seen if t.startswith("persona.")]
    assert persona_events == [], (
        f"telemetry leaked from /persona/*: {persona_events!r}"
    )

    # All recorded events should be dashboard.* — no other event types
    # from the /persona/* responses.
    for _h, event_type, _fields in events:
        assert event_type.startswith("dashboard."), (
            f"unexpected non-dashboard telemetry event: {event_type!r}"
        )
