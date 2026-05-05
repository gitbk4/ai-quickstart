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
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------- paragraph ID helpers (Wave 1A) ----------
#
# Paragraphs in persona.md may carry a stable identifier embedded in an HTML
# comment of the form ``<!-- p:NNN -->`` (3+ digit counter). The marker can
# appear anywhere inside the paragraph but conventionally sits at the very
# beginning. IDs survive heal regenerations so downstream consumers (suggest,
# eval, telemetry) can refer to specific paragraphs.

PARAGRAPH_ID_PATTERN = re.compile(r"<!--\s*p:(\d{3,})\s*-->")
_PARAGRAPH_ID_TEMPLATE = "<!-- p:{n:03d} -->"


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


# ---------- paragraph splitting / ID helpers ----------

def _split_paragraphs_with_offsets(prose: str) -> List[Tuple[int, int, str]]:
    """Split prose on blank-line separators.

    Returns a list of (start_offset, end_offset, paragraph_text) tuples where
    offsets are character positions within ``prose``. The text preserves
    internal layout (no rstrip of internal lines) but trailing whitespace on
    the paragraph as a whole is trimmed.
    """
    out: List[Tuple[int, int, str]] = []
    if not prose:
        return out
    n = len(prose)
    i = 0
    while i < n:
        # Skip leading blank lines.
        while i < n:
            line_end = prose.find("\n", i)
            if line_end == -1:
                line_end = n
            line = prose[i:line_end]
            if line.strip() == "":
                i = line_end + 1 if line_end < n else n
                continue
            break
        if i >= n:
            break
        start = i
        # Walk forward until a blank line or EOF.
        while i < n:
            line_end = prose.find("\n", i)
            if line_end == -1:
                line_end = n
            line = prose[i:line_end]
            if line.strip() == "":
                break
            i = line_end + 1 if line_end < n else n
        end = i
        text = prose[start:end].rstrip()
        if text:
            out.append((start, end, text))
        # Skip the trailing blank-line separator.
        while i < n:
            line_end = prose.find("\n", i)
            if line_end == -1:
                line_end = n
            line = prose[i:line_end]
            if line.strip() != "":
                break
            i = line_end + 1 if line_end < n else n
    return out


