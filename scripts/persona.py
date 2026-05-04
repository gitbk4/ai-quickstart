#!/usr/bin/env python3
"""Persona primitive: parse, write, and append anecdotes for the persona system.

Purpose-built event-log -> summary primitive (NOT compathy-shaped).

Module surface:
  - parse_persona(path) -> {"frontmatter": dict, "prose": str}
  - write_persona(path, frontmatter, prose) -> None  (atomic, backs up)
  - append_anecdote(anecdotes_dir, project_slug, content) -> Path
  - default_persona() -> dict   (fresh frontmatter matching PLAN.md schema)
  - diff_persona(old_prose, new_prose) -> str  (unified diff)

Includes a tiny flat-YAML parser/serializer that supports:
  - scalars (str, int, bool, None, ISO-datetime as str)
  - flat lists of scalars
  - one level of nested dict (e.g. identity: {role: ..., industry: ...})

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import difflib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------- schema defaults ----------

def _utcnow_iso() -> str:
    """Return current UTC time as an ISO-8601 string with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_persona() -> Dict[str, Any]:
    """Return a fresh persona frontmatter dict with all schema fields populated.

    Schema mirrors PLAN.md "Persona schema" section exactly.
    """
    now = _utcnow_iso()
    return {
        "identity": {
            "role": "",
            "industry": "",
            "archetype": "exploring",
        },
        "goals": {
            "top_problems": [],
            "desired_outcomes": [],
        },
        "preferences": {
            "project_style": "minimal",
            "coding_languages": [],
            "skill_tolerance": "permissive",
        },
        "activity": {
            "project_count": 0,
            "total_skill_uses": 0,
            "top_projects": [],
            "last_active": now,
        },
        "generated": {
            "updated_at": now,
            "anecdote_count": 0,
            "version": 1,
        },
    }


# ---------- flat-YAML parser ----------

def _parse_scalar(v: str) -> Any:
    """Parse a scalar value: quoted string, bool, null, int, float, or bare string."""
    v = v.strip()
    if v == "":
        return ""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    # Try int (allow leading + or -)
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _parse_value(val: str, lineno: int) -> Any:
    """Parse a top-level value: list-on-one-line or scalar."""
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        if "[" in inner or "]" in inner:
            raise ValueError(
                f"frontmatter line {lineno}: nested lists not allowed"
            )
        # Split on commas; respect quotes minimally (we don't allow commas in values).
        parts = _split_top_commas(inner)
        return [_parse_scalar(p) for p in parts if p.strip()]
    return _parse_scalar(val)


def _split_top_commas(s: str) -> List[str]:
    """Split on commas not inside single or double quotes."""
    out: List[str] = []
    cur: List[str] = []
    quote: Optional[str] = None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            cur.append(ch)
            continue
        if ch == ",":
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur).strip())
    return out


def _parse_frontmatter_yaml(text: str) -> Dict[str, Any]:
    """Parse a flat-with-one-level-nesting YAML block into a dict.

    Accepts forms:
      key: scalar
      key: [a, b, c]
      key:                       # block mapping start
        subkey: scalar
        subkey: [a, b]

    Raises ValueError on malformed input.
    """
    data: Dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        lineno = i + 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        # Top-level lines must NOT be indented.
        if raw.startswith((" ", "\t")):
            raise ValueError(
                f"frontmatter line {lineno}: unexpected indentation at top level"
            )
        if ":" not in raw:
            raise ValueError(
                f"frontmatter line {lineno}: missing ':' separator"
            )
        key, _, val = raw.partition(":")
        key = key.strip()
        val_part = val.rstrip()
        if not key:
            raise ValueError(f"frontmatter line {lineno}: empty key")
        # If val is empty, look for an indented block-mapping under this key.
        if val_part.strip() == "":
            sub: Dict[str, Any] = {}
            i += 1
            while i < len(lines):
                sub_raw = lines[i]
                sub_lineno = i + 1
                if sub_raw.strip() == "" or sub_raw.lstrip().startswith("#"):
                    i += 1
                    continue
                if not sub_raw.startswith((" ", "\t")):
                    break
                # Single level of nesting only.
                sub_line = sub_raw.lstrip()
                if ":" not in sub_line:
                    raise ValueError(
                        f"frontmatter line {sub_lineno}: missing ':' separator"
                    )
                sk, _, sv = sub_line.partition(":")
                sk = sk.strip()
                if not sk:
                    raise ValueError(
                        f"frontmatter line {sub_lineno}: empty key"
                    )
                sv_stripped = sv.strip()
                if sv_stripped == "":
                    raise ValueError(
                        f"frontmatter line {sub_lineno}: nested mappings beyond one level not allowed"
                    )
                sub[sk] = _parse_value(sv_stripped, sub_lineno)
                i += 1
            data[key] = sub
            continue
        data[key] = _parse_value(val_part.strip(), lineno)
        i += 1
    return data


