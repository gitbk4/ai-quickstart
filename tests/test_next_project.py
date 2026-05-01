"""Tests for scripts/next_project.py.

Covers:
  * recommend() happy path with stocked persona + the real
    mappings/personas.yaml file (smoke-tests integration with the
    curated mapping).
  * recommend() with persona that has no anecdotes -> low-confidence warning.
  * recommend() with archetype "exploring".
  * recommend() respects top_n.
  * recommend() raises FileNotFoundError when persona is missing.
  * recommend() raises a clear error when mapping is missing.
  * Determinism: identical persona + mapping produce identical output.
  * score_archetype_match(): archetype match vs mismatch, industry boost,
    goal-alignment boost, recency boost, starter boost.
  * _extract_skill_signals(): defaults, malformed types, ISO parse.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import next_project as nxt  # noqa: E402
import persona as persona_mod  # noqa: E402

REAL_MAPPING = REPO_ROOT / "mappings" / "personas.yaml"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stocked_persona(
    archetype: str = "job",
    industry: str = "engineering",
    role: str = "data engineer",
    top_problems: List[str] = None,
    project_count: int = 5,
    anecdote_count: int = 4,
    last_active_days_ago: int = 1,
) -> Dict[str, Any]:
    if top_problems is None:
        top_problems = ["pipeline reliability", "code review fatigue"]
    last_active = datetime.now(timezone.utc) - timedelta(days=last_active_days_ago)
    fm = persona_mod.default_persona()
    fm["identity"]["archetype"] = archetype
    fm["identity"]["industry"] = industry
    fm["identity"]["role"] = role
    fm["goals"]["top_problems"] = list(top_problems)
    fm["goals"]["desired_outcomes"] = ["ship more reliably"]
    fm["activity"]["project_count"] = project_count
    fm["activity"]["last_active"] = last_active.strftime("%Y-%m-%dT%H:%M:%SZ")
    fm["generated"]["anecdote_count"] = anecdote_count
    return fm


@pytest.fixture
def persona_path(tmp_path: Path) -> Path:
    """Write a stocked persona file and return its path."""
    p = tmp_path / "persona.md"
    fm = _stocked_persona()
    persona_mod.write_persona(p, fm, "I am a data engineer who ships pipelines.")
    return p


@pytest.fixture
def empty_persona_path(tmp_path: Path) -> Path:
    p = tmp_path / "persona.md"
    fm = persona_mod.default_persona()  # all defaults: no archetype yet, etc.
    fm["identity"]["archetype"] = ""  # explicit no-archetype
    fm["activity"]["project_count"] = 0
    fm["generated"]["anecdote_count"] = 0
    persona_mod.write_persona(p, fm, "")
    return p


@pytest.fixture
def exploring_persona_path(tmp_path: Path) -> Path:
    p = tmp_path / "persona.md"
    fm = _stocked_persona(
        archetype="exploring",
        industry="",
        role="curious dev",
        top_problems=["learning MCP basics"],
        project_count=0,
        anecdote_count=0,
        last_active_days_ago=2,
    )
    persona_mod.write_persona(p, fm, "Just exploring AI tools.")
    return p


@pytest.fixture
def small_mapping_path(tmp_path: Path) -> Path:
    """Write a minimal but valid mapping file for focused unit tests."""
    text = """schema_version: 1
archetypes:
  job:
    industry-engineering:
      project_templates: [code-review-bot, doc-generator]
      claude_skills:
        - name: claude-code-reference
          description: "Reference patterns"
          github: anthropics/claude-code
      mcp_servers:
        - id: github
          description: "GitHub access"
          search_keywords: [github]
    industry-marketing:
      project_templates: [content-research]
      claude_skills:
        - name: research-assistant
          description: "Research summaries"
          github: anthropics/anthropic-cookbook
      mcp_servers:
        - id: brave-search
          description: "Web search"
          search_keywords: [search]
  exploring:
    default:
      project_templates: [first-skill, mcp-hello-world]
      claude_skills:
        - name: anthropic-cookbook
          description: "Recipes"
          github: anthropics/anthropic-cookbook
      mcp_servers:
        - id: fetch
          description: "HTTP fetch"
          search_keywords: [fetch]
