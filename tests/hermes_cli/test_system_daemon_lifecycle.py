"""Stage 2 keeper-carry tests: system LaunchDaemon identity scoping + verified restart.

Covers the keeper-sync-rebuild plan (docs/plans/2026-08-21-keeper-sync-rebuild.md):

1. ``_system_daemon_identity_matches`` — a plist named
   ``ai.hermes.gateway.daemon.plist`` must point at THIS checkout/venv/
   HERMES_HOME before it is adopted as ours; foreign or malformed plists
   fail closed.
2. ``_probe_system_launchd_gateway`` — identity-gated: a foreign daemon is
   never reported as our loaded gateway (status/protect-set/restart all flow
   through this probe).
3. ``_restart_system_launchd_gateway`` / ``launchd_restart`` — every
   successful restart path ends with a VERIFIED fresh PID
   (PR #88949 parity); a silent KeepAlive failure exits non-zero instead of
   reporting success, and a foreign daemon is never restarted.

All launchctl/filesystem seams are mocked — fully hermetic, no system daemon
required.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gw


pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="launchd system-daemon paths are POSIX-only"
)


DAEMON_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.hermes.gateway.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{root}/scripts/run-gateway.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HERMES_HOME</key>
        <string>{home}</string>
        <key>VIRTUAL_ENV</key>
        <string>{root}/venv</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""


def _write_plist(tmp_path: Path, *, root: str, home: str, label: str = "ai.hermes.gateway.daemon") -> Path:
    text = DAEMON_PLIST.format(root=root, home=home)
    if label != "ai.hermes.gateway.daemon":
        text = text.replace("ai.hermes.gateway.daemon", label)
    path = tmp_path / "ai.hermes.gateway.daemon.plist"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def ours(monkeypatch, tmp_path):
    """Make PROJECT_ROOT/hermes-home/plist-path all point at tmp fixtures."""
    root = tmp_path / "checkout"
    root.mkdir()
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr(gw, "PROJECT_ROOT", root)
    monkeypatch.setattr(gw, "get_hermes_home", lambda: home)
    plist = _write_plist(tmp_path, root=str(root), home=str(home))
    monkeypatch.setattr(
        gw, "get_system_launchd_gateway_plist_path", lambda: plist
    )
    return SimpleNamespace(root=root, home=home, plist=plist)


class TestIdentity:
    def test_matching_plist_is_ours(self, ours):
        assert gw._system_daemon_identity_matches() is True

    def test_missing_plist_fails_closed(self, ours):
        ours.plist.unlink()
        assert gw._system_daemon_identity_matches() is False

    def test_foreign_checkout_rejected(self, ours, tmp_path):
        _write_plist(
            tmp_path,
            root="/opt/other-install",
            home=str(ours.home),
        )
        assert gw._system_daemon_identity_matches() is False

    def test_foreign_hermes_home_rejected(self, ours, tmp_path):
        _write_plist(
            tmp_path,
            root=str(ours.root),
            home=str(tmp_path / "someone-elses-home"),
        )
        assert gw._system_daemon_identity_matches() is False

    def test_wrong_label_rejected(self, ours, tmp_path):
        _write_plist(
            tmp_path,
            root=str(ours.root),
            home=str(ours.home),
            label="com.other.daemon",
        )
        assert gw._system_daemon_identity_matches() is False

    def test_malformed_plist_fails_closed(self, ours):
        ours.plist.write_text("<plist><dict>garbage", encoding="utf-8")
        assert gw._system_daemon_identity_matches() is False

    def test_venv_alone_can_establish_identity(self, ours, tmp_path):
        # ProgramArguments is /bin/bash (not under checkout) but VIRTUAL_ENV
        # points at this checkout's venv — identity holds via the venv field.
        text = DAEMON_PLIST.format(root=str(ours.root), home=str(ours.home))
        text = text.replace(
            f"<string>{ours.root}/scripts/run-gateway.sh</string>",
            "<string>/bin/bash</string>", 1,
        )
        ours.plist.write_text(text, encoding="utf-8")
        # First string in array is now /bin/bash; venv still matches.
        assert gw._system_daemon_identity_matches() is True


class TestProbeIdentityGated:
    def test_foreign_daemon_not_reported_as_ours(self, ours, monkeypatch):
        _write_plist(
            tmp_path=ours.plist.parent,
            root="/opt/other-install",
            home=str(ours.home),
        )
        # launchctl would happily answer — the identity gate must short-
        # circuit before any launchctl call happens.
        launched = []

        def _no_launchctl(*a, **k):
            launched.append(a)
            raise AssertionError("launchctl must not be called for a foreign daemon")

        monkeypatch.setattr(gw.subprocess, "run", _no_launchctl)
        loaded, pid, out = gw._probe_system_launchd_gateway()
        assert (loaded, pid, out) == (False, None, "")
        assert launched == []

    def test_our_daemon_still_probes_launchctl(self, ours, monkeypatch):
        printed = (
            "system/ai.hermes.gateway.daemon = {\n"
            "\tstate = running\n"
            "\tpid = 4242\n"
            "}\n"
        )
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=printed, stderr="")

        monkeypatch.setattr(gw.subprocess, "run", fake_run)
        loaded, pid, out = gw._probe_system_launchd_gateway()
        assert loaded is True and pid == 4242
        assert calls and calls[0][:3] == ["launchctl", "print", "system/ai.hermes.gateway.daemon"]


class TestVerifiedRestart:
    def _wire_restart(
        self,
        monkeypatch,
        *,
        self_restart_ok: bool = True,
        fresh_pid: int | None = 9999,
        drain_ok: bool = True,
    ):
        rec = SimpleNamespace(requests=[], drains=[], waits=[])

        monkeypatch.setattr(
            gw,
            "_request_gateway_self_restart",
            lambda pid: (rec.requests.append(pid), self_restart_ok)[1],
        )
        monkeypatch.setattr(
            gw,
            "_graceful_restart_via_sigusr1",
            lambda pid, wait_budget: (rec.drains.append(pid), drain_ok)[1],
        )

        state = {"pids": [1234]}

        def fake_probe():
            pid = state["pids"][-1]
            if pid is None:
                return False, None, ""
            return True, pid, "pid = %d" % pid

        def fake_wait(old_pid, timeout=30.0):
            rec.waits.append(old_pid)
            if fresh_pid is None:
                return False
            state["pids"] = [fresh_pid]
            return True

        monkeypatch.setattr(gw, "_probe_system_launchd_gateway", fake_probe)
        monkeypatch.setattr(gw, "_wait_for_system_daemon_pid", fake_wait)
        return rec

    def test_self_restart_success_requires_fresh_pid(self, ours, monkeypatch, capsys):
        rec = self._wire_restart(monkeypatch, self_restart_ok=True, fresh_pid=9999)
        gw.launchd_restart()
        output = capsys.readouterr().out
        assert rec.waits == [1234]
        assert "fresh PID" in output

    def test_self_restart_without_fresh_pid_exits_nonzero(self, ours, monkeypatch, capsys):
        self._wire_restart(monkeypatch, self_restart_ok=True, fresh_pid=None)
        with pytest.raises(SystemExit) as ei:
            gw.launchd_restart()
        assert ei.value.code == 1

    def test_drain_without_keepalive_relaunch_exits_nonzero(self, ours, monkeypatch, capsys):
        rec = self._wire_restart(
            monkeypatch, self_restart_ok=False, drain_ok=True, fresh_pid=None
        )
        with pytest.raises(SystemExit) as ei:
            gw.launchd_restart()
        assert ei.value.code == 1
        assert rec.drains == [1234]

    def test_drain_then_verified_relaunch_succeeds(self, ours, monkeypatch, capsys):
        rec = self._wire_restart(
            monkeypatch, self_restart_ok=False, drain_ok=True, fresh_pid=8888
        )
        gw.launchd_restart()
        output = capsys.readouterr().out
        assert rec.drains == [1234]
        assert rec.waits == [1234]
        assert "relaunched on a fresh PID" in output

    def test_foreign_daemon_never_restarted(self, ours, monkeypatch, capsys):
        # Identity fails (foreign root): launchd_restart must not touch the
        # system daemon at all — falls through to the per-user branch.
        _write_plist(
            tmp_path=ours.plist.parent,
            root="/opt/other-install",
            home=str(ours.home),
        )
        touched = []
        monkeypatch.setattr(gw, "_restart_system_launchd_gateway", lambda: touched.append(1))
        monkeypatch.setattr(gw, "_launchd_domain", lambda: "gui/501")
        monkeypatch.setattr(gw, "get_launchd_label", lambda: "ai.hermes.gateway")
        monkeypatch.setattr(
            gw, "get_launchd_plist_path",
            lambda: ours.plist.parent / "does-not-exist.plist",
        )
        gw.launchd_restart()
        assert touched == []


class TestPendingKeepalivePath:
    def test_pending_restart_verifies_eventual_pid(self, ours, monkeypatch, capsys):
        # daemon loaded but PID None (KeepAlive respawn pending): the old code
        # returned silently; now we verify a PID appears.
        probes = {"n": 0}

        def fake_probe():
            probes["n"] += 1
            if probes["n"] < 3:
                return True, None, ""
            return True, 7777, "pid = 7777"

        monkeypatch.setattr(gw, "_probe_system_launchd_gateway", fake_probe)
        monkeypatch.setattr(gw.time, "sleep", lambda s: None)
        gw._restart_system_launchd_gateway()
        out = capsys.readouterr().out
        assert "came back up" in out
        assert probes["n"] >= 3

    def test_pending_restart_never_gets_pid_exits_nonzero(self, ours, monkeypatch, capsys):
        monkeypatch.setattr(
            gw, "_probe_system_launchd_gateway", lambda: (True, None, "")
        )
        monkeypatch.setattr(gw.time, "sleep", lambda s: None)
        monkeypatch.setattr(gw, "_wait_for_system_daemon_pid", lambda old_pid, timeout=30.0: False)
        with pytest.raises(SystemExit) as ei:
            gw._restart_system_launchd_gateway()
        assert ei.value.code == 1


class TestWaitHelper:
    def test_wait_accepts_fresh_pid_immediately(self, ours, monkeypatch):
        monkeypatch.setattr(
            gw, "_probe_system_launchd_gateway", lambda: (True, 4321, "")
        )
        assert gw._wait_for_system_daemon_pid(old_pid=1111, timeout=1.0) is True

    def test_wait_rejects_same_pid_until_timeout(self, ours, monkeypatch):
        monkeypatch.setattr(
            gw, "_probe_system_launchd_gateway", lambda: (True, 1111, "")
        )
        sleeps = []
        monkeypatch.setattr(gw.time, "sleep", lambda s: sleeps.append(s))
        # Fake monotonic so the deadline expires after two polls.
        ticks = iter([0.0, 0.5, 1.0, 2.0])
        monkeypatch.setattr(gw.time, "monotonic", lambda: next(ticks))
        assert gw._wait_for_system_daemon_pid(old_pid=1111, timeout=1.0) is False
