"""Tests for scripts/interview.py.

Covers:
  * start_session validates archetype and writes a step-1 prompt file at the
    expected on-disk path under AI_QUICKSTART_HOME.
  * record_answers + read_answers roundtrip preserves arbitrary JSON-shaped
    answers exactly.
  * read_answers returns None for both missing and malformed answers.json.
  * compose_step2_context renders the step-2 template, persists the result
    on disk, and includes the substituted variables.

Tests use ``tmp_path`` plus ``AI_QUICKSTART_HOME`` to keep filesystem state
isolated. They never touch the network and never call the ``prompts``
module beyond what is already exercised through ``interview``.

Run with: ``python3 -m pytest tests/test_interview.py -v``
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import interview  # noqa: E402
import prompts  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect AI_QUICKSTART_HOME to a tmp dir for every test."""
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------


def test_start_session_writes_step1_prompt_at_expected_path(isolated_home):
    out = interview.start_session("job", run_id="20260429T100000Z-aaaaaa")

    assert out["run_id"] == "20260429T100000Z-aaaaaa"
    assert out["archetype"] == "job"

    prompt_path = Path(out["prompt_path"])
    expected = isolated_home / "runs" / "20260429T100000Z-aaaaaa" / "step-1-prompt.md"
    assert prompt_path == expected
    assert prompt_path.exists()

    body = prompt_path.read_text(encoding="utf-8")
    # Adversarial framing + archetype reference must appear in the prompt body.
    assert "Adversarial framing" in body
    assert "'job'" in body


def test_start_session_allocates_run_id_when_omitted():
    out = interview.start_session("personal")
    rid = out["run_id"]
    # Format documented in prompts.make_run_id().
    assert len(rid) > 10 and "-" in rid
    # The prompt was written to the corresponding run directory.
    assert Path(out["prompt_path"]).exists()


@pytest.mark.parametrize("bad", ["", "Job", "founder", "JOB", "explore"])
def test_start_session_rejects_unknown_archetype(bad):
    with pytest.raises(ValueError):
        interview.start_session(bad)


def test_start_session_includes_started_at_iso(isolated_home):
    out = interview.start_session("exploring")
    # "%Y-%m-%dT%H:%M:%SZ" — 20 chars, ends in Z.
    assert out["started_at"].endswith("Z")
    assert len(out["started_at"]) == 20


# ---------------------------------------------------------------------------
# record_answers / read_answers
# ---------------------------------------------------------------------------


def test_record_and_read_answers_roundtrip(isolated_home):
    rid = "20260429T100100Z-bbbbbb"
    answers = {
        "archetype": "job",
        "role": "demand-gen lead",
        "industry": "marketing",
        "top_problems": ["lead routing", "attribution"],
        "desired_outcomes": ["weekly report", "automated tagging"],
        "skill_tolerance": "permissive",
        "project_style": "minimal",
        "coding_languages": ["python"],
        "freeform_notes": "notes with unicode: \u2603",
    }
    target = interview.record_answers(rid, answers)
    assert target.exists()
    assert target.name == "answers.json"
    # File body is valid JSON and matches input.
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw == answers
    # read_answers returns the same dict.
    assert interview.read_answers(rid) == answers


def test_record_answers_is_atomic_no_tmp_left(isolated_home):
    rid = "20260429T100200Z-cccccc"
    interview.record_answers(rid, {"archetype": "job"})
    run_dir = isolated_home / "runs" / rid
    leftovers = [p.name for p in run_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_record_answers_overwrites_previous(isolated_home):
    rid = "20260429T100300Z-dddddd"
    interview.record_answers(rid, {"archetype": "job", "role": "v1"})
    interview.record_answers(rid, {"archetype": "job", "role": "v2"})
    assert interview.read_answers(rid) == {"archetype": "job", "role": "v2"}


def test_record_answers_rejects_non_dict():
    with pytest.raises(TypeError):
        interview.record_answers("rid-1", ["not", "a", "dict"])  # type: ignore[arg-type]


def test_record_answers_rejects_empty_run_id():
    with pytest.raises(ValueError):
        interview.record_answers("", {"archetype": "job"})


def test_read_answers_returns_none_when_missing(isolated_home):
    rid = "20260429T100400Z-eeeeee"
    # Directory created lazily by _answers_path; file itself absent.
    assert interview.read_answers(rid) is None


def test_read_answers_returns_none_when_malformed_json(isolated_home):
    rid = "20260429T100500Z-ffffff"
    run_dir = isolated_home / "runs" / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "answers.json").write_text("not { valid json", encoding="utf-8")
    assert interview.read_answers(rid) is None


