#!/usr/bin/env python3
"""Step 3: scaffold a compathy-structured project for an accepted suggestion.

After Steps 1-2 captured the user's profile and selected suggestions, this
module is the deterministic side of Step 3:

  1. Validate the project slug.
  2. Create the project directory.
  3. Shell out to compathy's ``scaffold.py`` to lay down the ``context/``
     wiki structure.
  4. Seed an anecdote at ``context/raw/anecdote.md`` via
     ``persona.append_anecdote`` (handles append-or-create + timestamping).
  5. Write a ``context/raw/skills.md`` listing the suggested skills with
     source provenance and freshness metadata.
  6. Write a ``context/raw/starting-files-todo.md`` placeholder noting that
     Step 5 will collect the user's actual starting files.
  7. Append the absolute project path to the central
     ``~/.ai-quickstart/managed-projects.json`` registry via
     ``hooks_install.add_managed_project``.

Public API:
  * ``find_compathy_path(env)`` -> ``Path`` to compathy's installed dir.
  * ``validate_slug(slug)`` -> raises ``ValueError`` if invalid.
  * ``scaffold_project(slug, dir, suggested_skills, anecdote_seed, *,
    dry_run=False)`` -> dict with the resulting paths and flags.
  * ``unscaffold(project_dir)`` -> best-effort cleanup of a partial scaffold.

Errors:
  * ``CompathyMissingError`` (subclass of ``FileNotFoundError``) — compathy
    not found at the expected path.
  * ``ProjectExistsError`` (subclass of ``FileExistsError``) — target dir
    already exists and is non-empty.
  * ``ScaffoldError`` (subclass of ``RuntimeError``) — compathy invocation
    failed; ``stderr`` carried in the exception message.

Stdlib only. Python 3.9+. Honors ``COMPATHY_HOME`` and ``AI_QUICKSTART_HOME``
env vars. All file IO is atomic where it matters (registry writes are atomic
in ``hooks_install``; the small markdown files are single-shot writes).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

# ---------------------------------------------------------------------------
# Sibling imports without ``from scripts import``.
# Mirrors heal.py: insert scripts/ at the front of sys.path so test
# monkeypatches reach the same module instance the production code uses.
# ---------------------------------------------------------------------------
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

import persona  # type: ignore  # noqa: E402
import hooks_install  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class CompathyMissingError(FileNotFoundError):
    """Raised when compathy is not installed at the expected location."""


class ProjectExistsError(FileExistsError):
    """Raised when the target project directory already exists and is non-empty."""


class ScaffoldError(RuntimeError):
    """Raised when compathy's ``scaffold.py`` exits non-zero."""


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------
# kebab-case: starts with a lowercase letter, then lowercase letters, digits,
# or hyphens. No double hyphens, no trailing hyphen. Length 1-60.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def validate_slug(slug: str) -> None:
    """Validate ``slug`` as kebab-case-only, 1-60 chars, starts with a letter.

    Raises ``ValueError`` with a clear message on invalid input.
    """
    if not isinstance(slug, str):
        raise ValueError(f"slug must be a string, got {type(slug).__name__}")
    if not slug:
        raise ValueError("slug must not be empty")
    if len(slug) > 60:
        raise ValueError(
            f"slug too long ({len(slug)} chars); max 60: {slug!r}"
        )
    if not _SLUG_RE.match(slug):
        raise ValueError(
            "slug must be kebab-case: lowercase letters, digits, and "
            f"single hyphens only, starting with a letter; got {slug!r}"
        )


