"""E2E: a second project creates its own anecdote, leaving the first untouched.

Each scaffolded project gets a per-project anecdote at
``<project>/context/raw/anecdote.md``. This test confirms two scaffolds in
the same home don't cross-contaminate.

(The persona/anecdotes/ tree under ~/.ai-quickstart is populated by the
heal loop separately; that's covered in test_second_run_persona.py and
test_manual_heal.py. This test focuses on the per-project anecdote
files written by scaffold.scaffold_project.)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _scaffold_one(cli_run, archetype: str, slug: str, project_dir: Path,
                  seed_text: str) -> str:
    rc, out, err = cli_run(["start", "--archetype", archetype])
    assert rc == 0, err
    run_id = json.loads(out)["run_id"]

    cli_run(
        ["record-answers", "--run-id", run_id],
        stdin_text=json.dumps({"archetype": archetype}),
    )
    cli_run(["suggest", "--run-id", run_id])

    rc, out, err = cli_run(
        ["accept", "--run-id", run_id],
        stdin_text=json.dumps({
            "project_specs": [{
                "slug": slug,
                "dir": str(project_dir),
                "anecdote_seed": seed_text,
                "skills": [],
            }]
        }),
    )
    assert rc == 0, err
    return run_id


def test_second_project_anecdote_independent_of_first(
    e2e_home: Path,
    mock_compathy: Path,
    mock_sources: dict,
    cli_run,
    tmp_path: Path,
):
    # First project: alpha
    alpha_dir = tmp_path / "projects" / "alpha"
    _scaffold_one(
        cli_run,
        archetype="job",
        slug="alpha",
        project_dir=alpha_dir,
        seed_text="alpha seed: kicked off ML pipeline scaffold",
    )

    alpha_anecdote = alpha_dir / "context" / "raw" / "anecdote.md"
    assert alpha_anecdote.is_file()
    alpha_text_before = alpha_anecdote.read_text(encoding="utf-8")
    assert "alpha seed" in alpha_text_before

    # Second project: beta — completely independent dir.
    beta_dir = tmp_path / "projects" / "beta"
    _scaffold_one(
        cli_run,
        archetype="exploring",
        slug="beta",
        project_dir=beta_dir,
        seed_text="beta seed: hello-world MCP server",
    )

    beta_anecdote = beta_dir / "context" / "raw" / "anecdote.md"
    assert beta_anecdote.is_file()
    beta_text = beta_anecdote.read_text(encoding="utf-8")
    assert "beta seed" in beta_text
    assert "alpha seed" not in beta_text, (
        "beta's anecdote must not leak alpha's seed text"
    )

    # Alpha's anecdote is byte-for-byte unchanged.
    alpha_text_after = alpha_anecdote.read_text(encoding="utf-8")
    assert alpha_text_before == alpha_text_after, (
        "alpha anecdote was modified during beta scaffold — they should be "
        "fully isolated per project directory"
    )

    # Both projects appear in the central registry.
    registry = json.loads(
        (e2e_home / "managed-projects.json").read_text(encoding="utf-8")
    )
    assert str(alpha_dir.resolve()) in registry
    assert str(beta_dir.resolve()) in registry
