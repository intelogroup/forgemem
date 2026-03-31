# forgememo-skill-version: 4
# Forgemem Memory Skill

You have access to Forgemem, your persistent long-term memory across sessions and projects.

## Setup (first time only)

If forgememo is not yet initialized, ask the user to run this in a real terminal:

```bash
pip install forgememo
forgememo init
```

Then tell the user: "Forgemem is installed. Please restart Gemini CLI to activate the memory MCP connection."

Do NOT try to bypass first-run setup with `forgememo init --yes` or a non-TTY session. The user must choose a provider interactively.

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