# ---------------------------------------------------------------------------
# Compathy discovery
# ---------------------------------------------------------------------------
def find_compathy_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """Return the path to compathy's installed scripts directory.

    Resolution order:
      1. ``COMPATHY_HOME`` env var (treated as the compathy skill root,
         which contains ``scripts/scaffold.py``).
      2. ``~/.claude/skills/compathy/`` (default install location).

    Returns the directory that contains ``scripts/scaffold.py``. Raises
    ``CompathyMissingError`` if it cannot be located.
    """
    candidates: List[Path] = []

    src = env if env is not None else os.environ
    override = src.get("COMPATHY_HOME")
    if override:
        candidates.append(Path(override).expanduser())

    candidates.append(Path.home() / ".claude" / "skills" / "compathy")

    for root in candidates:
        scaffold = root / "scripts" / "scaffold.py"
        if scaffold.is_file():
            return root

    tried = ", ".join(str(c) for c in candidates)
    raise CompathyMissingError(
        f"compathy not found. Looked at: {tried}. Install compathy at "
        "~/.claude/skills/compathy/ or set COMPATHY_HOME to its root."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _utcnow_iso() -> str:
    """Return current UTC time as an ISO-8601 string with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_non_empty_dir(p: Path) -> bool:
    if not p.exists():
        return False
    if not p.is_dir():
        return True
    try:
        next(p.iterdir())
    except StopIteration:
        return False
    return True


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to ``path`` atomically (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(
        str(tmp),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o644,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(path))


def _format_skills_md(slug: str, skills: List[Dict[str, Any]]) -> str:
    """Render the skills.md body for ``slug`` from a list of skill dicts.

    Each skill dict may contain:
      * ``name`` (required) - human/skill name
      * ``source`` - one of ``github``, ``mcp_registry``, ``mcpmarket``
      * ``url`` - canonical URL for the skill/server
      * ``stars``, ``last_commit`` - github freshness data
      * ``warnings`` - list of strings (e.g. ``"<100 stars"``)
    """
    lines: List[str] = []
    lines.append(f"# Suggested skills for {slug}")
    lines.append("")
    lines.append(f"Captured at {_utcnow_iso()} during ai-quickstart Step 2.")
    lines.append("")
    if not skills:
        lines.append("No skills were suggested for this project.")
        lines.append("")
        return "\n".join(lines)
    for idx, skill in enumerate(skills, start=1):
        name = str(skill.get("name", "<unnamed>"))
        source = str(skill.get("source", "unknown"))
        lines.append(f"## {idx}. {name}")
        lines.append("")
        lines.append(f"- source: {source}")
        url = skill.get("url")
        if url:
            lines.append(f"- url: {url}")
        stars = skill.get("stars")
        if stars is not None:
            lines.append(f"- stars: {stars}")
        last_commit = skill.get("last_commit")
        if last_commit:
            lines.append(f"- last_commit: {last_commit}")
        warnings = skill.get("warnings") or []
        if warnings:
            lines.append("- warnings:")
            for w in warnings:
                lines.append(f"  - {w}")
        # Pass through any extra keys we did not name explicitly.
        known = {"name", "source", "url", "stars", "last_commit", "warnings"}
        extras = {k: v for k, v in skill.items() if k not in known}
        for k, v in extras.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
    return "\n".join(lines)


def _format_starting_files_todo(slug: str) -> str:
    """Render the starting-files-todo.md placeholder body."""
    return (
        f"# Starting files for {slug}\n"
        "\n"
        "This is a placeholder. ai-quickstart Step 5 will prompt you to add\n"
        "the starting files (existing docs, specs, notes) you want compathy\n"
        "to compile into the wiki.\n"
        "\n"
        "Until then, drop any seed materials into ``context/raw/`` and they\n"
        "will be picked up the next time ``/compathy`` runs.\n"
        f"\n"
        f"Captured at {_utcnow_iso()}.\n"
    )


def _planned_actions(
    slug: str, project_dir: Path, suggested_skills: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Return the dry-run plan: paths and counts, no side effects."""
    raw = project_dir / "context" / "raw"
    return {
        "slug": slug,
        "path": str(project_dir),
        "compathy_initialized": False,
        "anecdote_path": str(raw / "anecdote.md"),
        "skills_path": str(raw / "skills.md"),
        "starting_files_path": str(raw / "starting-files-todo.md"),
        "registered": False,
        "skill_count": len(suggested_skills),
        "dry_run": True,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def scaffold_project(
    project_slug: str,
    project_dir: Path,
    suggested_skills: List[Dict[str, Any]],
    anecdote_seed: str,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Scaffold a compathy-structured project for a single accepted suggestion.

    See module docstring for the full contract. Returns a dict with::

        {
          "slug": str,
          "path": str,
          "compathy_initialized": bool,
          "anecdote_path": str,
          "skills_path": str,
          "registered": bool,
        }

    On compathy invocation failure, the partially-created ``project_dir`` is
    cleaned up best-effort before ``ScaffoldError`` is raised.
    """
    # 1. Validate slug.
    validate_slug(project_slug)

    project_dir = Path(project_dir).expanduser()

    # 2. Refuse to clobber a non-empty existing dir.
    if _is_non_empty_dir(project_dir):
        raise ProjectExistsError(
            f"refusing to scaffold into non-empty path: {project_dir}"
        )

    # 2b. Dry run short-circuit.
    if dry_run:
        return _planned_actions(project_slug, project_dir, suggested_skills)

    # 3. Locate compathy BEFORE we make any filesystem changes — surface
    #    "compathy missing" without leaving an empty dir behind.
    compathy_root = find_compathy_path()
    compathy_scaffold = compathy_root / "scripts" / "scaffold.py"

    # 4. Make the project dir (idempotent on empty existing dirs).
    project_dir.mkdir(parents=True, exist_ok=True)

    created_dir_here = True  # track for cleanup on failure

    try:
        # 5. Shell out to compathy.
        proc = subprocess.run(
            [
                sys.executable or "python3",
                str(compathy_scaffold),
                "--target",
                str(project_dir),
                "--project-name",
                project_slug,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or proc.stdout or "").strip()
            raise ScaffoldError(
                f"compathy scaffold failed (exit {proc.returncode}): {stderr}"
            )

        raw_dir = project_dir / "context" / "raw"
        # Compathy creates context/raw/ but defensive mkdir is cheap.
        raw_dir.mkdir(parents=True, exist_ok=True)

        # 6. Seed anecdote.md via persona.append_anecdote.
        #    persona writes ``{anecdotes_dir}/{slug}.md`` — we want a fixed
        #    filename ``anecdote.md`` inside raw/, so we point it at raw_dir
        #    and use ``anecdote`` as the slug. append_anecdote rejects
        #    slugs containing ``/`` or starting with ``.``; ``anecdote`` is
        #    safe.
        anecdote_path = persona.append_anecdote(
            raw_dir, "anecdote", anecdote_seed
        )

        # 7. Write skills.md.
        skills_path = raw_dir / "skills.md"
        _atomic_write_text(
            skills_path, _format_skills_md(project_slug, suggested_skills)
        )

        # 8. Write starting-files-todo.md.
        starting_files_path = raw_dir / "starting-files-todo.md"
        _atomic_write_text(
            starting_files_path, _format_starting_files_todo(project_slug)
        )

        # 9. Register in managed-projects.json.
        hooks_install.add_managed_project(project_dir)

        return {
            "slug": project_slug,
            "path": str(project_dir),
            "compathy_initialized": True,
            "anecdote_path": str(anecdote_path),
            "skills_path": str(skills_path),
            "registered": True,
        }
    except Exception:
        # Best-effort cleanup so a partial scaffold doesn't poison retries.
        if created_dir_here:
            try:
                unscaffold(project_dir)
            except Exception:  # pylint: disable=broad-except
                pass
        raise


def unscaffold(project_dir: Path) -> None:
    """Best-effort cleanup of a (partial) scaffold.

    Removes ``project_dir`` recursively if it exists, and removes its
    absolute path from the managed-projects registry. Suitable for use in
    tests or as a manual recovery tool when a scaffold is half-done.
    """
    project_dir = Path(project_dir).expanduser()
    abs_path = project_dir.resolve() if project_dir.exists() else project_dir

    # Remove from registry first (idempotent).
    try:
        hooks_install.remove_managed_project(abs_path)
    except Exception:  # pylint: disable=broad-except
        pass

    # Then remove the directory tree.
    if project_dir.exists():
        try:
            shutil.rmtree(project_dir)
        except OSError:
            # Last-ditch: try removing files individually.
            for child in sorted(
                project_dir.rglob("*"), key=lambda p: -len(str(p))
            ):
                try:
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                    else:
                        child.rmdir()
                except OSError:
                    pass
            try:
                project_dir.rmdir()
            except OSError:
                pass


__all__ = [
    "CompathyMissingError",
    "ProjectExistsError",
    "ScaffoldError",
    "find_compathy_path",
    "validate_slug",
    "scaffold_project",
    "unscaffold",
]
