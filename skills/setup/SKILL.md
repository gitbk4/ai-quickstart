---
name: setup
description: Use on first install of the ai-quickstart plugin, or when the user says "set up ai-quickstart", "do the onboarding", or "configure ai-quickstart for me". First-run wizard. Detects environment, suggests an archetype default, prompts for telemetry, and either runs the interview or hands off to /ai-quickstart:whoami.
allowed-tools: [Read, Bash, WebSearch]
---

# /ai-quickstart:setup

First-run wizard for the ai-quickstart plugin. Take a user who just
installed the plugin from zero state to "your persona file exists, you
know what telemetry is sending (if anything), and you know which slash
command to run next."

User input (interpret as `$ARGUMENTS`):

```
$ARGUMENTS
```

If `$ARGUMENTS` is empty, run the full guided onboarding below. If the
user passed something like `--quick`, `--skip-telemetry`, or
`--archetype X`, honor those flags by skipping the relevant prompts
(but never skip Phase 2: detecting an existing persona).

Keep the conversation tight. Each phase is 1-2 questions, not a paragraph.
The whole flow should land in roughly 3 minutes if the user moves quickly.

## Phase 1 - detect environment

Run these in one bash block to figure out whether we're in a dev context
(where compathy and the lane-p hook are relevant) or a chat-only
environment (where we skip those steps):

```bash
python3 - <<'PY'
import json, sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import setup_helpers
print(json.dumps(setup_helpers.detect_dev_context()))
PY
```

Read the JSON:

- `git_available: true, in_git_repo: true` -> dev context. Phase 5c
  (compathy auto-install) is offered.
- Either flag false -> chat-only context. Skip Phase 5c silently. Do
  NOT mention compathy unless the user asks.

Remember `project_root` for Phase 5c if it's set.

## Phase 2 - detect existing persona

Check whether the user has already been through onboarding:

```bash
python3 - <<'PY'
import json, os
from pathlib import Path
home = Path(os.environ.get("AI_QUICKSTART_HOME") or (Path.home() / ".ai-quickstart"))
md = home / "persona" / "persona.md"
js = home / "persona" / "persona.json"
print(json.dumps({"md_exists": md.is_file(), "json_exists": js.is_file(),
                  "home": str(home)}))
PY
```

If either file exists, the user has a persona already. Skip directly to
Phase 6 (the wrap-up) and suggest `/ai-quickstart:whoami` so they can
inspect what's there. Offer to re-run individual phases (telemetry
toggle, compathy refresh) but do NOT overwrite the persona file.

If neither exists, continue to Phase 3.

## Phase 3 - archetype hint from email domain

Pull the user's git email and look up a default archetype suggestion:

```bash
python3 - <<'PY'
import json, subprocess, sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import setup_helpers
try:
    email = subprocess.check_output(
        ["git", "config", "user.email"], text=True, timeout=5
    ).strip()
except Exception:
    email = ""
hint, reason = setup_helpers.archetype_hint_from_email_domain(email or None)
print(json.dumps({"email": email, "archetype": hint, "reason": reason}))
PY
```

This is a HINT, not a decision. Show the user the inference like:

> Your git email looks like a `<reason>`, so I'd guess `<archetype>` is
> the right archetype to seed your persona with. The three options are:
>
> - `job` - work projects, deliverable-driven, prefer stable tooling
> - `personal` - side projects, scratch ideas, evenings and weekends
> - `exploring` - learning the space, no fixed deliverable yet
>
> Want me to go with `<archetype>`, or pick a different one?

Wait for confirmation. Record the user's chosen archetype.

If the user passed `--archetype X` in `$ARGUMENTS`, use that and skip the
question. Validate X is one of the three values; otherwise ask.

## Phase 4 - telemetry opt-in

Ask whether to opt in to anonymous aggregated telemetry. Don't reinvent
the policy text; the foundation in `scripts/telemetry.py` already has an
honest prompt that names every field we send:

```bash
python3 - <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import telemetry
home = Path(os.environ.get("AI_QUICKSTART_HOME") or (Path.home() / ".ai-quickstart"))
status = telemetry.opt_in_status(home)
print(json.dumps({"status": status, "home": str(home)}))
PY
```

