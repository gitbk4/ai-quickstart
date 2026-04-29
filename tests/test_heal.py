"""Tests for scripts/heal.py.

Covers:
  * prepare-context happy path (lock acquired, JSON shape, persona/anecdotes read)
  * prepare-context with concurrent holder -> "heal in progress" + exit 2
  * prepare-context skips malformed JSONL lines (counted in result)
  * prepare-context with empty activity.jsonl + no anecdotes
  * write happy path (backup created, atomic rename, version bump, stderr diff)
  * write with disk-full simulated mid-write -> .bak intact, error logged, non-zero exit
  * write with no .bak after restoration cleanup logic
  * rotate triggers on first run of a new ISO week, idempotent on second run
  * rotate aggregates archived weeks into activity-summary.json on month boundary

Tests use ``tmp_path`` and override the ai-quickstart home via
``AI_QUICKSTART_HOME``. They cover the primary public surface of heal.py.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Make scripts/ importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import heal  # noqa: E402  pylint: disable=wrong-import-position
import persona  # noqa: E402  pylint: disable=wrong-import-position


# ---------- helpers ----------

@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """Provision an isolated AI_QUICKSTART_HOME for the test."""
    h = tmp_path / "aiq-home"
    monkeypatch.setenv("AI_QUICKSTART_HOME", str(h))
    h.mkdir(parents=True, exist_ok=True)
    (h / "persona").mkdir()
    (h / "persona" / "anecdotes").mkdir()
    return h


def _persona_path(home: Path) -> Path:
    return home / "persona" / "persona.md"


def _activity_path(home: Path) -> Path:
    return home / "persona" / "activity.jsonl"


def _activity_summary_path(home: Path) -> Path:
    return home / "persona" / "activity-summary.json"


def _anecdotes_dir(home: Path) -> Path:
    return home / "persona" / "anecdotes"


def _heal_errors_path(home: Path) -> Path:
    return home / "heal-errors.jsonl"


def _seed_persona(home: Path, prose: str = "first version of prose\n") -> None:
    fm = persona.default_persona()
    fm["identity"]["role"] = "data engineer"
    fm["identity"]["industry"] = "fintech"
    fm["identity"]["archetype"] = "job"
    fm["activity"]["project_count"] = 2
    fm["activity"]["top_projects"] = ["alpha", "beta"]
    persona.write_persona(_persona_path(home), fm, prose)


def _seed_activity(home: Path, lines: List[str]) -> None:
    p = _activity_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _now_iso(now: Optional[_dt.datetime] = None) -> str:
    ts = now or _dt.datetime.now(_dt.timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- prepare-context ----------

def test_prepare_context_happy_path(home: Path):
    _seed_persona(home, prose="initial prose paragraph.\n")
    persona.append_anecdote(
        _anecdotes_dir(home), "alpha", "tried scaffolding compathy.\n"
    )
    persona.append_anecdote(
        _anecdotes_dir(home), "beta", "tested mcpmarket integration.\n"
    )
    _seed_activity(home, [
        json.dumps({"ts": _now_iso(), "event": "skill", "skill": "compathy", "cwd": "/p/alpha"}),
        json.dumps({"ts": _now_iso(), "event": "edit", "file": "x.py", "cwd": "/p/alpha"}),
    ])
    _activity_summary_path(home).write_text(
        json.dumps({"weeks": {}, "generated_at": "2026-04-01T00:00:00Z"}),
        encoding="utf-8",
    )

    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_prepare_context(stdout=out, stderr=err)
    assert rc == 0, err.getvalue()
    payload = json.loads(out.getvalue())

    assert payload["lock_acquired"] is True
    assert isinstance(payload["run_id"], str) and len(payload["run_id"]) > 0
    assert payload["current_frontmatter"]["identity"]["role"] == "data engineer"
    assert payload["current_prose"].strip() == "initial prose paragraph."
    slugs = sorted(a["slug"] for a in payload["anecdotes"])
    assert slugs == ["alpha", "beta"]
    assert "tried scaffolding compathy." in payload["anecdotes"][0]["content"] + payload["anecdotes"][1]["content"]
    assert len(payload["activity_recent"]) == 2
    assert payload["activity_summary"]["generated_at"] == "2026-04-01T00:00:00Z"
    assert payload["malformed_lines_skipped"] == 0


def test_prepare_context_empty_inputs(home: Path):
    """No persona, no anecdotes, no activity -> still emits a valid context."""
    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_prepare_context(stdout=out, stderr=err)
    assert rc == 0, err.getvalue()
    payload = json.loads(out.getvalue())
    assert payload["lock_acquired"] is True
    assert payload["anecdotes"] == []
    assert payload["activity_recent"] == []
    assert payload["activity_summary"] == {}
    assert payload["malformed_lines_skipped"] == 0
    # Defaults from persona.default_persona() must still be present.
    assert "identity" in payload["current_frontmatter"]
    assert payload["current_prose"] == ""


def test_prepare_context_skips_malformed_jsonl(home: Path):
    _seed_persona(home)
    _seed_activity(home, [
        json.dumps({"ts": _now_iso(), "event": "skill", "skill": "compathy", "cwd": "/p/x"}),
        "this is not json {{{ broken",
        json.dumps({"ts": _now_iso(), "event": "edit", "file": "y.py", "cwd": "/p/x"}),
        "",  # blank lines are not malformed
        "[]",  # not a dict -> malformed
    ])
    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_prepare_context(stdout=out, stderr=err)
    assert rc == 0
    payload = json.loads(out.getvalue())
    assert len(payload["activity_recent"]) == 2
    assert payload["malformed_lines_skipped"] == 2


def _hold_lock_subprocess(home_str: str, ready_path: str, exit_path: str):
    """Subprocess target: acquire heal lock, signal ready, wait for exit signal."""
    os.environ["AI_QUICKSTART_HOME"] = home_str
    # Reload heal so it picks up the env var fresh in this process.
    import importlib  # noqa: WPS433
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import heal as _heal  # noqa: WPS433
    importlib.reload(_heal)
    handle = _heal._acquire_lock()
    Path(ready_path).write_text("ready", encoding="utf-8")
    # Hold until the parent signals it's done checking.
    deadline = time.time() + 10
    while time.time() < deadline:
        if Path(exit_path).exists():
            break
        time.sleep(0.05)
    handle.release()


def test_prepare_context_lock_contention(home: Path, tmp_path: Path):
    """A concurrent process holding the lock causes prepare-context to exit 2."""
    ready = tmp_path / "ready.txt"
    exit_signal = tmp_path / "exit.txt"

    ctx = mp.get_context("spawn")
    proc = ctx.Process(
        target=_hold_lock_subprocess,
        args=(str(home), str(ready), str(exit_signal)),
    )
    proc.start()
    try:
        # Wait for the holder to acquire the lock.
        deadline = time.time() + 5
        while time.time() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists(), "lock holder failed to start"

        out = io.StringIO()
        err = io.StringIO()
        rc = heal.cmd_prepare_context(stdout=out, stderr=err)
        assert rc == 2
        assert "heal in progress" in err.getvalue()
        assert out.getvalue() == ""
    finally:
        exit_signal.write_text("go", encoding="utf-8")
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)


# ---------- write ----------

def test_write_happy_path(home: Path):
    _seed_persona(home, prose="old prose paragraph.\n")
    parsed_before = persona.parse_persona(_persona_path(home))
    version_before = parsed_before["frontmatter"]["generated"]["version"]

    out = io.StringIO()
    err = io.StringIO()
    sin = io.StringIO("brand new prose paragraph.\n")
    rc = heal.cmd_write(stdin=sin, stdout=out, stderr=err)
    assert rc == 0, err.getvalue()

    # Backup created with prior bytes.
    bak = home / "persona" / "persona.md.bak"
    assert bak.exists()
    assert b"old prose paragraph." in bak.read_bytes()

    # Persona has new prose + bumped version.
    parsed_after = persona.parse_persona(_persona_path(home))
    assert "brand new prose paragraph." in parsed_after["prose"]
    assert parsed_after["frontmatter"]["generated"]["version"] == version_before + 1

    # Diff was emitted on stderr.
    err_text = err.getvalue()
    assert "old prose paragraph." in err_text or "-old prose" in err_text
    assert "+brand new prose" in err_text or "brand new prose" in err_text
    # stdout has summary JSON.
    summary = json.loads(out.getvalue())
    assert summary["ok"] is True
    assert summary["version"] == version_before + 1


def test_write_from_prose_file(home: Path, tmp_path: Path):
    _seed_persona(home, prose="vA\n")
    prose_file = tmp_path / "new_prose.txt"
    prose_file.write_text("vB\n", encoding="utf-8")

    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_write(prose_file=str(prose_file), stdout=out, stderr=err)
    assert rc == 0, err.getvalue()
    parsed = persona.parse_persona(_persona_path(home))
    assert "vB" in parsed["prose"]


def test_write_no_prose_change_emits_no_diff_marker(home: Path):
    _seed_persona(home, prose="same\n")
    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_write(stdin=io.StringIO("same\n"), stdout=out, stderr=err)
    assert rc == 0
    assert "no prose changes" in err.getvalue()


def test_write_applies_activity_overrides(home: Path):
    _seed_persona(home, prose="x\n")
    overrides = json.dumps({
        "project_count": 7,
        "total_skill_uses": 42,
        "top_projects": ["alpha", "gamma"],
        "last_active": "2026-04-29T13:00:00Z",
    })
    rc = heal.cmd_write(
        stdin=io.StringIO("y\n"),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        activity_json=overrides,
    )
    assert rc == 0
    parsed = persona.parse_persona(_persona_path(home))
    a = parsed["frontmatter"]["activity"]
    assert a["project_count"] == 7
    assert a["total_skill_uses"] == 42
    assert a["top_projects"] == ["alpha", "gamma"]
    assert a["last_active"] == "2026-04-29T13:00:00Z"


def test_write_disk_full_restores_bak_and_logs(home: Path, monkeypatch):
    """Simulate a disk-full failure mid-write; original persona must be intact."""
    _seed_persona(home, prose="precious prose\n")
    original_bytes = _persona_path(home).read_bytes()

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(persona, "write_persona", boom)

    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_write(stdin=io.StringIO("attempted update\n"), stdout=out, stderr=err)
    assert rc != 0
    # Persona file is untouched (restored from .bak).
    assert _persona_path(home).read_bytes() == original_bytes
    # .bak still has the prior bytes.
    bak = home / "persona" / "persona.md.bak"
    assert bak.exists()
    assert bak.read_bytes() == original_bytes
    # Error logged.
    log = _heal_errors_path(home)
    assert log.exists()
    last_line = log.read_text(encoding="utf-8").strip().splitlines()[-1]
    record = json.loads(last_line)
    assert record["phase"] == "write"
    assert "No space left" in record["error"] or "OSError" in record["error"]


def test_write_no_prior_persona_no_bak_left_after_restore(home: Path, monkeypatch):
    """If persona.md doesn't exist beforehand and write fails, no .bak is created."""

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(persona, "write_persona", boom)

    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_write(stdin=io.StringIO("first attempt\n"), stdout=out, stderr=err)
    assert rc != 0
    # No persona.md (we never had one).
    assert not _persona_path(home).exists()
    # No .bak (we couldn't have created one — nothing to back up).
    assert not (home / "persona" / "persona.md.bak").exists()


