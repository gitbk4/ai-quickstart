"""Tests for scripts/init.py — the CLI orchestrator.

Covers all 6 subcommands:
  * start                — happy path + bad archetype
  * record-answers       — happy path, missing run-id, malformed JSON, non-object
  * suggest              — happy path (sources mocked), missing answers, mapping missing
  * accept               — happy, mixed success/failure, dry-run, malformed payload
  * add-starting-files   — happy, missing source, symlink rejection, non-string entry
  * status               — happy with managed projects + persona present
  * --help / unknown subcommand
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import init as init_mod  # noqa: E402
import interview  # noqa: E402
import persona  # noqa: E402
import scaffold  # noqa: E402


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "aiq-home"
    h.mkdir()
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(h))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    (tmp_path / "claude-home").mkdir()
    return h


def _run(argv, stdin_text=""):
    out = io.StringIO()
    err = io.StringIO()
    sin = io.StringIO(stdin_text)
    rc = init_mod.main(argv, stdin=sin, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


# ---------- start ----------

def test_start_happy_path(home: Path):
    rc, out, err = _run(["start", "--archetype", "job"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["archetype"] == "job"
    assert "run_id" in payload
    assert Path(payload["prompt_path"]).exists()


def test_start_rejects_unknown_archetype(home: Path):
    # argparse rejects via choices; SystemExit raised.
    with pytest.raises(SystemExit):
        _run(["start", "--archetype", "weird"])


# ---------- record-answers ----------

def test_record_answers_happy(home: Path):
    rc1, out1, _ = _run(["start", "--archetype", "personal"])
    run_id = json.loads(out1)["run_id"]
    answers = {"role": "writer", "industry": "publishing", "top_problems": ["a", "b"]}
    rc2, out2, err2 = _run(
        ["record-answers", "--run-id", run_id], stdin_text=json.dumps(answers)
    )
    assert rc2 == 0, err2
    payload = json.loads(out2)
    assert payload["ok"] is True
    assert payload["run_id"] == run_id
    assert Path(payload["answers_path"]).exists()


def test_record_answers_malformed_json_exits_2(home: Path):
    rc, _, err = _run(
        ["record-answers", "--run-id", "abc"], stdin_text="not json"
    )
    assert rc == 2
    assert "invalid JSON" in err


def test_record_answers_non_object_rejected(home: Path):
    rc, _, err = _run(
        ["record-answers", "--run-id", "abc"], stdin_text='["not", "an", "object"]'
    )
    assert rc == 2
    assert "JSON object" in err


def test_record_answers_empty_stdin_rejected(home: Path):
    rc, _, err = _run(["record-answers", "--run-id", "abc"], stdin_text="")
    assert rc == 2


# ---------- suggest ----------

def test_suggest_missing_answers_exits_2(home: Path):
    rc, _, err = _run(["suggest", "--run-id", "no-such-run"])
    assert rc == 2
    assert "no answers recorded" in err


def test_suggest_happy_path_with_mocked_sources(home: Path, monkeypatch):
    # 1. start a session
    rc1, out1, _ = _run(["start", "--archetype", "job"])
    run_id = json.loads(out1)["run_id"]
    answers = {
        "archetype": "job",
        "role": "data engineer",
        "industry": "industry-engineering",
        "top_problems": ["pipeline reliability"],
    }
    _run(["record-answers", "--run-id", run_id], stdin_text=json.dumps(answers))

    # 2. mock all 3 sources
    import suggest as suggest_mod

    fake_repo = {
        "stars": 500,
        "forks": 10,
        "contributors": None,
        "last_commit_iso": "2026-04-01T00:00:00Z",
        "watchers": 50,
        "warning_low_quality": False,
        "source_tier": "unauth",
    }
    fake_search = {
        "results": [{"id": "test-server", "title": "Test", "description": "x"}],
        "source": "mcp-registry",
        "warnings": [],
    }
    fake_market = {
        "results": [],
        "source": "mcpmarket",
        "warnings": [],
        "source_tier": "scrape",
    }
    monkeypatch.setattr(suggest_mod.github, "fetch_repo", mock.Mock(return_value=fake_repo))
    monkeypatch.setattr(suggest_mod.mcp_registry, "search", mock.Mock(return_value=fake_search))
    monkeypatch.setattr(suggest_mod.mcpmarket, "search", mock.Mock(return_value=fake_market))

    rc, out, err = _run(["suggest", "--run-id", run_id])
    assert rc == 0, err
    payload = json.loads(out)
    assert "skills" in payload or "project_templates" in payload


def test_suggest_with_explicit_mapping_path(home: Path, tmp_path: Path):
    # Bad mapping path → fails cleanly with exit 1
    answers_run = "fake-run"
    runs_dir = home / "runs" / answers_run
    runs_dir.mkdir(parents=True)
    (runs_dir / "answers.json").write_text(
        json.dumps({"archetype": "job", "industry": "marketing"}), encoding="utf-8"
    )
    bogus = tmp_path / "no-such.yaml"
    rc, _, err = _run(["suggest", "--run-id", answers_run, "--mapping", str(bogus)])
    assert rc == 1
    assert "cannot load mapping" in err


# ---------- accept ----------

def test_accept_dry_run_succeeds_without_writes(home: Path, monkeypatch):
    payload = {
        "project_specs": [
            {
                "slug": "alpha-project",
                "dir": str(home / "projects" / "alpha"),
                "anecdote_seed": "exploring alpha",
                "skills": [],
            }
        ]
    }
    # mock scaffold so dry-run path doesn't actually need compathy
    monkeypatch.setattr(
        scaffold,
        "scaffold_project",
        mock.Mock(return_value={"slug": "alpha-project", "path": str(home / "projects" / "alpha"),
                                "compathy_initialized": False, "anecdote_path": "", "skills_path": "",
                                "registered": False, "dry_run": True}),
    )
    rc, out, err = _run(
        ["accept", "--run-id", "rid", "--dry-run"], stdin_text=json.dumps(payload)
    )
    assert rc == 0, err
    parsed = json.loads(out)
    assert len(parsed["projects"]) == 1
    assert parsed["projects"][0]["ok"] is True


def test_accept_partial_failure_exits_nonzero(home: Path, monkeypatch):
    def fake_scaffold(project_slug, project_dir, suggested_skills, anecdote_seed, dry_run=False):
        if project_slug == "fail-one":
            raise RuntimeError("boom")
        return {
            "slug": project_slug,
            "path": str(project_dir),
            "compathy_initialized": True,
            "anecdote_path": "",
            "skills_path": "",
            "registered": True,
        }
    monkeypatch.setattr(scaffold, "scaffold_project", fake_scaffold)
    payload = {
        "project_specs": [
            {"slug": "ok-one", "dir": str(home / "p1"), "anecdote_seed": "x", "skills": []},
            {"slug": "fail-one", "dir": str(home / "p2"), "anecdote_seed": "y", "skills": []},
        ]
    }
    rc, out, err = _run(
        ["accept", "--run-id", "rid"], stdin_text=json.dumps(payload)
    )
    assert rc == 1
    parsed = json.loads(out)
    assert parsed["projects"][0]["ok"] is True
    assert parsed["projects"][1]["ok"] is False


def test_accept_missing_slug_or_dir_marked_failed(home: Path):
    payload = {"project_specs": [{"slug": "", "dir": "", "anecdote_seed": ""}]}
    rc, out, _ = _run(
        ["accept", "--run-id", "rid"], stdin_text=json.dumps(payload)
    )
    assert rc == 1
    parsed = json.loads(out)
    assert parsed["projects"][0]["ok"] is False
    assert "slug" in parsed["projects"][0]["error"]


def test_accept_non_list_specs_rejected(home: Path):
    rc, _, err = _run(
        ["accept", "--run-id", "rid"],
        stdin_text=json.dumps({"project_specs": "not-a-list"}),
    )
    assert rc == 2
    assert "must be a list" in err


def test_accept_non_object_payload_rejected(home: Path):
    rc, _, err = _run(["accept", "--run-id", "rid"], stdin_text='["array"]')
    assert rc == 2
    assert "JSON object" in err


# ---------- add-starting-files ----------

def test_add_starting_files_happy(home: Path, tmp_path: Path):
    project = tmp_path / "p"
    (project / "context" / "raw").mkdir(parents=True)
    src = tmp_path / "data.txt"
    src.write_text("hello world", encoding="utf-8")
    rc, out, err = _run(
        ["add-starting-files", "--project-dir", str(project)],
        stdin_text=json.dumps([str(src)]),
    )
    assert rc == 0, err
    parsed = json.loads(out)
    assert len(parsed["copied"]) == 1
    assert (project / "context" / "raw" / "data.txt").read_text() == "hello world"


def test_add_starting_files_rejects_missing_dir(home: Path, tmp_path: Path):
    rc, _, err = _run(
        ["add-starting-files", "--project-dir", str(tmp_path / "nope")],
        stdin_text="[]",
    )
    assert rc == 2
    assert "is not a directory" in err


def test_add_starting_files_rejects_missing_raw_subdir(home: Path, tmp_path: Path):
    project = tmp_path / "p"
    project.mkdir()
    rc, _, err = _run(
        ["add-starting-files", "--project-dir", str(project)],
        stdin_text="[]",
    )
    assert rc == 2
    assert "doesn't exist" in err


def test_add_starting_files_skips_missing_source(home: Path, tmp_path: Path):
    project = tmp_path / "p"
    (project / "context" / "raw").mkdir(parents=True)
    rc, out, _ = _run(
        ["add-starting-files", "--project-dir", str(project)],
        stdin_text=json.dumps([str(tmp_path / "no-such.txt")]),
    )
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["copied"] == []
    assert len(parsed["skipped"]) == 1


def test_add_starting_files_rejects_symlink(home: Path, tmp_path: Path):
    project = tmp_path / "p"
    (project / "context" / "raw").mkdir(parents=True)
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    rc, out, _ = _run(
        ["add-starting-files", "--project-dir", str(project)],
        stdin_text=json.dumps([str(link)]),
    )
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["copied"] == []
    assert any("symlink" in s["reason"] or "not a regular" in s["reason"]
               for s in parsed["skipped"])


def test_add_starting_files_skips_non_string(home: Path, tmp_path: Path):
    project = tmp_path / "p"
    (project / "context" / "raw").mkdir(parents=True)
    rc, out, _ = _run(
        ["add-starting-files", "--project-dir", str(project)],
        stdin_text=json.dumps([42, None]),
    )
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["copied"] == []
    assert len(parsed["skipped"]) == 2


def test_add_starting_files_invalid_stdin_json(home: Path, tmp_path: Path):
    project = tmp_path / "p"
    (project / "context" / "raw").mkdir(parents=True)
    rc, _, err = _run(
        ["add-starting-files", "--project-dir", str(project)],
        stdin_text="garbage",
    )
    assert rc == 2
    assert "invalid JSON" in err


def test_add_starting_files_non_list_rejected(home: Path, tmp_path: Path):
    project = tmp_path / "p"
    (project / "context" / "raw").mkdir(parents=True)
    rc, _, err = _run(
        ["add-starting-files", "--project-dir", str(project)],
        stdin_text=json.dumps({"not": "a list"}),
    )
    assert rc == 2
    assert "JSON list" in err


# ---------- status ----------

def test_status_empty_home(home: Path):
    rc, out, _ = _run(["status"])
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["ai_quickstart_home"] == str(home)
    assert parsed["managed_projects_count"] == 0
    assert parsed["latest_run_id"] is None
    assert parsed["persona"]["exists"] is False


def test_status_with_persona_and_managed_projects(home: Path):
    # Create a persona file
    (home / "persona").mkdir()
    fm = persona.default_persona()
    fm["identity"]["role"] = "tester"
    persona.write_persona(home / "persona" / "persona.md", fm, "test prose")

    # Create a managed-projects.json with 2 entries
    (home / "managed-projects.json").write_text(
        json.dumps(["/p1", "/p2"]), encoding="utf-8"
    )

    # Create a runs subdir with a fake run-id
    runs_dir = home / "runs"
    runs_dir.mkdir()
    (runs_dir / "20260430T120000Z-aabbcc").mkdir()

    rc, out, _ = _run(["status"])
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["managed_projects_count"] == 2
    assert parsed["latest_run_id"] == "20260430T120000Z-aabbcc"
    assert parsed["persona"]["exists"] is True
    assert parsed["persona"]["version"] == 2  # default 1 → bumped to 2 on first write


# ---------- argparse ----------

def test_help_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        init_mod.main(["--help"])
    assert excinfo.value.code == 0


def test_unknown_subcommand_rejected():
    with pytest.raises(SystemExit):
        init_mod.main(["nonexistent-cmd"])


def test_no_subcommand_rejected():
    with pytest.raises(SystemExit):
        init_mod.main([])
