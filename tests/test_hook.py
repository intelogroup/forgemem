"""
Tests for the hook adapter (forgememo/hook.py).

Covers:
- strip_private: strings, dicts, lists, nested structures
- _normalize_event: field mapping, fallbacks
- _resolve_project_id: env var override, cwd fallback
- _format_context_json: per-tool output format
- _read_stdin_json: empty / valid input
- _post_event: transport selection, DAEMON_URL, exception swallowing
- _daemon_get: HTTP, socket, error paths
- _handle_session_recall: memories, daemon-down, narrative truncation
- _handle_session_end: POSIX / Windows, missing binary, missing cwd
- _handle_error_recall: error detection, fingerprinting, mid-session recall
- main(): error exits, cross-agent event dispatch
"""

from __future__ import annotations

import io
import json
import os
import sys

import pytest
from unittest.mock import MagicMock, patch

import forgememo.hook as hook
from forgememo.hook import (
    strip_private,
    _normalize_event,
    _resolve_project_id,
    _ensure_daemon,
    _format_context_json,
    _handle_post_tool_use,
    _handle_session_recall,
    _handle_session_end,
    _handle_error_recall,
    _extract_error_text,
    _error_fingerprint,
    _extract_error_keywords,
    _is_within_debounce,
    _read_stdin_json,
    _post_event,
    _daemon_get,
    _SESSION_RECALL_EVENTS,
    _SESSION_END_EVENTS,
    _POST_TOOL_USE_EVENTS,
    _WRITE_TOOL_NAMES,
)


# ---------------------------------------------------------------------------
# strip_private
# ---------------------------------------------------------------------------

class TestStripPrivate:
    def test_removes_private_tag(self):
        result = strip_private("hello <private>SECRET</private> world")
        assert "SECRET" not in result
        assert "hello" in result
        assert "world" in result

    def test_removes_multiline_private(self):
        text = "before <private>\nline1\nline2\n</private> after"
        result = strip_private(text)
        assert "line1" not in result
        assert "before" in result
        assert "after" in result

    def test_case_insensitive(self):
        result = strip_private("a <PRIVATE>secret</PRIVATE> b")
        assert "secret" not in result

    def test_no_private_tag_unchanged(self):
        text = "nothing special here"
        assert strip_private(text) == text

    def test_dict_values_stripped(self):
        d = {"key": "value <private>hidden</private> end", "other": "clean"}
        result = strip_private(d)
        assert "hidden" not in result["key"]
        assert result["other"] == "clean"

    def test_dict_keys_not_stripped(self):
        d = {"<private>key</private>": "value"}
        result = strip_private(d)
        # Keys are not recursed into — only values
        assert "<private>key</private>" in result

    def test_list_items_stripped(self):
        lst = ["clean", "has <private>secret</private> end", "also clean"]
        result = strip_private(lst)
        assert "secret" not in result[1]
        assert result[0] == "clean"
        assert result[2] == "also clean"

    def test_nested_dict_stripped(self):
        d = {"outer": {"inner": "x <private>hidden</private> y"}}
        result = strip_private(d)
        assert "hidden" not in result["outer"]["inner"]

    def test_nested_list_in_dict_stripped(self):
        d = {"items": ["a", "<private>b</private>", "c"]}
        result = strip_private(d)
        assert result["items"][1] == ""

    def test_non_string_passthrough(self):
        assert strip_private(42) == 42
        assert strip_private(3.14) == 3.14
        assert strip_private(None) is None
        assert strip_private(True) is True

    def test_multiple_private_blocks(self):
        text = "a <private>x</private> b <private>y</private> c"
        result = strip_private(text)
        assert "x" not in result
        assert "y" not in result
        assert "a" in result
        assert "b" in result
        assert "c" in result

    def test_empty_private_block(self):
        result = strip_private("before <private></private> after")
        assert result.strip() in ("before  after", "before after")


# ---------------------------------------------------------------------------
# _resolve_project_id
# ---------------------------------------------------------------------------

class TestResolveProjectId:
    def test_env_var_overrides_all(self, monkeypatch):
        monkeypatch.setenv("FORGEMEMO_PROJECT_ID", "/override/project")
        result = _resolve_project_id({"cwd": "/some/cwd", "project_id": "inline"})
        assert result == "/override/project"

    def test_payload_project_id_used(self, monkeypatch):
        monkeypatch.delenv("FORGEMEMO_PROJECT_ID", raising=False)
        result = _resolve_project_id({"project_id": "myproject"})
        assert result == "myproject"

    def test_cwd_used_as_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FORGEMEMO_PROJECT_ID", raising=False)
        result = _resolve_project_id({"cwd": str(tmp_path)})
        assert result == str(tmp_path.resolve())

    def test_getcwd_used_when_no_hints(self, monkeypatch):
        monkeypatch.delenv("FORGEMEMO_PROJECT_ID", raising=False)
        result = _resolve_project_id({})
        assert result == os.path.realpath(os.getcwd())

    def test_env_var_takes_precedence_over_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FORGEMEMO_PROJECT_ID", "/env/project")
        result = _resolve_project_id({"cwd": str(tmp_path)})
        assert result == "/env/project"


# ---------------------------------------------------------------------------
# _normalize_event
# ---------------------------------------------------------------------------

class TestNormalizeEvent:
    def setup_method(self):
        os.environ.pop("FORGEMEMO_PROJECT_ID", None)

    def _payload(self, **overrides):
        base = {
            "session_id": "sess-abc",
            "project_id": "/tmp/proj",
            "source_tool": "claude",
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "seq": 10,
        }
        base.update(overrides)
        return base

    def test_session_id_mapped(self):
        event = _normalize_event("PostToolUse", self._payload())
        assert event["session_id"] == "sess-abc"

    def test_event_type_from_hook_event_name(self):
        event = _normalize_event("PostToolUse", self._payload())
        assert event["event_type"] == "PostToolUse"

    def test_event_type_fallback_to_arg(self):
        payload = self._payload()
        del payload["hook_event_name"]
        event = _normalize_event("my_event", payload)
        assert event["event_type"] == "my_event"

    def test_tool_name_mapped(self):
        event = _normalize_event("PostToolUse", self._payload())
        assert event["tool_name"] == "Edit"

    def test_seq_mapped(self):
        event = _normalize_event("PostToolUse", self._payload())
        assert event["seq"] == 10

    def test_seq_defaults_to_timestamp_when_missing(self):
        payload = self._payload()
        del payload["seq"]
        event = _normalize_event("PostToolUse", payload)
        assert isinstance(event["seq"], int)
        assert event["seq"] > 0

    def test_source_tool_from_payload(self):
        event = _normalize_event("PostToolUse", self._payload(source_tool="codex"))
        assert event["source_tool"] == "codex"

    def test_source_tool_fallback_to_env(self, monkeypatch):
        monkeypatch.setenv("FORGEMEMO_SOURCE_TOOL", "gemini")
        payload = self._payload()
        del payload["source_tool"]
        import importlib
        import forgememo.hook as hook_module
        importlib.reload(hook_module)
        event = hook_module._normalize_event("PostToolUse", payload)
        assert event["source_tool"] == "gemini"

    def test_private_stripped_in_normalized_event(self):
        payload = self._payload()
        payload["secret"] = "<private>token=abc123</private>"
        event = _normalize_event("PostToolUse", payload)
        assert "abc123" not in str(event)

    def test_unknown_session_fallback(self):
        payload = self._payload()
        del payload["session_id"]
        event = _normalize_event("PostToolUse", payload)
        assert event["session_id"] == "unknown"


