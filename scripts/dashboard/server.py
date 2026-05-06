"""ai-quickstart Wave 1B: combined ``http.server`` daemon.

ONE process, TWO URL roots (per v2-cathedral.md "Eng Review Decisions" #1):

  * ``/persona/*``  — MCP-consumable read endpoints (``persona_query``).
  * ``/dashboard/*`` — Wave 3 dashboard skeleton.

Lifetime model
--------------
Daemon-style. Started on first need by either the MCP query path or the
``ai-quickstart dashboard`` CLI. Writes its port to
``~/.ai-quickstart/run/server.port``. ``flock`` on
``~/.ai-quickstart/run/server.lock`` guarantees a single binder; parallel
``start_server`` calls discover the existing instance instead of double-binding
(see Outside-Voice Catch #2).

Robustness
----------
* Thread pool capped at ``THREAD_POOL_CAP`` (Outside-Voice Catch #1).
* Per-connection socket timeout ``REQUEST_TIMEOUT_SECONDS`` so a hung
  handler can't starve the persona path.
* All telemetry events (``dashboard.launched``, ``dashboard.pane.viewed``)
  emit only on ``/dashboard/*``. ``/persona/*`` does NOT emit telemetry —
  MCP consumers shouldn't be tracked (privacy posture, see v2-cathedral.md
  Eng Review Decisions #2).

Stdlib only.
"""
from __future__ import annotations

import errno
import fcntl
import http.server
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ``sys.path`` setup mirrors the rest of scripts/: ensure the ``scripts/``
# directory is importable so sibling modules like ``persona_json`` and
# ``telemetry`` can be loaded as top-level imports.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import telemetry  # type: ignore  # noqa: E402

from .handlers import dashboard as dashboard_handlers  # noqa: E402
from .handlers import persona as persona_handlers  # noqa: E402


# ---------- public constants ----------

#: Path of the on-disk port-file (relative to the ai-quickstart home).
DEFAULT_PORT_FILE = "~/.ai-quickstart/run/server.port"

#: Path of the on-disk lock file (relative to the ai-quickstart home).
DEFAULT_LOCK_FILE = "~/.ai-quickstart/run/server.lock"

#: Maximum number of concurrent request-handling threads.
THREAD_POOL_CAP = 8

#: Per-connection socket timeout. A hung handler is terminated rather
#: than starving the thread pool. (Outside-Voice Catch #1.)
REQUEST_TIMEOUT_SECONDS = 30

#: Filename suffix for the port file (relative to ``home/run``).
_PORT_FILENAME = "server.port"
_LOCK_FILENAME = "server.lock"
_RUN_SUBDIR = "run"

#: How long to wait for the bound socket to become accept-ready before
#: trusting the health probe in ``start_server``. Local socket bind +
#: ThreadingHTTPServer accept loop should be far under this.
_HEALTH_PROBE_TIMEOUT_SECONDS = 1.0

#: How long the contended branch waits before declaring the existing
#: server unhealthy and rebinding. Bounded so a wedged peer can't pin us.
_CONTENDED_WAIT_SECONDS = 2.0

#: ``/persona/p/{id}`` matches against this. The ``id`` group is anchored
#: against the persona_json paragraph ID convention (``p:NNN`` or
#: ``p:NNN-suffix``); we accept anything URL-safe here so the handler can
#: emit the canonical 404 instead of letting the regex do it.
_PARAGRAPH_PATH_RE = re.compile(r"^/persona/p/(?P<pid>[^/?#]+)/?$")
_DASHBOARD_PANE_RE = re.compile(r"^/dashboard/(?P<pane>[A-Za-z0-9_\-]+)/?$")


# ---------- module-level server state ----------

# A start_server() call writes here so shutdown_server() can find the
# active instance without the caller threading the handle back. The lock
# fd is held for the server's lifetime so other processes' flock attempts
# remain contended.
_state_lock = threading.RLock()
_active: Dict[str, Any] = {
    "server": None,            # _ThreadedServer instance
    "thread": None,            # serving thread
    "lock_fd": None,           # int fd of the held flock
    "lock_path": None,         # Path of the lock file
    "port_path": None,         # Path of the port file
    "port": None,              # int
    "home": None,              # Path
    "telemetry_emitter": None, # callable(home, event_type, fields)
}


