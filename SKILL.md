---
name: ai-quickstart
description: Bridge a vague AI goal to a scaffolded project with curated skills, MCP servers, and a self-healing persona. Interviews the user, queries live freshness data (GitHub stars, MCP registry, mcpmarket), scaffolds a compathy-structured project, and maintains a global persona that informs future suggestions.
metadata:
  dependencies: [compathy]
  compathy_version_file: COMPATHY_VERSION
---

# ai-quickstart

You are orchestrating the `ai-quickstart` skill. Your job is to take the user
from "I have a vague AI goal" to "here is a scaffolded project + recommended
toolkit + a persona that gets smarter as I use it."

This is a three-step flow:

1. **Interview** — progressive Qs with mid-flight web research, audited as a
   prompt file under `~/.ai-quickstart/runs/<run-id>/step-1-interview.md`.
2. **Suggest** — curated `mappings/personas.yaml` overlaid with live freshness
   data from GitHub, the Anthropic MCP registry, and mcpmarket.com.
3. **Scaffold** — shell out to compathy to create a project, write an
   anecdote, register the project in `~/.ai-quickstart/managed-projects.json`,
   and trigger persona heal.

`{skill_dir}` below is the directory containing this SKILL.md.

---

## Phase 0 — Boot, self-update, dependency check, hook install

Phase 0 runs on every invocation. It is fast (<2s on warm cache) and
non-blocking — every check soft-fails with a stderr warning rather than
aborting the skill.

### Phase 0a — Self-update ai-quickstart

```bash
python3 {skill_dir}/scripts/update.py
```

Runs `git pull --ff-only` in the ai-quickstart repo. Prints the version
delta (from `VERSION`) on success. Soft-fails on dirty tree, no remote, or
no network — never blocks the skill.

### Phase 0b — Detect Claude Code version

Read `$CLAUDE_CODE_VERSION` (set by the harness) or shell out to
`claude --version`. Parse semver. Compare against the known-compatible range
declared in `metadata.compatible_claude_code` (currently any 1.x). If outside
the range, warn the user but continue.

```bash
claude --version 2>/dev/null || echo "unknown"
```

If parsing fails, warn and continue with reduced confidence — Phase 0c
hook install needs a Claude Code that supports `~/.claude/settings.json`
PostToolUse hooks. Older versions: skip hook install with a clear message.

### Phase 0c — Detect compathy and auto-install at pinned SHA

Compathy is a soft dep. ai-quickstart's Step 3 shells out to it.

```bash
COMPATHY_DIR="${HOME}/.claude/skills/compathy"
PINNED_SHA="$(cat {skill_dir}/COMPATHY_VERSION)"

if [ -d "$COMPATHY_DIR/.git" ]; then
  # Update existing install, then checkout the pinned SHA.
  python3 "$COMPATHY_DIR/scripts/update.py" || true
  git -C "$COMPATHY_DIR" fetch --quiet origin
  git -C "$COMPATHY_DIR" checkout --quiet "$PINNED_SHA" 2>/dev/null \
    || echo "ai-quickstart: WARNING: could not pin compathy to $PINNED_SHA — using HEAD" >&2
else
  echo "ai-quickstart: compathy not found; cloning to $COMPATHY_DIR"
  git clone --quiet https://github.com/Memento-Teams/compathy.git "$COMPATHY_DIR" \
    || { echo "ai-quickstart: WARNING: compathy clone failed — Step 3 will be unavailable" >&2; }
  if [ -d "$COMPATHY_DIR/.git" ]; then
    git -C "$COMPATHY_DIR" checkout --quiet "$PINNED_SHA" 2>/dev/null || true
  fi
fi
```

The pinned SHA in `COMPATHY_VERSION` is bumped by the maintainer after
testing — never pulled from HEAD automatically. This avoids supply-chain
risk and reproducibility loss.

If clone fails, Step 3 will surface the failure and offer manual install
instructions. Steps 1 and 2 are still usable.

### Phase 0d — Filesystem-sync detection

flock semantics are unreliable on iCloud Drive, Dropbox, OneDrive,
Google Drive, and NFS-mounted home directories. Run the detector:

```bash
python3 {skill_dir}/scripts/paths.py --detect "${HOME}/.ai-quickstart"
```

If the JSON output's `detect` field is non-null (e.g. `"icloud"`), warn
the user that:

> Your `~/.ai-quickstart/` directory appears to be on a sync'd filesystem.
> Heal-loop file locking may behave unexpectedly. Recommended: move
> `~/.ai-quickstart/` to a local-only volume, or run `/ai-quickstart heal`
> from one Claude Code session at a time.

Continue regardless — this is warn-only.

### Phase 0e — Ensure ~/.ai-quickstart/ exists

```bash
python3 {skill_dir}/scripts/paths.py --ensure-dirs
```