def test_write_invalid_activity_json_warns_and_proceeds(home: Path):
    _seed_persona(home, prose="x\n")
    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_write(
        stdin=io.StringIO("y\n"),
        stdout=out,
        stderr=err,
        activity_json="not-json{{",
    )
    assert rc == 0
    assert "not valid JSON" in err.getvalue()


def test_write_activity_json_not_object_warns(home: Path):
    _seed_persona(home, prose="x\n")
    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_write(
        stdin=io.StringIO("y\n"),
        stdout=out,
        stderr=err,
        activity_json=json.dumps([1, 2, 3]),
    )
    assert rc == 0
    assert "must be a JSON object" in err.getvalue()


def test_write_activity_json_wrong_type_ignored(home: Path):
    _seed_persona(home, prose="x\n")
    out = io.StringIO()
    err = io.StringIO()
    rc = heal.cmd_write(
        stdin=io.StringIO("y\n"),
        stdout=out,
        stderr=err,
        activity_json=json.dumps({"project_count": "seven"}),
    )
    assert rc == 0
    parsed = persona.parse_persona(_persona_path(home))
    # Original count preserved (default 2 from _seed_persona).
    assert parsed["frontmatter"]["activity"]["project_count"] == 2


# ---------- rotate ----------

def test_rotate_idempotent_when_in_same_week(home: Path):
    # Activity file's mtime is "now" so we're still in the same ISO week.
    _seed_activity(home, [
        json.dumps({"ts": _now_iso(), "event": "skill", "skill": "compathy", "cwd": "/p/x"}),
    ])
    rc = heal.cmd_rotate(stdout=io.StringIO(), stderr=io.StringIO())
    assert rc == 0
    # No archive created.
    archives = list((home / "persona").glob("activity-*.jsonl"))
    assert archives == []
    assert _activity_path(home).exists()