If `status == "unprompted"`, run the actual prompt (it is interactive
and prints the policy text):

```bash
python3 - <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")
import telemetry
home = Path(os.environ.get("AI_QUICKSTART_HOME") or (Path.home() / ".ai-quickstart"))
home.mkdir(parents=True, exist_ok=True)
(home / "persona").mkdir(parents=True, exist_ok=True)
decision = telemetry.opt_in_prompt()
telemetry.set_opt_in(home, decision)
print("opted_in" if decision else "opted_out")
PY
```

If `status` is already `opted_in` or `opted_out`, confirm the current
setting and offer a one-liner to toggle (call `telemetry.set_opt_in`
with the inverse). Do not nag.

Honor `--skip-telemetry` in `$ARGUMENTS` by recording `opted_out` via
`telemetry.set_opt_in(home, False)` without showing the prompt.

## Phase 5 - persona scaffold

Offer the user three sub-flows. Pick the one that matches their stated
intent (or default to (a) if they just say "let's go"):

### (a) Run the interview

Standard path. Three init.py subcommands drive it:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init.py start --archetype <chosen>
```

That writes `~/.ai-quickstart/runs/<run-id>/step-1-interview.md`. Read
the prompt file, ask the user the questions in your own voice, and
collect the answers as a JSON object. Then:

```bash
echo '<answers-json>' | python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init.py \
    record-answers --run-id <run-id>
```

Then drive Phase 2 of the interview:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init.py suggest --run-id <run-id>
```

Read the suggestions, present 3-5 to the user, and stop. The user can
return later with `/ai-quickstart:suggest <run-id>` to continue.

### (b) Show me what's possible first

The user wants a tour before committing to the interview. If a persona
already exists (it shouldn't, given Phase 2, but defensively), invoke
`/ai-quickstart:whoami` to dump the existing summary. Otherwise sketch
a demo persona summary inline so they see the shape:

> A typical persona looks like:
>
> > You are exploring | software engineer | climate tech | project-style: minimal
> > Persona last healed: 2026-04-12  (3 anecdotes)
> >
> > Top signals:
> >   - [trust 5] You learn fastest by shipping a tiny working demo, ...
> >   - [trust 4] You prefer Python and stdlib-first solutions, ...
>
> Want to start the interview now, or come back later with
> `/ai-quickstart:setup`?

If they want to come back later, stop here.

### (c) Compathy auto-install (dev context only)

Skip this sub-flow if Phase 1 reported chat-only context. Otherwise,
ask:

> Want me to also scaffold a compathy wiki for this project? Compathy
> is a structured markdown knowledge base that gets smarter the more
> you use it, and ai-quickstart wires its skills to read from it.

If yes, point at the documented soft-dep flow (`PLAN.md` "Compathy
dependency" section) and the SHA pin at
`${CLAUDE_PLUGIN_ROOT}/COMPATHY_VERSION`. The actual clone and bootstrap
should be driven through compathy's own entry point if it's already
installed at `~/.claude/skills/compathy/`, otherwise via the
ai-quickstart standalone `SKILL.md` Phase 0c instructions (which this
skill file does NOT duplicate). Surface any failures plainly and
continue; compathy is a soft dep.

Honor `--no-compathy` in `$ARGUMENTS` by skipping this sub-flow entirely.

## Phase 6 - wrap up

Print a 3-5 line summary in the user's chat:

> Setup done.
>
> - Persona: `~/.ai-quickstart/persona/persona.md` (archetype: <chosen>)
> - Telemetry: `<opted_in | opted_out>`
> - Compathy: `<installed | skipped | n/a>`
>
> Next:
>   - `/ai-quickstart:whoami` for a 30-second persona summary anytime
>   - `/ai-quickstart:suggest <run-id>` for ranked tool / skill picks
>   - `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/heal.py` to refresh the
>     persona once you've accumulated some activity

Stop. Do not loop back to ask more questions; the user knows where to go.

## Idempotency notes

This skill is safe to re-run. Phase 2's existing-persona check is the
primary guard against overwrite. Each sub-phase also re-reads its own
state (telemetry opt-in status, compathy install path) and offers a
refresh rather than a clobber. The interview in Phase 5a will start a
NEW run-id each time, so re-running setup does not corrupt a past run.
