"""Interview module: deterministic side of Step 1.

Pattern (compathy split):
  * Python (this module) is the deterministic half. It owns the run id, the
    on-disk layout under ``~/.ai-quickstart/runs/{run_id}/``, and the
    composition of adversarial prompt files via :mod:`prompts`.
  * Claude (in SKILL.md orchestration) is the synthesis half: it reads the
    Step 1 prompt this module writes, conducts the interview, and asks the
    caller to persist the resulting answers via :func:`record_answers`.

Public API:
  * ``start_session(archetype, run_id=None) -> dict``
  * ``record_answers(run_id, answers) -> Path``
  * ``read_answers(run_id) -> dict | None``
  * ``compose_step2_context(run_id, answers, source_results) -> str``

Answers schema (loose; only ``archetype`` is required for downstream code,
the rest are documented here so SKILL.md and tests share a vocabulary)::

    {
      "archetype":         "job" | "personal" | "exploring",
      "role":              str,
      "industry":          str,
      "top_problems":      list[str],
      "desired_outcomes":  list[str],
      "skill_tolerance":   "strict" | "permissive",
      "project_style":     "minimal" | "full",
      "coding_languages":  list[str],
      "freeform_notes":    str,
    }

Stdlib only. Python 3.9+. Honors ``AI_QUICKSTART_HOME``. All file IO is
atomic (tmp write + ``os.replace``).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Import sibling modules without going through a ``scripts.`` package path.
# heal.py uses the same pattern; keeping it consistent avoids the PEP 420
# namespace-package double-module bug (where two import paths produce two
# distinct module objects and ``monkeypatch.setattr`` fails to land on both).
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))
import prompts  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_ARCHETYPES = ("job", "personal", "exploring")
ANSWERS_FILENAME = "answers.json"

_TEMPLATES_DIR = _here.parent / "templates" / "prompts"
_STEP1_TEMPLATE = _TEMPLATES_DIR / "step-1.md.tmpl"
_STEP2_TEMPLATE = _TEMPLATES_DIR / "step-2.md.tmpl"


# ---------------------------------------------------------------------------
# Step 1 — start session
# ---------------------------------------------------------------------------


def start_session(archetype: str, run_id: Optional[str] = None) -> Dict[str, Any]:
    """Begin an interview run.

    Validates the archetype, allocates a run id if one was not supplied,
    composes the Step 1 adversarial prompt body, and writes it to disk.

    Returns a dict with keys: ``run_id``, ``archetype``, ``prompt_path``,
    ``started_at``.
    """
    if archetype not in VALID_ARCHETYPES:
        raise ValueError(
            f"archetype must be one of {VALID_ARCHETYPES!r}, got {archetype!r}"
        )

    rid = run_id or prompts.make_run_id()
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    body = prompts.compose_adversarial(
        prior_step_summary=(
            "This is the start of a new ai-quickstart run. The user picked "
            f"archetype '{archetype}'. The 3-question entry interview has not "
            "yet captured role, industry, or specific goals. Treat the user "
            "as a stranger and pull concrete facts; do not invent details "
            "that were not stated."
        ),
        next_step_topic=(
            f"Step 1 deeper interview for a '{archetype}' user"
        ),
    )

    prompt_path = prompts.write_prompt(rid, 1, body)

    return {
        "run_id": rid,
        "archetype": archetype,
        "prompt_path": str(prompt_path),
        "started_at": started_at,
    }


# ---------------------------------------------------------------------------
# Answer persistence
# ---------------------------------------------------------------------------


def _runs_root() -> Path:
    """Return the per-run artifact root, honoring ``AI_QUICKSTART_HOME``."""
    env = os.environ.get("AI_QUICKSTART_HOME")
    if env:
        base = Path(env)
    else:
        base = Path.home() / ".ai-quickstart"
    return base / "runs"


def _answers_path(run_id: str) -> Path:
    if not run_id:
        raise ValueError("run_id must be a non-empty string")
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / ANSWERS_FILENAME


def record_answers(run_id: str, answers: Dict[str, Any]) -> Path:
    """Atomically persist ``answers`` for ``run_id``.

    Schema is loose; the only requirement is JSON-serializability. We write
    to a temp file alongside the target and rename so a partial write cannot
    leave a half-formed answers.json on disk.
    """
    if not isinstance(answers, dict):
        raise TypeError("answers must be a dict")

    target = _answers_path(run_id)
    payload = json.dumps(answers, ensure_ascii=False, indent=2, sort_keys=True)

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_answers(run_id: str) -> Optional[Dict[str, Any]]:
    """Return the persisted answers dict or ``None`` if missing / malformed.

    Malformed JSON or a non-dict top-level value both yield ``None`` so
    callers can treat "no usable answers yet" as a single control-flow
    signal, the same way :func:`prompts.read_prompt` does.
    """
    target = _answers_path(run_id)
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Step 2 — compose context
# ---------------------------------------------------------------------------


def _summarize_answers(answers: Dict[str, Any]) -> str:
    """Render answers as a compact, human-readable bullet list.

    Used as the ``prior_summary`` substitution in step-2.md.tmpl.
    """
    def _val(key: str, default: str = "(not stated)") -> str:
        v = answers.get(key)
        if v is None or v == "":
            return default
        if isinstance(v, list):
            if not v:
                return default
            return ", ".join(str(x) for x in v)
        return str(v)

    lines = [
        "Interview answers from Step 1:",
        "",
        f"- Archetype: {_val('archetype')}",
        f"- Role: {_val('role')}",
        f"- Industry: {_val('industry')}",
        f"- Top problems: {_val('top_problems')}",
        f"- Desired outcomes: {_val('desired_outcomes')}",
        f"- Skill tolerance: {_val('skill_tolerance')}",
        f"- Project style: {_val('project_style')}",
        f"- Coding languages: {_val('coding_languages')}",
    ]
    notes = answers.get("freeform_notes")
    if isinstance(notes, str) and notes.strip():
        lines.append("")
        lines.append("Freeform notes:")
        lines.append(notes.strip())
    return "\n".join(lines)


def _summarize_source_results(source_results: Dict[str, Any]) -> str:
    """Render source-query results as a short prose block.

    ``source_results`` is whatever ``suggest.gather`` returns — typically
    ``{"project_templates": [...], "skills": [...], "warnings": [...]}``.
    We deliberately keep this compact: the LLM gets the full structured
    payload through SKILL.md; this prose summary is just for the audit
    artifact.
    """
    if not isinstance(source_results, dict) or not source_results:
        return "_No source results were gathered for this run._"

    lines = ["Source query results:"]
    for key in ("project_templates", "skills", "mcp_servers"):
        items = source_results.get(key)
        if isinstance(items, list):
            lines.append(f"- {key}: {len(items)} item(s)")
    warnings = source_results.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines)


def compose_step2_context(
    run_id: str,
    answers: Dict[str, Any],
    source_results: Dict[str, Any],
) -> str:
    """Render and persist the Step 2 adversarial prompt for ``run_id``.

    Combines the Step 1 answers and the live source query results into a
    single ``prior_summary`` string, renders ``step-2.md.tmpl`` against it,
    writes the result to ``runs/{run_id}/step-2-prompt.md``, and returns
    the rendered text. Missing optional answer fields are filled with
    ``"(not stated)"`` so the template's required ``${...}`` substitutions
    never raise.
    """
    if not isinstance(answers, dict):
        raise TypeError("answers must be a dict")
    if not isinstance(source_results, dict):
        raise TypeError("source_results must be a dict")

    answers_summary = _summarize_answers(answers)
    sources_summary = _summarize_source_results(source_results)
    prior_summary = answers_summary + "\n\n" + sources_summary

    archetype = str(answers.get("archetype") or "(not stated)")
    industry = str(answers.get("industry") or "(not stated)")
    role = str(answers.get("role") or "(not stated)")

    rendered = prompts.render_template(
        _STEP2_TEMPLATE,
        {
            "run_id": run_id,
            "prior_summary": prior_summary,
            "user_archetype": archetype,
            "user_industry": industry,
            "user_role": role,
        },
    )

    prompts.write_prompt(run_id, 2, rendered)
    return rendered


__all__ = [
    "VALID_ARCHETYPES",
    "start_session",
    "record_answers",
    "read_answers",
    "compose_step2_context",
]