def test_rotate_renames_when_week_advanced(home: Path):
    """Frozen-time test: simulate today being in a later ISO week than activity mtime."""
    # Seed activity, then back-date its mtime to two weeks ago.
    _seed_activity(home, [
        json.dumps({"ts": "2026-04-15T10:00:00Z", "event": "skill",
                    "skill": "compathy", "cwd": "/p/x"}),
    ])
    act = _activity_path(home)
    old_ts = _dt.datetime(2026, 4, 15, 10, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
    os.utime(act, (old_ts, old_ts))

    # "Now" is two weeks later.
    frozen_now = _dt.datetime(2026, 4, 29, 12, 0, 0, tzinfo=_dt.timezone.utc)
    rc = heal.cmd_rotate(now=frozen_now, stdout=io.StringIO(), stderr=io.StringIO())
    assert rc == 0
    assert not act.exists(), "activity.jsonl should have been renamed"
    archives = list((home / "persona").glob("activity-*.jsonl"))
    assert len(archives) == 1
    # The archive name uses the ISO week of the data (2026-W16, since 2026-04-15 is Wed of W16).
    iso_year, iso_week, _ = _dt.datetime(2026, 4, 15).isocalendar()
    expected = f"activity-{iso_year:04d}-{iso_week:02d}.jsonl"
    assert archives[0].name == expected

    # Idempotent: running again with same frozen_now does nothing.
    rc2 = heal.cmd_rotate(now=frozen_now, stdout=io.StringIO(), stderr=io.StringIO())
    assert rc2 == 0
    archives2 = list((home / "persona").glob("activity-*.jsonl"))
    assert len(archives2) == 1
    assert archives2[0].name == expected


def test_rotate_aggregates_archives_on_month_boundary(home: Path):
    """When today is in a different calendar month than archives, write summary."""
    pdir = home / "persona"
    # Two archived weekly files from March.
    arch_a = pdir / "activity-2026-10.jsonl"
    arch_b = pdir / "activity-2026-11.jsonl"
    arch_a.write_text(
        json.dumps({"ts": "2026-03-04T10:00:00Z", "event": "skill",
                    "skill": "compathy", "cwd": "/p/alpha", "duration_s": 5}) + "\n"
        + json.dumps({"ts": "2026-03-04T11:00:00Z", "event": "edit",
                      "file": "x.py", "cwd": "/p/alpha"}) + "\n",
        encoding="utf-8",
    )
    arch_b.write_text(
        json.dumps({"ts": "2026-03-11T10:00:00Z", "event": "skill",
                    "skill": "compathy", "cwd": "/p/beta", "duration_s": 3}) + "\n"
        + json.dumps({"ts": "2026-03-11T11:00:00Z", "event": "skill",
                      "skill": "ai-quickstart", "cwd": "/p/beta"}) + "\n",
        encoding="utf-8",
    )
    # Back-date their mtimes to March.
    march_ts = _dt.datetime(2026, 3, 15, 0, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
    os.utime(arch_a, (march_ts, march_ts))
    os.utime(arch_b, (march_ts, march_ts))

    frozen_now = _dt.datetime(2026, 4, 29, 12, 0, 0, tzinfo=_dt.timezone.utc)
    rc = heal.cmd_rotate(now=frozen_now, stdout=io.StringIO(), stderr=io.StringIO())
    assert rc == 0

    summary_path = _activity_summary_path(home)
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert "weeks" in summary
    assert set(summary["weeks"].keys()) == {"2026-10", "2026-11"}
    week_10 = summary["weeks"]["2026-10"]
    assert week_10["project_counts"]["alpha"] == 2
    assert any(s["name"] == "compathy" for s in week_10["top_skills"])
    week_11 = summary["weeks"]["2026-11"]
    assert week_11["project_counts"]["beta"] == 2
    skill_names = {s["name"] for s in week_11["top_skills"]}
    assert "compathy" in skill_names and "ai-quickstart" in skill_names

    # Idempotent: second run produces the same summary content (sans
    # generated_at timestamp). We compare weeks/project_counts.
    rc2 = heal.cmd_rotate(now=frozen_now, stdout=io.StringIO(), stderr=io.StringIO())
    assert rc2 == 0
    summary2 = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary2["weeks"] == summary["weeks"]


def test_rotate_with_no_activity_file(home: Path):
    """Rotate is a no-op when there's no activity.jsonl and no archives."""
    rc = heal.cmd_rotate(stdout=io.StringIO(), stderr=io.StringIO())
    assert rc == 0
    assert not _activity_path(home).exists()
    assert not _activity_summary_path(home).exists()


# ---------- main / argparse ----------

def test_main_prepare_context_dispatch(home: Path, capsys):
    rc = heal.main(["prepare-context"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["lock_acquired"] is True


def test_main_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit):
        heal.main(["--help"])
    out = capsys.readouterr().out
    assert "prepare-context" in out
    assert "write" in out
    assert "rotate" in out


def test_main_unknown_subcommand_exits(capsys):
    with pytest.raises(SystemExit):
        heal.main(["bogus"])


def test_main_rotate_dispatch(home: Path, capsys):
    rc = heal.main(["rotate"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "rotated" in payload and "aggregated" in payload


# ---------- error logging shape ----------

def test_heal_error_log_line_truncation(home: Path):
    """heal-errors.jsonl lines must stay <= 4096 bytes for atomic POSIX append."""
    long_msg = "x" * 8000
    heal._log_heal_error(
        phase="test",
        error=long_msg,
        tb_first_line="frame",
        home=home,
    )
    log = _heal_errors_path(home)
    assert log.exists()
    line = log.read_text(encoding="utf-8").strip().splitlines()[-1]
    encoded = line.encode("utf-8")
    assert len(encoded) + 1 <= heal.MAX_ERROR_LINE_BYTES
    record = json.loads(line)
    assert record["phase"] == "test"


def test_heal_error_log_swallows_failures(home: Path, monkeypatch):
    """Failure in error logging itself must not raise."""
    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    # Monkeypatch open inside heal's module to fail.
    real_open = open

    def selective_open(path, *args, **kwargs):
        if str(path).endswith("heal-errors.jsonl"):
            raise OSError("simulated")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", selective_open)
    # Should not raise.
    heal._log_heal_error(phase="test", error="x", tb_first_line="frame", home=home)