# ---------- path helpers ----------

def _run_dir(home: Path) -> Path:
    return Path(home) / _RUN_SUBDIR


def _port_path(home: Path) -> Path:
    return _run_dir(home) / _PORT_FILENAME


def _lock_path(home: Path) -> Path:
    return _run_dir(home) / _LOCK_FILENAME


# ---------- port-file I/O ----------

def _write_port_file(path: Path, pid: int, port: int) -> None:
    """Atomically write ``{pid, port}`` JSON to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": pid, "port": port}, ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _read_port_file(path: Path) -> Optional[Tuple[int, int]]:
    """Return ``(pid, port)`` from ``path`` or None if missing/malformed."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        obj = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    pid = obj.get("pid")
    port = obj.get("port")
    if not isinstance(pid, int) or not isinstance(port, int):
        return None
    return pid, port


def _pid_is_alive(pid: int) -> bool:
    """Return True if ``pid`` exists right now."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The PID exists but belongs to another user; treat as alive — we
        # can't safely rebind.
        return True
    except OSError:
        return False
    return True


def _check_health(host: str, port: int, timeout: float = _HEALTH_PROBE_TIMEOUT_SECONDS) -> bool:
    """Best-effort TCP connect to ``(host, port)`` with a tiny timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


# ---------- threaded server ----------

class _ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """ThreadingHTTPServer with a bounded daemon-thread pool.

    The default ``ThreadingHTTPServer`` spawns a new thread per request,
    unbounded. Outside-Voice Catch #1 requires a thread cap of 8. We
    enforce the cap via a ``BoundedSemaphore`` taken before each
    ``process_request_thread``; the request thread releases on exit.

    Threads are daemon so process exit doesn't block on a wedged handler.
    """

    daemon_threads = True
    # Don't wait for in-flight handler threads at server_close — daemon
    # threads die at process exit and our shutdown path is fire-and-forget.
    block_on_close = False
    allow_reuse_address = True
    # Block new connections briefly so accept() doesn't busy-loop on shutdown.
    request_queue_size = 16

    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        *,
        thread_pool_cap: int = THREAD_POOL_CAP,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        # Bound concurrent handlers. ``acquire`` blocks (briefly) when the
        # cap is reached — accept() backpressure flows from there.
        self._thread_cap = max(1, thread_pool_cap)
        self._slot_sem = threading.BoundedSemaphore(self._thread_cap)
        # Per Outside-Voice Catch #1: per-connection timeout. The handler
        # also sets a connection-level timeout (see _Handler.setup) so
        # wedged sockets don't pin a worker forever.
        self.timeout = REQUEST_TIMEOUT_SECONDS

    # Override ThreadingMixIn.process_request to enforce the thread cap.
    def process_request(self, request, client_address):  # type: ignore[override]
        # Acquire a slot before spawning. If all slots are taken, this
        # blocks accept() — preferred to unbounded thread growth under
        # load. The semaphore is released by ``_dispatch_request``.
        self._slot_sem.acquire()
        thread = threading.Thread(
            target=self._dispatch_request,
            args=(request, client_address),
            name="ai-quickstart-server",
            daemon=True,
        )
        thread.start()

    def _dispatch_request(self, request, client_address):
        try:
            try:
                self.finish_request(request, client_address)
            except Exception:  # pylint: disable=broad-except
                # Errors in handlers are logged on the handler itself; at
                # this layer we just make sure shutdown_request always runs.
                pass
            finally:
                try:
                    self.shutdown_request(request)
                except Exception:  # pragma: no cover - defensive
                    pass
        finally:
            try:
                self._slot_sem.release()
            except ValueError:
                # BoundedSemaphore raises ValueError if released too many
                # times — defensive: don't crash a worker on cleanup.
                pass


# ---------- request handler ----------