Creates `persona/`, `persona/anecdotes/`, `runs/`, `cache/github/`, and
`cache/mcpmarket/` if missing. Idempotent.

### Phase 0f — Install Claude Code hooks (delegated)

The hook installer is owned by Lane F. Phase 0 delegates to:

```bash
python3 {skill_dir}/scripts/hooks_install.py --check
```

If the script reports `installed: false`, it will prompt:

> ai-quickstart will add 2 PostToolUse hooks to ~/.claude/settings.json.
> These fire only when cwd is a managed project (stat-check <1ms).
> OK to install? [Y/n]

On consent, the installer atomically merges the hooks into `settings.json`
and records exact entries into `~/.ai-quickstart/installed-hooks.json` for
later uninstall. This step is also a no-op when Claude Code's hook system
is unavailable (Phase 0b detected an incompatible version).

<!-- LANE F implements scripts/hooks_install.py and hook_runner.py. Phase 0f
calls it; the installer itself lives elsewhere. -->

---

## Phase 1 — Interview the user

<!-- LANE H — implements scripts/interview.py and Step-1 prompt template at
templates/prompts/step-1.md.tmpl. Phase 1 invokes it via:

    python3 {skill_dir}/scripts/interview.py --run-id <run-id>

The interview MUST:
  * Open with 3 anchor questions (role, goal, AI exposure level).
  * Allow progressive deeper Qs based on initial answers.
  * Permit mid-flight web research when more context is needed.
  * Confirm/challenge assumptions before exit.
  * Write the full transcript and conclusions to
    ~/.ai-quickstart/runs/<run-id>/step-1-interview.md
-->

---

## Phase 2 — Suggest projects, skills, and MCP servers

<!-- LANE I (and Lane H for the upstream interview→suggest handoff) —
implements scripts/suggest.py and the source modules under scripts/sources/.

Phase 2 invokes:

    python3 {skill_dir}/scripts/suggest.py --run-id <run-id> \
      --interview ~/.ai-quickstart/runs/<run-id>/step-1-interview.md

It MUST:
  * Read mappings/personas.yaml for the curated baseline.
  * Query GitHub (3-tier auth), MCP registry, and mcpmarket in parallel
    via concurrent.futures.ThreadPoolExecutor (max_workers=3).
  * Apply quality warnings: <100 stars or <100 users → warning badge.
  * Render the merged suggestion list to step-2-suggestions.md.
  * Let the user accept/reject items inline.
-->

---

## Phase 3 — Scaffold the project

<!-- LANE I — implements scripts/scaffold.py.

Phase 3 invokes:

    python3 {skill_dir}/scripts/scaffold.py --run-id <run-id>

It MUST:
  * Shell out to compathy (auto-installed in Phase 0c) to create the project.
  * Append the new project to ~/.ai-quickstart/managed-projects.json.
  * Write a per-project anecdote to
    ~/.ai-quickstart/persona/anecdotes/<project-slug>.md.
  * Trigger persona heal (auto-fire when there are new anecdotes).
  * Write the scaffold receipt to step-3-scaffold.md.
-->

---

## Subcommands

`/ai-quickstart` with no args runs Phase 0 → Phase 1 → Phase 2 → Phase 3.

`/ai-quickstart heal` — manual persona heal. <!-- LANE G implements heal.py. -->

`/ai-quickstart uninstall` — removes hooks per `installed-hooks.json` manifest;
optionally `rm -rf ~/.ai-quickstart` after confirmation.
<!-- LANE F implements uninstall logic. -->

---

## Rules You Follow

1. **Never block on a soft-fail.** Phase 0 update, version detect, compathy
   install, sync detection, and hook install all warn-and-continue.
2. **Stdlib only.** No requests, PyYAML, or click — see `scripts/sources/`
   for the urllib + html.parser pattern.
3. **Audit every run.** Each invocation gets a unique run-id (ISO timestamp
   + short uuid) and a directory under `~/.ai-quickstart/runs/`.
4. **Curated > live.** When live sources fail, fall back to the curated
   `mappings/personas.yaml` baseline.
5. **Hook commands stay tiny.** The PostToolUse one-liner does a stat-check
   first and only execs Python when cwd is a managed project.
6. **Persona heal is flock-protected.** Concurrent invocations fail fast
   rather than corrupting the file.
7. **Compathy is never vendored.** Always shelled out to. SHA pin in
   `COMPATHY_VERSION` is the contract.

---

## When You're Done

Print a compact summary:

```
ai-quickstart v<version>  [compathy <pinned-sha[:7]>]
  run-id:        <run-id>
  archetype:     <job|personal|exploring>
  project:       <project-slug>  (path: <abs-path>)
  suggestions:   <N> skills, <M> MCP servers
  persona:       healed (anecdotes: <K>)
  next:          run `/ai-quickstart` again to start another project
```

Get the version with:

```bash
cat {skill_dir}/VERSION
cat {skill_dir}/COMPATHY_VERSION
```