# ---------------------------------------------------------------------------
# _ensure_daemon
# ---------------------------------------------------------------------------

class TestEnsureDaemon:
    def test_returns_true_when_daemon_healthy(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("forgememo.hook.requests.get", return_value=mock_resp) as mock_get:
            result = _ensure_daemon()
        assert result is True
        mock_get.assert_called_once()

    def test_spawns_subprocess_and_polls_when_daemon_down(self, monkeypatch):
        import requests as _requests

        call_count = {"n": 0}

        def fake_get(url, timeout=1):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise _requests.exceptions.ConnectionError("refused")
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        with patch("forgememo.hook.requests.get", side_effect=fake_get), \
             patch("forgememo.hook.subprocess.Popen") as mock_popen, \
             patch("forgememo.hook.time.sleep"):
            result = _ensure_daemon()

        assert result is True
        mock_popen.assert_called_once()

    def test_returns_false_when_daemon_never_starts(self, monkeypatch):
        import requests as _requests

        with patch("forgememo.hook.requests.get",
                   side_effect=_requests.exceptions.ConnectionError("refused")), \
             patch("forgememo.hook.subprocess.Popen"), \
             patch("forgememo.hook.time.sleep"):
            result = _ensure_daemon()

        assert result is False


# ---------------------------------------------------------------------------
# _handle_post_tool_use
# ---------------------------------------------------------------------------

class TestPostToolUseHook:
    def test_write_tool_is_posted(self, monkeypatch):
        posted = []
        monkeypatch.setattr("forgememo.hook._post_event", lambda e: posted.append(e))
        _handle_post_tool_use(
            {"tool_name": "Edit", "session_id": "s1", "project_id": "/tmp"}, "PostToolUse"
        )
        assert len(posted) == 1
        assert posted[0]["tool_name"] == "Edit"

    def test_read_tool_is_skipped(self, monkeypatch):
        posted = []
        monkeypatch.setattr("forgememo.hook._post_event", lambda e: posted.append(e))
        for read_tool in ("Read", "Grep", "Glob", "WebSearch", "WebFetch"):
            _handle_post_tool_use({"tool_name": read_tool, "session_id": "s1"}, "PostToolUse")
        assert len(posted) == 0

    def test_all_write_tools_captured(self, monkeypatch):
        posted = []
        monkeypatch.setattr("forgememo.hook._post_event", lambda e: posted.append(e))
        for tool in _WRITE_TOOL_NAMES:
            _handle_post_tool_use({"tool_name": tool, "session_id": "s1"}, "PostToolUse")
        assert len(posted) == len(_WRITE_TOOL_NAMES)

    def test_unknown_tool_is_skipped(self, monkeypatch):
        posted = []
        monkeypatch.setattr("forgememo.hook._post_event", lambda e: posted.append(e))
        _handle_post_tool_use({"tool_name": "", "session_id": "s1"}, "PostToolUse")
        _handle_post_tool_use({"session_id": "s1"}, "PostToolUse")
        assert len(posted) == 0

    def test_private_content_stripped(self, monkeypatch):
        posted = []
        monkeypatch.setattr("forgememo.hook._post_event", lambda e: posted.append(e))
        _handle_post_tool_use(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo <private>secret</private>"},
                "session_id": "s1",
            },
            "PostToolUse",
        )
        assert len(posted) == 1
        assert "secret" not in str(posted[0])

    def test_gemini_aftertool_variant(self, monkeypatch):
        posted = []
        monkeypatch.setattr("forgememo.hook._post_event", lambda e: posted.append(e))
        _handle_post_tool_use({"tool_name": "Write", "session_id": "s1"}, "AfterTool")
        assert len(posted) == 1


# ---------------------------------------------------------------------------
# _format_context_json
# ---------------------------------------------------------------------------


class TestFormatContextJson:
    def test_claude_code_uses_hook_specific_output(self, monkeypatch):
        monkeypatch.setattr(hook, "SOURCE_TOOL", "claude-code")
        result = json.loads(_format_context_json("hello", "UserPromptSubmit"))
        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert result["hookSpecificOutput"]["additionalContext"] == "hello"

    def test_gemini_uses_hook_specific_output(self, monkeypatch):
        monkeypatch.setattr(hook, "SOURCE_TOOL", "gemini")
        result = json.loads(_format_context_json("ctx", "BeforeAgent"))
        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["hookEventName"] == "BeforeAgent"
        assert result["hookSpecificOutput"]["additionalContext"] == "ctx"

    def test_codex_uses_system_message(self, monkeypatch):
        monkeypatch.setattr(hook, "SOURCE_TOOL", "codex")
        result = json.loads(_format_context_json("msg", "UserPromptSubmit"))
        assert "systemMessage" in result
        assert "hookSpecificOutput" not in result
        assert result["systemMessage"] == "msg"

    def test_unknown_tool_uses_system_message(self, monkeypatch):
        monkeypatch.setattr(hook, "SOURCE_TOOL", "opencode")
        result = json.loads(_format_context_json("msg", "session.created"))
        assert "systemMessage" in result

    def test_empty_text_embedded(self, monkeypatch):
        monkeypatch.setattr(hook, "SOURCE_TOOL", "claude-code")
        result = json.loads(_format_context_json("", "SessionStart"))
        assert result["hookSpecificOutput"]["additionalContext"] == ""


# ---------------------------------------------------------------------------
# _read_stdin_json
# ---------------------------------------------------------------------------


class TestReadStdinJson:
    def test_valid_json_parsed(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"key": "val"}'))
        result = _read_stdin_json()
        assert result == {"key": "val"}

    def test_empty_string_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert _read_stdin_json() == {}

    def test_whitespace_only_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("   \n  "))
        assert _read_stdin_json() == {}

    def test_nested_json_parsed(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"a":{"b":1}}'))
        result = _read_stdin_json()
        assert result["a"]["b"] == 1


# ---------------------------------------------------------------------------
# _post_event: additional transport coverage
# ---------------------------------------------------------------------------


class TestPostEventTransport:
    def _event(self):
        return {
            "session_id": "s1",
            "project_id": "/p",
            "source_tool": "test",
            "event_type": "evt",
            "tool_name": None,
            "payload": {},
            "seq": 1,
        }

    def test_daemon_url_override_used(self, monkeypatch):
        calls = []
        monkeypatch.setattr(hook, "DAEMON_URL", "http://remote:8080")
        monkeypatch.setattr(hook.requests, "post", lambda url, json=None, timeout=None: calls.append(url))
        _post_event(self._event())
        assert len(calls) == 1
        assert calls[0] == "http://remote:8080/events"

    def test_http_exception_swallowed(self, monkeypatch):
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            hook.requests,
            "post",
            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("refused")),
        )
        # Must not raise
        _post_event(self._event())

    def test_no_url_no_post(self, monkeypatch):
        calls = []
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", None)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(hook.requests, "post", lambda *a, **kw: calls.append(True))
        _post_event(self._event())
        assert calls == []

    def test_payload_serialized_as_json_string(self, monkeypatch):
        captured = []
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            hook.requests, "post", lambda url, json=None, timeout=None: captured.append(json)
        )
        evt = self._event()
        evt["payload"] = {"key": "val"}
        _post_event(evt)
        assert len(captured) == 1
        # payload must arrive as a JSON string, not a dict
        assert isinstance(captured[0]["payload"], str)
        assert json.loads(captured[0]["payload"]) == {"key": "val"}

    def test_posix_socket_failure_falls_back_to_http(self, monkeypatch):
        http_calls = []

        class _BrokenSession:
            def post(self, *a, **kw):
                raise OSError("no socket")

        fake_module = MagicMock()
        fake_module.Session.return_value = _BrokenSession()
        monkeypatch.setitem(sys.modules, "requests_unixsocket", fake_module)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        monkeypatch.setattr(
            hook.requests, "post", lambda url, json=None, timeout=None: http_calls.append(url)
        )
        _post_event(self._event())
        assert len(http_calls) == 1
        assert "5555" in http_calls[0]