def _split_frontmatter(text: str) -> Tuple[Optional[str], str]:
    """Return (frontmatter_text, body) or (None, full_text) if no frontmatter."""
    if not (text.startswith("---\n") or text.startswith("---\r\n") or text == "---" or text.startswith("---")):
        return None, text
    # Robust: must start with '---' on its own line.
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end_idx = i
            break
    if end_idx is None:
        raise ValueError("frontmatter: missing closing '---' delimiter")
    fm_text = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1:])
    return fm_text, body


# ---------- flat-YAML serializer ----------

def _needs_quoting(s: str) -> bool:
    """Decide if a string scalar needs to be quoted in YAML output."""
    if s == "":
        return True
    # Quote if it contains characters that would confuse the parser, or
    # if it would otherwise be parsed as bool/null/number.
    bad_chars = set(":#[]'\"\n\t")
    if any(c in bad_chars for c in s):
        return True
    if s.strip() != s:
        return True
    if s.lower() in ("true", "false", "null", "~", "yes", "no"):
        return True
    # Looks numeric?
    try:
        int(s)
        return True
    except ValueError:
        pass
    try:
        float(s)
        return True
    except ValueError:
        pass
    return False


def _dump_scalar(v: Any) -> str:
    """Serialize a scalar to its YAML form."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        if _needs_quoting(v):
            # Use double quotes; escape embedded double quotes and backslashes.
            esc = v.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{esc}"'
        return v
    # Fallback: str() and quote
    s = str(v)
    return _dump_scalar(s)


def _dump_list(items: List[Any]) -> str:
    """Serialize a flat list of scalars as [a, b, c]."""
    return "[" + ", ".join(_dump_scalar(x) for x in items) + "]"


def _dump_value(v: Any) -> str:
    """Serialize a top-level value (scalar or flat list)."""
    if isinstance(v, list):
        return _dump_list(v)
    return _dump_scalar(v)


def _dump_frontmatter(fm: Dict[str, Any]) -> str:
    """Serialize the frontmatter dict back to flat YAML with one level of nesting."""
    out: List[str] = []
    for key, val in fm.items():
        if isinstance(val, dict):
            out.append(f"{key}:")
            for sk, sv in val.items():
                if isinstance(sv, dict):
                    raise ValueError(
                        f"frontmatter key '{key}.{sk}': nesting beyond one level not allowed"
                    )
                out.append(f"  {sk}: {_dump_value(sv)}")
        else:
            out.append(f"{key}: {_dump_value(val)}")
    return "\n".join(out) + "\n"


# ---------- public API ----------

def parse_persona(path: Path) -> Dict[str, Any]:
    """Parse a persona markdown file into {'frontmatter': dict, 'prose': str}.

    On missing file: returns minimal-valid defaults with empty prose.
    On malformed frontmatter: logs a warning to stderr and returns defaults
    with the body as prose.
    """
    path = Path(path)
    if not path.exists():
        return {"frontmatter": default_persona(), "prose": ""}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        sys.stderr.write(
            f"[persona] warning: cannot read {path}: {e}; using defaults\n"
        )
        return {"frontmatter": default_persona(), "prose": ""}
    try:
        fm_text, body = _split_frontmatter(text)
    except ValueError as e:
        sys.stderr.write(
            f"[persona] warning: malformed frontmatter in {path}: {e}; using defaults\n"
        )
        return {"frontmatter": default_persona(), "prose": text}
    if fm_text is None:
        # No frontmatter at all -> defaults, full text is prose.
        return {"frontmatter": default_persona(), "prose": text}
    try:
        fm = _parse_frontmatter_yaml(fm_text)
    except ValueError as e:
        sys.stderr.write(
            f"[persona] warning: malformed frontmatter in {path}: {e}; using defaults\n"
        )
        return {"frontmatter": default_persona(), "prose": body.lstrip("\n")}
    # Merge into defaults so missing fields are filled but parsed values win.
    merged = _merge_into_defaults(fm)
    return {"frontmatter": merged, "prose": body.lstrip("\n")}


def _merge_into_defaults(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in any missing schema fields from defaults; parsed values win."""
    base = default_persona()
    for k, v in parsed.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


