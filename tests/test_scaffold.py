"""Tests for scripts/scaffold.py.

Covers:
  * validate_slug accepts kebab-case and rejects every malformed input
    listed in the lane brief.
  * find_compathy_path honors COMPATHY_HOME and falls back to
    ~/.claude/skills/compathy/; raises CompathyMissingError when missing.
  * scaffold_project happy path: dir created, compathy invoked (mocked),
    anecdote/skills/todo files written, registry appended.
  * dry_run: returns plan, performs no filesystem writes.
  * Existing non-empty dir -> ProjectExistsError.
  * Compathy invocation fails -> ScaffoldError, project_dir cleaned up.
  * Multiple skills with mixed sources -> skills.md formatted with all
    metadata and warnings rendered.
  * unscaffold removes the directory and the registry entry.

Tests use ``tmp_path`` and override ``AI_QUICKSTART_HOME`` and
``COMPATHY_HOME`` via ``monkeypatch``. ``subprocess.run`` is mocked so the
real compathy binary is never executed.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Make scripts/ importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scaffold  # noqa: E402  pylint: disable=wrong-import-position
import hooks_install  # noqa: E402  pylint: disable=wrong-import-position


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def aq_home(tmp_path: Path, monkeypatch) -> Path:
    """Provision an isolated AI_QUICKSTART_HOME for the test."""
    h = tmp_path / "aq-home"
    h.mkdir()
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(h))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    return h


@pytest.fixture
def fake_compathy(tmp_path: Path, monkeypatch) -> Path:
    """Create a fake compathy install dir with a stub scaffold.py inside."""
    root = tmp_path / "compathy"
    (root / "scripts").mkdir(parents=True)
    # Minimal stub so find_compathy_path's is_file check succeeds. We mock
    # subprocess.run separately, so this content never executes.
    (root / "scripts" / "scaffold.py").write_text(
        "#!/usr/bin/env python3\nprint('stub')\n", encoding="utf-8"
    )
    monkeypatch.setenv("COMPATHY_HOME", str(root))
    return root


class _FakeCompletedProcess:
    """Stand-in for subprocess.CompletedProcess used by scaffold.scaffold_project."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        side_effect=None,
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.side_effect = side_effect


def _patch_subprocess_run(monkeypatch, *, returncode=0, stderr="", make_dirs=True):
    """Patch subprocess.run with a fake that records the args + optionally
    creates ``context/raw/`` so the real compathy step is fully simulated.
    """
    captured: Dict[str, Any] = {}

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        # Simulate compathy's directory creation when returncode would be 0.
        if make_dirs and returncode == 0:
            # cmd is e.g. [python, scaffold.py, --target, <dir>, --project-name, <slug>]
            try:
                target_idx = cmd.index("--target") + 1
                target = Path(cmd[target_idx])
                (target / "context" / "raw").mkdir(parents=True, exist_ok=True)
                (target / "context" / "wiki").mkdir(parents=True, exist_ok=True)
            except (ValueError, IndexError):
                pass
        return _FakeCompletedProcess(
            returncode=returncode, stdout="", stderr=stderr
        )

    monkeypatch.setattr(scaffold.subprocess, "run", fake_run)
    return captured


# ---------------------------------------------------------------------------
# validate_slug
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "good",
    [
        "my-project",
        "alpha",
        "alpha-beta-gamma",
        "a",
        "a-1",
        "a1-b2-c3",
    ],
)
def test_validate_slug_accepts_kebab_case(good: str):
    # Should not raise.
    scaffold.validate_slug(good)


@pytest.mark.parametrize(
    "bad",
    [
        "Bad",
        "ALL_CAPS",
        "spaces here",
        "-leading",
        "trailing-",
        "double--hyphen",
        "1starts-with-number",
        "a" * 61,  # > 60 chars
        "",
        "has_underscore",
        "has.period",
        "Mixed-Case",
    ],
)
def test_validate_slug_rejects_bad(bad: str):
    with pytest.raises(ValueError):
        scaffold.validate_slug(bad)


def test_validate_slug_rejects_non_string():
    with pytest.raises(ValueError):
        scaffold.validate_slug(123)  # type: ignore[arg-type]


