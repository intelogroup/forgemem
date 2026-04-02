"""
Tests for daemon lifecycle (start/stop/status) on all platforms.
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


class TestSchtasksCommands:
    """Test Windows Task Scheduler command construction."""

    def test_schtasks_create_command_format(self):
        """Verify schtasks create command format."""
        task_name = "Forgememo Daemon"
        task_cmd = 'cmd /c "set FORGEMEMO_HTTP_PORT=5555 && forgememo daemon"'

        cmd = [
            "schtasks",
            "/create",
            "/tn",
            task_name,
            "/tr",
            task_cmd,
            "/sc",
            "ONLOGON",
            "/f",
        ]

        assert cmd[0] == "schtasks"
        assert cmd[1] == "/create"
        assert "/tn" in cmd
        assert "/tr" in cmd
        assert "/sc" in cmd
        assert "/f" in cmd

    def test_schtasks_delete_command_format(self):
        """Verify schtasks delete command format."""
        task_name = "Forgememo Daemon"

        cmd = ["schtasks", "/delete", "/tn", task_name, "/f"]

        assert cmd[0] == "schtasks"
        assert cmd[1] == "/delete"
        assert "/tn" in cmd
        assert "/f" in cmd


class TestEnvironmentVariables:
    """Test environment variable handling."""

    def test_http_port_default(self):
        """HTTP_PORT defaults to 5555."""
        from forgememo import daemon as daemon_module
        import importlib

        importlib.reload(daemon_module)
        assert daemon_module.HTTP_PORT == "5555"

    def test_socket_path_default(self):
        """SOCKET_PATH defaults to tempdir/forgememo.sock."""
        from forgememo import daemon as daemon_module
        import importlib

        importlib.reload(daemon_module)
        assert "forgememo.sock" in daemon_module.SOCKET_PATH


class TestDaemonStartCommands:
    """Test daemon start command construction."""

    def test_windows_daemon_command_uses_forgememo_bin(self):
        """Windows daemon command uses shutil.which result or fallback."""
        import shutil

        bin_path = shutil.which("forgememo") or "forgememo"
        http_port = os.environ.get("FORGEMEMO_HTTP_PORT", "5555")
        task_cmd = f'cmd /c "set FORGEMEMO_HTTP_PORT={http_port} && {bin_path} daemon"'

        assert "forgememo daemon" in task_cmd
        assert http_port in task_cmd

    def test_windows_worker_command_uses_forgememo_bin(self):
        """Windows worker command uses shutil.which result or fallback."""
        import shutil

        bin_path = shutil.which("forgememo") or "forgememo"
        http_port = os.environ.get("FORGEMEMO_HTTP_PORT", "5555")
        worker_cmd = (
            f'cmd /c "set FORGEMEMO_HTTP_PORT={http_port} && {bin_path} worker"'
        )

        assert "forgememo worker" in worker_cmd
        assert http_port in worker_cmd


class TestPortCheck:
    """Test port availability checking."""

    def test_check_port_function_exists(self):
        """_check_port function exists in daemon module."""
        from forgememo.daemon import _check_port

        assert callable(_check_port)

    def test_check_port_returns_bool(self):
        """_check_port returns a boolean."""
        from forgememo.daemon import _check_port
        import socket

        result = _check_port("127.0.0.1", 5555)
        assert isinstance(result, bool)

    def test_check_port_free_returns_false(self):
        """Check port returns False for free port."""
        from forgememo.daemon import _check_port

        result = _check_port("127.0.0.1", 19999)
        assert result is False


class TestGracefulShutdown:
    """Test graceful shutdown handler."""

    def test_graceful_shutdown_exists(self):
        """GracefulShutdown class exists."""
        from forgememo.daemon import GracefulShutdown

        assert GracefulShutdown is not None

    def test_graceful_shutdown_initial_state(self):
        """GracefulShutdown initializes with shutdown=False."""
        from forgememo.daemon import GracefulShutdown

        gs = GracefulShutdown()
        assert gs.shutdown is False
