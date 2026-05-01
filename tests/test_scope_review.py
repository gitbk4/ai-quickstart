"""Tests for scripts/scope_review.py — Lane O (Phase 2.5 plan-ceo-review hookup).

Covers:
  * prepare() with full answers + suggestions: plan doc has every required
    section header and the title is shaped as expected.
  * prepare() with sparse / missing answer fields: the doc still renders
    and missing fields show as ``(not stated)`` placeholders rather than
    raising.
  * prepare() writes atomically: no leftover ``.tmp`` files are visible
    after a successful write.
  * prepare() output is human-readable markdown: starts with ``# `` and
    contains all six expected ``## `` section headers.
  * prepare_invocation_prompt() includes the plan content + a framing
    line referencing the project slug; raises FileNotFoundError when the
    plan does not exist.
  * read_review_outcome() returns None when the outcome file is missing
    and a populated dict (with ``content``) when present.
  * Determinism: same inputs produce byte-for-byte identical plan output
    across calls.

Run with: ``python3 -m pytest tests/test_scope_review.py -v``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scope_review  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect AI_QUICKSTART_HOME to a tmp dir for every test."""
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def full_answers() -> Dict[str, Any]:
    return {
        "archetype": "job",
        "role": "growth marketer",
        "industry": "publishing",
        "top_problems": [
            "I spend too long researching topics manually",
            "I cannot keep up with industry news",
        ],
        "desired_outcomes": [
            "30 minutes saved per content brief",
            "weekly trend digest in my inbox",
        ],
        "skill_tolerance": "strict",
        "project_style": "minimal",
        "coding_languages": ["python"],
        "freeform_notes": "I have already tried no-code tools and they were too generic.",
    }


@pytest.fixture
def full_suggestions() -> Dict[str, Any]:
    return {
        "project_templates": ["content-research", "audience-personas"],
        "skills": [
            {
                "name": "research-assistant",
                "description": "Research and summarize web sources",
                "github": "anthropics/anthropic-cookbook",
                "stars": 12345,
                "source_tier": "github",
                "warnings": [],
            },
            {
                "name": "courses-curriculum",
                "description": "Curriculum patterns",
                "github": "anthropics/courses",
                "stars": 800,
                "source_tier": "github",
                "warnings": [],
            },
        ],
        "mcp_servers": [
            {
                "id": "brave-search",
                "description": "Web search via Brave",
                "registry_match": True,
                "source_tier": "mcp-registry",
            }
        ],
        "warnings": [],
    }


@pytest.fixture
def project_spec() -> Dict[str, Any]:
    return {
        "slug": "content-research",
        "project_template": "content-research",
        "dir": "/Users/example/Code/content-research",
        "anecdote_seed": "Built to cut research time on weekly content briefs.",
    }


# ---------------------------------------------------------------------------
# prepare()
# ---------------------------------------------------------------------------


def test_prepare_full_inputs_has_all_required_sections(
    isolated_home, full_answers, full_suggestions, project_spec
):
    plan_path = scope_review.prepare(
        run_id="20260429T100000Z-aaaaaa",
        project_spec=project_spec,
        answers=full_answers,
        suggestions=full_suggestions,
    )

    assert plan_path.exists()
    body = plan_path.read_text(encoding="utf-8")

    # Title shaped as the spec demands.
    assert body.startswith("# Project Plan: content-research"), body[:80]

    # All six required section headers present and in order.
    expected_headers = [
        "## Problem statement",
        "## Proposed scope",
        "## User profile",
        "## Constraints",
        "## Open questions / Where pressure-testing helps",
        "## Context for the reviewer",
    ]
    last_idx = -1
    for h in expected_headers:
        idx = body.find(h)
        assert idx != -1, f"missing section: {h}"
        assert idx > last_idx, f"section out of order: {h}"
        last_idx = idx

    # Concrete content from answers leaks through.
    assert "growth marketer" in body
    assert "publishing" in body
    assert "I spend too long researching topics manually" in body
    assert "research-assistant" in body
    assert "brave-search" in body
    # Free-form notes are quoted (markdown blockquote).
    assert "> I have already tried no-code tools" in body


def test_prepare_writes_to_expected_path(
    isolated_home, full_answers, full_suggestions, project_spec
):
    plan_path = scope_review.prepare(
        run_id="20260429T100000Z-bbbbbb",
        project_spec=project_spec,
        answers=full_answers,
        suggestions=full_suggestions,
    )
    expected = (
        isolated_home / "runs" / "20260429T100000Z-bbbbbb" / "scope-review-plan.md"
    )
    assert plan_path == expected