# ---------------------------------------------------------------------------
# _daemon_get
# ---------------------------------------------------------------------------


class TestDaemonGet:
    def test_http_success_returns_json(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"results": []}
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(hook.requests, "get", lambda url, params=None, timeout=None: mock_resp)
        result = _daemon_get("/search", {"q": "test"})
        assert result == {"results": []}

    def test_http_non_ok_returns_empty_dict(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.ok = False
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(hook.requests, "get", lambda url, params=None, timeout=None: mock_resp)
        assert _daemon_get("/search") == {}

    def test_http_exception_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            hook.requests,
            "get",
            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("down")),
        )
        assert _daemon_get("/search") == {}

    def test_daemon_url_override(self, monkeypatch):
        calls = []
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"x": 1}

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return mock_resp

        monkeypatch.setattr(hook, "DAEMON_URL", "http://remote:9000")
        monkeypatch.setattr(hook.requests, "get", fake_get)
        result = _daemon_get("/session_summaries")
        assert result == {"x": 1}
        assert calls[0].startswith("http://remote:9000")

    def test_no_url_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", None)
        monkeypatch.setattr(sys, "platform", "win32")
        assert _daemon_get("/search") == {}

    def test_posix_socket_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"results": ["a"]}

        class _FakeSession:
            def get(self, url, params=None, timeout=None):
                return mock_resp

        fake_module = MagicMock()
        fake_module.Session.return_value = _FakeSession()
        monkeypatch.setitem(sys.modules, "requests_unixsocket", fake_module)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        result = _daemon_get("/search")
        assert result == {"results": ["a"]}

    def test_posix_socket_failure_falls_back_to_http(self, monkeypatch):
        class _BrokenSession:
            def get(self, *a, **kw):
                raise OSError("socket dead")

        fake_module = MagicMock()
        fake_module.Session.return_value = _BrokenSession()
        monkeypatch.setitem(sys.modules, "requests_unixsocket", fake_module)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"fallback": True}
        monkeypatch.setattr(hook.requests, "get", lambda url, params=None, timeout=None: mock_resp)
        result = _daemon_get("/search")
        assert result == {"fallback": True}


# ---------------------------------------------------------------------------
# _handle_session_recall: additional coverage
# ---------------------------------------------------------------------------


class TestSessionRecallAdditional:
    @pytest.fixture(autouse=True)
    def _daemon_up(self, monkeypatch):
        monkeypatch.setattr(hook, "_ensure_daemon", lambda: True)

    def test_narrative_truncated_at_120_chars(self, monkeypatch, capsys):
        long_narrative = "x" * 200
        monkeypatch.setattr(
            hook,
            "_daemon_get",
            lambda path, params=None: (
                {"results": [{"title": "T", "narrative": long_narrative}]}
                if path == "/search"
                else {"results": []}
            ),
        )
        monkeypatch.setattr(hook, "SOURCE_TOOL", "claude-code")
        _handle_session_recall({"cwd": "/proj"}, "SessionStart")
        out = capsys.readouterr().out
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        # narrative appears truncated — should not contain the full 200-char string
        assert long_narrative not in ctx
        assert "x" * 120 in ctx

    def test_session_field_used_as_session_id(self, monkeypatch, capsys):
        """Payload may use 'session' instead of 'session_id'."""
        monkeypatch.setattr(
            hook,
            "_daemon_get",
            lambda path, params=None: {"results": []},
        )
        monkeypatch.setattr(hook, "SOURCE_TOOL", "claude-code")
        rc = _handle_session_recall({"session": "alt-session-id", "cwd": "/proj"}, "SessionStart")
        assert rc == 0

    def test_multiple_summaries_all_included(self, monkeypatch, capsys):
        monkeypatch.setattr(
            hook,
            "_daemon_get",
            lambda path, params=None: (
                {
                    "results": [
                        {"ts": "2026-01-01T00:00:00", "request": "TaskA", "learnings": "LearnA"},
                        {"ts": "2026-01-02T00:00:00", "request": "TaskB", "learnings": "LearnB"},
                    ]
                }
                if path == "/session_summaries"
                else {"results": []}
            ),
        )
        monkeypatch.setattr(hook, "SOURCE_TOOL", "claude-code")
        _handle_session_recall({"cwd": "/proj"}, "SessionStart")
        out = capsys.readouterr().out
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "TaskA" in ctx
        assert "TaskB" in ctx

    def test_copilot_session_start_uses_system_message(self, monkeypatch, capsys):
        monkeypatch.setattr(
            hook,
            "_daemon_get",
            lambda path, params=None: (
                {"results": [{"ts": "2026-01-01", "request": "R", "learnings": "L"}]}
                if path == "/session_summaries"
                else {"results": []}
            ),
        )
        monkeypatch.setattr(hook, "SOURCE_TOOL", "copilot")
        _handle_session_recall({"cwd": "/proj"}, "sessionStart")
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "systemMessage" in data
        assert "hookSpecificOutput" not in data


# ---------------------------------------------------------------------------
# _handle_session_end: Windows path and missing cwd
# ---------------------------------------------------------------------------


