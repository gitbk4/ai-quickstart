"""ai-quickstart Wave 1C: privacy-respecting telemetry foundation.

This module provides the foundation for opt-in, anonymous, fire-and-forget
telemetry. Wave 1C ships the foundation only — actual call sites are wired
in by Wave 1B (dashboard.*), Wave 2 (suggestion.*, persona.lock.*), and
Wave 3 (full integration).

Privacy posture
---------------
* ``anonymous_id`` is per-install random, NEVER tied to user/repo/path.
  The raw bytes live in ``~/.ai-quickstart/persona/.id`` (chmod 0600);
  the value sent on the wire is ``sha256(bytes).hex()[:16]``.
* If the user deletes ``.id``, a fresh id is generated on next read.
* Outgoing event records: ``{event_type, ts, anonymous_id, version, fields}``
  ONLY. Never persona content, never project paths, never filenames,
  never user-typed prose.
* Local ``activity.jsonl`` always records (it's the user's own dashboard
  source). Network POST happens only when ``opt_in_status == 'opted_in'``.
* Endpoint outage / DNS failure / non-2xx -> swallow exception, retain batch.
  Never block the user. ``log_event`` and ``flush_aggregated`` NEVER raise.

Allowed fields per event type (KEEP THIS COMMENT IN SYNC)
---------------------------------------------------------
* persona.heal.started:       {trigger: "auto"|"manual"|"threshold"}
* persona.heal.committed:     {paragraph_count: int, locked_count: int,
                               duration_ms: int}
* persona.lock.added:         {paragraph_index: int}
* persona.lock.removed:       {paragraph_index: int}
* suggestion.surfaced:        {category: str, rank: int}
* suggestion.alternative.clicked: {category: str, alternative_rank: int}
* dashboard.launched:         {duration_ms: int}
* dashboard.pane.viewed:      {pane: str, duration_ms: int}

ALLOWED VALUE TYPES: int, bool, float, short ASCII enum strings.
NEVER: free-form strings, paths, file names, user prose, dict/list values
that can carry arbitrary content.

Endpoint
--------
``https://gitbk4.dev/telemetry/ai-quickstart/v1/events`` — see
``v2-cathedral.md`` "Eng Review Decisions" #2. Outside-Voice Catches #1
notes there is no kill plan for the domain; this module's defense is to
swallow ALL network exceptions and retain on-disk batches. If the domain
is unreachable forever, batches accumulate (capped at 100/day per
``queue_for_aggregation``); rotation is the user's persona dir hygiene
job, not ours.

Stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Public constants.
# ---------------------------------------------------------------------------

TELEMETRY_ENDPOINT = "https://gitbk4.dev/telemetry/ai-quickstart/v1/events"

# Matches scripts/heal.py and scripts/hook_runner.py: POSIX atomic-append
# guarantees only hold for writes <= PIPE_BUF (~4096 bytes). NEVER exceed.
ACTIVITY_LINE_MAX = 4096

# Wave 1C event taxonomy (see v2-cathedral.md "Initial event taxonomy").
EVENT_TYPES = frozenset(
    [
        "persona.heal.started",
        "persona.heal.committed",
        "persona.lock.added",
        "persona.lock.removed",
        "suggestion.surfaced",
        "suggestion.alternative.clicked",
        "dashboard.launched",
        "dashboard.pane.viewed",
    ]
)

# Telemetry schema version. Bumped when the wire format changes
# (event_type names, field shapes, anonymous_id derivation).
TELEMETRY_VERSION = "v1"

# Network timeout for POSTs. Performance Budgets table: telemetry POST is
# fire-and-forget, 2s urllib timeout.
_URLOPEN_TIMEOUT_SECONDS = 2.0

# Daily/event-count caps for the on-disk batch queue. Rotation policy is
# "current batch closes at midnight UTC OR at 100 events, whichever first."
_BATCH_MAX_EVENTS = 100

# Filenames under ~/.ai-quickstart/.
_OPT_IN_FILE = ".telemetry-opt-in"
_ANON_ID_FILE = ".id"  # under persona/, see _anon_id_path
_PENDING_DIR = ".pending-telemetry"  # under persona/, see _pending_dir

# Internal: bytes of randomness for the raw anonymous id seed. 32 bytes is
# overkill for sha256 input but cheap and future-proof.
_ANON_ID_SEED_BYTES = 32


# ---------------------------------------------------------------------------
# Path helpers (intentionally local — we don't import paths.py to keep this
# module standalone-testable and to avoid the runtime-detection surface).
# ---------------------------------------------------------------------------


def _persona_dir(home: Path) -> Path:
    return Path(home) / "persona"


def _activity_path(home: Path) -> Path:
    return _persona_dir(home) / "activity.jsonl"


def _opt_in_path(home: Path) -> Path:
    return Path(home) / _OPT_IN_FILE


def _anon_id_path(home: Path) -> Path:
    return _persona_dir(home) / _ANON_ID_FILE


def _pending_dir(home: Path) -> Path:
    return _persona_dir(home) / _PENDING_DIR


# ---------------------------------------------------------------------------
# Time helpers.
# ---------------------------------------------------------------------------


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _utcnow_date_key() -> str:
    return _utcnow().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Atomic write helper. Reused for opt-in file + batch closing.
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    """Write ``payload`` to ``path`` atomically via tmp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Anonymous ID.
# ---------------------------------------------------------------------------


