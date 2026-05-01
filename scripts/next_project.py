#!/usr/bin/env python3
"""Next-project recommender: deterministic side of the /next-project subskill.

This is the v1.1 consumer of the persona system v1 built. v1 collected data
(persona frontmatter + activity + anecdotes); this module reads that data
back and scores each archetype/industry combo in
``mappings/personas.yaml`` to produce a ranked list of recommended next
projects.

Module surface:
  * ``recommend(persona_path, mapping_path, top_n=5) -> dict``
  * ``score_archetype_match(persona, archetype, industry, mapping) -> (float, [str])``
  * ``_extract_skill_signals(persona) -> dict``

Pure computation. No network, no subprocess, no LLM call. Stdlib only.
Python 3.9+.

Output shape:

    {
      "recommendations": [
        {
          "project_template": str,
          "archetype": str,
          "industry": str | None,
          "skills": [str, ...],     # curated skill names from mapping
          "score": float,           # 0..1
          "why": [str, ...],        # signals that contributed to the score
        },
        ...
      ],
      "reasoning": [str, ...],      # top-level human-readable framing
      "persona_signals": {...},     # what we read from the persona
      "warnings": [str, ...],       # any extraction issues
    }

Determinism:
  * Identical persona + identical mapping always produce identical output
    (sorted by ``(-score, archetype, industry, project_template)``).
  * No wall-clock time enters scoring except activity recency, which is
    derived from ``persona.activity.last_active`` (a string in the persona).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Sibling-import pattern matching heal.py / suggest.py.
_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))
import persona as persona_mod  # type: ignore  # noqa: E402
import suggest as suggest_mod  # type: ignore  # noqa: E402


# Scoring weights (must sum to 1.0 max for the 0..1 contract).
WEIGHT_ARCHETYPE = 0.40
WEIGHT_INDUSTRY = 0.30
WEIGHT_GOAL = 0.15
WEIGHT_RECENCY = 0.10
WEIGHT_STARTER = 0.05

# Activity recency window (days).
RECENCY_DAYS = 30

# Project-count threshold below which a starter boost applies.
STARTER_PROJECT_COUNT_THRESHOLD = 3


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 string with optional 'Z' suffix; return None on failure."""
    if not isinstance(s, str) or not s:
        return None
    text = s.strip()
    # datetime.fromisoformat doesn't accept 'Z' before Python 3.11.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _extract_skill_signals(persona: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the signal-bearing fields out of a persona frontmatter dict.

    Returns a flat mapping with safe defaults so downstream code can index
    without ``KeyError`` paths.
    """
    identity = persona.get("identity") or {}
    goals = persona.get("goals") or {}
    preferences = persona.get("preferences") or {}
    activity = persona.get("activity") or {}
    generated = persona.get("generated") or {}

    archetype = identity.get("archetype") if isinstance(identity, dict) else None
    industry = identity.get("industry") if isinstance(identity, dict) else None
    role = identity.get("role") if isinstance(identity, dict) else None

    top_problems = goals.get("top_problems") if isinstance(goals, dict) else []
    desired_outcomes = goals.get("desired_outcomes") if isinstance(goals, dict) else []
    if not isinstance(top_problems, list):
        top_problems = []
    if not isinstance(desired_outcomes, list):
        desired_outcomes = []

    coding_languages = (
        preferences.get("coding_languages") if isinstance(preferences, dict) else []
    )
    if not isinstance(coding_languages, list):
        coding_languages = []

    project_count = activity.get("project_count") if isinstance(activity, dict) else 0
    try:
        project_count = int(project_count) if project_count is not None else 0
    except (TypeError, ValueError):
        project_count = 0

    last_active_raw = activity.get("last_active") if isinstance(activity, dict) else None
    last_active_dt = _parse_iso(last_active_raw) if isinstance(last_active_raw, str) else None

    anecdote_count = (
        generated.get("anecdote_count") if isinstance(generated, dict) else 0
    )
    try:
        anecdote_count = int(anecdote_count) if anecdote_count is not None else 0
    except (TypeError, ValueError):
        anecdote_count = 0

    return {
        "archetype": archetype if isinstance(archetype, str) else None,
        "industry": industry if isinstance(industry, str) and industry else None,
        "role": role if isinstance(role, str) else None,
        "top_problems": [str(x) for x in top_problems if x],
        "desired_outcomes": [str(x) for x in desired_outcomes if x],
        "coding_languages": [str(x) for x in coding_languages if x],
        "project_count": project_count,
        "last_active": last_active_raw if isinstance(last_active_raw, str) else None,
        "last_active_dt": last_active_dt,
        "anecdote_count": anecdote_count,
    }


def _industry_matches(persona_industry: Optional[str], block_industry: Optional[str]) -> bool:
    """Decide if the persona's industry contains/matches the mapping block's industry.

    Both sides are lower-cased and stripped. The mapping side may be a slug
    like ``industry-engineering``; we strip the leading ``industry-`` for
    comparison. ``persona_industry`` can be free-form (e.g. ``data engineering``).
    """
    if not persona_industry or not block_industry:
        return False
    p = persona_industry.strip().lower()
    b = block_industry.strip().lower()
    if b.startswith("industry-"):
        b = b[len("industry-"):]
    if not p or not b:
        return False
    return b in p or p in b


def _goal_alignment(top_problems: List[str], project_template: str) -> bool:
    """Substring-match a project_template name against any top_problem string."""
    if not project_template:
        return False
    name = project_template.lower()
    # Tokenize the template name on hyphens so 'content-research' aligns with
    # a problem like 'doing research for content briefs'.
    tokens = [t for t in name.replace("_", "-").split("-") if t]
    for problem in top_problems:
        if not isinstance(problem, str):
            continue
        prob_low = problem.lower()
        if name in prob_low:
            return True
        for tok in tokens:
            if len(tok) >= 3 and tok in prob_low:
                return True
    return False


def score_archetype_match(
    persona: Dict[str, Any],
    archetype: str,
    industry: Optional[str],
    mapping: Dict[str, Any],
) -> Tuple[float, List[str]]:
    """Score a (archetype, industry) combo against the persona.

    Args:
      persona: parsed persona frontmatter (the ``frontmatter`` dict from
        ``persona_mod.parse_persona``).
      archetype: archetype key being scored, e.g. ``"job"``.
      industry: the industry sub-key from the mapping (without the
        ``industry-`` prefix), or ``None`` for the ``default`` block.
      mapping: full parsed mapping (used to look up project_templates for
        goal-alignment).

    Returns ``(score_in_0_1, reasoning_signals)``. Score components are
    bounded so the total cannot exceed 1.0 even with all factors firing.
    """
    signals: List[str] = []
    score = 0.0

    sig = _extract_skill_signals(persona)

    # Archetype match.
    if sig["archetype"] and sig["archetype"] == archetype:
        score += WEIGHT_ARCHETYPE
        signals.append(f"archetype matches persona ({archetype})")
    elif sig["archetype"] is None:
        signals.append("persona has no archetype set")

    # Industry match.
    if industry and _industry_matches(sig["industry"], industry):
        score += WEIGHT_INDUSTRY
        signals.append(
            f"industry '{industry}' matches persona industry '{sig['industry']}'"
        )

    # Goal alignment: any top_problem substring-matches a project_template name.
    archetypes = mapping.get("archetypes", {}) if isinstance(mapping, dict) else {}
    arch_block = archetypes.get(archetype) if isinstance(archetypes, dict) else None
    if isinstance(arch_block, dict):
        block_key = (
            f"industry-{industry}" if industry else "default"
        )
        sub_block = arch_block.get(block_key)
        if isinstance(sub_block, dict):
            templates = sub_block.get("project_templates") or []
            if isinstance(templates, list):
                for tpl in templates:
                    if isinstance(tpl, str) and _goal_alignment(sig["top_problems"], tpl):
                        score += WEIGHT_GOAL
                        signals.append(
                            f"goal '{tpl}' aligns with persona top_problems"
                        )
                        break  # at most one goal-alignment bonus per combo

    # Activity recency.
    last_active_dt = sig["last_active_dt"]
    if last_active_dt is not None:
        delta = _now_utc() - last_active_dt
        if 0 <= delta.days <= RECENCY_DAYS:
            score += WEIGHT_RECENCY
            signals.append(
                f"persona active within {RECENCY_DAYS} days "
                f"(last_active={sig['last_active']})"
            )

    # Starter boost for low project counts (anecdote diversity proxy).
    if sig["project_count"] < STARTER_PROJECT_COUNT_THRESHOLD:
        score += WEIGHT_STARTER
        signals.append(
            f"starter boost (project_count={sig['project_count']} < "
            f"{STARTER_PROJECT_COUNT_THRESHOLD})"
        )

    # Clamp to [0, 1] for the contract; defensive since weights already sum to 1.0.
    if score < 0.0:
        score = 0.0
    if score > 1.0:
        score = 1.0
    return score, signals


def _iter_mapping_combos(
    mapping: Dict[str, Any],
) -> List[Tuple[str, Optional[str], Dict[str, Any]]]:
    """Walk every (archetype, industry-or-None, sub_block) tuple in the mapping.

    ``industry`` is the slug WITHOUT the ``industry-`` prefix; ``None`` for
    the ``default`` sub-block.
    """
    out: List[Tuple[str, Optional[str], Dict[str, Any]]] = []
    archetypes = mapping.get("archetypes", {}) if isinstance(mapping, dict) else {}
    if not isinstance(archetypes, dict):
        return out
    for arch_name, arch_block in archetypes.items():
        if not isinstance(arch_block, dict):
            continue
        for sub_key, sub_block in arch_block.items():
            if not isinstance(sub_block, dict):
                continue
            if sub_key == "default":
                out.append((arch_name, None, sub_block))
            elif isinstance(sub_key, str) and sub_key.startswith("industry-"):
                out.append((arch_name, sub_key[len("industry-"):], sub_block))
    return out


def recommend(
    persona_path: Path,
    mapping_path: Path,
    top_n: int = 5,
) -> Dict[str, Any]:
    """Recommend next projects based on persona + mapping.

    Reads the persona at ``persona_path`` (raises ``FileNotFoundError`` if
    missing) and the mapping at ``mapping_path`` (delegates errors to
    ``suggest.load_mapping``). Scores every (archetype, industry,
    project_template) combo and returns the top ``top_n`` by score.

    Each project_template in a sub-block is emitted as its own
    recommendation so the user sees a flat ranked list of projects, not
    archetype/industry combos.

    Pure computation: no network, no subprocess, no LLM call.
    """
    persona_path = Path(persona_path)
    mapping_path = Path(mapping_path)

    if not persona_path.exists():
        raise FileNotFoundError(f"persona file not found: {persona_path}")

    parsed = persona_mod.parse_persona(persona_path)
    persona_fm = parsed.get("frontmatter") or {}
    sig = _extract_skill_signals(persona_fm)

    warnings: List[str] = []
    if not sig["archetype"]:
        warnings.append(
            "persona has no archetype set; results will rely on goals + activity only"
        )
    if sig["anecdote_count"] == 0 and sig["project_count"] == 0:
        warnings.append(
            "persona has no anecdotes or project history yet; "
            "recommendations are low-confidence"
        )

    mapping = suggest_mod.load_mapping(mapping_path)

    raw: List[Dict[str, Any]] = []
    for archetype, industry, sub_block in _iter_mapping_combos(mapping):
        templates = sub_block.get("project_templates") or []
        if not isinstance(templates, list):
            continue
        raw_skills = sub_block.get("claude_skills") or []
        if not isinstance(raw_skills, list):
            raw_skills = []
        skill_names: List[str] = []
        for s in raw_skills:
            if isinstance(s, dict):
                nm = s.get("name")
                if isinstance(nm, str) and nm:
                    skill_names.append(nm)

        score, why = score_archetype_match(persona_fm, archetype, industry, mapping)

        for tpl in templates:
            if not isinstance(tpl, str) or not tpl:
                continue
            # Per-template goal alignment is ALSO worth surfacing in why.
            tpl_why = list(why)
            if _goal_alignment(sig["top_problems"], tpl) and not any(
                "goal" in w and tpl in w for w in tpl_why
            ):
                tpl_why.append(f"template '{tpl}' aligns with stated goals")
            raw.append({
                "project_template": tpl,
                "archetype": archetype,
                "industry": industry,
                "skills": list(skill_names),
                "score": round(score, 4),
                "why": tpl_why,
            })

    # Deterministic sort: highest score first, then archetype/industry/template.
    raw.sort(
        key=lambda r: (
            -r["score"],
            r["archetype"],
            r["industry"] or "",
            r["project_template"],
        )
    )

    if top_n is None or top_n < 0:
        top_n = 5
    top = raw[: int(top_n)]

    reasoning: List[str] = []
    if sig["archetype"]:
        reasoning.append(
            f"persona archetype is '{sig['archetype']}'; "
            "matching combos ranked higher"
        )
    if sig["industry"]:
        reasoning.append(
            f"persona industry '{sig['industry']}' boosts industry-aligned blocks"
        )
    if sig["top_problems"]:
        reasoning.append(
            f"goals: {', '.join(sig['top_problems'][:3])}"
            + (" ..." if len(sig["top_problems"]) > 3 else "")
        )
    if not reasoning:
        reasoning.append(
            "no strong persona signals; recommending starter combos"
        )

    return {
        "recommendations": top,
        "reasoning": reasoning,
        "persona_signals": {
            "archetype": sig["archetype"],
            "industry": sig["industry"],
            "role": sig["role"],
            "top_problems": sig["top_problems"],
            "project_count": sig["project_count"],
            "anecdote_count": sig["anecdote_count"],
            "last_active": sig["last_active"],
        },
        "warnings": warnings,
    }


__all__ = [
    "recommend",
    "score_archetype_match",
    "_extract_skill_signals",
]
