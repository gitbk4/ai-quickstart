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

Open the session, then conduct the interview yourself (you're the LLM):

```bash
python3 {skill_dir}/scripts/init.py start --archetype <job|personal|exploring>
```

This emits JSON: `{run_id, archetype, prompt_path, started_at}`. Read the
prompt file at `prompt_path` — it contains adversarial framing for the
interview. Use it to drive the conversation.

Open with the 3 anchor questions for the chosen archetype:
- **job** — what's your job and what industry?
- **personal** — what are you trying to create? what problem do you want to solve?
- **exploring** — do you want to learn something, solve a problem, or both? what do you do most on the computer?

Then ask progressive deeper questions based on the answers. Use WebSearch
mid-flight when you need more context to ask sharper follow-ups. Push back
on vague answers; surface contradictions.

When the interview is complete, persist the captured answers:

```bash
python3 {skill_dir}/scripts/init.py record-answers --run-id <run-id> <<EOF
{
  "archetype": "...",
  "role": "...",
  "industry": "...",
  "top_problems": ["..."],
  "desired_outcomes": ["..."],
  "skill_tolerance": "strict | permissive",
  "project_style": "minimal | full",
  "coding_languages": ["..."],
  "freeform_notes": "..."
}
EOF
```

---

## Phase 2 — Suggest projects, skills, and MCP servers

Run the suggestion engine:

```bash
python3 {skill_dir}/scripts/init.py suggest --run-id <run-id>
```

Stdout emits JSON `{project_templates, skills, mcp_servers, warnings}`. Each
skill includes live freshness data: stars, last_commit, contributors,
warning_low_quality flag (true when stars <100). The step-2 adversarial
prompt is also written to disk at `runs/<run-id>/step-2-prompt.md` — read
it for framing on how to present these to the user.

Show the user the ranked list. Highlight any `warning_low_quality` items
explicitly so they can make an informed call. Push back if a SaaS-only
suggestion appears where a real OSS alternative exists in the GitHub
results — propose the alternative.

Capture the user's accept/reject decisions and project naming choices.
Each accepted project needs: a slug (kebab-case), a target directory,
an anecdote_seed (1-2 sentence summary of why this project), and the
selected skills list.

---

## Phase 3 — Scaffold the project

Scaffold each accepted project (compathy creates the structure, anecdote
seeded, project registered):

```bash
python3 {skill_dir}/scripts/init.py accept --run-id <run-id> <<EOF
{
  "project_specs": [
    {
      "slug": "my-research-bot",
      "dir": "/Users/<user>/Code/my-research-bot",
      "anecdote_seed": "Started as a quickstart for marketing research workflows.",
      "skills": [<the skill objects from suggest>]
    }
  ]
}
EOF
```

Stdout emits per-project results. Use `--dry-run` first if you want to
preview without writing.

After scaffolding succeeds, ask the user for starting files (Step 5
folded into Step 3 per locked plan). Copy them into each project:

```bash
python3 {skill_dir}/scripts/init.py add-starting-files \
  --project-dir /Users/<user>/Code/my-research-bot <<EOF
["/path/to/existing-doc.md", "/path/to/data-snapshot.json"]
EOF
```

Finally, trigger a persona heal so the new anecdotes propagate into the
global persona:

```bash
python3 {skill_dir}/scripts/heal.py prepare-context | <synthesize new prose> | \
  python3 {skill_dir}/scripts/heal.py write
```

(For now you can skip the heal step in v1 — it'll auto-fire on the next
`/ai-quickstart` invocation. Heal is also available standalone via
`/ai-quickstart heal`.)

---

## Subcommands

`/ai-quickstart` with no args runs Phase 0 → Phase 1 → Phase 2 → Phase 3.

`/ai-quickstart heal` — manual persona heal. Reads activity + anecdotes,
flock-protected, atomic write with backup, shows diff to user.

`/ai-quickstart uninstall` — removes hooks per `installed-hooks.json` manifest;
optionally `rm -rf ~/.ai-quickstart` after confirmation.

`/ai-quickstart eval` — runs the persona-heal eval suite in Claude-as-judge
mode (no API key needed). Run via:

```bash
python3 {skill_dir}/scripts/init.py eval
```

The harness prints structured prompt blocks to stdout. As Claude orchestrating
this skill, you read the output, then for each case: (1) synthesize a candidate
persona prose given the inputs, (2) judge it against the listed expectations,
(3) emit one JSON verdict per case, then a final summary `{total, passed,
score_avg}`. Optional flags: `--case-filter <name>` runs one case, `--eval-file
<path>` overrides the bundled fixture.

`/ai-quickstart next-project` recommends the user's NEXT project from
their existing persona plus the curated mapping. v1 collected the persona;
v1.1 reads it back to score every (archetype, industry, project_template)
combo and surface a ranked list. Run via:

```bash
python3 {skill_dir}/scripts/init.py next-project --top 5
```

Stdout emits JSON `{recommendations, reasoning, persona_signals, warnings}`.
Each recommendation includes `project_template`, `archetype`, `industry`,
`skills` (curated names), `score` (0..1), and `why` (signal list). Scoring
factors: archetype match (+0.4), industry match (+0.3), goal alignment with
top_problems (+0.15), activity within 30 days (+0.1), starter boost when
project_count < 3 (+0.05). Pure computation; no network.

Optional flags: `--top N` (default 5), `--mapping PATH` (default
bundled `mappings/personas.yaml`), `--persona PATH` (default
`$AI_QUICKSTART_HOME/persona/persona.md`). Exits 2 if the persona file is
missing with a clear stderr message; exits 1 on other errors.

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
