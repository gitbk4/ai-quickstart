"""E2E: ``init.py eval`` emits the persona-heal eval prompt structure.

Verifies:
  * Default (bundled fixture) run prints the prelude, all case blocks, and
    the "JSON object on a single line" instruction.
  * ``--case-filter`` narrows to a single case.
  * Unknown ``--case-filter`` value exits non-zero with a clear stderr.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


def _count_case_headers(text: str) -> int:
    # Each case block opens with `# Case N/T: <name>` per render_case_prompt.
    return len(re.findall(r"^# Case \d+/\d+:", text, flags=re.MULTILINE))


def test_eval_prints_prelude_and_all_cases(cli_run):
    rc, out, err = cli_run(["eval"])
    assert rc == 0, err

    # Prelude: descriptive header + "Cases to evaluate" line.
    assert "Cases to evaluate:" in out
    assert "Claude-as-judge" in out

    # Eleven case blocks per the bundled fixture.
    n_cases = _count_case_headers(out)
    assert n_cases == 11, f"expected 11 case blocks, got {n_cases}"

    # Per-case task instruction is present.
    assert "JSON object on a single line" in out

    # And the final summary instruction.
    assert "summary" in out.lower()


def test_eval_case_filter_narrows_to_one(cli_run):
    rc, out, err = cli_run([
        "eval",
        "--case-filter", "exploring-zero-anecdotes",
    ])
    assert rc == 0, err
    n_cases = _count_case_headers(out)
    assert n_cases == 1, f"expected exactly 1 case, got {n_cases}"
    assert "exploring-zero-anecdotes" in out


def test_eval_unknown_case_filter_fails(cli_run):
    rc, out, err = cli_run([
        "eval",
        "--case-filter", "no-such-case-name",
    ])
    assert rc == 2, err
    assert "no case matches" in err