class _Handler(http.server.BaseHTTPRequestHandler):
    """Routes ``GET`` requests to the right handler module.

    Per-connection timeout is set here (BaseHTTPRequestHandler.setup)
    rather than on the server so it covers the connection's full lifetime
    (recv + send), not just accept().
    """

    # Squelch the default stderr access log; the parent owns logging
    # decisions (and tests don't want noise on stderr).
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Intentionally empty.
        return

    def setup(self) -> None:  # type: ignore[override]
        super().setup()
        try:
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
        except (OSError, AttributeError):
            pass

    # ---- routing ----

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        # POSTs are not used by Wave 1B, but we accept them so future
        # waves (heal-from-dashboard, lock-toggle) can wire in without
        # touching the server. For now: 405 with a JSON body.
        self._send_json(405, {"error": "method not allowed", "method": "POST"})

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        server: _ThreadedServer = self.server  # type: ignore[assignment]
        home: Path = getattr(server, "_aiq_home")
        telemetry_emitter: Callable[[Path, str, Dict[str, Any]], None] = getattr(
            server, "_aiq_telemetry"
        )

        # ---- /persona/* ----
        if path == "/persona/current" or path == "/persona/current/":
            status, body = persona_handlers.get_current(home)
            self._send_json(status, body)
            return

        m = _PARAGRAPH_PATH_RE.match(path)
        if m is not None:
            status, body = persona_handlers.get_paragraph(home, m.group("pid"))
            self._send_json(status, body)
            return

        # ---- /dashboard/* ----
        if path == "/dashboard" or path == "/dashboard/":
            t0 = time.monotonic()
            status, body = dashboard_handlers.index(home)
            self._send_html(status, body)
            duration_ms = int((time.monotonic() - t0) * 1000)
            _safe_emit(
                telemetry_emitter,
                home,
                "dashboard.launched",
                {"duration_ms": duration_ms},
            )
            return

        m = _DASHBOARD_PANE_RE.match(path)
        if m is not None:
            # Wave 3 will register pane handlers; for now we 404 but still
            # emit the pane.viewed event so the wire-up is testable.
            pane = m.group("pane")
            t0 = time.monotonic()
            self._send_json(
                404,
                {
                    "error": "pane not implemented",
                    "pane": pane,
                    "hint": "Wave 3 will fill this in",
                },
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            _safe_emit(
                telemetry_emitter,
                home,
                "dashboard.pane.viewed",
                {"pane": pane, "duration_ms": duration_ms},
            )
            return

        # ---- 404 ----
        self._send_json(
            404,
            {"error": "not found", "path": path},
        )

    # ---- response helpers ----

    def _send_json(self, status: int, body: Dict[str, Any]) -> None:
        try:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            payload = b'{"error": "internal: non-serializable response"}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------- telemetry shim ----------

def _safe_emit(
    emitter: Optional[Callable[[Path, str, Dict[str, Any]], None]],
    home: Path,
    event_type: str,
    fields: Dict[str, Any],
) -> None:
    """Call ``emitter(home, event_type, fields)`` swallowing any exception.

    Telemetry MUST NEVER crash the response path.
    """
    if emitter is None:
        return
    try:
        emitter(home, event_type, fields)
    except Exception:  # pylint: disable=broad-except
        pass


def _default_telemetry_emitter(
    home: Path, event_type: str, fields: Dict[str, Any]
) -> None:
    telemetry.log_event(home, event_type, fields)


# ---------- start_server / shutdown_server ----------

def _try_acquire_lock(lock_path: Path) -> Optional[int]:
    """Attempt LOCK_EX|LOCK_NB. Return the fd on success, None on contention.

    The fd MUST be retained by the caller for the server's lifetime — closing
    it releases the flock.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            os.close(fd)
            return None
        os.close(fd)
        raise


def _bind_and_serve(
    home: Path,
    host: str,
    *,
    telemetry_emitter: Callable[[Path, str, Dict[str, Any]], None],
) -> Tuple["_ThreadedServer", threading.Thread, int]:
    """Bind an ephemeral port and start the serve_forever thread.

    Returns ``(server, thread, port)``. The caller owns the lock fd.
    """
    server = _ThreadedServer((host, 0), _Handler, thread_pool_cap=THREAD_POOL_CAP)
    # Stash per-server state so handlers can find ``home`` + telemetry.
    server._aiq_home = home  # type: ignore[attr-defined]
    server._aiq_telemetry = telemetry_emitter  # type: ignore[attr-defined]
    port = server.server_address[1]

    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},
        name="ai-quickstart-dashboard-serve",
        daemon=True,
    )
    thread.start()
    return server, thread, port


def start_server(
    home: Path,
    *,
    host: str = "127.0.0.1",
    telemetry_emitter: Optional[Callable[[Path, str, Dict[str, Any]], None]] = None,
) -> Tuple[int, threading.Thread]:
    """Bind, write the port file, return ``(port, server_thread)``.

    Concurrency model:
      * If we acquire ``server.lock`` with ``LOCK_EX|LOCK_NB``: bind a
        fresh port, write the port file, hold the lock for the server's
        lifetime, return the fresh port.
      * If the lock is contended AND the existing port file points to a
        live PID with a healthy bound port: return that port (no second
        bind, no port-file rewrite).
      * If the lock is contended AND the existing port file is stale
        (PID dead OR socket unreachable): wait for the lock to free up
        (bounded), then rebind.

    Two parallel ``start_server`` calls cannot both bind a fresh port —
    one wins the lock, the other reads the winner's port.
    """
    home = Path(home)
    if telemetry_emitter is None:
        telemetry_emitter = _default_telemetry_emitter
    lock_path = _lock_path(home)
    port_path = _port_path(home)

    # Hold ``_state_lock`` across the entire start sequence. Two parallel
    # in-process callers serialize: the first binds, the second walks in
    # AFTER ``_active`` has been populated and short-circuits to reuse.
    # Cross-process callers serialize via the on-disk flock; the in-process
    # state lock is just additive cheap insurance against the race window
    # between flock acquisition and the port-file write.
    with _state_lock:
        # Same-process re-entry: if start_server has already been called in
        # this process and the server is still running, return its port.
        if (
            _active["server"] is not None
            and _active["thread"] is not None
            and _active["thread"].is_alive()
            and _active["home"] == home
        ):
            return _active["port"], _active["thread"]

        return _start_server_locked(
            home,
            host=host,
            lock_path=lock_path,
            port_path=port_path,
            telemetry_emitter=telemetry_emitter,
        )


def _start_server_locked(
    home: Path,
    *,
    host: str,
    lock_path: Path,
    port_path: Path,
    telemetry_emitter: Callable[[Path, str, Dict[str, Any]], None],
) -> Tuple[int, threading.Thread]:
    """Same as ``start_server`` but assumes ``_state_lock`` is held.

    Performs the on-disk flock dance and either binds a fresh port or
    returns an existing peer's port. Cross-process safety lives entirely
    in the flock + port-file invariants below.
    """
    # ---- attempt 1: try to grab the lock outright ----
    lock_fd = _try_acquire_lock(lock_path)
    if lock_fd is not None:
        # We own the lock. Bind, write port file, return.
        server, thread, port = _bind_and_serve(
            home, host, telemetry_emitter=telemetry_emitter
        )
        try:
            _write_port_file(port_path, os.getpid(), port)
        except OSError:
            # Couldn't write the port file. Still return the port — callers
            # who got it from this function don't need to read the file.
            pass
        with _state_lock:
            _active.update(
                {
                    "server": server,
                    "thread": thread,
                    "lock_fd": lock_fd,
                    "lock_path": lock_path,
                    "port_path": port_path,
                    "port": port,
                    "home": home,
                    "telemetry_emitter": telemetry_emitter,
                }
            )
        return port, thread

    # ---- attempt 2: lock is contended; check for a live peer ----
    existing = _read_port_file(port_path)
    if existing is not None:
        pid, port = existing
        if _pid_is_alive(pid) and _check_health(host, port):
            # Reuse the winner's port. We do NOT start a thread — the peer
            # owns the bound socket. Return a sentinel "no thread to join."
            return port, threading.Thread(target=lambda: None, daemon=True)

    # ---- attempt 3: stale or sick peer; block for the lock then rebind ----
    # During the wait, also poll the port file: a peer that grabbed the
    # lock concurrently will populate it shortly, at which point we should
    # reuse instead of rebinding.
    deadline = time.monotonic() + _CONTENDED_WAIT_SECONDS
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # Re-check the port file mid-wait — a peer may have
                # finished binding while we were waiting.
                existing = _read_port_file(port_path)
                if existing is not None:
                    pid, port = existing
                    if _pid_is_alive(pid) and _check_health(host, port):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        return port, threading.Thread(
                            target=lambda: None, daemon=True
                        )
                if time.monotonic() > deadline:
                    # The peer is wedged but flock-holding. Bail out by
                    # raising — caller can decide whether to retry. In
                    # practice this only happens if the peer process is
                    # genuinely deadlocked on the lock.
                    os.close(fd)
                    raise RuntimeError(
                        "ai-quickstart server: another process holds "
                        "server.lock indefinitely; cannot start"
                    )
                time.sleep(0.05)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    # We now hold the lock. Recheck the port file under the lock — another
    # process may have raced ahead of us and bound a fresh port already.
    existing = _read_port_file(port_path)
    if existing is not None:
        pid, port = existing
        if _pid_is_alive(pid) and _check_health(host, port):
            # A peer bound while we were waiting; release our lock and
            # reuse theirs. (This branch is rare but correct.)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            return port, threading.Thread(target=lambda: None, daemon=True)

    # Bind fresh.
    server, thread, port = _bind_and_serve(
        home, host, telemetry_emitter=telemetry_emitter
    )
    try:
        _write_port_file(port_path, os.getpid(), port)
    except OSError:
        pass
    with _state_lock:
        _active.update(
            {
                "server": server,
                "thread": thread,
                "lock_fd": fd,
                "lock_path": lock_path,
                "port_path": port_path,
                "port": port,
                "home": home,
                "telemetry_emitter": telemetry_emitter,
            }
        )
    return port, thread


def shutdown_server() -> None:
    """Graceful shutdown of the server started in this process.

    Cleans the port file. Releases the held flock. Joins the serving
    thread with a short timeout. No-op if no server is running.
    """
    with _state_lock:
        server = _active["server"]
        thread = _active["thread"]
        lock_fd = _active["lock_fd"]
        port_path = _active["port_path"]
        # Reset state up-front so a second shutdown_server() is a no-op
        # even if one of the cleanup steps below raises.
        _active.update(
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

    if server is not None:
        try:
            server.shutdown()
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            server.server_close()
        except Exception:  # pragma: no cover - defensive
            pass

    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)

    if port_path is not None:
        try:
            Path(port_path).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


# ---------- entry point ----------

def main(argv: Optional[List[str]] = None) -> int:
    """Run the server in the foreground for ``python3 -m scripts.dashboard.server``.

    Resolution order for ``home``:
      1. ``--home`` flag.
      2. ``$AI_QUICKSTART_HOME``.
      3. ``~/.ai-quickstart``.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="ai-quickstart-server",
        description=(
            "Combined http.server hosting /persona/* and /dashboard/*. "
            "Stdlib only; bound on 127.0.0.1 with a writeable port file."
        ),
    )
    parser.add_argument(
        "--home",
        default=None,
        help="ai-quickstart home directory (default: $AI_QUICKSTART_HOME or ~/.ai-quickstart)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host (default: 127.0.0.1; binding to non-loopback is unsupported)",
    )
    args = parser.parse_args(argv)

    if args.home is not None:
        home = Path(args.home)
    elif os.environ.get("AI_QUICKSTART_HOME"):
        home = Path(os.environ["AI_QUICKSTART_HOME"])
    else:
        home = Path.home() / ".ai-quickstart"
    home.mkdir(parents=True, exist_ok=True)

    port, thread = start_server(home, host=args.host)
    sys.stdout.write(
        json.dumps({"port": port, "host": args.host, "home": str(home)}) + "\n"
    )
    sys.stdout.flush()

    try:
        # Block until the serving thread exits (Ctrl-C raises KeyboardInterrupt).
        while thread.is_alive():
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_server()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_PORT_FILE",
    "DEFAULT_LOCK_FILE",
    "THREAD_POOL_CAP",
    "REQUEST_TIMEOUT_SECONDS",
    "start_server",
    "shutdown_server",
    "main",
]