def write_persona(path: Path, frontmatter: Dict[str, Any], prose: str) -> None:
    """Atomically write a persona file. Backs up any existing file to .md.bak.

    Bumps `generated.updated_at` to now and increments `generated.version`
    on the way through.
    """
    path = Path(path)
    # Mutate-safe copy so callers don't see surprise changes.
    fm = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
          for k, v in frontmatter.items()}
    gen = fm.setdefault("generated", {})
    if not isinstance(gen, dict):
        gen = {}
        fm["generated"] = gen
    gen["updated_at"] = _utcnow_iso()
    cur_version = gen.get("version", 0)
    if not isinstance(cur_version, int):
        try:
            cur_version = int(cur_version)
        except (TypeError, ValueError):
            cur_version = 0
    gen["version"] = cur_version + 1
    gen.setdefault("anecdote_count", 0)

    # Backup existing file BEFORE writing.
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        # Copy bytes so .bak is independent of the live file.
        backup.write_bytes(path.read_bytes())

    fm_block = _dump_frontmatter(fm)
    body = prose if prose.endswith("\n") or prose == "" else prose + "\n"
    content = f"---\n{fm_block}---\n{body}"

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    # Atomic write: open via os.open + os.fdopen, write, fsync, replace.
    fd = os.open(
        str(tmp_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o644,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync may fail on some filesystems; replace is still atomic.
                pass
    except Exception:
        # Clean up tmp on failure.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    os.replace(str(tmp_path), str(path))


def append_anecdote(anecdotes_dir: Path, project_slug: str, content: str) -> Path:
    """Append a timestamped anecdote entry for `project_slug`.

    Creates `{anecdotes_dir}/{project_slug}.md` if missing. Each entry gets
    a `## <ISO timestamp>` header and the content body. Returns the file path.
    """
    if not project_slug or "/" in project_slug or project_slug.startswith("."):
        raise ValueError(f"invalid project_slug: {project_slug!r}")
    anecdotes_dir = Path(anecdotes_dir)
    anecdotes_dir.mkdir(parents=True, exist_ok=True)
    target = anecdotes_dir / f"{project_slug}.md"
    ts = _utcnow_iso()
    body = content.rstrip("\n") + "\n"
    entry = f"## {ts}\n\n{body}\n"
    if target.exists():
        with open(target, "a", encoding="utf-8") as f:
            f.write(entry)
    else:
        header = f"# Anecdotes: {project_slug}\n\n"
        target.write_text(header + entry, encoding="utf-8")
    return target


def diff_persona(old_prose: str, new_prose: str) -> str:
    """Return a unified diff of two prose blocks for terminal display."""
    old_lines = old_prose.splitlines(keepends=True)
    new_lines = new_prose.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="persona.md (before)",
        tofile="persona.md (after)",
        n=3,
    )
    return "".join(diff)


# ---------- locked-paragraph protocol (lane-q, v2) ----------
#
# Mechanism choice: HTML-comment markers wrapping the locked content.
#
#     <!-- lock:start -->
#     This paragraph will not be rewritten by the heal flow.
#     <!-- lock:end -->
#
# Why HTML comments rather than a `locked_sections:` frontmatter list?
#   1. The persona frontmatter parser is flat-with-one-level-of-nesting only;
#      adding paragraph IDs or anchor lookups requires either a deeper schema
#      (rejected by ``_dump_frontmatter``) or duplicating prose verbatim in
#      frontmatter (which is what we are trying to prevent drift on).
#   2. HTML comments are valid Markdown and survive every renderer untouched.
#   3. The lock travels with the prose itself, so reordering paragraphs cannot
#      orphan a lock.
#   4. The user (or the LLM during interview) can hand-edit the markers in any
#      text editor with zero tooling.
#
# Public API:
#   * ``LOCK_START`` / ``LOCK_END`` — the literal marker strings.
#   * ``extract_locked_segments(prose)`` -> list of ``{"start", "end", "text"}``
#     dicts giving each locked region's character offsets and verbatim body.
#   * ``strip_locks_for_rewrite(prose)`` -> ``(stripped, segments)`` where
#     ``stripped`` replaces each locked region with a single placeholder line
#     so the LLM sees a structured "do not touch" hint without the original
#     text being a candidate for paraphrasing.
#   * ``restitch_locks(rewritten, segments)`` -> ``(final_prose, restored)``
#     puts the verbatim locked segments back where the placeholders sit.
#     ``restored`` is the count of segments successfully re-stitched (matches
#     ``len(segments)`` on the happy path; less if the LLM dropped placeholders).
#   * ``locked_paragraph_count(prose)`` -> int convenience wrapper.

LOCK_START = "<!-- lock:start -->"
LOCK_END = "<!-- lock:end -->"
# A unique-looking placeholder we can scan for after the LLM rewrite.
# Indexed (``{i}``) so we can correlate placeholders with their originals if
# the LLM reorders or drops some.
_LOCK_PLACEHOLDER_FMT = "<!-- ai-quickstart:locked-paragraph #{i} -->"


def _placeholder_for(index: int) -> str:
    return _LOCK_PLACEHOLDER_FMT.format(i=index)


def extract_locked_segments(prose: str) -> List[Dict[str, Any]]:
    """Return the verbatim locked segments in ``prose`` in document order.

    Each segment dict has::

        {"start": int, "end": int, "text": str}

    where ``start`` and ``end`` are character offsets pointing at the
    ``LOCK_START`` and the character just past ``LOCK_END`` respectively, and
    ``text`` is the full lock block including its markers (so re-stitching
    restores the markers verbatim).

    Malformed (unclosed) markers are tolerated: a ``LOCK_START`` with no
    matching ``LOCK_END`` is logged to stderr and the segment is dropped
    (treated as unlocked). This mirrors the codebase's existing "malformed
    frontmatter -> warn + fall back" handler.
    """
    segments: List[Dict[str, Any]] = []
    if not prose or LOCK_START not in prose:
        return segments
    cursor = 0
    while True:
        s_idx = prose.find(LOCK_START, cursor)
        if s_idx == -1:
            break
        e_idx = prose.find(LOCK_END, s_idx + len(LOCK_START))
        if e_idx == -1:
            sys.stderr.write(
                "[persona] warning: unclosed lock marker at offset "
                f"{s_idx}; treating as unlocked\n"
            )
            break
        end = e_idx + len(LOCK_END)
        segments.append({"start": s_idx, "end": end, "text": prose[s_idx:end]})
        cursor = end
    return segments


def locked_paragraph_count(prose: str) -> int:
    """Return how many lock blocks are present in ``prose``."""
    return len(extract_locked_segments(prose))


def strip_locks_for_rewrite(prose: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Replace each locked block with an indexed placeholder line.

    Returns ``(stripped, segments)`` where ``segments`` matches the result of
    :func:`extract_locked_segments` on the original prose (so callers can pass
    it straight to :func:`restitch_locks`). The placeholder line is on its
    own line and surrounded by blank lines, so the LLM is unlikely to merge
    it into adjacent paragraphs.
    """
    segments = extract_locked_segments(prose)
    if not segments:
        return prose, []
    out: List[str] = []
    cursor = 0
    for i, seg in enumerate(segments):
        out.append(prose[cursor:seg["start"]])
        out.append(_placeholder_for(i))
        cursor = seg["end"]
    out.append(prose[cursor:])
    return "".join(out), segments


def restitch_locks(
    rewritten: str,
    segments: List[Dict[str, Any]],
) -> Tuple[str, int]:
    """Put verbatim locked segments back where placeholders sit.

    Returns ``(final_prose, restored)``. ``restored`` is the count of segments
    that were successfully placed (i.e. their placeholder survived in the
    rewrite). If a placeholder is missing, that segment is appended at the
    end of the prose so no locked content is silently dropped, and a stderr
    warning is emitted.
    """
    if not segments:
        return rewritten, 0
    restored = 0
    out = rewritten
    missing: List[Dict[str, Any]] = []
    for i, seg in enumerate(segments):
        token = _placeholder_for(i)
        if token in out:
            out = out.replace(token, seg["text"], 1)
            restored += 1
        else:
            missing.append(seg)
    if missing:
        sys.stderr.write(
            f"[persona] warning: {len(missing)} locked paragraph placeholder(s) "
            "missing from rewrite; appending verbatim originals to preserve them\n"
        )
        # Append missing segments separated by blank lines, after a marker.
        suffix_parts: List[str] = []
        if not out.endswith("\n"):
            suffix_parts.append("\n")
        for seg in missing:
            suffix_parts.append("\n")
            suffix_parts.append(seg["text"])
            if not seg["text"].endswith("\n"):
                suffix_parts.append("\n")
        out = out + "".join(suffix_parts)
        # We still count appended-but-rescued segments as restored, since they
        # are present byte-for-byte in the final prose.
        restored += len(missing)
    return out, restored


__all__ = [
    "parse_persona",
    "write_persona",
    "append_anecdote",
    "default_persona",
    "diff_persona",
    "LOCK_START",
    "LOCK_END",
    "extract_locked_segments",
    "locked_paragraph_count",
    "strip_locks_for_rewrite",
    "restitch_locks",
]
