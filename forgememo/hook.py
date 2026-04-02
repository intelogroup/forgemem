#!/usr/bin/env python3
"""
Forgememo hook adapter — normalize tool events and POST to daemon.

Usage:
  echo '{...}' | python forgememo/hook.py post_tool_use
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Any

import requests


DAEMON_URL = os.environ.get("FORGEMEMO_DAEMON_URL")
SOCKET_PATH = os.environ.get(
    "FORGEMEMO_SOCKET", os.path.join(tempfile.gettempdir(), "forgememo.sock")
)
HTTP_PORT = os.environ.get("FORGEMEMO_HTTP_PORT", "5555")
SOURCE_TOOL = os.environ.get("FORGEMEMO_SOURCE_TOOL", "unknown")

_PRIVATE_RE = None


def _ensure_daemon() -> bool:
    """Check daemon health; auto-restart if unreachable. Returns True if alive."""
    port = HTTP_PORT or "5555"
    url = f"http://127.0.0.1:{port}/health"
    try:
        requests.get(url, timeout=1).raise_for_status()
        return True
    except Exception:
        pass
    try:
        subprocess.Popen(
            [sys.executable, "-m", "forgememo.daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(5):
            time.sleep(1)
            try:
                requests.get(url, timeout=1).raise_for_status()
                return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _compile_private_re():
    import re

    return re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


def strip_private(obj: Any):
    """Recursively strip <private>...</private> from any string in a dict/list."""
    global _PRIVATE_RE
    if _PRIVATE_RE is None:
        _PRIVATE_RE = _compile_private_re()
    if isinstance(obj, str):
        return _PRIVATE_RE.sub("", obj).strip()
    if isinstance(obj, dict):
        return {k: strip_private(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_private(v) for v in obj]
    return obj


def _read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _resolve_project_id(payload: dict) -> str:
    override = os.environ.get("FORGEMEMO_PROJECT_ID")
    if override:
        return override
    if payload.get("project_id"):
        return str(payload["project_id"])
    if payload.get("cwd"):
        return os.path.realpath(str(payload["cwd"]))
    return os.path.realpath(os.getcwd())


def _normalize_event(event_name: str, payload: dict) -> dict:
    session_id = payload.get("session_id") or payload.get("session") or "unknown"
    project_id = _resolve_project_id(payload)
    event_type = payload.get("hook_event_name") or event_name
    tool_name = payload.get("tool_name")
    source_tool = payload.get("source_tool") or SOURCE_TOOL
    seq = payload.get("seq") or payload.get("sequence")
    if seq is None:
        seq = int(time.time() * 1000)

    event = {
        "session_id": session_id,
        "project_id": project_id,
        "source_tool": source_tool,
        "event_type": event_type,
        "tool_name": tool_name,
        "payload": payload,
        "seq": int(seq),
    }
    return strip_private(event)


def _post_event(event: dict) -> None:
    # Daemon expects payload as JSON string
    event = dict(event)
    event["payload"] = json.dumps(event["payload"])

    # Socket-first (requests-unixsocket if available, POSIX only)
    if not DAEMON_URL and sys.platform != "win32":
        try:
            import requests_unixsocket

            session = requests_unixsocket.Session()
            socket_url = "http+unix://" + SOCKET_PATH.replace("/", "%2F")
            session.post(f"{socket_url}/events", json=event, timeout=1.5)
            return
        except Exception:
            pass

    # Fallback HTTP
    try:
        if DAEMON_URL:
            url = DAEMON_URL.rstrip("/")
        elif HTTP_PORT:
            url = f"http://127.0.0.1:{HTTP_PORT}"
        else:
            return
        requests.post(f"{url}/events", json=event, timeout=1.5)
    except Exception:
        # Hook must never crash the host process
        pass


def _daemon_get(path: str, params: dict | None = None) -> dict:
    """GET from daemon — never raises (hook must not crash host process)."""
    if not DAEMON_URL and sys.platform != "win32":
        try:
            import requests_unixsocket

            session = requests_unixsocket.Session()
            socket_url = "http+unix://" + SOCKET_PATH.replace("/", "%2F")
            resp = session.get(f"{socket_url}{path}", params=params, timeout=3)
            if resp.ok:
                return resp.json()
        except Exception:
            pass
    try:
        if DAEMON_URL:
            url = DAEMON_URL.rstrip("/")
        elif HTTP_PORT:
            url = f"http://127.0.0.1:{HTTP_PORT}"
        else:
            return {}
        resp = requests.get(f"{url}{path}", params=params, timeout=3)
        return resp.json() if resp.ok else {}
    except Exception:
        return {}


def _format_context_json(text: str, event_name: str) -> str:
    """Return platform-appropriate JSON for context injection."""
    if SOURCE_TOOL in ("claude-code", "gemini"):
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": text,
                }
            }
        )
    return json.dumps({"systemMessage": text})


def _handle_session_recall(payload: dict, event_name: str) -> int:
    """Fetch recent memories and inject them as context on session start."""
    if not _ensure_daemon():
        print(
            _format_context_json(
                "Forgememo daemon unreachable — run: forgememo start", event_name
            )
        )
        return 0
    project_id = _resolve_project_id(payload)
    summaries = _daemon_get("/session_summaries", {"project_id": project_id, "k": 2})
    search = _daemon_get("/search", {"q": "recent", "project_id": project_id, "k": 5})

    parts = []
    for s in summaries.get("results", []):
        ts = (s.get("ts") or "")[:10]
        parts.append(
            f"[Session {ts}] {s.get('request', '')} — {s.get('learnings', '')}"
        )
    for r in search.get("results", []):
        narrative = (r.get("narrative") or "")[:120]
        parts.append(f"[Memory] {r.get('title', '')}: {narrative}")

    if not parts:
        print(_format_context_json("", event_name))
        return 0

    context = "Forgememo context from previous sessions:\n" + "\n".join(parts)
    print(_format_context_json(context, event_name))
    return 0


# ---------------------------------------------------------------------------
# Error detection + mid-session recall
# ---------------------------------------------------------------------------

# Patterns that indicate an error in tool output
_ERROR_PATTERNS = re.compile(
    r"(?:"
    r"Traceback \(most recent call last\)"
    r"|(?:^|\n)\s*(?:Error|ERROR|error)[\s:[]"
    r"|(?:^|\n)\s*(?:FAILED|FAIL)\b"
    r"|exit (?:code|status)\s*[1-9]"
    r"|CalledProcessError"
    r"|ModuleNotFoundError"
    r"|ImportError"
    r"|SyntaxError"
    r"|TypeError"
    r"|ValueError"
    r"|KeyError"
    r"|AttributeError"
    r"|NameError"
    r"|FileNotFoundError"
    r"|PermissionError"
    r"|RuntimeError"
    r"|OSError"
    r"|ConnectionError"
    r"|TimeoutError"
    r"|command not found"
    r"|No such file or directory"
    r"|npm ERR!"
    r"|Cannot find module"
    r"|Compilation failed"
    r"|Build failed"
    r"|undefined is not"
    r"|is not defined"
    r"|segmentation fault"
    r"|panic:"
    r"|fatal:"
    r")",
    re.IGNORECASE,
)

# Noise to strip when fingerprinting errors
_FINGERPRINT_NOISE = re.compile(
    r"(?:"
    r"0x[0-9a-fA-F]+"            # hex addresses
    r"|line \d+"                   # line numbers
    r"|:\d+:\d+"                   # file:line:col
    r"|/[\w./-]+"                  # file paths
    r"|\b\d{10,}\b"               # timestamps
    r"|\b[0-9a-f]{8,}\b"          # hashes/ids
    r"|\bat \w+\s*\(.*?\)"        # stack frame locations
    r")"
)


def _extract_error_text(payload: dict) -> str | None:
    """Extract error text from a PostToolUse payload, or None if no error."""
    # Claude Code provides tool_result (string or dict with stdout/stderr)
    result = payload.get("tool_result") or payload.get("toolResult") or ""
    if isinstance(result, dict):
        # Bash tool results may have stdout/stderr/exitCode
        parts = []
        if result.get("stderr"):
            parts.append(str(result["stderr"]))
        if result.get("stdout"):
            parts.append(str(result["stdout"]))
        if result.get("content"):
            parts.append(str(result["content"]))
        # Non-zero exit code is an error signal
        exit_code = result.get("exitCode") or result.get("exit_code")
        if exit_code and int(exit_code) != 0:
            parts.append(f"exit code {exit_code}")
        result = "\n".join(parts)
    elif not isinstance(result, str):
        result = str(result)

    if not result:
        return None

    if _ERROR_PATTERNS.search(result):
        return result
    return None


def _error_fingerprint(error_text: str) -> str:
    """Produce a stable fingerprint for an error by stripping noise."""
    # Extract the most meaningful error line (first error-like line)
    lines = error_text.strip().splitlines()
    key_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _ERROR_PATTERNS.search(stripped):
            key_lines.append(stripped)
        if len(key_lines) >= 3:
            break
    if not key_lines:
        key_lines = [lines[0]] if lines else ["unknown"]

    core = "\n".join(key_lines)
    # Remove noise (paths, line numbers, hex addresses)
    core = _FINGERPRINT_NOISE.sub("", core)
    # Collapse whitespace
    core = re.sub(r"\s+", " ", core).strip().lower()
    return hashlib.sha256(core.encode()).hexdigest()[:16]


def _extract_error_keywords(error_text: str) -> str:
    """Extract searchable keywords from an error for memory lookup."""
    lines = error_text.strip().splitlines()
    key_lines = []
    for line in lines:
        stripped = line.strip()
        if _ERROR_PATTERNS.search(stripped):
            key_lines.append(stripped)
        if len(key_lines) >= 3:
            break

    if not key_lines:
        key_lines = lines[:2]

    text = " ".join(key_lines)
    # Remove file paths and noise but keep meaningful words
    text = re.sub(r"/[\w./-]+", "", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "", text)
    text = re.sub(r"\b\d{6,}\b", "", text)
    # Keep only words 3+ chars
    words = [w for w in re.findall(r"[a-zA-Z_]\w{2,}", text)]
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for w in words:
        wl = w.lower()
        if wl not in seen:
            seen.add(wl)
            unique.append(w)
    return " ".join(unique[:12])


def _daemon_post(path: str, data: dict) -> dict:
    """POST to daemon — never raises (hook must not crash host process)."""
    if not DAEMON_URL and sys.platform != "win32":
        try:
            import requests_unixsocket

            session = requests_unixsocket.Session()
            socket_url = "http+unix://" + SOCKET_PATH.replace("/", "%2F")
            resp = session.post(f"{socket_url}{path}", json=data, timeout=3)
            if resp.ok:
                return resp.json()
        except Exception:
            pass
    try:
        if DAEMON_URL:
            url = DAEMON_URL.rstrip("/")
        elif HTTP_PORT:
            url = f"http://127.0.0.1:{HTTP_PORT}"
        else:
            return {}
        resp = requests.post(f"{url}{path}", json=data, timeout=3)
        return resp.json() if resp.ok else {}
    except Exception:
        return {}


# Minimum seconds between error-recall injections for the same fingerprint.
# Prevents hot-reload loops (next dev, nodemon) from flooding searches.
_ERROR_RECALL_DEBOUNCE_SECS = int(
    os.environ.get("FORGEMEMO_ERROR_DEBOUNCE_SECS", "300")
)


def _is_within_debounce(last_ts: str | None) -> bool:
    """Return True if last_ts is within the debounce window (too soon)."""
    if not last_ts:
        return False
    try:
        from datetime import datetime, timezone

        # SQLite CURRENT_TIMESTAMP is UTC, format: "YYYY-MM-DD HH:MM:SS"
        last_dt = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return elapsed < _ERROR_RECALL_DEBOUNCE_SECS
    except Exception:
        return False


def _handle_error_recall(payload: dict, event_name: str) -> int | None:
    """Detect repeated errors mid-session and inject relevant memories.

    Returns 0 with context printed if a repeated error was found and memories
    were injected. Returns None if no error or no repeat — caller should
    continue with normal PostToolUse handling.

    Debounced: at most one injection per error fingerprint per
    _ERROR_RECALL_DEBOUNCE_SECS (default 300s / 5 min) to avoid flooding
    from hot-reload loops (next dev, nodemon, etc.).
    """
    error_text = _extract_error_text(payload)
    if not error_text:
        return None

    fingerprint = _error_fingerprint(error_text)
    session_id = payload.get("session_id") or payload.get("session") or "unknown"
    project_id = _resolve_project_id(payload)

    # Check if we've seen this error before in this session
    check = _daemon_get(
        "/error_events",
        {
            "session_id": session_id,
            "fingerprint": fingerprint,
        },
    )
    prior_count = check.get("count", 0)

    # Always record this error occurrence
    _daemon_post(
        "/error_events",
        {
            "session_id": session_id,
            "project_id": project_id,
            "fingerprint": fingerprint,
            "error_keywords": _extract_error_keywords(error_text),
            "error_text": error_text[:500],
        },
    )

    if prior_count < 1:
        # First occurrence — no recall needed
        return None

    # Debounce: skip if we already injected context for this error recently.
    # This prevents hot-reload loops from flooding searches and exhausting
    # the agent's context window.
    last_ts = check.get("last_ts")
    if _is_within_debounce(last_ts):
        return None

    # Repeated error! Search memories for relevant lessons
    keywords = _extract_error_keywords(error_text)
    if not keywords:
        return None

    search = _daemon_get("/search", {"q": keywords, "project_id": project_id, "k": 5})
    # Also search cross-project for broader lessons
    search_global = _daemon_get("/search", {"q": keywords, "k": 3})

    parts = []
    seen_ids = set()
    for results in [search.get("results", []), search_global.get("results", [])]:
        for r in results:
            rid = r.get("id")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            title = r.get("title", "")
            rtype = r.get("type", "")
            parts.append(f"- [{rtype}] {title}")

    if not parts:
        return None

    # Fetch full narratives for top results (up to 3)
    detailed = []
    for r in list(search.get("results", []))[:3]:
        rid = r.get("id", "")
        if ":" not in rid:
            continue
        prefix, row_id = rid.split(":", 1)
        detail = _daemon_get(f"/observation/{prefix}/{row_id}")
        narrative = detail.get("narrative") or detail.get("learnings") or ""
        if narrative:
            title = detail.get("title") or r.get("title", "")
            detailed.append(f"- {title}: {str(narrative)[:200]}")

    context_lines = [
        f"Forgememo: This error has been seen {prior_count + 1} times this session.",
        "Relevant lessons from memory:",
    ]
    if detailed:
        context_lines.extend(detailed)
    else:
        context_lines.extend(parts[:5])

    context = "\n".join(context_lines)
    print(_format_context_json(context, event_name))
    return 0


def _handle_session_end(payload: dict) -> int:
    """Spawn background end-session synthesis; return immediately."""
    _ensure_daemon()  # best-effort; background subprocess needs daemon up
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    import shutil as _shutil

    forgememo_bin = _shutil.which("forgememo")
    if not forgememo_bin:
        print(json.dumps({}))
        return 0
    cmd = [
        forgememo_bin,
        "end-session",
        "--session-id",
        session_id,
        "--project-dir",
        cwd,
    ]
    try:
        if sys.platform == "win32":
            env = {**os.environ, "FORGEMEMO_HTTP_PORT": HTTP_PORT or "5555"}
            subprocess.Popen(
                cmd,
                env=env,
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass
    print(json.dumps({}))
    return 0


# ---------------------------------------------------------------------------
# Event dispatch tables
# ---------------------------------------------------------------------------

_SESSION_RECALL_EVENTS = {
    "UserPromptSubmit",  # Claude Code, Codex
    "BeforeAgent",  # Gemini
    "sessionStart",  # Copilot
    "session.created",  # OpenCode
    "SessionStart",  # generic
}

_SESSION_END_EVENTS = {
    "Stop",  # Claude Code, Codex
    "SessionEnd",  # Claude Code, Gemini
    "AfterAgent",  # Gemini (per-turn fallback)
    "agentStop",  # Copilot
    "session.idle",  # OpenCode (agent finished)
    "session.deleted",  # OpenCode (session closed)
}

_WRITE_TOOL_NAMES = {"Edit", "Write", "Bash", "NotebookEdit", "MultiEdit"}

_POST_TOOL_USE_EVENTS = {
    "PostToolUse",  # Claude Code, Codex
    "AfterTool",    # Gemini
    "tool.done",    # OpenCode
}


def _handle_post_tool_use(payload: dict, event_name: str) -> int:
    """Post write-op tool events to daemon; silently skip read-only tools.

    Also detects repeated errors and injects relevant memories mid-session.
    """
    # Check for repeated errors first — applies to ALL tools (Bash, Edit, etc.)
    error_rc = _handle_error_recall(payload, event_name)
    if error_rc is not None:
        # Error recall handled output; still record the write event if applicable
        tool_name = payload.get("tool_name") or payload.get("toolName") or ""
        if tool_name in _WRITE_TOOL_NAMES:
            event = _normalize_event(event_name, payload)
            _post_event(event)
        return error_rc

    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    if tool_name not in _WRITE_TOOL_NAMES:
        return 0
    event = _normalize_event(event_name, payload)
    _post_event(event)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python forgememo/hook.py <event_name>", file=sys.stderr)
        return 2
    event_name = sys.argv[1]
    try:
        payload = _read_stdin_json()
    except Exception as e:
        print(f"Invalid JSON payload: {e}", file=sys.stderr)
        return 1

    if event_name in _SESSION_RECALL_EVENTS:
        return _handle_session_recall(payload, event_name)
    if event_name in _SESSION_END_EVENTS:
        return _handle_session_end(payload)
    if event_name in _POST_TOOL_USE_EVENTS:
        return _handle_post_tool_use(payload, event_name)

    event = _normalize_event(event_name, payload)
    _post_event(event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
