---
name: setup
description: Use on first install of the ai-quickstart plugin, or when the user says "set up ai-quickstart", "do the onboarding", or "configure ai-quickstart for me". First-run wizard scaffold; final wiring (compathy auto-install, telemetry opt-in, persona scaffold, archetype defaults) is filled in by lane PB.
allowed-tools: [Read, Bash, WebSearch]
---

# /ai-quickstart:setup

First-run wizard for the ai-quickstart plugin. The job is to take a user
who just installed the plugin from zero state to "your persona file
exists, compathy is wired, and you know which slash command to run next".

This file is a **scaffold**. The conversational + side-effect logic is
filled in by lane PB.

User input (interpret as `$ARGUMENTS`):

```
$ARGUMENTS
```

## TODO-PB-1: extend with onboarding logic

Lane PB should wire the following steps. The ordering below is the spec
contract for what the wizard must cover; the body of each step is left
empty for PB to author.

### Step 1 - greet and explain

Greet the user, name the plugin, and explain in 2 sentences what it does
and what data it stores (`~/.ai-quickstart/` persona, runs, activity log).
Set expectations: this is a one-time setup, ~3 minutes.

### Step 2 - detect archetype default from `git config user.email`

Run:

```bash
git config --global user.email
```

Inspect the domain. Apply heuristic defaults:

- `*.edu` -> default archetype `exploring`
- `gmail.com` / `outlook.com` / `proton.me` / `pm.me` / personal-domain
  patterns -> default archetype `personal`
- corporate / company domain -> default archetype `job`

Show the inference to the user and let them confirm or override.

### Step 3 - compathy auto-install at pinned SHA

Compathy is the soft dependency. Read the pinned SHA:

```bash
cat ${CLAUDE_PLUGIN_ROOT}/COMPATHY_VERSION
```

Then drive the install flow defined in the standalone SKILL.md Phase 0c.
Confirm with the user before cloning. If the clone fails, warn and
continue - Phase 3 scaffolding will surface the failure later.

### Step 4 - telemetry opt-in

Show the user the telemetry policy:

- local `activity.jsonl` always records (heal needs it)
- remote aggregation is opt-in, anonymous install-id, no PII

Ask explicitly: opt in to remote aggregation? Default no.
Persist the decision to `~/.ai-quickstart/telemetry.json`.

### Step 5 - persona scaffold

If `~/.ai-quickstart/persona/persona.md` does not exist, create the empty
scaffold with the user's confirmed archetype, role (ask), industry (ask
or skip), and project_style (default `minimal`). This is the seed the
heal cycle will grow into a full persona over time.

### Step 6 - hand off

Print a 3-line summary and point at the next command:

> Setup done. Your persona scaffold is at `~/.ai-quickstart/persona/persona.md`.
> Run `/ai-quickstart:start` to kick off your first project interview, or
> `/ai-quickstart:whoami` to see what we've got so far.

## TODO-PB-2: idempotency

The wizard must be safe to re-run. On a second invocation, detect existing
persona / telemetry / compathy install and offer to refresh each step
rather than overwrite. Lane PB owns this UX.
