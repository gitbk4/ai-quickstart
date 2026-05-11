---
name: whoami
description: Use when the user asks "who am I to ai-quickstart", "what does my persona say", "give me my 30-second summary", or otherwise wants a quick read of their stored persona. Reads ~/.ai-quickstart/persona/persona.md (or .json) and prints role / archetype / industry / project_style plus 1-2 high-trust paragraphs.
allowed-tools: [Read, Bash]
---

# /ai-quickstart:whoami

A 30-second persona summary. No interview, no scaffolding - just a quick
read of what ai-quickstart already knows about the user.

## Step 1 - resolve the persona path

```bash
python3 - <<'PY'
import json, os
from pathlib import Path
home = Path(os.environ.get("AI_QUICKSTART_HOME") or (Path.home() / ".ai-quickstart"))
md = home / "persona" / "persona.md"
js = home / "persona" / "persona.json"
print(json.dumps({"md": str(md), "md_exists": md.is_file(),
                  "json": str(js), "json_exists": js.is_file(),
                  "home": str(home)}))
PY
```

## Step 2 - no persona yet?

If neither file exists, do NOT make up a persona. Print:

> I don't see a persona for you yet. Run `/ai-quickstart:setup` to do the
> first-run onboarding, or `/ai-quickstart:start` to kick off an interview
> and let the heal cycle build one from your activity.

Then stop.

## Step 3 - read and render

If `persona.json` exists, prefer it (it has structured fields). Read it
with the Read tool. Extract:

- `frontmatter.archetype`
- `frontmatter.role`
- `frontmatter.industry`
- `frontmatter.project_style`
- `frontmatter.generated.updated_at` and `anecdote_count`
- The top 1-2 paragraphs by `trust.score` (descending). If two paragraphs
  tie at the top score, prefer ones with `provenance == "pinned"` or
  `"anecdote"`. Skip any paragraph wrapped in `<!-- lock:start -->` markers
  in the .md - those are user-locked identity claims and should be
  surfaced verbatim.

If only `persona.md` exists, read it. Pull the YAML frontmatter and the
top 1-2 paragraphs (use `<!-- p:NNN -->` markers if present; otherwise
take the first two non-empty paragraphs of the body).

## Step 4 - print the summary

Render compact, chat-friendly:

```
You are <archetype> | <role> | <industry> | project-style: <minimal|full>
Persona last healed: <updated_at>  (<anecdote_count> anecdotes)

Top signals:
  - <trust badge> <paragraph 1 first sentence...>
  - <trust badge> <paragraph 2 first sentence...>

Want more? Read ~/.ai-quickstart/persona/persona.md for the full prose,
or run `/ai-quickstart:suggest <run-id>` to use this persona to rank
project ideas.
```

Keep it under 12 lines. The whole point of `whoami` is fast.