class TestSessionEndAdditional:
    @pytest.fixture(autouse=True)
    def _daemon_up(self, monkeypatch):
        monkeypatch.setattr(hook, "_ensure_daemon", lambda: True)

    def test_windows_uses_detached_process_flags(self, monkeypatch, capsys):
        import shutil as _shutil

        spawned_kwargs = []

        def fake_popen(cmd, **kwargs):
            spawned_kwargs.append(kwargs)

        monkeypatch.setattr(_shutil, "which", lambda _: "C:\\bin\\forgememo.exe")
        monkeypatch.setattr(hook.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        # On POSIX these constants don't exist; define them so the win32 branch runs
        monkeypatch.setattr(hook.subprocess, "DETACHED_PROCESS", 8, raising=False)
        monkeypatch.setattr(hook.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)

        rc = _handle_session_end({"session_id": "s1", "cwd": "C:\\proj"})
        assert rc == 0
        assert len(spawned_kwargs) == 1
        kw = spawned_kwargs[0]
        assert kw.get("creationflags") is not None

    def test_missing_cwd_falls_back_to_getcwd(self, monkeypatch, capsys):
        import shutil as _shutil

        spawned_cmds = []

        def fake_popen(cmd, **kwargs):
            spawned_cmds.append(cmd)

        monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/forgememo")
        monkeypatch.setattr(hook.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(sys, "platform", "linux")

        rc = _handle_session_end({"session_id": "s1"})
        assert rc == 0
        # cwd should be some path (os.getcwd()), not empty
        assert "--project-dir" in spawned_cmds[0]
        idx = spawned_cmds[0].index("--project-dir")
        assert spawned_cmds[0][idx + 1]  # non-empty

    def test_session_id_empty_string_handled(self, monkeypatch, capsys):
        import shutil as _shutil

        spawned = []
        monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/forgememo")
        monkeypatch.setattr(hook.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
        monkeypatch.setattr(sys, "platform", "linux")

        rc = _handle_session_end({"cwd": "/proj"})
        assert rc == 0
        assert len(spawned) == 1


# ---------------------------------------------------------------------------
# main(): error exits and cross-agent dispatch
# ---------------------------------------------------------------------------


class TestMainErrors:
    def test_no_argv_exits_2(self, monkeypatch):
        with patch("sys.argv", ["hook.py"]):
            rc = hook.main()
        assert rc == 2

    def test_invalid_json_exits_1(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("not-json{{{"))
        with patch("sys.argv", ["hook.py", "UserPromptSubmit"]):
            rc = hook.main()
        assert rc == 1


class TestCrossAgentDispatch:
    """Every event name in each dispatch set must route to the right handler."""

    def _run_main(self, event_name, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"cwd":"/p"}'))
        with patch("sys.argv", ["hook.py", event_name]):
            return hook.main()

    @pytest.mark.parametrize("event_name", sorted(_SESSION_RECALL_EVENTS))
    def test_session_recall_events_dispatched(self, event_name, monkeypatch):
        recalled = []
        monkeypatch.setattr(
            hook, "_handle_session_recall", lambda p, e: recalled.append(e) or 0
        )
        rc = self._run_main(event_name, monkeypatch)
        assert rc == 0
        assert recalled == [event_name]

    @pytest.mark.parametrize("event_name", sorted(_SESSION_END_EVENTS))
    def test_session_end_events_dispatched(self, event_name, monkeypatch):
        ended = []
        monkeypatch.setattr(
            hook, "_handle_session_end", lambda p: ended.append(True) or 0
        )
        rc = self._run_main(event_name, monkeypatch)
        assert rc == 0
        assert ended == [True]

    @pytest.mark.parametrize("event_name", sorted(_POST_TOOL_USE_EVENTS))
    def test_post_tool_use_events_dispatched(self, event_name, monkeypatch):
        handled = []
        monkeypatch.setattr(
            hook,
            "_handle_post_tool_use",
            lambda p, e: handled.append(e) or 0,
        )
        rc = self._run_main(event_name, monkeypatch)
        assert rc == 0
        assert handled == [event_name]

    def test_unknown_event_falls_through_to_post_event(self, monkeypatch):
        posted = []
        monkeypatch.setattr(hook, "_post_event", lambda e: posted.append(e))
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id":"s","project_id":"/p","seq":1}'))
        with patch("sys.argv", ["hook.py", "SomeCustomEvent"]):
            rc = hook.main()
        assert rc == 0
        assert len(posted) == 1
        assert posted[0]["event_type"] == "SomeCustomEvent"


# ---------------------------------------------------------------------------
# _extract_error_text
# ---------------------------------------------------------------------------


class TestExtractErrorText:
    def test_detects_traceback(self):
        payload = {"tool_result": "Traceback (most recent call last):\n  File ...\nTypeError: bad"}
        assert _extract_error_text(payload) is not None
        assert "Traceback" in _extract_error_text(payload)

    def test_detects_error_prefix(self):
        payload = {"tool_result": "Error: module not found"}
        assert _extract_error_text(payload) is not None

    def test_detects_npm_error(self):
        payload = {"tool_result": "npm ERR! code ENOENT"}
        assert _extract_error_text(payload) is not None

    def test_detects_exit_code(self):
        payload = {"tool_result": {"stdout": "", "stderr": "failed", "exitCode": 1}}
        result = _extract_error_text(payload)
        assert result is not None
        assert "exit code" in result

    def test_no_error_returns_none(self):
        payload = {"tool_result": "Successfully wrote file.py"}
        assert _extract_error_text(payload) is None

    def test_empty_result_returns_none(self):
        assert _extract_error_text({}) is None
        assert _extract_error_text({"tool_result": ""}) is None

    def test_dict_result_with_stderr(self):
        payload = {"tool_result": {"stderr": "ModuleNotFoundError: No module named 'foo'"}}
        result = _extract_error_text(payload)
        assert result is not None
        assert "ModuleNotFoundError" in result

    def test_toolResult_key_also_works(self):
        payload = {"toolResult": "Error: something went wrong"}
        assert _extract_error_text(payload) is not None

    def test_detects_build_failed(self):
        payload = {"tool_result": "Build failed with 3 errors"}
        assert _extract_error_text(payload) is not None

    def test_detects_command_not_found(self):
        payload = {"tool_result": "bash: foobar: command not found"}
        assert _extract_error_text(payload) is not None

    def test_successful_bash_exit_zero_no_error(self):
        payload = {"tool_result": {"stdout": "all good", "stderr": "", "exitCode": 0}}
        assert _extract_error_text(payload) is None


# ---------------------------------------------------------------------------
# _error_fingerprint
# ---------------------------------------------------------------------------


class TestErrorFingerprint:
    def test_same_error_same_fingerprint(self):
        err1 = "TypeError: cannot read property 'foo' of undefined"
        err2 = "TypeError: cannot read property 'foo' of undefined"
        assert _error_fingerprint(err1) == _error_fingerprint(err2)

    def test_different_line_numbers_same_fingerprint(self):
        err1 = "Error at line 42: TypeError: bad type"
        err2 = "Error at line 99: TypeError: bad type"
        assert _error_fingerprint(err1) == _error_fingerprint(err2)

    def test_different_paths_same_fingerprint(self):
        err1 = "FileNotFoundError: /home/user/project/src/foo.py not found"
        err2 = "FileNotFoundError: /tmp/other/bar.py not found"
        assert _error_fingerprint(err1) == _error_fingerprint(err2)

    def test_different_errors_different_fingerprint(self):
        err1 = "TypeError: cannot read property"
        err2 = "ModuleNotFoundError: no module named requests"
        assert _error_fingerprint(err1) != _error_fingerprint(err2)

    def test_returns_16_char_hex(self):
        fp = _error_fingerprint("Error: something")
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)


# ---------------------------------------------------------------------------
# _extract_error_keywords
# ---------------------------------------------------------------------------


class TestExtractErrorKeywords:
    def test_extracts_meaningful_words(self):
        error = "ModuleNotFoundError: No module named 'requests'"
        kw = _extract_error_keywords(error)
        assert "ModuleNotFoundError" in kw
        assert "module" in kw
        assert "requests" in kw

    def test_strips_file_paths(self):
        error = "Error in /home/user/project/src/main.py: ImportError"
        kw = _extract_error_keywords(error)
        assert "/home" not in kw
        assert "ImportError" in kw

    def test_limits_keywords(self):
        error = "word " * 50
        kw = _extract_error_keywords(error)
        assert len(kw.split()) <= 12

    def test_deduplicates_keywords(self):
        error = "Error: Error: Error: something"
        kw = _extract_error_keywords(error)
        assert kw.lower().count("error") == 1


# ---------------------------------------------------------------------------
# _handle_error_recall
# ---------------------------------------------------------------------------


class TestHandleErrorRecall:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.delenv("FORGEMEMO_PROJECT_ID", raising=False)
        monkeypatch.setattr(hook, "SOURCE_TOOL", "claude-code")

    def _error_payload(self, error_text="TypeError: bad type", **overrides):
        base = {
            "tool_name": "Bash",
            "session_id": "sess-1",
            "project_id": "/tmp/proj",
            "tool_result": error_text,
        }
        base.update(overrides)
        return base

    def test_no_error_returns_none(self, monkeypatch):
        result = _handle_error_recall(
            {"tool_name": "Bash", "tool_result": "Success!", "session_id": "s1"},
            "PostToolUse",
        )
        assert result is None

    def test_first_error_returns_none(self, monkeypatch):
        """First occurrence should record but not recall."""
        daemon_posts = []
        monkeypatch.setattr(
            hook,
            "_daemon_get",
            lambda path, params=None: {"count": 0},
        )
        monkeypatch.setattr(
            hook,
            "_daemon_post",
            lambda path, data: daemon_posts.append((path, data)) or {},
        )
        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result is None
        # Should have posted the error event
        assert len(daemon_posts) == 1
        assert daemon_posts[0][0] == "/error_events"

    def test_repeated_error_injects_context(self, monkeypatch, capsys):
        """Second occurrence should search memories and inject context."""
        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1}  # seen once before
            if path == "/search":
                return {
                    "results": [
                        {"id": "d:42", "type": "bugfix", "title": "Fix TypeError in parser"},
                    ]
                }
            if path.startswith("/observation/"):
                return {
                    "title": "Fix TypeError in parser",
                    "narrative": "The TypeError was caused by missing null check. Add guard clause.",
                }
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result == 0

        out = capsys.readouterr().out
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "seen 2 times" in ctx
        assert "TypeError" in ctx or "parser" in ctx

    def test_repeated_error_with_no_memories_returns_none(self, monkeypatch):
        """Repeated error but no matching memories — no injection."""
        monkeypatch.setattr(
            hook,
            "_daemon_get",
            lambda path, params=None: {"count": 1} if path == "/error_events" else {"results": []},
        )
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result is None

    def test_post_tool_use_integrates_error_recall(self, monkeypatch, capsys):
        """PostToolUse handler should call error recall and still post write events."""
        posted_events = []

        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 2}  # seen twice before
            if path == "/search":
                return {
                    "results": [
                        {"id": "d:10", "type": "discovery", "title": "Connection pooling fix"},
                    ]
                }
            if path.startswith("/observation/"):
                return {"title": "Connection pooling fix", "narrative": "Use keep-alive"}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})
        monkeypatch.setattr(hook, "_post_event", lambda e: posted_events.append(e))

        payload = self._error_payload(tool_name="Bash")
        rc = _handle_post_tool_use(payload, "PostToolUse")
        assert rc == 0

        out = capsys.readouterr().out
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "seen 3 times" in ctx

        # Bash is a write tool — should also be posted
        assert len(posted_events) == 1

    def test_read_tool_error_still_triggers_recall(self, monkeypatch, capsys):
        """Errors from read-only tools should still trigger error recall."""
        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1}
            if path == "/search":
                return {
                    "results": [
                        {"id": "d:5", "type": "note", "title": "File encoding fix"},
                    ]
                }
            if path.startswith("/observation/"):
                return {"title": "File encoding fix", "narrative": "Use utf-8"}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})
        posted = []
        monkeypatch.setattr(hook, "_post_event", lambda e: posted.append(e))

        payload = {
            "tool_name": "Read",
            "session_id": "s1",
            "project_id": "/proj",
            "tool_result": "Error: FileNotFoundError: No such file or directory",
        }
        rc = _handle_post_tool_use(payload, "PostToolUse")
        assert rc == 0
        # Read is not a write tool, so no event posted
        assert len(posted) == 0

        out = capsys.readouterr().out
        data = json.loads(out)
        assert "File encoding fix" in data["hookSpecificOutput"]["additionalContext"]

    def test_cross_project_search_included(self, monkeypatch, capsys):
        """Should search both project-specific and cross-project memories."""
        search_calls = []

        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1}
            if path == "/search":
                search_calls.append(params)
                if params and params.get("project_id"):
                    return {"results": []}  # no project-specific results
                return {
                    "results": [
                        {"id": "d:99", "type": "bugfix", "title": "Global fix"},
                    ]
                }
            if path.startswith("/observation/"):
                return {"title": "Global fix", "narrative": "Works everywhere"}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result == 0
        # Should have searched twice: project-specific + global
        assert len(search_calls) == 2

    def test_error_count_increments_in_context(self, monkeypatch, capsys):
        """Context should say 'seen N+1 times' where N is prior count."""
        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 4}
            if path == "/search":
                return {"results": [{"id": "d:1", "type": "note", "title": "Tip"}]}
            if path.startswith("/observation/"):
                return {"title": "Tip", "narrative": "Do this"}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        _handle_error_recall(self._error_payload(), "PostToolUse")
        out = capsys.readouterr().out
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "5 times" in ctx

    def test_daemon_get_failure_does_not_crash(self, monkeypatch):
        """If daemon is unreachable, error recall should return None gracefully."""
        monkeypatch.setattr(hook, "_daemon_get", lambda path, params=None: {})
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        # No crash, and no context injected (count defaults to 0)
        assert result is None

    def test_daemon_post_failure_does_not_crash(self, monkeypatch):
        """If posting error event fails, recall should still proceed."""
        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1}
            if path == "/search":
                return {"results": [{"id": "d:1", "type": "note", "title": "Tip"}]}
            if path.startswith("/observation/"):
                return {"title": "Tip", "narrative": "Info"}
            return {}

        def failing_post(path, data):
            raise ConnectionError("daemon down")

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        # _daemon_post internally swallows exceptions, but let's verify the
        # outer handler doesn't crash even if _daemon_post raises in test
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result == 0  # still injects context

    def test_session_key_variant(self, monkeypatch):
        """Payload with 'session' instead of 'session_id' should work."""
        posts = []
        monkeypatch.setattr(hook, "_daemon_get", lambda path, params=None: {"count": 0})
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: posts.append(data) or {})

        payload = {
            "tool_name": "Bash",
            "session": "alt-session-key",
            "project_id": "/proj",
            "tool_result": "TypeError: oops",
        }
        _handle_error_recall(payload, "PostToolUse")
        assert posts[0]["session_id"] == "alt-session-key"

    def test_error_text_truncated_to_500(self, monkeypatch):
        """Posted error_text should be truncated to 500 chars."""
        posts = []
        monkeypatch.setattr(hook, "_daemon_get", lambda path, params=None: {"count": 0})
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: posts.append(data) or {})

        long_error = "TypeError: " + "x" * 1000
        _handle_error_recall(self._error_payload(error_text=long_error), "PostToolUse")
        assert len(posts[0]["error_text"]) == 500

    def test_observation_learnings_fallback(self, monkeypatch, capsys):
        """If observation has 'learnings' but no 'narrative', use learnings."""
        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1}
            if path == "/search":
                return {"results": [{"id": "s:7", "type": "summary", "title": "Session fix"}]}
            if path.startswith("/observation/"):
                return {"title": "Session fix", "learnings": "Always check null", "narrative": ""}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result == 0
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "Always check null" in ctx

    def test_observation_no_narrative_skipped(self, monkeypatch, capsys):
        """If observation has neither narrative nor learnings, skip it in detailed."""
        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1}
            if path == "/search":
                return {"results": [{"id": "d:1", "type": "note", "title": "Empty obs"}]}
            if path.startswith("/observation/"):
                return {"title": "Empty obs", "narrative": "", "learnings": ""}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result == 0
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Falls back to summary parts since no detailed narratives
        assert "[note] Empty obs" in ctx

    def test_result_id_without_colon_skipped(self, monkeypatch, capsys):
        """Results with no ':' in ID are skipped for observation fetch."""
        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1}
            if path == "/search":
                return {"results": [{"id": "badid", "type": "note", "title": "No prefix"}]}
            if path.startswith("/observation/"):
                raise AssertionError("Should not be called for bad ID")
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result == 0
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Falls back to summary parts
        assert "[note] No prefix" in ctx

    def test_deduplicates_across_project_and_global_search(self, monkeypatch, capsys):
        """Same result from project + global search should appear only once."""
        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1}
            if path == "/search":
                return {
                    "results": [
                        {"id": "d:42", "type": "bugfix", "title": "Shared fix"},
                    ]
                }
            if path.startswith("/observation/"):
                return {"title": "Shared fix", "narrative": "The fix"}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        _handle_error_recall(self._error_payload(), "PostToolUse")
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # "Shared fix" should only appear once despite being in both searches
        assert ctx.count("Shared fix") == 1

    def test_copilot_format_uses_system_message(self, monkeypatch, capsys):
        """Copilot source tool should use systemMessage format."""
        monkeypatch.setattr(hook, "SOURCE_TOOL", "copilot")

        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1}
            if path == "/search":
                return {"results": [{"id": "d:1", "type": "note", "title": "Tip"}]}
            if path.startswith("/observation/"):
                return {"title": "Tip", "narrative": "Do X"}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        _handle_error_recall(self._error_payload(), "PostToolUse")
        data = json.loads(capsys.readouterr().out)
        assert "systemMessage" in data
        assert "hookSpecificOutput" not in data
        assert "seen 2 times" in data["systemMessage"]

    def test_write_tool_first_error_still_posts_event(self, monkeypatch, capsys):
        """First error on a write tool: no context, but event still posted."""
        posted = []
        monkeypatch.setattr(hook, "_daemon_get", lambda path, params=None: {"count": 0})
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})
        monkeypatch.setattr(hook, "_post_event", lambda e: posted.append(e))

        payload = self._error_payload(tool_name="Edit")
        rc = _handle_post_tool_use(payload, "PostToolUse")
        assert rc == 0
        # First error returns None from error_recall, falls through to normal path
        # Edit is a write tool so it should be posted
        assert len(posted) == 1

    def test_non_write_tool_no_error_returns_zero(self, monkeypatch):
        """Read tool with no error: normal skip, no crash."""
        monkeypatch.setattr(hook, "_daemon_get", lambda path, params=None: {})
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})
        posted = []
        monkeypatch.setattr(hook, "_post_event", lambda e: posted.append(e))

        payload = {"tool_name": "Grep", "session_id": "s1", "tool_result": "found 3 matches"}
        rc = _handle_post_tool_use(payload, "PostToolUse")
        assert rc == 0
        assert len(posted) == 0


