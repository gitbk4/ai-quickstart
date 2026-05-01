"""Shared fixtures for the E2E test suite.

These fixtures provision an isolated ``~/.ai-quickstart/`` per test, stub out
all external dependencies (compathy, GitHub, MCP registry, mcpmarket), and
expose a single ``cli_run`` helper that drives ``scripts/init.py`` in-process.

Each E2E test composes a small subset of these fixtures so the same tests
can run on any machine with no network and no compathy install.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

import pytest

# Make scripts/ importable as bare modules — same pattern the unit tests use.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Home + Claude home isolation
# ---------------------------------------------------------------------------

@pytest.fixture
def e2e_home(tmp_path: Path, monkeypatch) -> Path:
    """Provision a fresh ~/.ai-quickstart/ via tmp_path + AI_QUICKSTART_HOME.

    Also sets CLAUDE_HOME to a sibling tmp dir so hooks_install never touches
    the real ~/.claude/. Both are isolated per-test by pytest's tmp_path.
    """
    aq = tmp_path / "ai-quickstart-home"
    aq.mkdir()
    claude = tmp_path / "claude-home"
    claude.mkdir()
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(aq))
    monkeypatch.setenv("CLAUDE_HOME", str(claude))
    return aq


# ---------------------------------------------------------------------------
# Fake compathy
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_compathy(tmp_path: Path, monkeypatch) -> Path:
    """Provision a fake compathy install + monkeypatch scaffold to find it.

    The fake compathy lives at tmp_path/fake-compathy/scripts/scaffold.py.
    The stub script is never actually executed: scaffold.scaffold_project's
    subprocess.run is patched to a no-op that creates the project's
    context/raw and context/wiki directories (matching what compathy would
    have laid down).

    Returns the path to the fake compathy root.
    """
    import scaffold  # type: ignore

    root = tmp_path / "fake-compathy"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "scaffold.py").write_text(
        "#!/usr/bin/env python3\nprint('fake compathy stub')\n",
        encoding="utf-8",
    )

    # Point COMPATHY_HOME at the fake install (find_compathy_path consults env).
    monkeypatch.setenv("COMPATHY_HOME", str(root))

    # Also explicitly monkeypatch find_compathy_path so callers that pass an
    # empty env mapping still resolve correctly (matches the public API
    # documented in scripts/scaffold.py).
    real_find = scaffold.find_compathy_path

    def _patched_find(env=None):
        if env is None:
            env = {"COMPATHY_HOME": str(root)}
        return real_find(env)

    monkeypatch.setattr(scaffold, "find_compathy_path", _patched_find)

    # Replace subprocess.run inside scaffold so the fake stub is never executed.
    # The replacement creates the directories compathy would have created,
    # which is what scaffold_project expects on a successful invocation.
    class _FakeProc:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(cmd, *args, **kwargs):
        # cmd is [python, .../scaffold.py, --target, <dir>, --project-name, <slug>]
        try:
            target_idx = cmd.index("--target") + 1
            target = Path(cmd[target_idx])
            (target / "context" / "raw").mkdir(parents=True, exist_ok=True)
            (target / "context" / "wiki").mkdir(parents=True, exist_ok=True)
        except (ValueError, IndexError):  # pragma: no cover - defensive
            pass
        return _FakeProc(returncode=0)

    monkeypatch.setattr(scaffold.subprocess, "run", _fake_run)
    return root


# ---------------------------------------------------------------------------
# Mock external sources (GitHub / MCP registry / mcpmarket)
# ---------------------------------------------------------------------------

def _default_fake_repo() -> Dict[str, Any]:
    """Canned GitHub fetch_repo result — looks like a healthy OSS repo."""
    return {
        "stars": 850,
        "forks": 120,
        "contributors": 42,
        "last_commit_iso": "2026-04-25T12:00:00Z",
        "watchers": 60,
        "warning_low_quality": False,
        "source_tier": "unauth",
    }


def _default_fake_mcp_search() -> Dict[str, Any]:
    return {
        "results": [
            {"id": "fetch", "title": "Fetch", "description": "HTTP fetch MCP"},
            {"id": "github", "title": "GitHub", "description": "GitHub MCP"},
        ],
        "source": "mcp-registry",
        "warnings": [],
    }


def _default_fake_market_search() -> Dict[str, Any]:
    return {
        "results": [],
        "source": "mcpmarket",
        "warnings": [],
        "source_tier": "scrape",
    }


@pytest.fixture
def mock_sources(monkeypatch):
    """Stub out all three live source modules used by suggest.gather.

    Returns a dict of mock objects so tests can assert call counts or
    swap return values mid-test::

        m = mock_sources
        m["fetch_repo"].return_value = {...}     # override
        assert m["mcp_search"].call_count >= 1
    """
    import suggest  # type: ignore

    fetch_repo = mock.Mock(return_value=_default_fake_repo())
    mcp_search = mock.Mock(return_value=_default_fake_mcp_search())
    market_search = mock.Mock(return_value=_default_fake_market_search())

    monkeypatch.setattr(suggest.github, "fetch_repo", fetch_repo)
    monkeypatch.setattr(suggest.mcp_registry, "search", mcp_search)
    monkeypatch.setattr(suggest.mcpmarket, "search", market_search)

    return {
        "fetch_repo": fetch_repo,
        "mcp_search": mcp_search,
        "market_search": market_search,
    }


# ---------------------------------------------------------------------------
# In-process CLI runner
# ---------------------------------------------------------------------------

@pytest.fixture
def heal_lock_release(monkeypatch):
    """Capture every flock acquired by heal._acquire_lock and release them on demand.

    Heal's lock semantics: ``cmd_prepare_context`` acquires .heal.lock and
    intentionally holds it until process exit (the production caller is a
    short-lived Python process, so this works fine). In a pytest process we
    run multiple heals in sequence, and the lock from heal #1 leaks into
    heal #2, causing a spurious "heal in progress" failure.

    This fixture wraps ``_acquire_lock`` so every returned handle is appended
    to a list, and yields a release() function tests can call between heals
    to drop all outstanding locks.
    """
    import heal  # type: ignore

    captured = []
    real_acquire = heal._acquire_lock

    def _capturing_acquire(home=None):
        handle = real_acquire(home)
        captured.append(handle)
        return handle

    monkeypatch.setattr(heal, "_acquire_lock", _capturing_acquire)

    def release():
        while captured:
            h = captured.pop()
            try:
                h.release()
            except Exception:  # pragma: no cover - defensive
                pass

    yield release
    # Ensure any leftover locks are dropped after the test.
    release()


@pytest.fixture
def cli_run():
    """Return a helper that invokes ``scripts/init.py main`` in-process.

    Usage::

        rc, stdout, stderr = cli_run(["start", "--archetype", "job"])
        rc, stdout, stderr = cli_run(
            ["record-answers", "--run-id", rid],
            stdin_text=json.dumps(answers),
        )
    """
    import init as init_mod  # type: ignore

    def _run(argv: List[str], stdin_text: str = "") -> Tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        sin = io.StringIO(stdin_text)
        rc = init_mod.main(argv, stdin=sin, stdout=out, stderr=err)
        return rc, out.getvalue(), err.getvalue()

    return _run
