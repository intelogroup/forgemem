from __future__ import annotations

import importlib
import os
import tempfile


def test_daemon_log_fallback_to_tmp(monkeypatch, tmp_path):
    # Force makedirs to fail so the fallback-to-tmp logic triggers.
    # Create a regular file where a directory would need to be — this makes
    # makedirs fail on every OS (Linux, macOS, Windows).
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    impossible = os.path.join(str(blocker), "subdir", "forgememo_daemon.log")
    monkeypatch.setenv("FORGEMEMO_DAEMON_LOG", impossible)
    monkeypatch.setenv("FORGEMEMO_ALLOW_TMP_LOG", "1")

    import forgememo.daemon as daemon

    importlib.reload(daemon)

    expected = os.path.join(tempfile.gettempdir(), "forgememo_daemon.log")
    assert daemon.LOG_FILE == expected
