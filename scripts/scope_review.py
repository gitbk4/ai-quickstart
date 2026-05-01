"""Scope review module: deterministic side of Phase 2.5 (gstack /plan-ceo-review hookup).

Pattern (skill-calls-skill):
  * Python (this module) is the deterministic half. It composes a markdown
    plan doc shaped to ``/plan-ceo-review``'s expected input (problem +
    proposed scope + user profile + constraints + open questions + reviewer
    context) and writes it atomically to disk.
  * Claude (in SKILL.md Phase 2.5 orchestration) is the synthesis half. It
    reads the plan path printed by ``init.py prepare-scope-review``, invokes
    the gstack ``/plan-ceo-review`` skill via the Skill tool with that plan
    as input, and optionally writes the review outcome back via
    :func:`read_review_outcome`.

The plan doc never invents content the user did not state. Missing answer
fields gracefully degrade to ``"(not stated)"`` placeholders so the doc
always renders, but the reviewer can see what was unknown going in.

Public API:
  * ``prepare(run_id, project_spec, answers, suggestions) -> Path``
  * ``prepare_invocation_prompt(plan_path, project_slug) -> str``
  * ``read_review_outcome(run_id, project_slug) -> dict | None``

Stdlib only. Python 3.9+. Honors ``AI_QUICKSTART_HOME``. All file IO is
atomic (tmp write + ``os.replace``).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _runs_root() -> Path:
    """Return the per-run artifact root, honoring ``AI_QUICKSTART_HOME``."""
    env = os.environ.get("AI_QUICKSTART_HOME")
    if env:
        base = Path(env)
    else:
        base = Path.home() / ".ai-quickstart"
    return base / "runs"


def _run_dir(run_id: str) -> Path:
    if not run_id or not isinstance(run_id, str):
        raise ValueError("run_id must be a non-empty string")
    d = _runs_root() / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_text(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` via tmp+rename. Cleans up on failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Section composition
# ---------------------------------------------------------------------------


_NOT_STATED = "(not stated)"


def _bullets(items: Any) -> List[str]:
    """Render a value as a list of bullet lines.

    Lists become one bullet per item; scalars become a single bullet; empty
    or missing values become a single ``- (not stated)`` bullet so the
    section still has visible structure for the reviewer.
    """
    if items is None or items == "":
        return [f"- {_NOT_STATED}"]
    if isinstance(items, list):
        cleaned = [str(x).strip() for x in items if str(x).strip()]
        if not cleaned:
            return [f"- {_NOT_STATED}"]
        return [f"- {x}" for x in cleaned]
    return [f"- {str(items).strip()}"]


def _scalar(value: Any) -> str:
    """Render a value as a one-line scalar, defaulting to ``(not stated)``."""
    if value is None or value == "":
        return _NOT_STATED
    if isinstance(value, list):
        cleaned = [str(x).strip() for x in value if str(x).strip()]
        if not cleaned:
            return _NOT_STATED
        return ", ".join(cleaned)
    return str(value).strip()


def _section_problem(answers: Dict[str, Any]) -> List[str]:
    """Compose the '## Problem statement' section.

    Synthesized from ``answers.top_problems`` and ``answers.desired_outcomes``.
    Both are listed verbatim so the reviewer can see what the user actually
    said, not a paraphrase.
    """
    lines = ["## Problem statement", ""]
    lines.append("Top problems the user wants to address:")
    lines.append("")
    lines.extend(_bullets(answers.get("top_problems")))
    lines.append("")
    lines.append("Desired outcomes:")
    lines.append("")
    lines.extend(_bullets(answers.get("desired_outcomes")))
    return lines


def _section_scope(
    project_spec: Dict[str, Any], suggestions: Dict[str, Any]
) -> List[str]:
    """Compose the '## Proposed scope' section.

    Describes what the project does, derived from the project spec's slug +
    template plus the suggested skills and MCP servers. The reviewer needs
    this to challenge whether the scope is the narrowest valuable wedge.
    """
    lines = ["## Proposed scope", ""]
    slug = _scalar(project_spec.get("slug"))
    template = _scalar(
        project_spec.get("project_template") or project_spec.get("template")
    )
    lines.append(f"- Project slug: {slug}")
    lines.append(f"- Project template: {template}")

    anecdote = project_spec.get("anecdote_seed")
    if isinstance(anecdote, str) and anecdote.strip():
        lines.append(f"- Why the user wants this: {anecdote.strip()}")

    target_dir = project_spec.get("dir")
    if isinstance(target_dir, str) and target_dir.strip():
        lines.append(f"- Target directory: {target_dir.strip()}")

    skills = suggestions.get("skills") if isinstance(suggestions, dict) else None
    if isinstance(skills, list) and skills:
        lines.append("")
        lines.append("Suggested Claude skills (curated + ranked):")
        lines.append("")
        for s in skills:
            if not isinstance(s, dict):
                continue
            name = _scalar(s.get("name") or s.get("id"))
            desc = _scalar(s.get("description"))
            lines.append(f"- **{name}**: {desc}")
    else:
        lines.append("")
        lines.append("Suggested Claude skills: (none gathered)")

    servers = suggestions.get("mcp_servers") if isinstance(suggestions, dict) else None
    if isinstance(servers, list) and servers:
        lines.append("")
        lines.append("Suggested MCP servers:")
        lines.append("")
        for s in servers:
            if not isinstance(s, dict):
                continue
            sid = _scalar(s.get("id") or s.get("name"))
            desc = _scalar(s.get("description"))
            lines.append(f"- **{sid}**: {desc}")

    templates = (
        suggestions.get("project_templates") if isinstance(suggestions, dict) else None
    )
    if isinstance(templates, list) and templates:
        lines.append("")
        lines.append("Other project templates the user could pick instead:")
        lines.append("")
        for t in templates:
            t_str = str(t).strip()
            if t_str and t_str != slug:
                lines.append(f"- {t_str}")
    return lines


def _section_user_profile(answers: Dict[str, Any]) -> List[str]:
    """Compose the '## User profile' section.

    Surfaces archetype, role, industry, skill_tolerance, project_style,
    coding_languages so /plan-ceo-review can calibrate the review to the
    user's stated context (a marketer in publishing gets different
    feedback than a senior engineer in fintech).
    """
    lines = ["## User profile", ""]
    lines.append(f"- Archetype: {_scalar(answers.get('archetype'))}")
    lines.append(f"- Role: {_scalar(answers.get('role'))}")
    lines.append(f"- Industry: {_scalar(answers.get('industry'))}")
    lines.append(f"- Skill tolerance: {_scalar(answers.get('skill_tolerance'))}")
    lines.append(f"- Project style: {_scalar(answers.get('project_style'))}")
    lines.append(f"- Coding languages: {_scalar(answers.get('coding_languages'))}")
    notes = answers.get("freeform_notes")
    if isinstance(notes, str) and notes.strip():
        lines.append("")
        lines.append("Freeform notes from the user:")
        lines.append("")
        lines.append("> " + notes.strip().replace("\n", "\n> "))
    return lines


def _section_constraints(answers: Dict[str, Any]) -> List[str]:
    """Compose the '## Constraints' section.

    Derives constraints from ``project_style`` (minimal vs full) and any
    time-box hint in answers. The reviewer uses these to keep their scope
    proposals realistic for the user's stated capacity.
    """
    lines = ["## Constraints", ""]
    style = _scalar(answers.get("project_style"))
    if style == "minimal":
        lines.append(
            "- Project style is **minimal**: the user explicitly wants a "
            "small, focused scope. Resist scope expansion that requires "
            "more than a few hours of work."
        )
    elif style == "full":
        lines.append(
            "- Project style is **full**: the user is willing to invest in a "
            "more complete scaffold. Scope expansion is welcome if it pays "
            "off in the medium term."
        )
    else:
        lines.append(f"- Project style: {style}")

    tolerance = _scalar(answers.get("skill_tolerance"))
    if tolerance == "strict":
        lines.append(
            "- Skill tolerance is **strict**: prefer well-known, high-star "
            "Claude skills and MCP servers. Avoid bleeding-edge picks."
        )
    elif tolerance == "permissive":
        lines.append(
            "- Skill tolerance is **permissive**: the user is fine with "
            "newer or lower-star tools if they fit the goal."
        )

    time_box = answers.get("time_box") or answers.get("time_budget")
    if isinstance(time_box, str) and time_box.strip():
        lines.append(f"- User-stated time box: {time_box.strip()}")

    languages = _scalar(answers.get("coding_languages"))
    if languages != _NOT_STATED:
        lines.append(f"- Coding languages the user is comfortable in: {languages}")

    if len(lines) == 2:  # only header + blank — no constraints derived
        lines.append(f"- {_NOT_STATED}")
    return lines


def _section_open_questions(
    project_spec: Dict[str, Any], answers: Dict[str, Any]
) -> List[str]:
    """Compose the '## Open questions / Where pressure-testing helps' section.

    Auto-generated questions that prime the reviewer to pressure-test the
    plan against the 10-star bar without forcing them into a fixed mode.
    """
    slug = _scalar(project_spec.get("slug"))
    archetype = _scalar(answers.get("archetype"))
    industry = _scalar(answers.get("industry"))

    lines = [
        "## Open questions / Where pressure-testing helps",
        "",
        "These are the angles where a CEO-style review will add the most signal:",
        "",
        f"- Is **{slug}** the narrowest valuable wedge? Could a 1-day scope "
        "deliver 80% of the user's stated value?",
        "- Does this match the user's stated goals, or did the curated "
        "mapping push them toward a generic answer?",
        f"- For a {archetype} user in {industry}, is there a higher-leverage "
        "project hiding behind this one (scope expansion candidate)?",
        "- Are there hidden premises in the proposed scope that, if "
        "challenged, would change the project shape entirely?",
        "- What would a 10-star version of this project look like, and "
        "what would it cost to get there from here?",
        "- Are the suggested skills and MCP servers actually the right "
        "tools, or are they curated-mapping defaults that could be "
        "swapped for something closer to the user's real workflow?",
    ]
    return lines


def _section_reviewer_context(
    project_spec: Dict[str, Any], answers: Dict[str, Any]
) -> List[str]:
    """Compose the '## Context for the reviewer' section.

    Explains the situation: this is a pre-implementation plan generated by
    ai-quickstart's curated suggestion engine, the user has accepted (in
    principle) but wants scope pressure-testing before scaffolding.
    """
    archetype = _scalar(answers.get("archetype"))
    industry = _scalar(answers.get("industry"))
    role = _scalar(answers.get("role"))
    slug = _scalar(project_spec.get("slug"))
    return [
        "## Context for the reviewer",
        "",
        "This is a **pre-implementation** plan generated by ai-quickstart "
        "(a Claude Code skill that bridges from a vague AI goal to a "
        "scaffolded project + recommended toolkit).",
        "",
        f"The user is a **{archetype}** user; role: **{role}**; industry: "
        f"**{industry}**. They have not yet committed to building "
        f"**{slug}**. They are asking for a scope pressure-test before "
        "scaffolding.",
        "",
        "Treat this as if the user opened with: \"I am about to start this "
        "project. Tell me what's wrong with the scope, what I'm missing, "
        "and whether a different shape would be a better use of my time.\"",
        "",
        "Pick the mode (SCOPE EXPANSION, SELECTIVE EXPANSION, HOLD SCOPE, "
        "or SCOPE REDUCTION) that best fits this user's stated profile and "
        "constraints. Calibrate ambition to their archetype + industry + "
        "skill tolerance.",
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _slug_for_filename(slug: str) -> str:
    """Sanitize a project slug for safe inclusion in a filename.

    Allowed: lowercase letters, digits, hyphens, underscores. Other chars
    are replaced with ``-``. Empty input becomes ``project``.
    """
    s = (slug or "").strip().lower()
    if not s:
        return "project"
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("-")
    cleaned = "".join(out).strip("-_") or "project"
    return cleaned


def prepare(
    run_id: str,
    project_spec: Dict[str, Any],
    answers: Dict[str, Any],
    suggestions: Dict[str, Any],
) -> Path:
    """Compose and atomically write the scope-review plan doc.

    Returns the absolute path to the written markdown file at
    ``{AI_QUICKSTART_HOME}/runs/{run_id}/scope-review-plan.md``.

    Raises ``ValueError`` for missing/empty ``run_id``. Other inputs are
    accepted with permissive defaults — the doc always renders even with
    sparse answers (missing fields show as ``(not stated)``).
    """
    if not isinstance(project_spec, dict):
        raise TypeError("project_spec must be a dict")
    if not isinstance(answers, dict):
        raise TypeError("answers must be a dict")
    if not isinstance(suggestions, dict):
        raise TypeError("suggestions must be a dict")

    slug = _scalar(project_spec.get("slug"))

    sections: List[str] = []
    sections.append(f"# Project Plan: {slug}")
    sections.append("")
    sections.append(
        "_Pre-implementation plan composed by ai-quickstart for pressure-"
        "testing via the gstack `/plan-ceo-review` skill._"
    )
    sections.append("")
    sections.extend(_section_problem(answers))
    sections.append("")
    sections.extend(_section_scope(project_spec, suggestions))
    sections.append("")
    sections.extend(_section_user_profile(answers))
    sections.append("")
    sections.extend(_section_constraints(answers))
    sections.append("")
    sections.extend(_section_open_questions(project_spec, answers))
    sections.append("")
    sections.extend(_section_reviewer_context(project_spec, answers))
    sections.append("")  # trailing newline

    body = "\n".join(sections)

    target = _run_dir(run_id) / "scope-review-plan.md"
    _atomic_write_text(target, body)
    return target


def prepare_invocation_prompt(plan_path: Path, project_slug: str) -> str:
    """Build the prompt text Claude pastes into the /plan-ceo-review invocation.

    Returns a string that contains a short framing line plus the full plan
    file content. Claude calls this from SKILL.md Phase 2.5; the result is
    passed to the Skill tool as the body of a /plan-ceo-review invocation.

    Raises ``FileNotFoundError`` if ``plan_path`` does not exist.
    """
    plan_path = Path(plan_path)
    if not plan_path.exists():
        raise FileNotFoundError(f"plan file not found: {plan_path}")
    content = plan_path.read_text(encoding="utf-8")
    slug = _scalar(project_slug)
    framing = (
        f"Please run /plan-ceo-review on the following pre-implementation "
        f"plan for the ai-quickstart project '{slug}'. The plan was "
        "generated from a curated mapping plus a structured user "
        "interview; the user wants scope pressure-testing before "
        "scaffolding. Calibrate the review to the user's archetype + "
        "industry + skill tolerance shown in the plan's 'User profile' "
        "section.\n\n---\n\n"
    )
    return framing + content


def _outcome_path(run_id: str, project_slug: str) -> Path:
    slug = _slug_for_filename(project_slug)
    return _run_dir(run_id) / f"scope-review-outcome-{slug}.md"


def read_review_outcome(run_id: str, project_slug: str) -> Optional[Dict[str, Any]]:
    """Return the persisted /plan-ceo-review outcome for a project, or None.

    The outcome file is markdown, written by Claude after invoking the
    /plan-ceo-review skill. We return a small dict so callers can check
    presence + read content without a second filesystem call::

        {"path": "...", "content": "...", "read_at": "ISO-8601"}

    Returns ``None`` when:
      * the file does not exist
      * the file cannot be read (OSError)

    Never raises.
    """
    if not run_id:
        return None
    try:
        target = _outcome_path(run_id, project_slug)
    except ValueError:
        return None
    if not target.exists():
        return None
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return None
    return {
        "path": str(target),
        "content": content,
        "read_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


__all__ = [
    "prepare",
    "prepare_invocation_prompt",
    "read_review_outcome",
]