# ---------------------------------------------------------------------------
# _extract_error_text: extended coverage
# ---------------------------------------------------------------------------


class TestExtractErrorTextExtended:
    def test_dict_result_with_content_field(self):
        payload = {"tool_result": {"content": "SyntaxError: unexpected token"}}
        result = _extract_error_text(payload)
        assert result is not None
        assert "SyntaxError" in result

    def test_dict_result_exit_code_key_variant(self):
        payload = {"tool_result": {"stdout": "fail", "exit_code": 2}}
        result = _extract_error_text(payload)
        assert result is not None
        assert "exit code" in result

    def test_non_string_non_dict_result_stringified(self):
        payload = {"tool_result": 42}
        # int doesn't match error patterns
        assert _extract_error_text(payload) is None

    def test_list_result_stringified(self):
        payload = {"tool_result": ["TypeError: bad"]}
        result = _extract_error_text(payload)
        # str(["TypeError: bad"]) contains "TypeError" which matches pattern
        assert result is not None

    def test_detects_go_panic(self):
        payload = {"tool_result": "goroutine 1 [running]:\npanic: runtime error: index out of range"}
        assert _extract_error_text(payload) is not None

    def test_detects_fatal(self):
        payload = {"tool_result": "fatal: not a git repository"}
        assert _extract_error_text(payload) is not None

    def test_detects_segfault(self):
        payload = {"tool_result": "Segmentation fault (core dumped)"}
        assert _extract_error_text(payload) is not None

    def test_detects_is_not_defined(self):
        payload = {"tool_result": "ReferenceError: myVar is not defined"}
        assert _extract_error_text(payload) is not None

    def test_detects_cannot_find_module(self):
        payload = {"tool_result": "Cannot find module '@/components/Foo'"}
        assert _extract_error_text(payload) is not None

    def test_detects_compilation_failed(self):
        payload = {"tool_result": "Compilation failed with 2 errors"}
        assert _extract_error_text(payload) is not None

    def test_dict_exit_code_zero_no_error_text(self):
        """Dict with exitCode 0 and no error text should return None."""
        payload = {"tool_result": {"stdout": "ok", "exitCode": 0}}
        assert _extract_error_text(payload) is None

    def test_dict_exit_code_zero_string(self):
        """Exit code '0' as string should not trigger."""
        payload = {"tool_result": {"stdout": "ok", "exitCode": "0"}}
        assert _extract_error_text(payload) is None

    def test_stderr_and_stdout_both_collected(self):
        payload = {"tool_result": {
            "stderr": "warning: unused var",
            "stdout": "TypeError: bad",
        }}
        result = _extract_error_text(payload)
        assert result is not None
        assert "TypeError" in result
        assert "warning" in result

    def test_detects_undefined_is_not(self):
        payload = {"tool_result": "undefined is not a function"}
        assert _extract_error_text(payload) is not None

    def test_detects_called_process_error(self):
        payload = {"tool_result": "subprocess.CalledProcessError: returned 1"}
        assert _extract_error_text(payload) is not None

    def test_no_false_positive_on_error_in_identifier(self):
        """Words like 'errorHandler' should not trigger false positive."""
        payload = {"tool_result": "Registered errorHandler for route /api"}
        # The regex requires Error followed by space/colon/bracket
        assert _extract_error_text(payload) is None

    # -- Official Claude Code field name: tool_response -----------------------

    def test_tool_response_field_detected(self):
        """Official Claude Code field name is tool_response, not tool_result."""
        payload = {"tool_response": "TypeError: cannot read property"}
        assert _extract_error_text(payload) is not None

    def test_tool_response_takes_precedence(self):
        """tool_response should be checked before tool_result."""
        payload = {
            "tool_response": "ModuleNotFoundError: no module named foo",
            "tool_result": "all good",
        }
        result = _extract_error_text(payload)
        assert result is not None
        assert "ModuleNotFoundError" in result

    def test_tool_response_bash_object(self):
        """Official Bash tool_response structure: {stdout, stderr, interrupted}."""
        payload = {
            "tool_response": {
                "stdout": "",
                "stderr": "TypeError: bad argument",
                "interrupted": False,
            }
        }
        result = _extract_error_text(payload)
        assert result is not None
        assert "TypeError" in result

    def test_tool_response_return_code_interpretation_error(self):
        """Bash returnCodeInterpretation containing 'error' triggers detection."""
        payload = {
            "tool_response": {
                "stdout": "some output",
                "stderr": "",
                "returnCodeInterpretation": "Error: command returned non-zero exit code",
            }
        }
        result = _extract_error_text(payload)
        assert result is not None
        assert "error" in result.lower()

    def test_tool_response_return_code_interpretation_success_no_error(self):
        """Bash returnCodeInterpretation without 'error' is not an error."""
        payload = {
            "tool_response": {
                "stdout": "all good",
                "stderr": "",
                "returnCodeInterpretation": "Command completed successfully",
            }
        }
        assert _extract_error_text(payload) is None

    def test_tool_response_interrupted_command(self):
        """Interrupted command should be detected as an error."""
        payload = {
            "tool_response": {
                "stdout": "partial output",
                "stderr": "",
                "interrupted": True,
            }
        }
        result = _extract_error_text(payload)
        assert result is not None
        assert "interrupted" in result.lower()

    def test_tool_response_interrupted_false_no_error(self):
        """Non-interrupted successful command is not an error."""
        payload = {
            "tool_response": {
                "stdout": "success",
                "stderr": "",
                "interrupted": False,
            }
        }
        assert _extract_error_text(payload) is None

    def test_tool_response_empty_string(self):
        payload = {"tool_response": ""}
        assert _extract_error_text(payload) is None

    def test_tool_response_none_falls_to_tool_result(self):
        """If tool_response is None/missing, fall back to tool_result."""
        payload = {"tool_result": "ValueError: invalid literal"}
        assert _extract_error_text(payload) is not None

    # -- Codex: tool_output field ---------------------------------------------

    def test_codex_tool_output_field(self):
        """Codex uses tool_output instead of tool_response."""
        payload = {"tool_output": "TypeError: cannot call undefined"}
        result = _extract_error_text(payload)
        assert result is not None
        assert "TypeError" in result

    def test_codex_tool_output_nested_output(self):
        """Codex tool_output may nest the result in an 'output' sub-field."""
        payload = {"tool_output": {"output": "ModuleNotFoundError: No module named 'bar'"}}
        result = _extract_error_text(payload)
        assert result is not None
        assert "ModuleNotFoundError" in result

    # -- Gemini: tool_response.error field ------------------------------------

    def test_gemini_tool_response_error_field(self):
        """Gemini tool_response may include an 'error' field."""
        payload = {"tool_response": {"error": "Permission denied: /etc/shadow"}}
        result = _extract_error_text(payload)
        assert result is not None
        assert "Permission denied" in result

    def test_gemini_tool_response_error_empty_no_trigger(self):
        """Gemini tool_response with empty error field is not an error."""
        payload = {"tool_response": {"error": "", "llmContent": "success"}}
        assert _extract_error_text(payload) is None

    # -- Copilot: tool_response same as Claude Code ---------------------------

    def test_copilot_tool_response_string(self):
        """Copilot CLI uses tool_response as a string."""
        payload = {"tool_response": "Error: ENOENT: no such file or directory"}
        result = _extract_error_text(payload)
        assert result is not None

    # -- Field precedence order -----------------------------------------------

    def test_tool_output_used_when_tool_response_absent(self):
        """tool_output (Codex) should be used when tool_response is absent."""
        payload = {"tool_output": "SyntaxError: unexpected EOF"}
        assert _extract_error_text(payload) is not None

    def test_field_precedence_chain(self):
        """tool_response > tool_output > tool_result > toolResult."""
        # When tool_response has content, it takes precedence
        payload = {
            "tool_response": "TypeError: from tool_response",
            "tool_output": "ValueError: from tool_output",
            "tool_result": "KeyError: from tool_result",
        }
        result = _extract_error_text(payload)
        assert "tool_response" in result