def test_validate_slug_60_char_limit_inclusive():
    # 60 chars is the max allowed.
    s = "a" + ("-b" * 29) + "-c"  # length: 1 + 58 + 2 = 61? compute carefully
    # Use a known-60-char kebab string instead.
    s = "a" + "-b" * 29  # length 1 + 58 = 59. Add one more char.
    s = s + "c"  # length 60, ends with "c" (after a hyphen-letter pattern)
    assert len(s) == 60
    scaffold.validate_slug(s)  # should pass

    s_61 = s + "d"
    assert len(s_61) == 61
    with pytest.raises(ValueError):
        scaffold.validate_slug(s_61)


# ---------------------------------------------------------------------------
# find_compathy_path
# ---------------------------------------------------------------------------
def test_find_compathy_path_uses_env_override(tmp_path: Path):
    root = tmp_path / "custom-compathy"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "scaffold.py").write_text("x", encoding="utf-8")

    out = scaffold.find_compathy_path({"COMPATHY_HOME": str(root)})
    assert out == root


def test_find_compathy_path_raises_when_absent(tmp_path: Path, monkeypatch):
    # Neither override nor the default location is valid.
    monkeypatch.delenv("COMPATHY_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))
    with pytest.raises(scaffold.CompathyMissingError):
        scaffold.find_compathy_path({})


def test_find_compathy_path_defaults_to_claude_skills(
    tmp_path: Path, monkeypatch
):
    fake_home = tmp_path / "fake-home"
    skills_dir = fake_home / ".claude" / "skills" / "compathy"
    (skills_dir / "scripts").mkdir(parents=True)
    (skills_dir / "scripts" / "scaffold.py").write_text("x", encoding="utf-8")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    out = scaffold.find_compathy_path({})
    assert out == skills_dir


def test_find_compathy_path_env_takes_precedence_over_default(
    tmp_path: Path, monkeypatch
):
    # Both default AND override exist; override should win.
    fake_home = tmp_path / "fake-home"
    default_dir = fake_home / ".claude" / "skills" / "compathy"
    (default_dir / "scripts").mkdir(parents=True)
    (default_dir / "scripts" / "scaffold.py").write_text("x", encoding="utf-8")

    override = tmp_path / "override"
    (override / "scripts").mkdir(parents=True)
    (override / "scripts" / "scaffold.py").write_text("x", encoding="utf-8")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    out = scaffold.find_compathy_path({"COMPATHY_HOME": str(override)})
    assert out == override


# ---------------------------------------------------------------------------
# scaffold_project: happy path
# ---------------------------------------------------------------------------
def test_scaffold_project_happy_path(
    tmp_path: Path, aq_home: Path, fake_compathy: Path, monkeypatch
):
    captured = _patch_subprocess_run(monkeypatch)

    project_dir = tmp_path / "projects" / "my-cool-app"
    skills = [
        {
            "name": "research",
            "source": "github",
            "url": "https://github.com/example/research",
            "stars": 1234,
            "last_commit": "2026-04-01",
        }
    ]
    result = scaffold.scaffold_project(
        "my-cool-app",
        project_dir,
        skills,
        "Initial spark: user wants a marketing-research toolchain.",
    )

    # Result shape.
    assert result["slug"] == "my-cool-app"
    assert result["path"] == str(project_dir)
    assert result["compathy_initialized"] is True
    assert result["registered"] is True

    # Filesystem state.
    raw = project_dir / "context" / "raw"
    assert (raw / "anecdote.md").is_file()
    assert (raw / "skills.md").is_file()
    assert (raw / "starting-files-todo.md").is_file()

    # Anecdote content includes the seed text.
    anecdote = (raw / "anecdote.md").read_text(encoding="utf-8")
    assert "marketing-research toolchain" in anecdote
    assert anecdote.startswith("# Anecdotes: anecdote")

    # Skills content.
    skills_md = (raw / "skills.md").read_text(encoding="utf-8")
    assert "research" in skills_md
    assert "github" in skills_md
    assert "1234" in skills_md
    assert "https://github.com/example/research" in skills_md

    # Starting files placeholder.
    todo = (raw / "starting-files-todo.md").read_text(encoding="utf-8")
    assert "my-cool-app" in todo
    assert "Step 5" in todo

    # Registry updated.
    registry_path = aq_home / "managed-projects.json"
    assert registry_path.is_file()
    with open(registry_path) as fh:
        projects = json.load(fh)
    assert str(project_dir.resolve()) in projects

    # subprocess.run was called with the right args.
    cmd = captured["cmd"]
    assert "--target" in cmd
    assert "--project-name" in cmd
    assert str(project_dir) in cmd
    assert "my-cool-app" in cmd
    # Path to compathy scaffold.py.
    assert any("scaffold.py" in str(c) for c in cmd)


# ---------------------------------------------------------------------------
# scaffold_project: dry run
# ---------------------------------------------------------------------------
def test_scaffold_project_dry_run_does_not_write(
    tmp_path: Path, aq_home: Path, fake_compathy: Path, monkeypatch
):
    # Tracking flag: subprocess.run should NEVER be called for a dry-run.
    called = {"n": 0}

    def fake_run(*a, **kw):  # noqa: ANN001
        called["n"] += 1
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(scaffold.subprocess, "run", fake_run)

    project_dir = tmp_path / "projects" / "dry-run-app"
    plan = scaffold.scaffold_project(
        "dry-run-app",
        project_dir,
        [{"name": "foo", "source": "github"}],
        "test seed",
        dry_run=True,
    )

    assert plan["dry_run"] is True
    assert plan["slug"] == "dry-run-app"
    assert plan["compathy_initialized"] is False
    assert plan["registered"] is False
    assert plan["skill_count"] == 1
    assert plan["path"] == str(project_dir)

    # Nothing on disk.
    assert not project_dir.exists()
    assert not (aq_home / "managed-projects.json").exists()
    # subprocess never invoked.
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# scaffold_project: existing non-empty dir
# ---------------------------------------------------------------------------
def test_scaffold_project_refuses_existing_non_empty_dir(
    tmp_path: Path, aq_home: Path, fake_compathy: Path, monkeypatch
):
    project_dir = tmp_path / "projects" / "already-here"
    project_dir.mkdir(parents=True)
    (project_dir / "existing-file.txt").write_text("hello", encoding="utf-8")

    _patch_subprocess_run(monkeypatch)

    with pytest.raises(scaffold.ProjectExistsError):
        scaffold.scaffold_project(
            "already-here", project_dir, [], "seed"
        )

    # Existing file is untouched.
    assert (project_dir / "existing-file.txt").read_text() == "hello"


def test_scaffold_project_allows_empty_existing_dir(
    tmp_path: Path, aq_home: Path, fake_compathy: Path, monkeypatch
):
    """An empty pre-existing dir should be acceptable (mkdir with exist_ok)."""
    project_dir = tmp_path / "projects" / "empty-here"
    project_dir.mkdir(parents=True)

    _patch_subprocess_run(monkeypatch)

    result = scaffold.scaffold_project(
        "empty-here", project_dir, [], "seed text"
    )
    assert result["registered"] is True


# ---------------------------------------------------------------------------
# scaffold_project: compathy failure
# ---------------------------------------------------------------------------
def test_scaffold_project_compathy_failure_cleans_up(
    tmp_path: Path, aq_home: Path, fake_compathy: Path, monkeypatch
):
    _patch_subprocess_run(
        monkeypatch,
        returncode=1,
        stderr="mock failure: target busy",
        make_dirs=False,
    )

    project_dir = tmp_path / "projects" / "fails-here"

    with pytest.raises(scaffold.ScaffoldError) as excinfo:
        scaffold.scaffold_project(
            "fails-here", project_dir, [], "seed"
        )

    assert "mock failure" in str(excinfo.value)
    # Project directory cleaned up best-effort.
    assert not project_dir.exists()
    # And not left in the registry.
    registry_path = aq_home / "managed-projects.json"
    if registry_path.exists():
        with open(registry_path) as fh:
            projects = json.load(fh)
        assert str(project_dir.resolve()) not in projects


def test_scaffold_project_compathy_missing_raises(
    tmp_path: Path, aq_home: Path, monkeypatch
):
    monkeypatch.delenv("COMPATHY_HOME", raising=False)
    # Make Path.home() return a dir with no .claude/skills/compathy.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-c"))

    project_dir = tmp_path / "projects" / "nope"

    with pytest.raises(scaffold.CompathyMissingError):
        scaffold.scaffold_project("nope", project_dir, [], "seed")


# ---------------------------------------------------------------------------
# scaffold_project: skills.md formatting with mixed sources
# ---------------------------------------------------------------------------
def test_scaffold_project_skills_md_mixed_sources(
    tmp_path: Path, aq_home: Path, fake_compathy: Path, monkeypatch
):
    _patch_subprocess_run(monkeypatch)

    project_dir = tmp_path / "projects" / "mixed-skills"
    skills: List[Dict[str, Any]] = [
        {
            "name": "research",
            "source": "github",
            "url": "https://github.com/example/research",
            "stars": 1234,
            "last_commit": "2026-04-01",
        },
        {
            "name": "filesystem-mcp",
            "source": "mcp_registry",
            "url": "mcp://registry/filesystem",
            "warnings": ["<100 users"],
        },
        {
            "name": "shady-skill",
            "source": "mcpmarket",
            "url": "https://mcpmarket.com/s/shady",
            "warnings": ["<100 stars", "no recent commits"],
        },
        {
            "name": "bare-name-only",
            "source": "github",
        },
    ]
    scaffold.scaffold_project(
        "mixed-skills", project_dir, skills, "anecdote text"
    )

    skills_md = (project_dir / "context" / "raw" / "skills.md").read_text(
        encoding="utf-8"
    )

    # Header + names appear.
    assert "# Suggested skills for mixed-skills" in skills_md
    assert "research" in skills_md
    assert "filesystem-mcp" in skills_md
    assert "shady-skill" in skills_md
    assert "bare-name-only" in skills_md

    # Sources appear.
    assert "github" in skills_md
    assert "mcp_registry" in skills_md
    assert "mcpmarket" in skills_md

    # Stars + last_commit on the github skill.
    assert "1234" in skills_md
    assert "2026-04-01" in skills_md

    # Warnings rendered as a sub-list.
    assert "<100 stars" in skills_md
    assert "<100 users" in skills_md
    assert "no recent commits" in skills_md

    # URLs.
    assert "https://github.com/example/research" in skills_md
    assert "mcp://registry/filesystem" in skills_md
    assert "https://mcpmarket.com/s/shady" in skills_md


def test_scaffold_project_empty_skills_list(
    tmp_path: Path, aq_home: Path, fake_compathy: Path, monkeypatch
):
    _patch_subprocess_run(monkeypatch)

    project_dir = tmp_path / "projects" / "no-skills"
    scaffold.scaffold_project("no-skills", project_dir, [], "seed text")

    skills_md = (project_dir / "context" / "raw" / "skills.md").read_text(
        encoding="utf-8"
    )
    assert "No skills were suggested" in skills_md


# ---------------------------------------------------------------------------
# scaffold_project: anecdote append behavior
# ---------------------------------------------------------------------------
def test_scaffold_project_invalid_slug_short_circuits(
    tmp_path: Path, aq_home: Path, fake_compathy: Path, monkeypatch
):
    called = {"n": 0}

    def fake_run(*a, **kw):  # noqa: ANN001
        called["n"] += 1
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(scaffold.subprocess, "run", fake_run)

    project_dir = tmp_path / "projects" / "invalid"

    with pytest.raises(ValueError):
        scaffold.scaffold_project("Bad-Slug", project_dir, [], "seed")

    assert called["n"] == 0
    assert not project_dir.exists()


# ---------------------------------------------------------------------------
# unscaffold
# ---------------------------------------------------------------------------
def test_unscaffold_removes_dir_and_registry_entry(
    tmp_path: Path, aq_home: Path, fake_compathy: Path, monkeypatch
):
    _patch_subprocess_run(monkeypatch)

    project_dir = tmp_path / "projects" / "to-remove"
    scaffold.scaffold_project("to-remove", project_dir, [], "seed")

    # Sanity: it landed.
    assert project_dir.is_dir()
    registry = aq_home / "managed-projects.json"
    with open(registry) as fh:
        projects = json.load(fh)
    assert str(project_dir.resolve()) in projects

    # Now unscaffold.
    scaffold.unscaffold(project_dir)

    assert not project_dir.exists()
    with open(registry) as fh:
        projects = json.load(fh)
    assert str(project_dir.resolve()) not in projects


def test_unscaffold_idempotent_on_missing_dir(
    tmp_path: Path, aq_home: Path
):
    # No prior scaffold; should not raise.
    scaffold.unscaffold(tmp_path / "never-existed")


# ---------------------------------------------------------------------------
# Public API surface check
# ---------------------------------------------------------------------------
def test_module_exports_documented_api():
    expected = {
        "CompathyMissingError",
        "ProjectExistsError",
        "ScaffoldError",
        "find_compathy_path",
        "validate_slug",
        "scaffold_project",
        "unscaffold",
    }
    assert expected.issubset(set(scaffold.__all__))
    # Error subclassing per spec.
    assert issubclass(scaffold.CompathyMissingError, FileNotFoundError)
    assert issubclass(scaffold.ProjectExistsError, FileExistsError)
    assert issubclass(scaffold.ScaffoldError, RuntimeError)