def get_or_create_anonymous_id(home: Path) -> str:
    """Return a stable per-install anonymous id.

    The raw seed bytes are persisted at ``~/.ai-quickstart/persona/.id``
    (chmod 0600). The returned value is ``sha256(bytes).hex()[:16]`` — 16
    hex chars, ~64 bits of identity.

    Idempotent: calling twice on the same home returns the same id.
    Regenerating: if ``.id`` is deleted, a fresh seed is generated on the
    next call (i.e. a new anonymous identity).

    This id is NEVER derived from user, hostname, repo, file path, or any
    other identifying signal — it's pure random.
    """
    path = _anon_id_path(home)
    seed: Optional[bytes] = None
    if path.exists():
        try:
            seed = path.read_bytes()
        except OSError:
            seed = None
    if not seed:
        seed = secrets.token_bytes(_ANON_ID_SEED_BYTES)
        try:
            _atomic_write_bytes(path, seed, mode=0o600)
        except OSError:
            # If we can't persist the seed, we can still derive an id for
            # this call — but it won't be stable across calls. Best-effort.
            pass
    return hashlib.sha256(seed).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Opt-in / opt-out persistence.
# ---------------------------------------------------------------------------

OPT_IN = "opted_in"
OPT_OUT = "opted_out"
UNPROMPTED = "unprompted"


def opt_in_status(home: Path) -> str:
    """Return ``'opted_in'``, ``'opted_out'``, or ``'unprompted'``.

    Reads ``~/.ai-quickstart/.telemetry-opt-in`` (a JSON file with shape
    ``{decision: bool, timestamp: iso8601}``). Missing file or malformed
    contents -> ``'unprompted'``.
    """
    path = _opt_in_path(home)
    if not path.exists():
        return UNPROMPTED
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return UNPROMPTED
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return UNPROMPTED
    if not isinstance(obj, dict):
        return UNPROMPTED
    decision = obj.get("decision")
    if decision is True:
        return OPT_IN
    if decision is False:
        return OPT_OUT
    return UNPROMPTED


def set_opt_in(home: Path, decision: bool) -> None:
    """Persist the user's opt-in decision atomically.

    Writes ``{decision: bool, timestamp: iso8601}`` to
    ``~/.ai-quickstart/.telemetry-opt-in`` via tmp + ``os.replace``.
    """
    record = {
        "decision": bool(decision),
        "timestamp": _utcnow_iso(),
    }
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_bytes(_opt_in_path(home), payload, mode=0o600)


def opt_in_prompt() -> bool:
    """Interactive first-run prompt. Returns True (opt in) / False (opt out).

    Privacy-first default: empty input -> False. The wording must be honest;
    we describe exactly what is sent (event_type names, anonymous_id shape,
    version, allowed field types) and what is NEVER sent (persona content,
    project paths, filenames, prose).
    """
    print(
        "ai-quickstart anonymous telemetry (opt-in)\n"
        "----------------------------------------\n"
        "If you opt in, we send these per event:\n"
        "  - event_type (one of 8 fixed names: persona.heal.started,\n"
        "    persona.heal.committed, persona.lock.added, persona.lock.removed,\n"
        "    suggestion.surfaced, suggestion.alternative.clicked,\n"
        "    dashboard.launched, dashboard.pane.viewed)\n"
        "  - timestamp (UTC ISO 8601)\n"
        "  - anonymous_id (random 16 hex chars; never tied to user/repo/path)\n"
        "  - version (telemetry schema version)\n"
        "  - fields (small int/bool counters per event type — see telemetry.py)\n"
        "\n"
        "We NEVER send:\n"
        "  - your persona content, project paths, filenames\n"
        "  - any text you type, any code, any directory contents\n"
        "  - your IP, hostname, OS user (only what your TLS connection reveals)\n"
        "\n"
        "Opt out and your local activity log still works for your own dashboard.\n"
        "Endpoint: " + TELEMETRY_ENDPOINT + "\n"
        "\n"
        "Opt in? [y/N]: ",
        end="",
        flush=True,
    )
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Event recording: log_event (always-local) + queue_for_aggregation (POST).
# ---------------------------------------------------------------------------