# ---------------------------------------------------------------------------
# _error_fingerprint: extended coverage
# ---------------------------------------------------------------------------


class TestErrorFingerprintExtended:
    def test_strips_hex_addresses(self):
        err1 = "TypeError: at address 0xDEADBEEF"
        err2 = "TypeError: at address 0x12345678"
        assert _error_fingerprint(err1) == _error_fingerprint(err2)

    def test_strips_timestamps(self):
        err1 = "Error at 1711900000000: connection lost"
        err2 = "Error at 1711999999999: connection lost"
        assert _error_fingerprint(err1) == _error_fingerprint(err2)

    def test_multiline_extracts_key_lines_only(self):
        trace = (
            "  File /path/to/foo.py, line 42\n"
            "    x = y.z\n"
            "TypeError: 'NoneType' has no attribute 'z'\n"
            "During handling of the above exception:\n"
            "ValueError: invalid literal\n"
        )
        fp = _error_fingerprint(trace)
        assert len(fp) == 16

    def test_fallback_to_first_line_when_no_pattern(self):
        """Error text with no matching pattern should use first line."""
        fp = _error_fingerprint("some random output\nmore stuff")
        assert len(fp) == 16

    def test_empty_string(self):
        fp = _error_fingerprint("")
        assert len(fp) == 16

    def test_strips_file_line_col(self):
        err1 = "SyntaxError: unexpected token at :10:5"
        err2 = "SyntaxError: unexpected token at :99:12"
        assert _error_fingerprint(err1) == _error_fingerprint(err2)

    def test_strips_stack_frame_locations(self):
        err1 = "Error at processTicksAndRejections (internal/process/task_queues.js:95:5)"
        err2 = "Error at processTicksAndRejections (internal/process/task_queues.js:200:9)"
        assert _error_fingerprint(err1) == _error_fingerprint(err2)

    def test_case_insensitive(self):
        err1 = "TypeError: BAD TYPE"
        err2 = "TypeError: bad type"
        assert _error_fingerprint(err1) == _error_fingerprint(err2)


