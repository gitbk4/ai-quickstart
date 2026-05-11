---
name: start
description: Use when the user wants to start a new ai-quickstart run, kicks off project scaffolding, or says "interview me for a new project". Opens the 3-question entry interview and persists the answers so suggest/accept can pick up the run-id.
allowed-tools: [Read, Bash, WebSearch]
---

# /ai-quickstart:start

Kick off a new ai-quickstart interview run. This is Phase 1 of the
three-step flow (interview -> suggest -> accept). Phases 0, 2, 3, and the
heal cycle live in the standalone `ai-quickstart` skill at the repo root and
the other plugin skills.

User input (interpret as `$ARGUMENTS`):

```
$ARGUMENTS
```

If `$ARGUMENTS` is empty, ask the user one short question:

> Which archetype best describes this run? `job` (you have a role and an
> industry), `personal` (you have a problem or product idea), or `exploring`
> (you want to learn or shop around). Default: `personal`.

## Step 1 - open the session

Run the deterministic CLI to create the run-id, the run directory, and the
step-1 adversarial prompt:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init.py start --archetype <archetype>
```

The script emits JSON like
`{"run_id": "...", "archetype": "...", "prompt_path": "...", "started_at": "..."}`.
Capture the `run_id` for the rest of this skill and the follow-on
`/ai-quickstart:suggest` invocation.

## Step 2 - conduct the interview

Read the file at `prompt_path` for adversarial framing. Then open with the
three anchor questions for the archetype:

- **job** - "What's your job, and what industry?"
- **personal** - "What are you trying to create? What problem do you want to solve?"
- **exploring** - "Do you want to learn something, solve a problem, or both? What do you spend the most computer time on?"

Ask 2-4 progressive follow-ups based on the answers. Use WebSearch if you
need fresher context to ask sharper questions. Push back on vague answers
and surface contradictions; do not just take what the user says at face
value.

## Step 3 - persist the answers

Once you have enough signal, call `record-answers` with the run-id and
pipe a JSON blob on stdin:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init.py record-answers --run-id <run-id> <<'EOF'
{
  "archetype": "...",
  "role": "...",
  "industry": "...",
  "top_problems": ["..."],
  "desired_outcomes": ["..."],
  "skill_tolerance": "strict",
  "project_style": "minimal",
  "coding_languages": ["..."],
  "freeform_notes": "..."
}
EOF
```

## Step 4 - hand off

Tell the user the run-id and prompt them to continue:

> Interview captured. Run `/ai-quickstart:suggest <run-id>` to see curated
> projects, skills, and MCP servers ranked for your profile.
