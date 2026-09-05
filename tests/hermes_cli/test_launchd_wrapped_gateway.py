"""A launchd service PID can be Hermes's logger, above the real gateway."""

import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import gateway as gw

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="launchd uses POSIX signals")


@pytest.fixture
def wrapped_gateway(monkeypatch):
    processes = {}

    class Process:
        def __init__(self, pid, argv, children=()):
            self.pid = pid
            self.argv = argv
            self.child_pids = children
            processes[pid] = self

        def cmdline(self):
            return self.argv

        def children(self, recursive=False):
            assert not recursive
            return [processes[pid] for pid in self.child_pids]

    child = Process(202, ["python", "-m", "hermes_cli.main", "--profile", "work",
                          "gateway", "run", "--external-supervisor"])
    Process(203, list(child.argv))
    wrapper = Process(200, ["python", "-m", "hermes_cli.stderr_timestamp",
                            "--error-log", "/tmp/fake.log", "--", *child.argv], (202,))
    Process(999, ["python", "unrelated.py"])
    monkeypatch.setattr(psutil, "Process", lambda pid: processes[pid])
    monkeypatch.setattr(gw, "is_macos", lambda: True)
    monkeypatch.setattr(gw, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gw, "get_system_launchd_gateway_plist_path", lambda: Path("/absent"))
    monkeypatch.setattr(gw, "get_launchd_label", lambda: "ai.hermes.gateway-work")
    monkeypatch.setattr(gw, "launchd_gateway_labels_for_install", lambda: ["ai.hermes.gateway-work"])
    monkeypatch.setattr(gw, "_locate_launchd_gateway_service", lambda label: ("gui/501", 200))
    monkeypatch.setattr(gw.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    return wrapper


def test_wrapped_gateway_drains_child_and_waits_for_wrapper_exit(wrapped_gateway, monkeypatch):
    signals, waits = [], []
    monkeypatch.setattr(gw.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(gw, "_wait_for_pid_exit", lambda pid, timeout: waits.append((pid, timeout)) or True)

    assert gw._graceful_restart_via_sigusr1(200, 17.0)
    assert signals == [(202, signal.SIGUSR1)]
    assert waits == [(200, 17.0)]


def test_service_sweep_protects_wrapped_gateway_child(wrapped_gateway):
    assert gw._get_service_pids(all_profiles=True) == {200, 202}


def test_named_profile_fleet_protects_system_wrapper_and_child(wrapped_gateway, monkeypatch, tmp_path):
    plist = tmp_path / "daemon.plist"
    plist.touch()
    monkeypatch.setattr(gw, "get_system_launchd_gateway_plist_path", lambda: plist)
    monkeypatch.setattr(gw, "_probe_system_launchd_gateway", lambda: (False, None, "other home"))
    monkeypatch.setattr(gw, "_probe_system_launchd_gateway_for_install", lambda: (True, 200, ""))
    monkeypatch.setattr(gw, "_locate_launchd_gateway_service", lambda label: (None, None))
    assert gw._get_service_pids() == set()
    assert gw._get_service_pids(all_profiles=True) == {200, 202}


@pytest.mark.parametrize("children,expected", [((), False), ((202, 999), True), ((202, 203), False)])
def test_wrapper_restart_requires_one_gateway_child(wrapped_gateway, monkeypatch, children, expected):
    wrapped_gateway.child_pids = children
    signals = []
    monkeypatch.setattr(gw.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(gw, "_wait_for_pid_exit", lambda *a: True)

    result = gw._graceful_restart_via_sigusr1(200, 1.0)
    assert result is expected
    assert signals == ([(202, signal.SIGUSR1)] if expected else [])


def test_direct_gateway_still_receives_restart(wrapped_gateway, monkeypatch):
    signals = []
    monkeypatch.setattr(gw.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(gw, "_wait_for_pid_exit", lambda *a: True)
    assert gw._graceful_restart_via_sigusr1(202, 1.0)
    assert signals == [(202, signal.SIGUSR1)]


def test_current_profile_restart_compares_wrapper_generation(wrapped_gateway, monkeypatch):
    signals, waits, verified = [], [], []
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 202)
    monkeypatch.setattr(gw, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gw, "_get_parent_pid", lambda pid: 200)
    monkeypatch.setattr(gw, "_request_gateway_self_restart", lambda pid: False)
    monkeypatch.setattr(gw, "probe_gateway_loop_liveness", lambda pid: gw.GATEWAY_LOOP_ALIVE)
    monkeypatch.setattr(gw, "_get_restart_exit_wait_budget", lambda: 17.0)
    monkeypatch.setattr(gw.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(gw, "_wait_for_pid_exit", lambda pid, timeout: waits.append(pid) or True)
    monkeypatch.setattr(gw, "_wait_for_launchd_service_pid", lambda label, old_pid, **kw: verified.append(old_pid) or True)
    monkeypatch.setattr(gw, "_clear_launchd_unsupported_marker", lambda: None)

    gw.launchd_restart()

    assert signals == [(202, signal.SIGUSR1)]
    assert waits == [200]
    assert verified == [200]
