from __future__ import annotations

import os
import tempfile
import sys
from pathlib import Path

import pytest


_tmp_log = os.path.join(tempfile.gettempdir(), "forgememo_daemon.log")
os.environ.setdefault("FORGEMEMO_DAEMON_LOG", _tmp_log)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def disable_mock_transport(monkeypatch):
    """Disable mock transport for transport-specific tests."""
    monkeypatch.setenv("FORGEMEMO_MOCK_TRANSPORT", "0")
    monkeypatch.setenv("FORGEMEMO_DISABLE_BREAKER", "0")
    import forgememo.mcp_server as mcp_server
    import forgememo.daemon as daemon_module

    mcp_server.MOCK_TRANSPORT = False
    # Reset the global state for circuit breaker tests
    daemon_module._error_events_consecutive_failures = 0
    daemon_module._error_events_disabled = False