def _hash_paragraph_text(text: str) -> str:
    """Stable content hash. We strip the ID marker (if any) before hashing so
    the same prose carries the same hash regardless of which marker variant
    is currently embedded."""
    stripped = PARAGRAPH_ID_PATTERN.sub("", text)
    # Collapse internal whitespace conservatively: every run of ws becomes a
    # single space. This prevents trivial reformat (e.g. line wrapping) from
    # invalidating the content hash.
    norm = " ".join(stripped.split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def extract_paragraph_ids(md_text: str) -> Dict[str, str]:
    """Return ``{id: paragraph_text}`` for every ``<!-- p:NNN -->`` marker.

    Operates on the prose body of ``md_text`` (frontmatter, if any, is
    skipped). The returned text is the paragraph with the marker stripped.
    """
    body = md_text
    if md_text.startswith("---"):
        try:
            _fm, body = _split_frontmatter(md_text)
        except ValueError:
            # Malformed frontmatter: scan the whole text for markers anyway.
            body = md_text
    out: Dict[str, str] = {}
    for _start, _end, para in _split_paragraphs_with_offsets(body):
        m = PARAGRAPH_ID_PATTERN.search(para)
        if not m:
            continue
        pid = f"p:{m.group(1)}"
        cleaned = PARAGRAPH_ID_PATTERN.sub("", para, count=1).strip()
        out[pid] = cleaned
    return out


def _next_id(used: set) -> str:
    """Smallest unused p:NNN strictly greater than current max."""
    if not used:
        return "p:001"
    nums = []
    for pid in used:
        if pid.startswith("p:"):
            digits = []
            for ch in pid[2:]:
                if ch.isdigit():
                    digits.append(ch)
                else:
                    break
            if digits:
                nums.append(int("".join(digits)))
    n = max(nums) + 1 if nums else 1
    while f"p:{n:03d}" in used:
        n += 1
    return f"p:{n:03d}"


def assign_paragraph_ids(
    md_text: str, prior_ids: Optional[Dict[str, str]] = None
) -> str:
    """Assign ``<!-- p:NNN -->`` markers to paragraphs that don't have one.

    For paragraphs whose normalised content matches an entry in
    ``prior_ids`` (a {id: text} mapping from a prior persona.md), reuse that
    ID so identifiers remain stable across user edits.

    Frontmatter (if present) is preserved verbatim; only the prose body is
    rewritten.
    """
    prior_ids = prior_ids or {}
    fm_text, body = (None, md_text)
    prefix = ""
    if md_text.startswith("---"):
        try:
            fm_text, body = _split_frontmatter(md_text)
            if fm_text is not None:
                prefix = f"---\n{fm_text}---\n"
        except ValueError:
            # Malformed frontmatter: bail and just operate on the whole text
            # as a prose body. Caller will surface the underlying error.
            fm_text, body = None, md_text
            prefix = ""

    # Build hash -> id map from prior_ids for fallback recovery.
    prior_hash_to_id: Dict[str, str] = {}
    for pid, ptext in prior_ids.items():
        prior_hash_to_id.setdefault(_hash_paragraph_text(ptext), pid)

    paragraphs = _split_paragraphs_with_offsets(body)
    if not paragraphs:
        return md_text

    # First pass: collect existing IDs in the markdown.
    existing_ids: List[Optional[str]] = []
    for _s, _e, para in paragraphs:
        m = PARAGRAPH_ID_PATTERN.search(para)
        existing_ids.append(f"p:{m.group(1)}" if m else None)

    used: set = {pid for pid in existing_ids if pid}
    # Also reserve all prior IDs so we don't accidentally reuse one for a
    # new paragraph (only paragraph-content match should reuse).
    used.update(prior_ids.keys())

    # Second pass: hash-fallback for paragraphs missing an ID.
    new_ids: List[str] = []
    for (s, e, para), existing in zip(paragraphs, existing_ids):
        if existing is not None:
            new_ids.append(existing)
            continue
        h = _hash_paragraph_text(para)
        recovered = prior_hash_to_id.get(h)
        # Only recover if no other paragraph already grabbed it this run.
        if recovered and recovered not in [nid for nid in new_ids]:
            new_ids.append(recovered)
            used.add(recovered)
            continue
        fresh = _next_id(used)
        new_ids.append(fresh)
        used.add(fresh)

    # Rewrite body: insert markers at start of each paragraph that needs one.
    out_chunks: List[str] = []
    cursor = 0
    for (start, end, para), pid, existing in zip(paragraphs, new_ids, existing_ids):
        out_chunks.append(body[cursor:start])
        if existing is not None:
            # Already has a marker; keep the paragraph as-is.
            out_chunks.append(body[start:end])
        else:
            marker = _PARAGRAPH_ID_TEMPLATE.format(n=int(pid[2:]))
            # Insert marker on its own line before the paragraph for
            # readability.
            out_chunks.append(f"{marker}\n{body[start:end]}")
        cursor = end
    out_chunks.append(body[cursor:])
    new_body = "".join(out_chunks)
    return prefix + new_body


def hash_fallback_reconstruct(
    md_text: str, prior_persona_json: Dict[str, Any]
) -> str:
    """Reconstruct paragraph IDs by hashing prior persona.json paragraphs.

    Use case: the user has hand-edited persona.md and either removed or
    duplicated ``<!-- p:NNN -->`` markers. We rebuild the marker placement
    by content-hash matching against ``prior_persona_json["paragraphs"]``.
    Paragraphs that don't match a prior hash get fresh IDs assigned via
    ``assign_paragraph_ids``.
    """
    prior_paragraphs = prior_persona_json.get("paragraphs") or []
    prior_ids: Dict[str, str] = {}
    if isinstance(prior_paragraphs, list):
        for entry in prior_paragraphs:
            if not isinstance(entry, dict):
                continue
            pid = entry.get("id")
            text = entry.get("text")
            if isinstance(pid, str) and isinstance(text, str):
                prior_ids[pid] = text

    # First, strip ALL existing markers — we want a clean slate so duplicates
    # and stale IDs don't outvote the hash recovery pass.
    fm_text, body = (None, md_text)
    prefix = ""
    if md_text.startswith("---"):
        try:
            fm_text, body = _split_frontmatter(md_text)
            if fm_text is not None:
                prefix = f"---\n{fm_text}---\n"
        except ValueError:
            fm_text, body = None, md_text
            prefix = ""
    cleaned_body = PARAGRAPH_ID_PATTERN.sub("", body)
    # Collapse any blank lines that the marker removal may have left double-
    # spaced. We tolerate one blank line between paragraphs.
    cleaned_body = re.sub(r"\n{3,}", "\n\n", cleaned_body)

    return assign_paragraph_ids(prefix + cleaned_body, prior_ids=prior_ids)


__all__ = [
    "parse_persona",
    "write_persona",
    "append_anecdote",
    "default_persona",
    "diff_persona",
    "PARAGRAPH_ID_PATTERN",
    "extract_paragraph_ids",
    "assign_paragraph_ids",
    "hash_fallback_reconstruct",
]
