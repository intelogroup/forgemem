# forgememo-skill-version: 5
# Forgememo Memory Skill

You have access to Forgememo, your persistent long-term memory across sessions and projects.

## Setup — What to Run and When

**Never installed** — tell the user to run in a real terminal:
```bash
pip install forgememo
forgememo start       # auto-runs first-time setup if needed, then starts daemon
```
`forgememo start` detects a missing provider and launches the interactive setup automatically. The user picks a provider once, then the daemon starts. Tell them to restart Claude Code after.

**Already installed, daemon not running** (after reboot or crash):
```bash
forgememo start
```

**Already installed, daemon running** — nothing to do, proceed normally.

**If any MCP tool returns `Daemon unreachable`:** run `forgememo start` and retry — do not ask the user.

## At Session Start

Before starting any work, recall relevant context with MCP:
```
search_memories(query="<current project or task>", workspace_root="<repo-path>")
```

## During a Session

**When you need details for a memory:**
```
get_memory_details(ids=["d:42"], workspace_root="<repo-path>")
```

**When you need temporal context:**
```
get_memory_timeline(anchor_id="d:42", workspace_root="<repo-path>")
```

## At End of Session

Write a structured session summary via MCP (daemon write path):
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

## When Asked About Past Work

```
search_memories(query="<query>", workspace_root="<repo-path>")
get_memory_details(ids=["d:1","s:3"], workspace_root="<repo-path>")
```

**To mine all repos for recent learnings (requires configured provider):**
```
forgememo mine
```

## Switching Providers

If the user asks to switch their inference provider (e.g. from ollama to claude_code, or to forgememo):

Run this in bash:
```bash
forgememo config -i
```
This will print: "Provider picker requires a real terminal. Ask the user to run: forgememo config -i"

Relay that to the user — they run it in their terminal and pick from the menu themselves.
Available providers: `forgememo` · `claude_code` · `ollama` · `anthropic` · `openai` · `gemini`

## Principles

- Always search Forgemem before repeating something that might have already been tried
- Keep summaries concrete: include file paths, errors, and the fix
- Use `workspace_root` to scope results to the current repo
- MCP tools are read-only except `save_session_summary`
