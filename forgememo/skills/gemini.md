# forgememo-skill-version: 5
# Forgememo Memory Skill

You have access to Forgememo, your persistent long-term memory across sessions and projects.

## Setup — What to Run and When

**Never installed** — tell the user to run in a real terminal:
```bash
pip install forgememo
forgememo start       # auto-runs first-time setup if needed, then starts daemon
```
`forgememo start` detects a missing provider and launches the interactive setup automatically. The user picks a provider once, then the daemon starts. Tell them to restart Gemini CLI after.

**Already installed, daemon not running** (after reboot or crash):
```bash
forgememo start
```

**Already installed, daemon running** — nothing to do, proceed normally.

**If any MCP tool returns `Daemon unreachable`:** run `forgememo start` and retry.

## At Session Start (REQUIRED)

Gemini CLI does not support shell hooks, so you MUST call this yourself at the beginning of every session:

```
session_sync(workspace_root="<repo-path>", request="<what the user asked for>")
```

This returns memory context from previous sessions AND registers the session start with the daemon so your work is captured. Do not skip it.

## During a Session

**Search for relevant context:**
```
search_memories(query="<current task>", workspace_root="<repo-path>")
```

**Get full content for a memory:**
```
get_memory_details(ids=["d:42"], workspace_root="<repo-path>")
```

**Get temporal context around a memory:**
```
get_memory_timeline(anchor_id="d:42", workspace_root="<repo-path>")
```

## At End of Session (REQUIRED)

Since Gemini CLI has no hook for session end, you MUST call this yourself before the conversation ends:

```
save_session_summary(
  request="<what the user asked for>",
  workspace_root="<repo-path>",
  investigation="<what you checked>",
  learnings="<key technical learnings>",
  next_steps="<what to do next>",
  concepts=["pattern","gotcha"]
)
```

## Principles

- Always call `session_sync` at the start — this is your hook replacement
- Always call `save_session_summary` at the end — do not rely on automatic synthesis
- Search Forgemem before repeating something that might have been tried before
- Keep summaries concrete: include file paths, errors, and the fix
- Use `workspace_root` to scope memory to the current repository