def _serialize_capped(record: Dict[str, Any]) -> bytes:
    """Serialize ``record`` as a single JSONL line within ACTIVITY_LINE_MAX.

    Strategy when over the cap:
      1. Drop ``fields`` to ``{}`` and add ``_truncated: true``.
      2. If still over, hard byte-truncate (defensive — should not happen
         given our schema and 4 KiB cap).

    Always returns a payload that ends in ``\\n`` and is ``<= ACTIVITY_LINE_MAX``.
    """
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    encoded = line.encode("utf-8") + b"\n"
    if len(encoded) <= ACTIVITY_LINE_MAX:
        return encoded

    # Truncate: drop fields, add a flag.
    candidate = dict(record)
    candidate["fields"] = {}
    candidate["_truncated"] = True
    line = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
    encoded = line.encode("utf-8") + b"\n"
    if len(encoded) <= ACTIVITY_LINE_MAX:
        return encoded

    # Last-resort hard byte truncation (should never happen with our schema).
    return encoded[: ACTIVITY_LINE_MAX - 1] + b"\n"


def _append_atomic(path: Path, payload: bytes) -> None:
    """O_APPEND-based atomic append. ``payload`` MUST be <= PIPE_BUF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(str(path), flags, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _build_event_record(
    event_type: str,
    fields: Optional[Dict[str, Any]],
    anonymous_id: str,
) -> Dict[str, Any]:
    """Build the canonical event record. Same shape on disk and on the wire."""
    return {
        "ts": _utcnow_iso(),
        "event_type": event_type,
        "anonymous_id": anonymous_id,
        "version": TELEMETRY_VERSION,
        "fields": dict(fields) if fields else {},
    }


def log_event(
    home: Path,
    event_type: str,
    fields: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a JSONL line to ``~/.ai-quickstart/persona/activity.jsonl``.

    Best-effort: NEVER raises. NEVER blocks. NEVER exceeds
    ``ACTIVITY_LINE_MAX`` bytes per line. Unknown ``event_type`` is silently
    dropped (defensive — Wave 2/3 callers might typo a name; we don't want
    to crash them).

    If the user is opted in, the event is also queued in
    ``.pending-telemetry/`` for the next ``flush_aggregated`` POST.
    """
    try:
        if event_type not in EVENT_TYPES:
            # Unknown event types are silently dropped — privacy AND safety:
            # we don't want a typo'd name to land on the wire as an unknown
            # field-shape leak.
            return

        anon_id = get_or_create_anonymous_id(home)
        record = _build_event_record(event_type, fields, anon_id)
        payload = _serialize_capped(record)
        _append_atomic(_activity_path(home), payload)

        # If opted in, also queue for POST. Never raise from queueing.
        if opt_in_status(home) == OPT_IN:
            try:
                queue_for_aggregation(home, record)
            except Exception:  # pylint: disable=broad-except
                pass
    except Exception:  # pylint: disable=broad-except
        # log_event is best-effort: a disk-full / permission / OS error
        # MUST NEVER bubble up and crash a user-facing flow.
        return


# ---------------------------------------------------------------------------
# Pending-batch queue + POST.
# ---------------------------------------------------------------------------


def _current_batch_path(home: Path) -> Path:
    """Path of the currently open batch file (rotates daily by date stamp)."""
    return _pending_dir(home) / f"batch-{_utcnow_date_key()}.jsonl"


def _list_batch_files(home: Path) -> List[Path]:
    """All batch files (closed + current) in deterministic order."""
    d = _pending_dir(home)
    if not d.exists():
        return []
    try:
        return sorted(d.glob("batch-*.jsonl"))
    except OSError:
        return []


