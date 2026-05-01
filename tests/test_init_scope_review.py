"""Tests for the ``prepare-scope-review`` subcommand on scripts/init.py.

Covers Lane O's CLI surface:
  * happy path: writes a plan file under runs/<run-id>/, prints
    {plan_path, prompt_path, project_slug} JSON.
  * missing --run-id: argparse rejects (SystemExit).
  * missing --project-slug: argparse rejects (SystemExit).
  * answers not recorded yet: exit 2 with a clear stderr.
  * --help works (smoke check).

Network-free: the subcommand re-runs ``suggest.gather`` against the
bundled curated mapping. We monkeypatch the live freshness fetchers in
``scripts.sources`` to return curated-only payloads so the test does not
hit GitHub or mcpmarket.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import init as init_mod  # noqa: E402
import interview  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    h = tmp_path / "aiq-home"
    h.mkdir()
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(h))
    return h


@pytest.fixture(autouse=True)
def stub_live_sources(monkeypatch):
    """Avoid hitting GitHub / mcpmarket / mcp-registry during gather()."""
    from sources import github, mcp_registry, mcpmarket

    def fake_fetch_repo(owner, repo):
        return {"stars": 0, "forks": 0, "last_commit_iso": None}

    def fake_market(query):
        return {"results": [], "warnings": []}

    def fake_registry(keywords):
        return {"results": [], "warnings": []}

    monkeypatch.setattr(github, "fetch_repo", fake_fetch_repo)
    monkeypatch.setattr(mcpmarket, "search", fake_market)
    monkeypatch.setattr(mcp_registry, "search", fake_registry)


def _run(argv, stdin_text=""):
    out = io.StringIO()
    err = io.StringIO()
    sin = io.StringIO(stdin_text)
    rc = init_mod.main(argv, stdin=sin, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


def _seed_answers(run_id: str, archetype: str = "job", industry: str = "marketing") -> None:
    answers = {
        "archetype": archetype,
        "role": "growth marketer",
        "industry": industry,
        "top_problems": ["spends too long on research"],
        "desired_outcomes": ["save 30 minutes per brief"],
        "skill_tolerance": "strict",
        "project_style": "minimal",
        "coding_languages": ["python"],
    }
    interview.record_answers(run_id, answers)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_prepare_scope_review_happy_path(isolated_home):
    run_id = "20260429T101010Z-cccccc"
    _seed_answers(run_id)

    rc, out, err = _run(
        [
            "prepare-scope-review",
            "--run-id",
            run_id,
            "--project-slug",
            "content-research",
        ]
    )
    assert rc == 0, err

    payload = json.loads(out)
    assert payload["project_slug"] == "content-research"

    plan_path = Path(payload["plan_path"])
    prompt_path = Path(payload["prompt_path"])
    assert plan_path.exists()
    assert prompt_path.exists()

    body = plan_path.read_text(encoding="utf-8")
    assert body.startswith("# Project Plan: content-research")
    for h in [
        "## Problem statement",
        "## Proposed scope",
        "## User profile",
        "## Constraints",
        "## Open questions / Where pressure-testing helps",
        "## Context for the reviewer",
    ]:
        assert h in body

    prompt = prompt_path.read_text(encoding="utf-8")
    assert "/plan-ceo-review" in prompt
    assert body in prompt


def test_prepare_scope_review_works_for_user_invented_slug(isolated_home):
    """Slugs not in the curated mapping are still accepted (user freedom)."""
    run_id = "rid-invented"
    _seed_answers(run_id)
    rc, out, err = _run(
        [
            "prepare-scope-review",
            "--run-id",
            run_id,
            "--project-slug",
            "my-custom-thing",
        ]
    )
    assert rc == 0, err
    payload = json.loads(out)
    body = Path(payload["plan_path"]).read_text(encoding="utf-8")
    assert "my-custom-thing" in body


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_prepare_scope_review_missing_run_id_argparse_exits(isolated_home):
    with pytest.raises(SystemExit):
        _run(["prepare-scope-review", "--project-slug", "content-research"])


def test_prepare_scope_review_missing_project_slug_argparse_exits(isolated_home):
    with pytest.raises(SystemExit):
        _run(["prepare-scope-review", "--run-id", "rid-x"])


def test_prepare_scope_review_empty_project_slug_returns_2(isolated_home):
    """Argparse accepts ``--project-slug ''`` (non-empty=False), so the
    subcommand body itself rejects an empty slug with exit 2."""
    run_id = "rid-empty-slug"
    _seed_answers(run_id)
    rc, out, err = _run(
        ["prepare-scope-review", "--run-id", run_id, "--project-slug", ""]
    )
    assert rc == 2
    assert "project-slug" in err.lower()


# ---------------------------------------------------------------------------
# Missing prerequisites
# ---------------------------------------------------------------------------


def test_prepare_scope_review_no_answers_recorded_returns_2(isolated_home):
    """No answers.json for the run_id -> exit 2 with a clear stderr."""
    rc, out, err = _run(
        [
            "prepare-scope-review",
            "--run-id",
            "rid-never-recorded",
            "--project-slug",
            "content-research",
        ]
    )
    assert rc == 2
    assert "no answers recorded" in err.lower()
    assert "rid-never-recorded" in err


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


def test_prepare_scope_review_help_smoke(isolated_home, capsys):
    """argparse --help prints usage and exits 0."""
    with pytest.raises(SystemExit) as excinfo:
        _run(["prepare-scope-review", "--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "prepare-scope-review" in captured.out
    assert "--run-id" in captured.out
    assert "--project-slug" in captured.out
