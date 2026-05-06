#!/usr/bin/env python3
"""persona_json: paragraph-addressable JSON snapshot of persona.md.

This is the load-bearing schema for v2 — other consumers (suggest, eval, healing
analytics) read structured persona state from ``persona.json`` rather than
reparsing markdown. Markdown remains the user-editable source of truth; JSON is
a derived projection that is regenerated after every heal.

Schema (v1)::

    {
      "schema_version": 1,
      "generated_at": "ISO8601",
      "from_md_sha": "<sha256 of persona.md at generation time>",
      "structured": {
        "role": str|None,
        "archetype": "job|personal|exploring",
        "industry": str|None,
        "skill_tolerance": "low|medium|high"|None,
        "project_style": str|None,
        "top_projects": [{"name": str, "scaffolded_at": "ISO8601"}]
      },
      "paragraphs": [
        {
          "id": "p:001",
          "text": "...",
          "provenance": "pinned|anecdote|heal|activity-inferred|multi-hop",
          "trust_score": int,
          "anchored_to": str|None,
          "locked": bool,
          "merged_from": ["p:NNN", ...]|None
        }
      ],
      "deleted_ids": ["p:NNN", ...]
    }

Wave 1A populates ``provenance`` / ``trust_score`` with conservative defaults
(``"heal"`` / ``3``). Wave 2B will replace these with real lineage tracking.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make sibling ``persona`` importable when run as a script.
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))
import persona  # type: ignore  # noqa: E402


PERSONA_JSON_SCHEMA_VERSION = 1

PERSONA_JSON_FILE = "persona.json"
PERSONA_JSON_BAK = "persona.json.bak"
PERSONA_MD_FILE = "persona.md"
PERSONA_MD_BAK = "persona.md.bak"
PERSONA_SUBDIR = "persona"

# Default provenance / trust_score values for fresh paragraphs that have not
# yet been calibrated by the trust pipeline. ``"uncalibrated"`` is a sentinel:
# trust.tag_persona_paragraph treats it the same as a missing prior_provenance
# and applies the first-run fallthrough rule ("heal", 3). Without this
# sentinel, the lane-1A default of "heal" got mis-read by calibrate as a
# previous heal cycle and degraded fresh paragraphs to ("activity-inferred", 2).
DEFAULT_PROVENANCE = "uncalibrated"
DEFAULT_TRUST_SCORE = 3

# Allowed provenance values (kept in sync with the design doc Defined Terms).
# "uncalibrated" is the sentinel used between persona_json emission and
# trust.calibrate_paragraph_scores; it should not appear in a calibrated
# persona.json.
_ALLOWED_PROVENANCE = {
    "pinned",
    "anecdote",
    "heal",
    "activity-inferred",
    "multi-hop",
    "uncalibrated",
}


# ---------- time helper ----------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- paths ----------

def persona_dir(home: Path) -> Path:
    return Path(home) / PERSONA_SUBDIR


def persona_md_path(home: Path) -> Path:
    return persona_dir(home) / PERSONA_MD_FILE


def persona_json_path(home: Path) -> Path:
    return persona_dir(home) / PERSONA_JSON_FILE


def persona_json_bak_path(home: Path) -> Path:
    return persona_dir(home) / PERSONA_JSON_BAK


# ---------- hashing ----------

def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_text(s: str) -> str:
    return _sha256_bytes(s.encode("utf-8"))


def _paragraph_hash(text: str) -> str:
    """Whitespace-normalised content hash (matches persona._hash_paragraph_text).

    Markers are stripped before hashing so identical prose with/without an
    embedded ``<!-- p:NNN -->`` produces the same hash.
    """
    pattern = getattr(persona, "PARAGRAPH_ID_PATTERN", None)
    stripped = pattern.sub("", text) if pattern is not None else text
    norm = " ".join(stripped.split())
    return _sha256_text(norm)


# ---------- structured-section extraction ----------

_ARCHETYPE_VALUES = {"job", "personal", "exploring"}
_SKILL_TOLERANCE_MAP = {
    # v1 frontmatter uses {strict, permissive}; the v2 JSON wants
    # {low, medium, high}. We map conservatively and leave None when
    # we don't have a confident mapping.
    "strict": "low",
    "permissive": "high",
    "low": "low",
    "medium": "medium",
    "high": "high",
}


def _to_optional_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return None


def _build_structured(frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    """Project the v1 frontmatter into the v2 structured section."""
    identity = frontmatter.get("identity") or {}
    preferences = frontmatter.get("preferences") or {}
    activity = frontmatter.get("activity") or {}

    if not isinstance(identity, dict):
        identity = {}
    if not isinstance(preferences, dict):
        preferences = {}
    if not isinstance(activity, dict):
        activity = {}

    archetype_val = identity.get("archetype")
    if archetype_val not in _ARCHETYPE_VALUES:
        archetype_val = "exploring"

    raw_tol = preferences.get("skill_tolerance")
    skill_tolerance = None
    if isinstance(raw_tol, str):
        skill_tolerance = _SKILL_TOLERANCE_MAP.get(raw_tol.strip().lower())

    # top_projects in the JSON schema is a list of {name, scaffolded_at}.
    # In v1 frontmatter it's a list of bare strings (project names). We
    # preserve names and fill scaffolded_at with the activity.last_active
    # timestamp as a best-effort default; downstream waves will replace
    # this with real per-project timestamps.
    last_active = activity.get("last_active")
    if not isinstance(last_active, str) or not last_active:
        last_active = _utcnow_iso()
    top_projects: List[Dict[str, Any]] = []
    raw_top = activity.get("top_projects") or []
    if isinstance(raw_top, list):
        for item in raw_top:
            if isinstance(item, str) and item.strip():
                top_projects.append(
                    {"name": item.strip(), "scaffolded_at": last_active}
                )
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                top_projects.append(
                    {
                        "name": item["name"],
                        "scaffolded_at": item.get("scaffolded_at") or last_active,
                    }
                )

    return {
        "role": _to_optional_str(identity.get("role")),
        "archetype": archetype_val,
        "industry": _to_optional_str(identity.get("industry")),
        "skill_tolerance": skill_tolerance,
        "project_style": _to_optional_str(preferences.get("project_style")),
        "top_projects": top_projects,
    }


# ---------- paragraph splitting ----------

def _split_paragraphs(prose: str) -> List[str]:
    """Split prose on blank lines into paragraph blocks.

    Returns the trimmed-but-non-empty paragraphs, preserving their internal
    whitespace. We do NOT collapse runs of whitespace — that's the caller's
    responsibility on a per-paragraph basis if they care.
    """
    if not prose:
        return []
    out: List[str] = []
    cur: List[str] = []
    for line in prose.splitlines():
        if line.strip() == "":
            if cur:
                out.append("\n".join(cur).rstrip())
                cur = []
            continue
        cur.append(line)
    if cur:
        out.append("\n".join(cur).rstrip())
    return [p for p in out if p.strip()]


def _strip_id_marker(paragraph: str) -> tuple[Optional[str], str]:
    """Return (id_or_None, paragraph_without_marker).

    The marker MAY appear anywhere in the paragraph — we take the first match
    and strip it, normalising surrounding whitespace.
    """
    m = persona.PARAGRAPH_ID_PATTERN.search(paragraph) if hasattr(persona, "PARAGRAPH_ID_PATTERN") else None
    if m is None:
        return None, paragraph
    pid = f"p:{m.group(1)}"
    # Remove the marker and tidy whitespace.
    text = paragraph[: m.start()] + paragraph[m.end():]
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = text.strip()
    return pid, text


# ---------- public API ----------

def generate_from_md(
    md_path: Path,
    prior_json_path: Optional[Path],
) -> Dict[str, Any]:
    """Build the persona.json payload from persona.md (+ optional prior .json).

    Behavior:
      * If ``md_path`` is missing, returns a payload with empty paragraphs and
        defaults-only structured section.
      * Reads prior persona.json if provided so paragraph IDs stay stable
        across regeneration. Hash-fallback is used to recover stability when
        the user has edited the .md and removed/duplicated markers.
      * Records IDs that disappeared since the prior payload in
        ``deleted_ids``; this list is consumed by downstream tools and reset
        on the next clean cycle.
    """
    md_path = Path(md_path)
    prior_json: Dict[str, Any] = {}
    if prior_json_path is not None and Path(prior_json_path).exists():
        try:
            prior_json = json.loads(
                Path(prior_json_path).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            prior_json = {}

    prior_paragraphs = prior_json.get("paragraphs") or []
    if not isinstance(prior_paragraphs, list):
        prior_paragraphs = []
    # Map prior text-hash -> id so we can preserve IDs when markers vanished.
    prior_hash_to_id: Dict[str, str] = {}
    prior_id_to_text: Dict[str, str] = {}
    prior_id_meta: Dict[str, Dict[str, Any]] = {}
    for entry in prior_paragraphs:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("id")
        text = entry.get("text")
        if not isinstance(pid, str) or not isinstance(text, str):
            continue
        prior_id_to_text[pid] = text
        prior_id_meta[pid] = entry
        prior_hash_to_id.setdefault(_paragraph_hash(text), pid)

    # Read persona.md.
    if not md_path.exists():
        md_text = ""
        frontmatter: Dict[str, Any] = persona.default_persona()
        prose = ""
    else:
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except OSError:
            md_text = ""
        parsed = persona.parse_persona(md_path) if md_text else {
            "frontmatter": persona.default_persona(),
            "prose": "",
        }
        frontmatter = parsed["frontmatter"]
        prose = parsed["prose"] or ""

    paragraphs_raw = _split_paragraphs(prose)

    # First pass: pull out existing IDs already embedded in the markdown.
    items: List[Dict[str, Any]] = []  # {pid: Optional[str], text: str}
    for raw in paragraphs_raw:
        pid, text = _strip_id_marker(raw)
        # Skip "paragraphs" that were only an ID marker with no prose.
        if not text.strip():
            continue
        items.append({"pid": pid, "text": text})

    # Second pass: hash-fallback for items missing an ID.
    used_ids: set = {it["pid"] for it in items if it["pid"]}
    for it in items:
        if it["pid"] is None:
            h = _paragraph_hash(it["text"])
            recovered = prior_hash_to_id.get(h)
            if recovered and recovered not in used_ids:
                it["pid"] = recovered
                used_ids.add(recovered)

    # Third pass: assign fresh IDs to anything still unassigned.
    next_id = _next_free_id(used_ids)
    for it in items:
        if it["pid"] is None:
            it["pid"] = next_id
            used_ids.add(next_id)
            next_id = _bump_id(next_id, used_ids)

    # Build paragraph entries.
    paragraphs_out: List[Dict[str, Any]] = []
    seen_ids_this_run: set[str] = set()
    for it in items:
        pid = it["pid"]
        text = it["text"]
        if pid in seen_ids_this_run:
            # Hash collision case where two paragraphs grabbed the same ID
            # via prior-hash recovery — give the second one a fresh ID.
            pid = _next_free_id(used_ids)
            used_ids.add(pid)
        seen_ids_this_run.add(pid)
        meta = prior_id_meta.get(pid, {})
        entry: Dict[str, Any] = {
            "id": pid,
            "text": text,
            "provenance": meta.get("provenance") if meta.get("provenance") in _ALLOWED_PROVENANCE else DEFAULT_PROVENANCE,
            "trust_score": meta.get("trust_score") if isinstance(meta.get("trust_score"), int) else DEFAULT_TRUST_SCORE,
            "anchored_to": meta.get("anchored_to") if isinstance(meta.get("anchored_to"), str) else None,
            "locked": bool(meta.get("locked", False)),
            "merged_from": None,
        }
        # If the prior entry had a merged_from list, preserve it (still
        # valid because the merge survived this regen).
        if isinstance(meta.get("merged_from"), list):
            entry["merged_from"] = list(meta["merged_from"])
        paragraphs_out.append(entry)

    # Compute deleted_ids (prior paragraphs that no longer appear).
    current_ids = {p["id"] for p in paragraphs_out}
    deleted_ids: List[str] = sorted(
        pid for pid in prior_id_to_text.keys() if pid not in current_ids
    )

    structured = _build_structured(frontmatter)

    payload = {
        "schema_version": PERSONA_JSON_SCHEMA_VERSION,
        "generated_at": _utcnow_iso(),
        "from_md_sha": _sha256_text(md_text) if md_text else _sha256_text(""),
        "structured": structured,
        "paragraphs": paragraphs_out,
        "deleted_ids": deleted_ids,
    }
    return payload


def _id_int(pid: str) -> int:
    """Extract the integer counter from ``p:NNN`` (or ``p:NNN-X``)."""
    body = pid[2:] if pid.startswith("p:") else pid
    # Allow disambiguator suffixes like ``-a`` for hash-collision cases.
    digits = []
    for ch in body:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    return int("".join(digits)) if digits else 0


def _format_id(n: int) -> str:
    return f"p:{n:03d}"


def _next_free_id(used: set[str]) -> str:
    """Smallest unused ``p:NNN`` strictly greater than the current max."""
    n = 1
    if used:
        n = max(_id_int(pid) for pid in used) + 1
    while _format_id(n) in used:
        n += 1
    return _format_id(n)


def _bump_id(prev: str, used: set[str]) -> str:
    n = _id_int(prev) + 1
    while _format_id(n) in used:
        n += 1
    return _format_id(n)


def write_persona_json(home: Path, payload: Dict[str, Any]) -> None:
    """Atomically write persona.json with a .bak snapshot of the prior file.

    Sequence:
      1. If persona.json exists, copy its bytes to persona.json.bak.
      2. Write the payload to a sibling tmp file.
      3. fsync + os.replace to persona.json.

    On any error mid-flight, the tmp is cleaned up and the original file is
    untouched (the .bak is left in place if it was created).
    """
    home = Path(home)
    pdir = persona_dir(home)
    pdir.mkdir(parents=True, exist_ok=True)
    target = persona_json_path(home)
    bak = persona_json_bak_path(home)

    # Snapshot prior file BEFORE writing.
    if target.exists():
        try:
            bak.write_bytes(target.read_bytes())
        except OSError as e:
            sys.stderr.write(
                f"[persona_json] warning: failed to write .bak: {e}\n"
            )

    body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".tmp-",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # Some filesystems don't support fsync; replace is still atomic.
                pass
        os.chmod(tmp_path, 0o644)
        os.replace(str(tmp_path), str(target))
    except Exception:
        # Leave tmp in place so callers can inspect it; only swallow OSError
        # cleanup failures. (Per the v1 atomic-write tests, tmp left after a
        # mid-write OSError is the documented behavior.)
        raise


def read_persona_json(home: Path) -> Optional[Dict[str, Any]]:
    """Read persona.json or return None if missing.

    Raises ``ValueError`` on schema-version mismatch so callers can decide
    whether to migrate or abort.
    """
    target = persona_json_path(home)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"persona.json unreadable: {e}") from e
    if not isinstance(payload, dict):
        raise ValueError("persona.json: expected top-level object")
    schema_version = payload.get("schema_version")
    if schema_version != PERSONA_JSON_SCHEMA_VERSION:
        raise ValueError(
            f"persona.json schema_version mismatch: "
            f"got {schema_version!r}, expected {PERSONA_JSON_SCHEMA_VERSION}"
        )
    return payload


def migrate_md_to_json(home: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """One-shot migration: build persona.json from existing persona.md.

    Idempotent: if persona.json already exists and matches what we'd
    generate (modulo ``generated_at``), no write happens.

    Backups: the existing persona.md is snapshotted to persona.md.bak before
    any mutation. Aborts with a stderr warning and returns ``{"ok": False}``
    if persona.md is malformed.
    """
    home = Path(home)
    md_path = persona_md_path(home)
    json_path = persona_json_path(home)
    md_bak_path = persona_dir(home) / PERSONA_MD_BAK

    result: Dict[str, Any] = {
        "ok": False,
        "wrote_json": False,
        "wrote_md_bak": False,
        "dry_run": dry_run,
    }

    if not md_path.exists():
        sys.stderr.write(
            f"[persona_json] migrate: persona.md not found at {md_path}\n"
        )
        return result

    # Detect malformed v1 persona by parsing it the same way the rest of the
    # system does; if the parser falls back to defaults AND the file has
    # frontmatter delimiters, that's an unrecoverable migration.
    try:
        md_text = md_path.read_text(encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"[persona_json] migrate: cannot read {md_path}: {e}\n")
        return result

    if not _is_migratable(md_text):
        sys.stderr.write(
            f"[persona_json] migrate: persona.md at {md_path} appears malformed; "
            f"refusing to write persona.json. Fix the markdown and re-run.\n"
        )
        return result

    payload = generate_from_md(md_path, json_path if json_path.exists() else None)

    # Idempotency check: if persona.json exists and matches modulo generated_at,
    # treat the call as a no-op.
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and _payload_data_equals(existing, payload):
            result["ok"] = True
            result["wrote_json"] = False
            return result

    if dry_run:
        result["ok"] = True
        return result

    # Make persona.md.bak BEFORE any mutation (the migration is itself a
    # mutation in the user's eyes — it derives a new file from .md).
    persona_dir(home).mkdir(parents=True, exist_ok=True)
    if not md_bak_path.exists():
        try:
            md_bak_path.write_bytes(md_path.read_bytes())
            result["wrote_md_bak"] = True
        except OSError as e:
            sys.stderr.write(
                f"[persona_json] migrate: failed to create .bak: {e}\n"
            )
            return result

    write_persona_json(home, payload)
    result["ok"] = True
    result["wrote_json"] = True
    return result


def _payload_data_equals(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Compare two payloads ignoring volatile fields (generated_at)."""
    def _normalise(p: Dict[str, Any]) -> Dict[str, Any]:
        c = dict(p)
        c.pop("generated_at", None)
        return c
    return _normalise(a) == _normalise(b)


def _is_migratable(md_text: str) -> bool:
    """Decide whether persona.md can be safely projected into JSON.

    A file is considered migratable if either:
      * it has no frontmatter delimiters at all (treated as pure prose), OR
      * it has well-formed v1 frontmatter the persona.parse_persona path
        does NOT warn on.

    A file with opening ``---`` but no closing ``---`` is NOT migratable.
    """
    if "---" not in md_text:
        return True
    lines = md_text.splitlines(keepends=True)
    if not lines:
        return True
    first = lines[0].rstrip("\r\n")
    if first != "---":
        # File has --- but not as a frontmatter delimiter; treat as prose.
        return True
    # Look for closing ---.
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return True
    return False


__all__ = [
    "PERSONA_JSON_SCHEMA_VERSION",
    "PERSONA_JSON_FILE",
    "DEFAULT_PROVENANCE",
    "DEFAULT_TRUST_SCORE",
    "persona_dir",
    "persona_md_path",
    "persona_json_path",
    "persona_json_bak_path",
    "generate_from_md",
    "write_persona_json",
    "read_persona_json",
    "migrate_md_to_json",
]