def _count_batch_events(path: Path) -> int:
    """Best-effort line count for the batch file."""
    if not path.exists():
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def queue_for_aggregation(home: Path, event_record: Dict[str, Any]) -> None:
    """Append ``event_record`` to the current pending batch.

    A batch rotates when:
      * the date stamp in the filename rolls over (UTC midnight), OR
      * the current batch reaches ``_BATCH_MAX_EVENTS`` events.

    Whichever comes first. Rotation here is implicit: the daily filename
    just changes; the 100-event cap is enforced by writing the overflowing
    event into a suffixed file so we never silently drop.

    Best-effort: failure to queue must not bubble up. Caller (``log_event``)
    already wraps this in try/except.
    """
    line = json.dumps(event_record, ensure_ascii=False, separators=(",", ":"))
    payload = line.encode("utf-8") + b"\n"
    # Apply the same per-line cap as activity.jsonl. Wire format is the
    # same record shape, so this is consistent.
    if len(payload) > ACTIVITY_LINE_MAX:
        capped = dict(event_record)
        capped["fields"] = {}
        capped["_truncated"] = True
        payload = (
            json.dumps(capped, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )

    base = _current_batch_path(home)
    target = base
    # If the current batch is full, append a numeric suffix until we find
    # one with room. Bounded loop: if we somehow have 1000 same-day overflow
    # batches (~100k events in one day), we stop trying and silently drop —
    # the privacy posture is more important than the metric.
    if _count_batch_events(target) >= _BATCH_MAX_EVENTS:
        for suffix in range(1, 1000):
            candidate = base.parent / f"{base.stem}.{suffix}.jsonl"
            if _count_batch_events(candidate) < _BATCH_MAX_EVENTS:
                target = candidate
                break
        else:
            return

    _append_atomic(target, payload)


def _post_batch(path: Path) -> Optional[str]:
    """POST the contents of ``path`` to ``TELEMETRY_ENDPOINT``.

    Returns ``None`` on success, or a short error string on any failure
    (network, DNS, non-2xx status, JSON encode error, etc.).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            events: List[Dict[str, Any]] = []
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    # Malformed line: skip; don't fail the whole batch.
                    continue
                events.append(obj)
        if not events:
            return None  # empty batch: treat as success so we delete it
        body = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    except OSError as e:
        return f"read-failed: {e}"

    req = urllib.request.Request(
        TELEMETRY_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"ai-quickstart-telemetry/{TELEMETRY_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_URLOPEN_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                # Older shim: getcode().
                status = resp.getcode() if hasattr(resp, "getcode") else 0
            if not (200 <= int(status) < 300):
                return f"http-{status}"
            return None
    except urllib.error.HTTPError as e:
        return f"http-{e.code}"
    except urllib.error.URLError as e:
        return f"urlerror: {e.reason}"
    except (socket.timeout, TimeoutError):
        return "timeout"
    except Exception as e:  # pylint: disable=broad-except
        return f"error: {type(e).__name__}"


def flush_aggregated(home: Path) -> Dict[str, Any]:
    """POST every pending batch; delete on success, retain on failure.

    Returns a summary dict::

        {"sent": <int>, "retained": <int>, "errors": [<str>, ...]}

    where ``sent`` is the count of batch FILES (not events) successfully
    delivered, ``retained`` is the count still on disk, and ``errors``
    lists short failure descriptors.

    NEVER raises. If the network is down, the user sees retained > 0 and
    can retry later; nothing here blocks the user's session.
    """
    sent = 0
    retained = 0
    errors: List[str] = []
    try:
        if opt_in_status(home) != OPT_IN:
            # Defensive: if the user toggled off between queueing and flush,
            # we keep the batches on disk but don't POST. They'll be sent
            # only after re-opt-in.
            batches = _list_batch_files(home)
            return {"sent": 0, "retained": len(batches), "errors": []}

        batches = _list_batch_files(home)
        # Skip the current (still-open) batch so we don't lose in-progress
        # writes mid-flush. Identified by today's date stamp filename.
        today_path = _current_batch_path(home)
        for batch in batches:
            if batch == today_path:
                # Don't POST the still-open batch — wait for tomorrow.
                retained += 1
                continue
            err = _post_batch(batch)
            if err is None:
                try:
                    batch.unlink()
                    sent += 1
                except OSError as e:
                    # Couldn't delete after success: count as sent but warn.
                    errors.append(f"delete-failed: {e}")
                    sent += 1
            else:
                retained += 1
                errors.append(err)
    except Exception as e:  # pylint: disable=broad-except
        # Hard outer guard: even if listing the directory raises, return a
        # sane shape. flush is meant to be called from a non-blocking
        # background context and must never raise.
        errors.append(f"flush-failed: {type(e).__name__}")
        try:
            print(f"ai-quickstart telemetry: flush failed: {e}", file=sys.stderr)
        except Exception:  # pylint: disable=broad-except
            pass
    return {"sent": sent, "retained": retained, "errors": errors}