"""
    path = tmp_path / "mapping.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _extract_skill_signals
# ---------------------------------------------------------------------------


def test_extract_skill_signals_defaults_on_empty_persona():
    out = nxt._extract_skill_signals({})
    assert out["archetype"] is None
    assert out["industry"] is None
    assert out["role"] is None
    assert out["top_problems"] == []
    assert out["coding_languages"] == []
    assert out["project_count"] == 0
    assert out["anecdote_count"] == 0
    assert out["last_active"] is None
    assert out["last_active_dt"] is None


def test_extract_skill_signals_full_persona():
    fm = _stocked_persona()
    out = nxt._extract_skill_signals(fm)
    assert out["archetype"] == "job"
    assert out["industry"] == "engineering"
    assert out["role"] == "data engineer"
    assert "pipeline reliability" in out["top_problems"]
    assert out["project_count"] == 5
    assert out["anecdote_count"] == 4
    assert out["last_active_dt"] is not None


def test_extract_skill_signals_handles_malformed_types():
    fm = {
        "identity": {"archetype": 42, "industry": None, "role": ["not", "a", "str"]},
        "goals": {"top_problems": "oops not a list"},
        "preferences": {"coding_languages": None},
        "activity": {"project_count": "nine", "last_active": "garbage"},
        "generated": {"anecdote_count": None},
    }
    out = nxt._extract_skill_signals(fm)
    # archetype is 42 (not str) -> None
    assert out["archetype"] is None
    # industry None -> None
    assert out["industry"] is None
    # role is a list -> None
    assert out["role"] is None
    # top_problems wasn't a list -> []
    assert out["top_problems"] == []
    # project_count couldn't parse -> 0
    assert out["project_count"] == 0
    # last_active malformed -> string passes through but dt is None
    assert out["last_active"] == "garbage"
    assert out["last_active_dt"] is None


def test_extract_skill_signals_iso_with_z_suffix():
    fm = persona_mod.default_persona()
    fm["activity"]["last_active"] = "2026-01-15T12:00:00Z"
    out = nxt._extract_skill_signals(fm)
    assert out["last_active_dt"] is not None
    assert out["last_active_dt"].year == 2026


# ---------------------------------------------------------------------------
# score_archetype_match
# ---------------------------------------------------------------------------


def test_score_archetype_match_matches_archetype_only(small_mapping_path):
    import suggest as suggest_mod
    mapping = suggest_mod.load_mapping(small_mapping_path)
    fm = _stocked_persona(
        archetype="job", industry="", project_count=10,
        anecdote_count=10, last_active_days_ago=400,
        top_problems=["unrelated stuff"],
    )
    score, why = nxt.score_archetype_match(fm, "job", "engineering", mapping)
    # No industry, no goal alignment, no recency, no starter:
    # match boost is partial (archetype matches but industry mismatch).
    assert score == pytest.approx(nxt.WEIGHT_ARCHETYPE)
    assert any("archetype matches" in w for w in why)


def test_score_archetype_match_archetype_mismatch_low(small_mapping_path):
    import suggest as suggest_mod
    mapping = suggest_mod.load_mapping(small_mapping_path)
    fm = _stocked_persona(
        archetype="personal", industry="", project_count=10,
        anecdote_count=10, last_active_days_ago=400,
        top_problems=["unrelated"],
    )
    score, why = nxt.score_archetype_match(fm, "job", "engineering", mapping)
    assert score == pytest.approx(0.0)
    assert not any("archetype matches" in w for w in why)


def test_score_archetype_match_industry_boost(small_mapping_path):
    import suggest as suggest_mod
    mapping = suggest_mod.load_mapping(small_mapping_path)
    fm = _stocked_persona(
        archetype="job", industry="engineering", project_count=10,
        anecdote_count=10, last_active_days_ago=400,
        top_problems=["unrelated"],
    )
    score, why = nxt.score_archetype_match(fm, "job", "engineering", mapping)
    # archetype + industry, no goal/recency/starter
    assert score == pytest.approx(nxt.WEIGHT_ARCHETYPE + nxt.WEIGHT_INDUSTRY)
    assert any("industry" in w for w in why)


def test_score_archetype_match_goal_alignment_boost(small_mapping_path):
    import suggest as suggest_mod
    mapping = suggest_mod.load_mapping(small_mapping_path)
    fm = _stocked_persona(
        archetype="job", industry="engineering", project_count=10,
        anecdote_count=10, last_active_days_ago=400,
        top_problems=["I need a code-review-bot in my workflow"],
    )
    score, why = nxt.score_archetype_match(fm, "job", "engineering", mapping)
    # archetype + industry + goal-alignment.
    assert score == pytest.approx(
        nxt.WEIGHT_ARCHETYPE + nxt.WEIGHT_INDUSTRY + nxt.WEIGHT_GOAL
    )
    assert any("goal" in w.lower() for w in why)


def test_score_archetype_match_recency_boost(small_mapping_path):
    import suggest as suggest_mod
    mapping = suggest_mod.load_mapping(small_mapping_path)
    # Active recently, but archetype/industry mismatch and no goal alignment.
    fm = _stocked_persona(
        archetype="personal", industry="", project_count=10,
        anecdote_count=10, last_active_days_ago=5,
        top_problems=["random"],
    )
    score, why = nxt.score_archetype_match(fm, "job", "engineering", mapping)
    assert score == pytest.approx(nxt.WEIGHT_RECENCY)
    assert any("active within" in w for w in why)


def test_score_archetype_match_starter_boost(small_mapping_path):
    import suggest as suggest_mod
    mapping = suggest_mod.load_mapping(small_mapping_path)
    fm = _stocked_persona(
        archetype="personal", industry="", project_count=0,
        anecdote_count=0, last_active_days_ago=400,
        top_problems=["random"],
    )
    score, why = nxt.score_archetype_match(fm, "job", "engineering", mapping)
    assert score == pytest.approx(nxt.WEIGHT_STARTER)
    assert any("starter boost" in w for w in why)


def test_score_archetype_match_score_is_clamped_to_one(small_mapping_path):
    import suggest as suggest_mod
    mapping = suggest_mod.load_mapping(small_mapping_path)
    fm = _stocked_persona(
        archetype="job", industry="engineering", project_count=0,
        anecdote_count=0, last_active_days_ago=1,
        top_problems=["I need a code-review-bot"],
    )
    score, _ = nxt.score_archetype_match(fm, "job", "engineering", mapping)
    assert score <= 1.0
    # All five factors fire.
    assert score == pytest.approx(
        nxt.WEIGHT_ARCHETYPE + nxt.WEIGHT_INDUSTRY + nxt.WEIGHT_GOAL
        + nxt.WEIGHT_RECENCY + nxt.WEIGHT_STARTER
    )


# ---------------------------------------------------------------------------
# recommend()
# ---------------------------------------------------------------------------


def test_recommend_happy_path_with_real_mapping(persona_path):
    out = nxt.recommend(persona_path, REAL_MAPPING, top_n=5)
    assert isinstance(out, dict)
    assert "recommendations" in out
    assert "reasoning" in out
    assert "persona_signals" in out
    assert "warnings" in out
    recs = out["recommendations"]
    assert 0 < len(recs) <= 5
    # Top recommendation should include a project_template, archetype,
    # skills list, score, why.
    top = recs[0]
    for k in ("project_template", "archetype", "industry", "skills", "score", "why"):
        assert k in top
    assert isinstance(top["score"], float)
    assert 0.0 <= top["score"] <= 1.0
    # Persona was stocked job/engineering, so top should be archetype=job.
    assert top["archetype"] == "job"


def test_recommend_no_anecdotes_emits_low_confidence_warning(empty_persona_path):
    out = nxt.recommend(empty_persona_path, REAL_MAPPING, top_n=3)
    assert any("low-confidence" in w for w in out["warnings"])
    # Still returns something (curated starter combos).
    assert len(out["recommendations"]) > 0


def test_recommend_archetype_exploring(exploring_persona_path):
    out = nxt.recommend(exploring_persona_path, REAL_MAPPING, top_n=5)
    # Top recommendation should be the exploring archetype.
    assert out["recommendations"][0]["archetype"] == "exploring"
    # Persona signals reflect the exploring persona.
    assert out["persona_signals"]["archetype"] == "exploring"


def test_recommend_respects_top_n(persona_path):
    out_three = nxt.recommend(persona_path, REAL_MAPPING, top_n=3)
    out_one = nxt.recommend(persona_path, REAL_MAPPING, top_n=1)
    assert len(out_three["recommendations"]) <= 3
    assert len(out_one["recommendations"]) == 1
    # The single top recommendation should equal out_three[0].
    assert out_one["recommendations"][0] == out_three["recommendations"][0]


def test_recommend_top_n_zero_returns_empty(persona_path):
    out = nxt.recommend(persona_path, REAL_MAPPING, top_n=0)
    assert out["recommendations"] == []


def test_recommend_top_n_negative_falls_back_to_default(persona_path):
    out = nxt.recommend(persona_path, REAL_MAPPING, top_n=-1)
    assert 0 < len(out["recommendations"]) <= 5


def test_recommend_persona_missing_raises(tmp_path):
    missing = tmp_path / "no.md"
    with pytest.raises(FileNotFoundError):
        nxt.recommend(missing, REAL_MAPPING, top_n=5)


def test_recommend_mapping_missing_raises(persona_path, tmp_path):
    missing = tmp_path / "no-mapping.yaml"
    with pytest.raises(FileNotFoundError):
        nxt.recommend(persona_path, missing, top_n=5)


def test_recommend_is_deterministic(persona_path):
    a = nxt.recommend(persona_path, REAL_MAPPING, top_n=5)
    b = nxt.recommend(persona_path, REAL_MAPPING, top_n=5)
    # JSON-serializable identical output.
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_recommend_reasoning_includes_archetype_when_set(persona_path):
    out = nxt.recommend(persona_path, REAL_MAPPING, top_n=5)
    assert any("archetype" in r for r in out["reasoning"])


def test_recommend_reasoning_falls_back_when_no_signals(empty_persona_path):
    out = nxt.recommend(empty_persona_path, REAL_MAPPING, top_n=5)
    # No archetype, no industry, no goals -> fallback reasoning.
    assert len(out["reasoning"]) >= 1


def test_recommend_persona_signals_reflect_persona(persona_path):
    out = nxt.recommend(persona_path, REAL_MAPPING, top_n=5)
    sig = out["persona_signals"]
    assert sig["archetype"] == "job"
    assert sig["industry"] == "engineering"
    assert sig["role"] == "data engineer"
    assert sig["project_count"] == 5
    assert sig["anecdote_count"] == 4


def test_recommend_skills_list_is_populated(persona_path):
    out = nxt.recommend(persona_path, REAL_MAPPING, top_n=5)
    # The top job/engineering combo should carry the skill names from the
    # curated mapping (claude-code-reference, mcp-server-examples).
    top_skills = out["recommendations"][0]["skills"]
    assert isinstance(top_skills, list)
    # Curated mapping has at least one skill name for the engineering combo.
    assert len(top_skills) >= 1


def test_recommend_with_small_mapping(persona_path, small_mapping_path):
    """End-to-end smoke against a synthetic mapping (decoupled from curated YAML)."""
    out = nxt.recommend(persona_path, small_mapping_path, top_n=10)
    # job/engineering combo has two project_templates; both should appear.
    templates = [r["project_template"] for r in out["recommendations"]]
    assert "code-review-bot" in templates
    assert "doc-generator" in templates


def test_recommend_sort_order_by_score(persona_path, small_mapping_path):
    out = nxt.recommend(persona_path, small_mapping_path, top_n=10)
    scores = [r["score"] for r in out["recommendations"]]
    assert scores == sorted(scores, reverse=True)


def test_recommend_handles_persona_with_malformed_frontmatter(tmp_path):
    """parse_persona logs and returns defaults; recommend must still produce output."""
    p = tmp_path / "persona.md"
    p.write_text(
        "---\n: invalid yaml\n---\nbody",
        encoding="utf-8",
    )
    out = nxt.recommend(p, REAL_MAPPING, top_n=3)
    # Should not raise; archetype falls back to default ("exploring") in
    # default_persona, so the warnings depend on what parse_persona returned.
    # The contract is: function returns a dict with recommendations.
    assert "recommendations" in out
    assert isinstance(out["recommendations"], list)
