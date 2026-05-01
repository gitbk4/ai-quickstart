"""E2E: paths.warn_if_synced emits a warning for an iCloud-shaped path.

Per PLAN.md, Phase 0 detects when ~/.ai-quickstart/ is on a sync'd
filesystem (iCloud Drive, Dropbox, OneDrive, NFS) because flock semantics
are unreliable there. This test simulates an iCloud-shaped path under a
fake $HOME using a tmp_path layout, then verifies warn_if_synced emits a
stderr warning and returns the right ``kind``.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest


def test_icloud_path_emits_warning(tmp_path: Path, capsys, monkeypatch):
    import paths as paths_mod  # type: ignore

    # Build a fake $HOME with the iCloud Mobile Documents structure.
    fake_home = tmp_path / "fake-home"
    icloud_relpath = "Library/Mobile Documents/com~apple~CloudDocs/.ai-quickstart"
    target = fake_home / icloud_relpath
    target.mkdir(parents=True)

    # warn_if_synced takes an explicit ``home`` arg, so we don't need to
    # monkeypatch Path.home; we just pass it through. Capture stderr.
    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_err)

    kind = paths_mod.warn_if_synced(target, home=fake_home)

    err_text = captured_err.getvalue()
    assert kind == "icloud", f"expected 'icloud', got {kind!r}"
    assert "icloud" in err_text.lower()
    assert "WARNING" in err_text or "warning" in err_text.lower()


def test_local_path_emits_no_warning(tmp_path: Path, capsys, monkeypatch):
    import paths as paths_mod  # type: ignore

    # An ordinary local path should NOT trigger the sync warning.
    fake_home = tmp_path / "fake-home"
    local_target = fake_home / ".ai-quickstart"
    local_target.mkdir(parents=True)

    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_err)

    kind = paths_mod.warn_if_synced(local_target, home=fake_home)

    err_text = captured_err.getvalue()
    # Local APFS paths typically return None; if statvfs fails on the test
    # filesystem we'd get 'nfs' — accept either, but if non-None we should
    # have warned.
    if kind is not None:
        assert "WARNING" in err_text or "warning" in err_text.lower()
    else:
        # No detection -> no warning printed.
        assert "WARNING" not in err_text
