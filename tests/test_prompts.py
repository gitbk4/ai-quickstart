"""Tests for scripts/prompts.py.

Run with: ``python3 -m pytest tests/test_prompts.py -v``
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import prompts  # noqa: E402  (path tweak required first)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect AI_QUICKSTART_HOME to a tmp dir for every test."""
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def template_root():
    return REPO_ROOT / "templates" / "prompts"


# ---------------------------------------------------------------------------
# Run id
# ---------------------------------------------------------------------------


def test_make_run_id_matches_expected_format():
    rid = prompts.make_run_id()
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", rid), rid


def test_make_run_id_is_unique_across_calls():
    ids = {prompts.make_run_id() for _ in range(50)}
    # uuid suffix gives us collision resistance even within the same second
    assert len(ids) == 50


# ---------------------------------------------------------------------------
# Run directory
# ---------------------------------------------------------------------------


def test_ensure_run_dir_creates_path(isolated_home):
    rid = "20260429T134500Z-abcdef"
    path = prompts.ensure_run_dir(rid)
    assert path.exists() and path.is_dir()
    assert path == isolated_home / "runs" / rid


def test_ensure_run_dir_is_idempotent(isolated_home):
    rid = "20260429T134500Z-abcdef"
    first = prompts.ensure_run_dir(rid)
    # drop a sentinel file, call again, verify the dir was not recreated
    sentinel = first / "sentinel.txt"
    sentinel.write_text("hi", encoding="utf-8")
    second = prompts.ensure_run_dir(rid)
    assert first == second
    assert sentinel.exists()
    assert sentinel.read_text(encoding="utf-8") == "hi"


def test_ensure_run_dir_rejects_empty_id():
    with pytest.raises(ValueError):
        prompts.ensure_run_dir("")


def test_ensure_run_dir_uses_default_home_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_QUICKSTART_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    rid = "20260429T134500Z-abcdef"
    path = prompts.ensure_run_dir(rid)
    assert path == tmp_path / ".ai-quickstart" / "runs" / rid
    assert path.exists()


# ---------------------------------------------------------------------------
# Write / read roundtrip
# ---------------------------------------------------------------------------


def test_write_read_roundtrip():
    rid = prompts.make_run_id()
    body = "# Hello\n\nadversarial body\n"
    target = prompts.write_prompt(rid, 1, body)
    assert target.name == "step-1-prompt.md"
    assert prompts.read_prompt(rid, 1) == body


def test_read_prompt_returns_none_when_absent():
    rid = prompts.make_run_id()
    assert prompts.read_prompt(rid, 2) is None


def test_write_prompt_is_atomic_no_tmp_left_behind():
    rid = prompts.make_run_id()
    prompts.write_prompt(rid, 1, "body")
    run_dir = prompts.ensure_run_dir(rid)
    leftovers = [p.name for p in run_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_write_prompt_overwrites_previous_content():
    rid = prompts.make_run_id()
    prompts.write_prompt(rid, 1, "first")
    prompts.write_prompt(rid, 1, "second")
    assert prompts.read_prompt(rid, 1) == "second"


def test_write_prompt_rejects_non_positive_step():
    rid = prompts.make_run_id()
    with pytest.raises(ValueError):
        prompts.write_prompt(rid, 0, "body")


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def test_render_template_substitutes_variables(tmp_path):
    tpl = tmp_path / "t.md.tmpl"
    tpl.write_text("hello ${name}, run=${run_id}", encoding="utf-8")
    out = prompts.render_template(tpl, {"name": "ada", "run_id": "abc"})
    assert out == "hello ada, run=abc"


def test_render_template_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        prompts.render_template(tmp_path / "nope.tmpl", {})


def test_render_template_missing_variable_raises(tmp_path):
    tpl = tmp_path / "t.md.tmpl"
    tpl.write_text("hello ${name}", encoding="utf-8")
    with pytest.raises(KeyError):
        prompts.render_template(tpl, {})


@pytest.mark.parametrize("step", [1, 2, 3])
def test_real_step_templates_render(template_root, step):
    tpl = template_root / f"step-{step}.md.tmpl"
    rendered = prompts.render_template(
        tpl,
        {
            "run_id": "20260429T134500Z-abcdef",
            "prior_summary": "User is a marketing lead at a B2B SaaS company.",
            "user_archetype": "job",
            "user_industry": "marketing",
            "user_role": "demand-gen lead",
        },
    )
    # Substitution actually happened
    assert "20260429T134500Z-abcdef" in rendered
    assert "${" not in rendered
    # Adversarial intent shows up
    assert "adversarial" in rendered.lower() or "challenge" in rendered.lower() or "push" in rendered.lower()


# ---------------------------------------------------------------------------
# Adversarial composition
# ---------------------------------------------------------------------------


def test_compose_adversarial_includes_framing_and_prior():
    out = prompts.compose_adversarial(
        prior_step_summary="User wants a weekly research digest on climate policy.",
        next_step_topic="Suggest skills and MCP servers",
    )
    assert "Adversarial framing" in out
    assert "Suggest skills and MCP servers" in out
    assert "weekly research digest on climate policy" in out
    assert "Prior step context" in out


def test_compose_adversarial_handles_empty_prior():
    out = prompts.compose_adversarial("", "Next thing")
    assert "No prior context" in out
    assert "Next thing" in out


def test_compose_adversarial_handles_empty_topic():
    out = prompts.compose_adversarial("some prior", "")
    # Falls back to a generic header rather than emitting "Next step: "
    assert out.startswith("# Next step")
    assert "some prior" in out


def test_composed_prompt_can_round_trip_to_disk():
    rid = prompts.make_run_id()
    body = prompts.compose_adversarial("prior body", "Step 2 topic")
    prompts.write_prompt(rid, 2, body)
    assert prompts.read_prompt(rid, 2) == body