def test_read_answers_returns_none_when_top_level_not_dict(isolated_home):
    rid = "20260429T100600Z-gggggg"
    run_dir = isolated_home / "runs" / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "answers.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert interview.read_answers(rid) is None


# ---------------------------------------------------------------------------
# compose_step2_context
# ---------------------------------------------------------------------------


def test_compose_step2_context_renders_and_writes(isolated_home):
    rid = "20260429T100700Z-hhhhhh"
    answers = {
        "archetype": "job",
        "role": "demand-gen lead",
        "industry": "marketing",
        "top_problems": ["lead routing"],
        "desired_outcomes": ["weekly digest"],
        "skill_tolerance": "permissive",
        "project_style": "full",
        "coding_languages": ["python", "sql"],
        "freeform_notes": "wants to ship something this week",
    }
    sources = {
        "project_templates": ["content-research"],
        "skills": [{"name": "research-assistant", "stars": 1234}],
        "mcp_servers": [{"id": "brave-search"}],
        "warnings": ["mcpmarket: parser fell back"],
    }

    rendered = interview.compose_step2_context(rid, answers, sources)

    # Substitutions resolved; no leftover ${...} placeholders.
    assert "${" not in rendered
    assert rid in rendered
    assert "demand-gen lead" in rendered
    assert "marketing" in rendered
    # Answers and source-result summaries both make it in.
    assert "lead routing" in rendered
    assert "weekly digest" in rendered
    assert "wants to ship something this week" in rendered
    assert "Source query results" in rendered
    assert "1 item" in rendered  # from "skills: 1 item(s)"
    assert "mcpmarket: parser fell back" in rendered

    # Persisted at the expected path.
    written = isolated_home / "runs" / rid / "step-2-prompt.md"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == rendered


def test_compose_step2_context_handles_minimal_answers(isolated_home):
    """Missing optional answer keys fall back to ``(not stated)`` placeholders."""
    rid = "20260429T100800Z-iiiiii"
    rendered = interview.compose_step2_context(rid, {"archetype": "exploring"}, {})
    assert "(not stated)" in rendered
    assert "exploring" in rendered
    # No source results -> the explicit no-results sentinel appears.
    assert "No source results" in rendered
    # File was still written.
    assert (isolated_home / "runs" / rid / "step-2-prompt.md").exists()


def test_compose_step2_context_rejects_non_dict_answers():
    with pytest.raises(TypeError):
        interview.compose_step2_context("rid", ["bad"], {})  # type: ignore[arg-type]


def test_compose_step2_context_rejects_non_dict_sources():
    with pytest.raises(TypeError):
        interview.compose_step2_context("rid", {"archetype": "job"}, "bad")  # type: ignore[arg-type]


def test_compose_step2_context_aggregates_warnings(isolated_home):
    rid = "20260429T100900Z-jjjjjj"
    rendered = interview.compose_step2_context(
        rid,
        {"archetype": "personal"},
        {
            "project_templates": [],
            "skills": [],
            "mcp_servers": [],
            "warnings": ["alpha warning", "beta warning"],
        },
    )
    assert "alpha warning" in rendered
    assert "beta warning" in rendered


def test_default_home_used_when_env_unset(tmp_path, monkeypatch):
    """When AI_QUICKSTART_HOME is unset, ``Path.home()/.ai-quickstart`` is used."""
    monkeypatch.delenv("AI_QUICKSTART_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    rid = "20260429T101000Z-kkkkkk"
    interview.record_answers(rid, {"archetype": "job", "role": "x"})

    expected = tmp_path / ".ai-quickstart" / "runs" / rid / "answers.json"
    assert expected.exists()
    assert interview.read_answers(rid) == {"archetype": "job", "role": "x"}