def test_prepare_atomic_no_leftover_tmp(
    isolated_home, full_answers, full_suggestions, project_spec
):
    plan_path = scope_review.prepare(
        run_id="rid-atomic",
        project_spec=project_spec,
        answers=full_answers,
        suggestions=full_suggestions,
    )
    run_dir = plan_path.parent
    leftovers = [p for p in run_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"unexpected tmp files: {leftovers}"


def test_prepare_starts_with_h1_and_is_human_readable(
    isolated_home, full_answers, full_suggestions, project_spec
):
    plan_path = scope_review.prepare(
        run_id="rid-readable",
        project_spec=project_spec,
        answers=full_answers,
        suggestions=full_suggestions,
    )
    body = plan_path.read_text(encoding="utf-8")
    # Markdown that a human reviewer can read: starts with H1, ends with a
    # newline, no template literals like ${var} left unsubstituted.
    assert body.startswith("# "), body[:40]
    assert body.endswith("\n")
    assert "${" not in body


def test_prepare_with_missing_fields_uses_placeholders(isolated_home, project_spec):
    # Sparse answers: only archetype is set. Suggestions empty.
    answers = {"archetype": "exploring"}
    suggestions: Dict[str, Any] = {}

    plan_path = scope_review.prepare(
        run_id="rid-sparse",
        project_spec=project_spec,
        answers=answers,
        suggestions=suggestions,
    )
    body = plan_path.read_text(encoding="utf-8")

    # Required structure still intact.
    assert body.startswith("# Project Plan: content-research")
    for h in ["## Problem statement", "## User profile", "## Constraints"]:
        assert h in body
    # Missing fields rendered as the documented placeholder.
    assert "(not stated)" in body
    # Empty suggestions don't crash; the proposed scope section still appears.
    assert "## Proposed scope" in body


def test_prepare_with_empty_lists_does_not_crash(isolated_home, project_spec):
    answers = {
        "archetype": "personal",
        "top_problems": [],
        "desired_outcomes": [],
        "coding_languages": [],
    }
    suggestions = {
        "project_templates": [],
        "skills": [],
        "mcp_servers": [],
        "warnings": [],
    }
    plan_path = scope_review.prepare(
        run_id="rid-empty-lists",
        project_spec=project_spec,
        answers=answers,
        suggestions=suggestions,
    )
    body = plan_path.read_text(encoding="utf-8")
    assert "## Problem statement" in body
    assert "(not stated)" in body


def test_prepare_minimal_project_style_constraint_message(
    isolated_home, full_suggestions, project_spec
):
    answers = {
        "archetype": "job",
        "project_style": "minimal",
        "skill_tolerance": "strict",
    }
    plan_path = scope_review.prepare(
        run_id="rid-minimal",
        project_spec=project_spec,
        answers=answers,
        suggestions=full_suggestions,
    )
    body = plan_path.read_text(encoding="utf-8")
    assert "minimal" in body
    assert "Resist scope expansion" in body
    assert "strict" in body


def test_prepare_full_project_style_constraint_message(
    isolated_home, full_suggestions, project_spec
):
    answers = {
        "archetype": "job",
        "project_style": "full",
        "skill_tolerance": "permissive",
    }
    plan_path = scope_review.prepare(
        run_id="rid-full",
        project_spec=project_spec,
        answers=answers,
        suggestions=full_suggestions,
    )
    body = plan_path.read_text(encoding="utf-8")
    assert "Scope expansion is welcome" in body
    assert "permissive" in body


def test_prepare_time_box_appears_in_constraints(
    isolated_home, full_suggestions, project_spec
):
    answers = {
        "archetype": "job",
        "project_style": "minimal",
        "time_box": "4 hours per week",
    }
    plan_path = scope_review.prepare(
        run_id="rid-timebox",
        project_spec=project_spec,
        answers=answers,
        suggestions=full_suggestions,
    )
    body = plan_path.read_text(encoding="utf-8")
    assert "4 hours per week" in body


def test_prepare_rejects_non_dict_inputs(isolated_home, project_spec, full_answers):
    with pytest.raises(TypeError):
        scope_review.prepare(
            run_id="rid-x",
            project_spec="not a dict",  # type: ignore[arg-type]
            answers=full_answers,
            suggestions={},
        )
    with pytest.raises(TypeError):
        scope_review.prepare(
            run_id="rid-x",
            project_spec=project_spec,
            answers="not a dict",  # type: ignore[arg-type]
            suggestions={},
        )
    with pytest.raises(TypeError):
        scope_review.prepare(
            run_id="rid-x",
            project_spec=project_spec,
            answers=full_answers,
            suggestions="not a dict",  # type: ignore[arg-type]
        )


def test_prepare_rejects_empty_run_id(isolated_home, project_spec, full_answers):
    with pytest.raises(ValueError):
        scope_review.prepare(
            run_id="",
            project_spec=project_spec,
            answers=full_answers,
            suggestions={},
        )


def test_prepare_is_deterministic(
    isolated_home, full_answers, full_suggestions, project_spec
):
    """Same inputs produce byte-for-byte identical plan output across calls."""
    p1 = scope_review.prepare(
        run_id="rid-det-1",
        project_spec=project_spec,
        answers=full_answers,
        suggestions=full_suggestions,
    )
    p2 = scope_review.prepare(
        run_id="rid-det-2",
        project_spec=project_spec,
        answers=full_answers,
        suggestions=full_suggestions,
    )
    body1 = p1.read_text(encoding="utf-8")
    body2 = p2.read_text(encoding="utf-8")
    assert body1 == body2


def test_prepare_other_templates_listed_excluding_chosen(
    isolated_home, full_answers, project_spec
):
    suggestions = {
        "project_templates": ["content-research", "audience-personas", "trend-digest"],
        "skills": [],
        "mcp_servers": [],
        "warnings": [],
    }
    plan_path = scope_review.prepare(
        run_id="rid-other-templates",
        project_spec=project_spec,
        answers=full_answers,
        suggestions=suggestions,
    )
    body = plan_path.read_text(encoding="utf-8")
    assert "Other project templates" in body
    assert "audience-personas" in body
    assert "trend-digest" in body
    # The chosen slug is not duplicated in the alternatives list section.
    others_section = body.split("Other project templates the user could pick instead:")[1]
    others_section = others_section.split("##")[0]  # stop at next header
    assert "- content-research" not in others_section


# ---------------------------------------------------------------------------
# prepare_invocation_prompt()
# ---------------------------------------------------------------------------


def test_prepare_invocation_prompt_includes_plan_and_framing(
    isolated_home, full_answers, full_suggestions, project_spec
):
    plan_path = scope_review.prepare(
        run_id="rid-prompt",
        project_spec=project_spec,
        answers=full_answers,
        suggestions=full_suggestions,
    )
    plan_body = plan_path.read_text(encoding="utf-8")

    prompt = scope_review.prepare_invocation_prompt(plan_path, "content-research")

    # Framing line: explicit ask for /plan-ceo-review and the project slug.
    assert "/plan-ceo-review" in prompt
    assert "content-research" in prompt
    # Plan content embedded verbatim.
    assert plan_body in prompt


def test_prepare_invocation_prompt_missing_plan_raises(tmp_path):
    missing = tmp_path / "nope.md"
    with pytest.raises(FileNotFoundError):
        scope_review.prepare_invocation_prompt(missing, "x")


# ---------------------------------------------------------------------------
# read_review_outcome()
# ---------------------------------------------------------------------------


def test_read_review_outcome_missing_returns_none(isolated_home):
    out = scope_review.read_review_outcome("rid-missing", "content-research")
    assert out is None


def test_read_review_outcome_present_returns_dict(isolated_home):
    run_id = "rid-outcome"
    run_dir = isolated_home / "runs" / run_id
    run_dir.mkdir(parents=True)
    target = run_dir / "scope-review-outcome-content-research.md"
    target.write_text("# Outcome\n\nReview found scope is too broad.\n", encoding="utf-8")

    out = scope_review.read_review_outcome(run_id, "content-research")
    assert out is not None
    assert out["path"] == str(target)
    assert "Review found scope is too broad." in out["content"]
    assert out["read_at"].endswith("Z")


def test_read_review_outcome_handles_unsafe_slug(isolated_home):
    run_id = "rid-slug-sanitize"
    run_dir = isolated_home / "runs" / run_id
    run_dir.mkdir(parents=True)
    # The function sanitizes "../etc" to "-etc" before looking up — the
    # sanitized filename "scope-review-outcome--etc.md" doesn't exist, so
    # we expect None (no traversal, no crash).
    out = scope_review.read_review_outcome(run_id, "../etc")
    assert out is None


def test_read_review_outcome_empty_run_id_returns_none(isolated_home):
    assert scope_review.read_review_outcome("", "x") is None


def test_read_review_outcome_handles_unreadable_file(isolated_home, monkeypatch):
    run_id = "rid-unreadable"
    run_dir = isolated_home / "runs" / run_id
    run_dir.mkdir(parents=True)
    target = run_dir / "scope-review-outcome-foo.md"
    target.write_text("body", encoding="utf-8")

    real_read_text = Path.read_text

    def boom(self, *a, **kw):
        if self == target:
            raise OSError("simulated read failure")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    assert scope_review.read_review_outcome(run_id, "foo") is None
