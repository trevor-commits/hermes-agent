"""The live gateway receipt must survive a test that clears os.environ.

2026-09-01: a candidate gate's tests/gateway/test_feishu.py overwrote the
operator's live gateway_state.json with a pytest PID. Every environment-based
isolation is defeated by ``patch.dict(os.environ, {}, clear=True)``, which
drops HOME and HERMES_HOME together so Path.home() falls through to the
passwd database and lands back on the real home.
"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import gateway.status as status


def _real_root():
    import pwd
    return (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes").resolve()


@patch.dict(os.environ, {}, clear=True)
def test_cleared_environment_cannot_write_the_live_receipt():
    """The exact incident shape: whole environment wiped, then a status write."""
    # Precondition: with the environment cleared, resolution really does reach
    # the operator's real home. If this ever stops being true the guard is
    # still correct, but this test would no longer be reproducing the bug.
    resolved = status._get_runtime_status_path().expanduser().resolve()
    assert str(resolved).startswith(str(_real_root())), (
        "expected a cleared environment to resolve back to the real root; "
        f"got {resolved}"
    )

    with pytest.raises(RuntimeError, match="gateway_status_write_guard"):
        status.write_runtime_status(gateway_state="running")


def test_guard_allows_writes_inside_an_isolated_home(tmp_path, monkeypatch):
    """The guard is a deny-list: a sandboxed HERMES_HOME must still work."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    resolved = status._get_runtime_status_path().expanduser().resolve()
    assert not str(resolved).startswith(str(_real_root()))

    status.write_runtime_status(gateway_state="running")
    assert resolved.exists(), "a sandboxed write must not be blocked"
