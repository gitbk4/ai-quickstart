---
name: whoami
description: Use when the user asks "who am I to ai-quickstart", "what does my persona say", "give me my 30-second summary", or otherwise wants a quick read of their stored persona. Reads ~/.ai-quickstart/persona/persona.json (or .md fallback) and prints role / archetype / industry / project_style plus 1-2 high-trust paragraphs.
allowed-tools: [Read, Bash]
---

# /ai-quickstart:whoami

A 30-second persona summary. No interview, no scaffolding, just a quick
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

The `.json` and `.md` files have DIFFERENT shapes; the path you read
depends on which file you're consuming. Get this right or you will
silently render nothing.

### If `persona.json` exists, prefer it

Read it with the Read tool. The structured fields live under
`structured.X` (NOT `frontmatter.X`):

- `structured.archetype`     (one of "job", "personal", "exploring")
- `structured.role`          (string or null)
- `structured.industry`      (string or null)
- `structured.project_style` (string or null)

Top-level metadata:

- `generated_at`             (ISO 8601 timestamp, top-level field)
- `paragraphs` length        (use as the "anecdote count" proxy)

Top paragraphs: sort `paragraphs[]` by `trust_score` descending (NOT
`trust.score`; the field is flat on each paragraph object). Pick the
top 1-2. On ties, prefer paragraphs whose `provenance` is `"pinned"` or
`"anecdote"`. Skip paragraphs with `locked: true` only if you would
otherwise summarize them; user-locked identity claims should be
surfaced verbatim when they're the top signal.

### If only `persona.md` exists, fall back to it

The markdown file uses a different field layout. Pull the YAML
frontmatter and read these paths (NOT `structured.X`):

- `identity.archetype`
- `identity.role`
- `identity.industry`
- `preferences.project_style`
- `generated.updated_at`
- `generated.anecdote_count`

For paragraphs, use `<!-- p:NNN -->` markers if present (the heal cycle
emits them); otherwise take the first two non-empty paragraphs of the
body. The `.md` has no trust scores or provenance flags inline, so
order by file position.

## Step 4 - print the summary

Render compact, chat-friendly:

```
You are <archetype> | <role> | <industry> | project-style: <style>
Persona last updated: <timestamp>  (<count> paragraphs)

Top signals:
  - [trust <n>] <paragraph 1 first sentence...>
  - [trust <n>] <paragraph 2 first sentence...>

Want more? Read ~/.ai-quickstart/persona/persona.md for the full prose,
or run `/ai-quickstart:suggest <run-id>` to use this persona to rank
project ideas.
```

Drop the `[trust <n>]` badges when rendering from the `.md` fallback;
the score field doesn't exist there. Keep the whole summary under 12
lines. The whole point of `whoami` is fast.