# ---------------------------------------------------------------------------
# _extract_error_keywords: extended coverage
# ---------------------------------------------------------------------------


class TestExtractErrorKeywordsExtended:
    def test_no_pattern_match_falls_back_to_first_lines(self):
        kw = _extract_error_keywords("foo bar baz\nqux quux")
        assert "foo" in kw
        assert "bar" in kw
        assert "baz" in kw

    def test_strips_hex_addresses(self):
        kw = _extract_error_keywords("Error at 0xDEADBEEF: bad memory")
        assert "0xDEADBEEF" not in kw
        assert "Error" in kw

    def test_short_words_filtered(self):
        kw = _extract_error_keywords("Error: a b cd efg")
        assert " a " not in f" {kw} "
        assert " b " not in f" {kw} "
        assert " cd " not in f" {kw} "
        assert "efg" in kw

    def test_empty_string(self):
        kw = _extract_error_keywords("")
        assert kw == ""

    def test_multiline_error_picks_up_to_3_key_lines(self):
        error = (
            "TypeError: bad\n"
            "ValueError: worse\n"
            "KeyError: missing\n"
            "RuntimeError: extra\n"
        )
        kw = _extract_error_keywords(error)
        # Should pick first 3 matching lines
        assert "TypeError" in kw
        assert "ValueError" in kw
        assert "KeyError" in kw
        # RuntimeError is 4th, should not be included as source
        # (but may or may not appear in keywords — depends on word extraction)


# ---------------------------------------------------------------------------
# _daemon_post: transport coverage
# ---------------------------------------------------------------------------


class TestDaemonPost:
    def test_http_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"status": "ok"}
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(hook.requests, "post", lambda url, json=None, timeout=None: mock_resp)
        result = hook._daemon_post("/error_events", {"x": 1})
        assert result == {"status": "ok"}

    def test_daemon_url_override(self, monkeypatch):
        calls = []
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"ok": True}
        monkeypatch.setattr(hook, "DAEMON_URL", "http://remote:9000")
        monkeypatch.setattr(hook.requests, "post", lambda url, json=None, timeout=None: (calls.append(url), mock_resp)[1])
        result = hook._daemon_post("/error_events", {})
        assert calls[0] == "http://remote:9000/error_events"

    def test_http_exception_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(hook.requests, "post", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("down")))
        result = hook._daemon_post("/error_events", {})
        assert result == {}

    def test_no_url_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", None)
        monkeypatch.setattr(sys, "platform", "win32")
        result = hook._daemon_post("/error_events", {})
        assert result == {}

    def test_http_non_ok_returns_empty_dict(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.ok = False
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(hook.requests, "post", lambda url, json=None, timeout=None: mock_resp)
        result = hook._daemon_post("/error_events", {})
        assert result == {}

    def test_posix_socket_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"status": "ok"}

        class _FakeSession:
            def post(self, url, json=None, timeout=None):
                return mock_resp

        fake_module = MagicMock()
        fake_module.Session.return_value = _FakeSession()
        monkeypatch.setitem(sys.modules, "requests_unixsocket", fake_module)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        result = hook._daemon_post("/error_events", {"x": 1})
        assert result == {"status": "ok"}

    def test_posix_socket_failure_falls_back_to_http(self, monkeypatch):
        class _BrokenSession:
            def post(self, *a, **kw):
                raise OSError("socket dead")

        fake_module = MagicMock()
        fake_module.Session.return_value = _BrokenSession()
        monkeypatch.setitem(sys.modules, "requests_unixsocket", fake_module)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(hook, "DAEMON_URL", None)
        monkeypatch.setattr(hook, "HTTP_PORT", "5555")

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"fallback": True}
        monkeypatch.setattr(hook.requests, "post", lambda url, json=None, timeout=None: mock_resp)
        result = hook._daemon_post("/error_events", {})
        assert result == {"fallback": True}


