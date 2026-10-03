"""A loaded system gateway owns restart even when an old GUI plist survives."""

from __future__ import annotations

import plistlib
import subprocess
from itertools import count
from types import SimpleNamespace

import pytest

from gateway import status
from hermes_cli import dashboard_procs, gateway as gw, gateway_launchd as launchd
from hermes_cli import update_cmd_fleet as fleet, update_receipt
from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan, match_runtime_outcomes


pytestmark = pytest.mark.platforms("macos")
LABEL = "ai.hermes.gateway.daemon"


@pytest.fixture
def system_owner(monkeypatch, tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    plist = tmp_path / "LaunchDaemons" / f"{LABEL}.plist"
    plist.parent.mkdir()
    plist.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "EnvironmentVariables": {"HERMES_HOME": str(home)},
        "KeepAlive": True,
    }))
    retired = tmp_path / "LaunchAgents" / "ai.hermes.gateway.plist"
    retired.parent.mkdir()
    retired.write_bytes(plistlib.dumps({
        "Label": "ai.hermes.gateway",
        "EnvironmentVariables": {"HERMES_HOME": str(home)},
    }))
    state = {"pid": 415, "gateway_pid": 415, "start": 100, "replacement_start": 101, "loaded": True, "gui": False}
    actions = []
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_receipt, "_profile_homes", lambda: [("default", home)])
    monkeypatch.setattr(launchd, "_SYSTEM_GATEWAY_PLIST", plist, raising=False)
    monkeypatch.setattr(gw, "_profile_suffix", lambda: "")
    monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: retired)
    monkeypatch.setattr(gw, "launchd_gateway_labels_for_install", lambda: ["ai.hermes.gateway"])
    monkeypatch.setattr(gw, "legacy_launchd_labels_for_install", lambda **kw: [])
    monkeypatch.setattr(gw, "_launchctl_supervised_pid", lambda label: None)
    monkeypatch.setattr(gw, "_is_pid_ancestor_of_current_process", lambda pid: False)
    monkeypatch.setattr(gw, "_request_gateway_self_restart", lambda pid: False)
    monkeypatch.setattr(gw, "wait_for_launchd_gateway_supervision", lambda **kw: True)
    monkeypatch.setattr(gw, "_get_restart_exit_wait_budget", lambda: 45)
    monkeypatch.setattr(gw, "_clear_launchd_unsupported_marker", lambda: None)
    monkeypatch.setattr(gw, "launchd_restart", lambda: actions.append(("gui-restart",)))
    monkeypatch.setattr(gw, "_locate_launchd_gateway_service", lambda label: (None, None))

    def probe(domain, label):
        if domain == "system" and label == LABEL:
            return state["loaded"], state["pid"] if state["loaded"] else None
        return state["gui"], 990 if state["gui"] else None

    monkeypatch.setattr(gw, "_launchd_print_service_pid", probe)
    monkeypatch.setattr(status, "live_gateway_pid_for_home", lambda path: state["gateway_pid"])
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: state["start"])
    monkeypatch.setattr(status, "_read_process_cmdline", lambda pid: "/x/python -m hermes_cli.main gateway run")
    monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: str(home))

    def drain(pid, *args, **kwargs):
        actions.append(("drain", pid))
        return True

    clock = {"now": 0.0}
    monkeypatch.setattr(launchd.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(launchd.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))

    def verify(label, old_pid, **kwargs):
        domain = kwargs["domain"]
        actions.append(("verify", domain, label, old_pid))
        state.update(pid=416, start=state["replacement_start"])
        return launchd._wait_for_launchd_service_pid(label, old_pid, **kwargs)

    monkeypatch.setattr(gw, "_graceful_restart_via_sigusr1", drain)
    monkeypatch.setattr(gw, "_wait_for_launchd_service_pid", verify)
    monkeypatch.setattr(gw.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    return home, plist, retired, state, actions


@pytest.mark.parametrize("entrypoint", [
    "fleet", "update_default", "cli", "drain_failed", "replacement_failed", "replacement_same_start", "replacement_wrong_command",
])
def test_update_drains_system_owner_without_reviving_retired_gui(monkeypatch, system_owner, entrypoint):
    home, _plist, retired, state, actions = system_owner
    old_gui_bytes = retired.read_bytes()
    restarted, failed = [], []

    if entrypoint == "drain_failed":
        monkeypatch.setattr(gw, "_graceful_restart_via_sigusr1", lambda pid, *a, **kw: actions.append(("drain", pid)) or False)
    if entrypoint == "replacement_failed":
        monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: str(home if pid == 415 else home / "other"))
    if entrypoint == "replacement_same_start":
        state["replacement_start"] = 100
    if entrypoint == "replacement_wrong_command":
        monkeypatch.setattr(status, "_read_process_cmdline", lambda pid: "/x/python unrelated.py")
    if entrypoint == "update_default":
        restarted, failed = fleet._restart_launchd_gateway_after_update()
    elif entrypoint == "cli":
        launchd.launchd_restart()
    else:
        fleet._restart_macos_launchd_gateways(restarted, failed, 45)

    expected = [("drain", 415)]
    if entrypoint != "drain_failed":
        expected.append(("verify", "system", LABEL, 415))
    assert actions == expected
    if entrypoint == "drain_failed" or entrypoint.startswith("replacement_"):
        assert restarted == [] and failed == [LABEL]
    elif entrypoint != "cli":
        assert restarted == [LABEL] and failed == []
    assert retired.read_bytes() == old_gui_bytes
    assert state["pid"] in gw._get_service_pids(all_profiles=True)
    monkeypatch.setattr(gw, "_profile_suffix", lambda: "ops")
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "ops"))
    assert state["pid"] not in gw._get_service_pids()
    assert state["pid"] in gw._get_service_pids(all_profiles=True)
    plan = UpdatePlan(runtimes=[RuntimeRecord("gateway", "default", 415, "launchd")])
    outcomes = match_runtime_outcomes(
        plan, restarted_services=restarted, relaunched_profiles=[], externally_supervised_profiles=[],
        killed_pids=set(), failed_units=failed,
    )
    expected_outcome = "failed" if failed else "unaccounted" if entrypoint == "cli" else "restarted"
    assert outcomes[0]["outcome"] == expected_outcome


