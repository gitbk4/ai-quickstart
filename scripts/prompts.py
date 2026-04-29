"""Adversarial prompt file authoring.

Each ai-quickstart run produces three artifacts under
``~/.ai-quickstart/runs/{run_id}/``:

    step-1-prompt.md
    step-2-prompt.md
    step-3-prompt.md

These files serve a dual purpose. They are durable audit artifacts capturing
what was asked of the model at each step, and they are also the prompt
context fed into the *next* step. The prose in each file is deliberately
adversarial: it asks the LLM to push back on weak inputs, surface gaps,
and refuse surface-level answers.

Stdlib only. Python 3.9+ compatible.
"""

from __future__ import annotations

import os
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Run identifiers and directory layout
# ---------------------------------------------------------------------------


def _runs_root() -> Path:
    """Return the root directory holding per-run artifacts.

    Honors the ``AI_QUICKSTART_HOME`` environment variable so tests and
    callers can redirect storage. Falls back to ``~/.ai-quickstart``.
    """

    override = os.environ.get("AI_QUICKSTART_HOME")
    if override:
        base = Path(override)
    else:
        base = Path.home() / ".ai-quickstart"
    return base / "runs"


def make_run_id() -> str:
    """Return a short, sortable run id.

    Format: ``YYYYMMDDTHHMMSSZ-xxxxxx`` where the suffix is six lowercase hex
    characters from a fresh uuid4. The timestamp is UTC, second-precision,
    chosen so the id sorts lexicographically by start time.
    """

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6]
    return f"{now}-{suffix}"


def ensure_run_dir(run_id: str) -> Path:
    """Create (if needed) and return the directory for ``run_id``.

    Idempotent: calling twice with the same id is a no-op on the second call
    and returns the same path.
    """

    if not run_id:
        raise ValueError("run_id must be a non-empty string")
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


# ---------------------------------------------------------------------------
# Prompt file IO
# ---------------------------------------------------------------------------


def _prompt_path(run_id: str, step: int) -> Path:
    return ensure_run_dir(run_id) / f"step-{step}-prompt.md"


def write_prompt(run_id: str, step: int, content: str) -> Path:
    """Atomically write a prompt file for ``step`` under ``run_id``.

    Writes to ``step-{N}-prompt.md.tmp`` first, then ``os.replace`` into the
    final filename so a partial write cannot leave a half-formed prompt
    visible to the next step.
    """

    if step < 1:
        raise ValueError("step must be >= 1")
    target = _prompt_path(run_id, step)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_prompt(run_id: str, step: int) -> Optional[str]:
    """Return the prompt body for ``step`` or ``None`` if it has not been written.

    A missing file is the expected signal that the prior step has not run
    yet; callers should treat ``None`` as a control-flow value, not an error.
    """

    target = _prompt_path(run_id, step)
    if not target.exists():
        return None
    return target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def render_template(template_path: Path, variables: dict) -> str:
    """Render a ``string.Template`` file with ``${var}`` substitutions.

    Uses ``Template.substitute`` so missing keys raise ``KeyError`` rather
    than silently leaving placeholder text in an audit artifact. Raises
    ``FileNotFoundError`` if the template path does not exist.
    """

    template_path = Path(template_path)
    if not template_path.exists():
        raise FileNotFoundError(f"prompt template not found: {template_path}")
    raw = template_path.read_text(encoding="utf-8")
    return string.Template(raw).substitute(variables)


# ---------------------------------------------------------------------------
# Adversarial composition
# ---------------------------------------------------------------------------


_ADVERSARIAL_FRAMING = """## Adversarial framing

You are reviewing prior output for a real user, not generating polite
agreement. Your job in the next step is to be the second pair of eyes that
catches what the first pass missed.

Concretely, this means:

- Refuse vague or aspirational answers. If the prior step left a goal as
  "use AI to be more productive," push for the specific task, frequency,
  and current pain point.
- Surface contradictions. If the user's stated role and the projects they
  asked for do not line up, name the mismatch out loud.
- Prefer fewer concrete recommendations to many hedge-bet ones. A short
  list with reasons is more useful than a long list with disclaimers.
- Flag when no good option exists. If the curated mapping has nothing that
  fits, say so plainly rather than pretending a weak match is strong.
- Ask for evidence. Concrete examples beat self-description every time.

Treat the prior context below as a draft to challenge, not a contract to
honor.
"""


def compose_adversarial(prior_step_summary: str, next_step_topic: str) -> str:
    """Compose the body of an adversarial prompt file.

    Returns markdown ready to pass to :func:`write_prompt`. The result
    always contains the adversarial framing block, the topic of the
    upcoming step, and the prior step's summary verbatim under a clearly
    labeled section so the next-step LLM can quote it.
    """

    topic = (next_step_topic or "").strip()
    prior = (prior_step_summary or "").strip()
    if not prior:
        prior = "_No prior context was supplied._"

    sections = [
        f"# Next step: {topic}" if topic else "# Next step",
        "",
        _ADVERSARIAL_FRAMING.rstrip(),
        "",
        "## Prior step context",
        "",
        prior,
        "",
    ]
    return "\n".join(sections)
