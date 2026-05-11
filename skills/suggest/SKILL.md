---
name: suggest
description: Use when the user has a run-id from /ai-quickstart:start (or asks "what should I build / which skills should I install"). Loads curated mapping + live freshness (GitHub stars, MCP registry, mcpmarket) and renders project templates, skills, MCP servers, alternatives, and trust badges in chat-friendly form.
allowed-tools: [Read, Bash]
---

# /ai-quickstart:suggest

Run Phase 2 of the ai-quickstart flow. Given a run-id (from
`/ai-quickstart:start`), load the curated `mappings/personas.yaml`, overlay
live freshness data, and present the user with a ranked list of project
templates, skills, MCP servers, and alternatives.

User input (interpret as `$ARGUMENTS`):

```
$ARGUMENTS
```

`$ARGUMENTS` should be the run-id. If empty, ask the user for the run-id
they got back from `/ai-quickstart:start` (or run `status` via the
standalone skill to find the latest).

## Step 1 - generate the suggestions

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/init.py suggest --run-id <run-id>
```

Stdout emits JSON of the shape:

```
{
  "project_templates": [...],
  "skills": [
    {"name": "...", "stars": 1234, "last_commit": "...", "warning_low_quality": false,
     "trust": {"score": 4, "provenance": "curated", "badge_text": "...", "badge_ansi": "..."},
     "alternatives": [{"name": "...", "kind": "saas|oss|skill|mcp|agent", "why": "..."}]},
    ...
  ],
  "mcp_servers": [...],
  "warnings": [...]
}
```

The script also writes a step-2 adversarial framing prompt to
`~/.ai-quickstart/runs/<run-id>/step-2-prompt.md`. Read it - it sharpens
how you should present these results.

## Step 2 - render for the user

Render the results chat-friendly, not a JSON dump:

1. **Project templates** - top 3 by score, with one-line rationale each.
2. **Skills** - grouped by category if obvious, otherwise ranked. For each
   skill show: name, trust badge text, one-line "why this for you", and
   1-2 alternatives. If `warning_low_quality` is true, flag it explicitly:
   "Heads up: <N> stars, last commit <date>. Curated baseline only - the
   live version is thin."
3. **MCP servers** - similar treatment.
4. **Warnings** - surface anything in the `warnings` list verbatim.

Push back if a SaaS-only suggestion appears where a real OSS alternative
exists in the live data. Propose the alternative explicitly.

## Step 3 - capture decisions

Ask the user which projects + skills they want to accept, and ask for:

- a kebab-case slug per project
- a target directory (absolute path)
- a 1-2 sentence anecdote seed ("why this project")
- their selected skills list (subset of what you showed)

Then tell them:

> Run `/ai-quickstart:setup` if you want the first-run wizard to wire
> compathy + persona scaffolding, or invoke the standalone `ai-quickstart`
> skill with the same run-id to proceed to Phase 3 (scaffold).