@pytest.mark.parametrize("fault", ["unloaded", "foreign_pid", "foreign_home", "unknown_start", "changed_start", "gui_owner", "unreadable"])
def test_uncertain_system_owner_prevents_gui_fallback(monkeypatch, system_owner, fault):
    _home, plist, retired, state, actions = system_owner
    if fault == "unloaded":
        state["loaded"] = False
    if fault == "foreign_pid":
        state["gateway_pid"] = 777
    if fault == "foreign_home":
        monkeypatch.setattr(dashboard_procs, "_hermes_home_for_pid", lambda pid: str(_home / "other"))
    if fault == "unknown_start":
        state["start"] = None
    if fault == "changed_start":
        starts = count(100)
        monkeypatch.setattr(status, "get_process_start_time", lambda pid: next(starts))
    if fault == "gui_owner":
        state["gui"] = True
    if fault == "unreadable":
        plist.write_text("not a plist")
    old_gui_bytes = retired.read_bytes()

    with pytest.raises(RuntimeError, match="[Oo]wner|[Oo]wnership"):
        fleet._restart_macos_launchd_gateways([], [], 45)

    assert actions == []
    assert retired.read_bytes() == old_gui_bytes

    # The enclosing update must not turn an ownership refusal into generic
    # recovery or a manual sweep of the same system-owned process.
    monkeypatch.setattr(gw, "find_gateway_pids", lambda **kw: [415])
    monkeypatch.setattr(fleet, "_scoped_manual_gateway_pids", lambda pids, **kw: pids)
    monkeypatch.setattr(fleet, "_restart_systemd_gateway_units", lambda *a, **kw: None)
    monkeypatch.setattr(fleet, "_restart_manual_gateways", lambda *a, **kw: actions.append(("manual-sweep",)))
    monkeypatch.setattr(fleet, "_recover_after_restart_phase_abort", lambda *a, **kw: actions.append(("generic-recovery",)))
    monkeypatch.setattr(fleet._GatewayRestartOutcome, "record_receipt", lambda *a, **kw: None)
    result = fleet._restart_gateway_fleet_after_update(UpdatePlan(), gateway_mode=False)
    assert result.incomplete
    assert result.failed_or_stale_units == [LABEL]
    assert actions == []


