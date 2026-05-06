"""Persona-side read handlers for the Wave 1B combined http.server.

Three endpoints back this module:

  * ``GET /persona/current`` -> entire ``persona.json`` payload, with a
    ``stale: true`` flag added when the heal lock is currently held by
    another process.
  * ``GET /persona/p/{id}`` -> a single paragraph (id, text, provenance,
    trust_score, locked).

Both routes are MCP-consumable. They never block on the heal lock — the
``LOCK_SH | LOCK_NB`` probe either succeeds (no heal in flight) or fails
immediately (we serve stale + flag).

Stdlib only.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Sibling-script imports work because the server module places ``scripts/``
# on ``sys.path`` before importing this package. We import lazily-defensive
# so a missing ``persona_json`` (e.g. partial install) yields a clean 500
# rather than an import error at server boot.
try:
    import persona_json  # type: ignore
except ImportError:  # pragma: no cover - defensive
    persona_json = None  # type: ignore


# ---------- heal-lock probe ----------

# Heal lock lives at ``~/.ai-quickstart/persona/.heal.lock`` and is taken
# with LOCK_EX|LOCK_NB by ``scripts/heal.py``. We probe it with
# LOCK_SH|LOCK_NB: if the probe fails, heal is in flight and the persona.json
# on disk may be in the process of being regenerated. We still return what's
# on disk (the atomic-write pattern guarantees the file is either pre- or
# post-heal, never half-written) but tag it ``stale: true``.

_HEAL_LOCK_FILENAME = ".heal.lock"


def _heal_lock_path(home: Path) -> Path:
    return Path(home) / "persona" / _HEAL_LOCK_FILENAME


def _heal_in_progress(home: Path) -> bool:
    """Return True if another process holds the heal lock right now.

    Never blocks. Never raises. If the lock file doesn't exist, that's a
    "no heal in flight" — heal creates the file on demand.
    """
    lock_path = _heal_lock_path(home)
    if not lock_path.exists():
        return False
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        # Couldn't open the lock file at all — treat as not-in-progress so
        # the caller still serves the on-disk payload.
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as e:
            # EWOULDBLOCK on some platforms surfaces as OSError, not
            # BlockingIOError; both mean "lock contended."
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            return False
        else:
            # We acquired LOCK_SH; release it immediately so we don't
            # interfere with future heal attempts.
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------- public handlers ----------

def get_current(home: Path) -> Tuple[int, Dict[str, Any]]:
    """Return ``(status, body)`` for ``GET /persona/current``.

    Behavior:
      * If ``persona.json`` is missing -> 404 with a helpful body.
      * If the heal lock is held by another process -> serve the on-disk
        payload with ``stale: true`` added (never block).
      * Otherwise -> serve the on-disk payload as-is.

    The 404 body shape matches the rest of the server: ``{"error": str,
    "hint": str}``.
    """
    home = Path(home)
    if persona_json is None:
        return 500, {"error": "persona_json module unavailable"}

    json_path = persona_json.persona_json_path(home)
    if not json_path.exists():
        return 404, {
            "error": "no persona",
            "hint": "run ai-quickstart",
        }

    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return 500, {
            "error": "persona.json unreadable",
            "detail": str(e),
        }

    if not isinstance(payload, dict):
        return 500, {"error": "persona.json: expected top-level object"}

    if _heal_in_progress(home):
        # Mutate a shallow copy so we don't accidentally leak the flag to
        # callers reusing the dict.
        out = dict(payload)
        out["stale"] = True
        return 200, out

    return 200, payload


def get_paragraph(home: Path, paragraph_id: str) -> Tuple[int, Dict[str, Any]]:
    """Return ``(status, body)`` for ``GET /persona/p/{id}``.

    Body shape on success::

        {
          "id": "p:NNN",
          "text": "...",
          "provenance": "heal|anecdote|...",
          "trust_score": 1..5,
          "locked": bool,
          "anchored_to": str|None
        }

    404 + ``{"error": "unknown paragraph", "id": <id>}`` if not found.
    """
    home = Path(home)
    if persona_json is None:
        return 500, {"error": "persona_json module unavailable"}

    json_path = persona_json.persona_json_path(home)
    if not json_path.exists():
        return 404, {
            "error": "no persona",
            "hint": "run ai-quickstart",
        }

    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return 500, {
            "error": "persona.json unreadable",
            "detail": str(e),
        }

    paragraphs = payload.get("paragraphs") if isinstance(payload, dict) else None
    if not isinstance(paragraphs, list):
        return 500, {"error": "persona.json: paragraphs missing or malformed"}

    for entry in paragraphs:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == paragraph_id:
            body: Dict[str, Any] = {
                "id": entry.get("id"),
                "text": entry.get("text", ""),
                "provenance": entry.get("provenance"),
                "trust_score": entry.get("trust_score"),
                "locked": bool(entry.get("locked", False)),
                "anchored_to": entry.get("anchored_to"),
            }
            return 200, body

    return 404, {
        "error": "unknown paragraph",
        "id": paragraph_id,
    }


__all__ = [
    "get_current",
    "get_paragraph",
]