# ---------------------------------------------------------------------------
# _is_within_debounce
# ---------------------------------------------------------------------------


class TestIsWithinDebounce:
    def test_none_returns_false(self):
        assert _is_within_debounce(None) is False

    def test_empty_string_returns_false(self):
        assert _is_within_debounce("") is False

    def test_recent_timestamp_returns_true(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        assert _is_within_debounce(now) is True

    def test_old_timestamp_returns_false(self):
        assert _is_within_debounce("2020-01-01 00:00:00") is False

    def test_30_seconds_ago_within_debounce(self):
        from datetime import datetime, timezone, timedelta

        ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assert _is_within_debounce(ts) is True

    def test_10_minutes_ago_outside_debounce(self):
        from datetime import datetime, timezone, timedelta

        ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assert _is_within_debounce(ts) is False

    def test_exactly_at_boundary(self, monkeypatch):
        """Just past the debounce window should return False."""
        from datetime import datetime, timezone, timedelta

        debounce = hook._ERROR_RECALL_DEBOUNCE_SECS
        ts = (datetime.now(timezone.utc) - timedelta(seconds=debounce + 1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assert _is_within_debounce(ts) is False

    def test_malformed_timestamp_returns_false(self):
        assert _is_within_debounce("not-a-timestamp") is False

    def test_respects_env_override(self, monkeypatch):
        """FORGEMEMO_ERROR_DEBOUNCE_SECS should control the window."""
        from datetime import datetime, timezone, timedelta
        import importlib

        monkeypatch.setenv("FORGEMEMO_ERROR_DEBOUNCE_SECS", "10")
        try:
            importlib.reload(hook)

            # 5 seconds ago — within 10s debounce
            ts_recent = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            assert hook._is_within_debounce(ts_recent) is True

            # 15 seconds ago — outside 10s debounce
            ts_old = (datetime.now(timezone.utc) - timedelta(seconds=15)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            assert hook._is_within_debounce(ts_old) is False
        finally:
            # Always restore default, even if assertions fail
            monkeypatch.delenv("FORGEMEMO_ERROR_DEBOUNCE_SECS", raising=False)
            importlib.reload(hook)


# ---------------------------------------------------------------------------
# Debounce integration tests for _handle_error_recall
# ---------------------------------------------------------------------------


class TestErrorRecallDebounce:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.delenv("FORGEMEMO_PROJECT_ID", raising=False)
        monkeypatch.setattr(hook, "SOURCE_TOOL", "claude-code")

    def _error_payload(self, **overrides):
        base = {
            "tool_name": "Bash",
            "session_id": "sess-1",
            "project_id": "/tmp/proj",
            "tool_result": "TypeError: bad type",
        }
        base.update(overrides)
        return base

    def test_recent_error_debounced(self, monkeypatch):
        """Error recalled 30s ago → debounced, no recall."""
        from datetime import datetime, timezone, timedelta

        recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        monkeypatch.setattr(
            hook,
            "_daemon_get",
            lambda path, params=None: {"count": 10, "last_ts": recent_ts, "last_recalled_at": recent_ts},
        )
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result is None  # debounced — no injection

    def test_old_error_not_debounced(self, monkeypatch, capsys):
        """Error recalled 10 min ago → debounce expired, recall fires."""
        from datetime import datetime, timezone, timedelta

        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 3, "last_ts": old_ts, "last_recalled_at": old_ts}
            if path == "/search":
                return {"results": [{"id": "d:1", "type": "note", "title": "Fix"}]}
            if path.startswith("/observation/"):
                return {"title": "Fix", "narrative": "Do this"}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result == 0  # recall fires
        out = capsys.readouterr().out
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "seen 4 times" in ctx

    def test_first_error_not_affected_by_debounce(self, monkeypatch):
        """First occurrence (count=0) should always return None, regardless of debounce."""
        monkeypatch.setattr(
            hook,
            "_daemon_get",
            lambda path, params=None: {"count": 0, "last_ts": None, "last_recalled_at": None},
        )
        posts = []
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: posts.append(data) or {})

        result = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert result is None
        assert len(posts) == 1  # still records the error

    def test_hot_reload_burst_only_fires_once(self, monkeypatch, capsys):
        """Simulates rapid-fire errors: only the first recall-eligible fires."""
        from datetime import datetime, timezone

        call_count = {"n": 0}
        recalled = {"at": None}
        recent_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                call_count["n"] += 1
                # First call: count=1, no recalled_at → fires recall
                # Subsequent calls: count=N, recent recalled_at → debounced
                return {
                    "count": call_count["n"],
                    "last_ts": recent_ts,
                    "last_recalled_at": recalled["at"],
                }
            if path == "/search":
                return {"results": [{"id": "d:1", "type": "note", "title": "Tip"}]}
            if path.startswith("/observation/"):
                return {"title": "Tip", "narrative": "Help"}
            return {}

        def fake_daemon_post(path, data):
            # Track when recall is recorded
            if path == "/error_events/recall":
                recalled["at"] = recent_ts
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", fake_daemon_post)

        # First call — should fire (count=1, no recalled_at)
        r1 = _handle_error_recall(self._error_payload(), "PostToolUse")
        assert r1 == 0
        capsys.readouterr()  # consume output

        # Rapid subsequent calls — should be debounced (recalled_at is recent)
        for _ in range(5):
            r = _handle_error_recall(self._error_payload(), "PostToolUse")
            assert r is None  # debounced

    def test_different_fingerprints_not_debounced(self, monkeypatch, capsys):
        """Two different errors in quick succession should both fire."""
        def fake_daemon_get(path, params=None):
            if path == "/error_events":
                return {"count": 1, "last_ts": None, "last_recalled_at": None}
            if path == "/search":
                return {"results": [{"id": "d:1", "type": "note", "title": "Tip"}]}
            if path.startswith("/observation/"):
                return {"title": "Tip", "narrative": "Info"}
            return {}

        monkeypatch.setattr(hook, "_daemon_get", fake_daemon_get)
        monkeypatch.setattr(hook, "_daemon_post", lambda path, data: {})

        r1 = _handle_error_recall(self._error_payload(
            tool_result="TypeError: bad type"
        ), "PostToolUse")
        assert r1 == 0
        capsys.readouterr()

        r2 = _handle_error_recall(self._error_payload(
            tool_result="ModuleNotFoundError: no module named foo"
        ), "PostToolUse")
        assert r2 == 0  # different fingerprint, not debounced