@pytest.mark.parametrize("boundary", ["drain", "verification"])
@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["launchctl", "print"], 5), OSError("probe unavailable")])
def test_system_restart_probe_failure_never_reenters_generic_recovery(monkeypatch, system_owner, boundary, error):
    _home, plist, retired, _state, actions = system_owner
    old_system, old_gui = plist.read_bytes(), retired.read_bytes()
    expected = [("drain", 415)]
    if boundary == "verification":
        expected.append(("verify", "system", LABEL, 415))

    def fail(*args, **kwargs):
        actions.append(expected[-1])
        raise error

    def generic_recovery(_error, _plan, outcome, **kwargs):
        actions.append(("generic-recovery",))
        outcome.incomplete = True

    method = "_graceful_restart_via_sigusr1" if boundary == "drain" else "_wait_for_launchd_service_pid"
    monkeypatch.setattr(gw, method, fail)
    monkeypatch.setattr(gw, "find_gateway_pids", lambda **kw: [415])
    monkeypatch.setattr(fleet, "_scoped_manual_gateway_pids", lambda pids, **kw: pids)
    monkeypatch.setattr(fleet, "_restart_systemd_gateway_units", lambda *a, **kw: None)
    monkeypatch.setattr(fleet, "_restart_manual_gateways", lambda *a, **kw: actions.append(("manual-sweep",)))
    monkeypatch.setattr(fleet, "_recover_after_restart_phase_abort", generic_recovery)
    monkeypatch.setattr(fleet._GatewayRestartOutcome, "record_receipt", lambda *a, **kw: None)

    outcome = fleet._restart_gateway_fleet_after_update(UpdatePlan(), gateway_mode=False)

    assert actions == expected
    assert outcome.incomplete and outcome.failed_or_stale_units == [LABEL]
    assert plist.read_bytes() == old_system and retired.read_bytes() == old_gui


@pytest.mark.parametrize("verb", ["start", "install", "stop", "uninstall", "restart"])
@pytest.mark.parametrize("dispatch", ["backend", "command", "all"])
def test_system_owner_lifecycle_never_falls_through_to_user_mutation(monkeypatch, system_owner, verb, dispatch):
    home, plist, retired, _state, actions = system_owner
    old_system, old_gui = plist.read_bytes(), retired.read_bytes()
    monkeypatch.setattr(gw, "_refuse_from_inside_gateway", lambda *a: None)
    monkeypatch.setattr(gw, "kill_gateway_processes", lambda **kw: actions.append(("kill",)) or 1)
    monkeypatch.setattr(gw, "stop_profile_gateway", lambda: actions.append(("stop-profile",)) or True)
    monkeypatch.setattr(gw, "run_gateway", lambda **kw: actions.append(("spawn",)))

    def run():
        if dispatch != "backend":
            # Force/all must not reach the broad sweep before the owner guard.
            gw.gateway_command(SimpleNamespace(gateway_command=verb, force=True, all=dispatch == "all"))
        else:
            getattr(launchd, f"launchd_{verb}")()

    if verb in {"install", "stop", "uninstall"} or dispatch == "all":
        with pytest.raises(SystemExit if dispatch != "backend" else launchd.LaunchdGatewayOwnershipError) as error:
            run()
        if dispatch != "backend":
            assert error.value.code == 1
        assert actions == []
    else:
        run()
        assert actions == ([] if verb == "start" else [("drain", 415), ("verify", "system", LABEL, 415)])
    assert plist.read_bytes() == old_system and retired.read_bytes() == old_gui

    # These guards have no authority over named profiles or another Hermes root.
    monkeypatch.setattr(gw, "_profile_suffix", lambda: "ops")
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "ops"))
    assert launchd._handle_system_launchd_gateway_action(verb) is False
    with pytest.raises(launchd.LaunchdGatewayOwnershipError, match="--all"):
        launchd._handle_system_launchd_gateway_action(verb, all_profiles=True)
    monkeypatch.setattr(gw, "_profile_suffix", lambda: "")
    foreign = home.parent / "foreign"
    foreign.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(foreign))
    monkeypatch.setattr(update_receipt, "_profile_homes", lambda: [("default", foreign)])
    assert launchd._handle_system_launchd_gateway_action(verb) is False
    assert launchd._handle_system_launchd_gateway_action(verb, all_profiles=True) is False
