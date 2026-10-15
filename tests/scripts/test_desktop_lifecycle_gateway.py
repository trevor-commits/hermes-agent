"""Packaged lifecycle cleanup is bound to the exact isolated gateway incarnation."""

from copy import deepcopy
import os
import json
import struct
from types import SimpleNamespace

import pytest

from apps.desktop.scripts import lifecycle_gateway as lifecycle
from gateway import control_socket

pytestmark = pytest.mark.platforms("macos", "linux")


@pytest.fixture
def owner(tmp_path, monkeypatch):
    sandbox = tmp_path / "hermes-desktop-fresh-install-lifecycle-test"
    home = sandbox / "hermes-home"
    home.mkdir(parents=True)
    identity = {
        "kind": "hermes-gateway", "pid": 4242, "start_time": 10013,
        "hermes_home": str(home), "supervisor": "none", "code_sha": "a" * 40,
        "instance_id": "owned", "runtime_protocol": 1, "authority_epoch": 1,
        "api_origin": "http://127.0.0.1:12345", "state": "ready",
    }
    status = {
        "answering_pid": 4242, "start_time": 10013, "hermes_home": str(home),
        "active_agents": 0, "active_work": None, "gateway_state": "running",
    }
    endpoint = {key: identity[key] for key in (
        "instance_id", "runtime_protocol", "authority_epoch", "api_origin", "code_sha", "supervisor",
    )}
    endpoint["profile_id"] = str(home)
    request = {
        "sandbox": str(sandbox), "code_sha": "a" * 40, "started_after": 99.0,
        "connection": {"mode": "local", "source": "local", "baseUrl": identity["api_origin"], "gatewayEndpoint": endpoint},
    }
    calls = []
    state = {"stopped": False, "keep_control": False, "process_started": 100.125}
    (home / "gateway.sock").touch()

    class Process:
        def create_time(self):
            return state["process_started"]

        def environ(self):
            return {"HERMES_HOME": str(home)}

        def uids(self):
            return SimpleNamespace(real=getattr(os, "getuid", lambda: 0)())

        def exe(self):
            return "/fixture/python"

        def wait(self, timeout):
            assert timeout == 20 and calls == [home]
            state["stopped"] = True
            if not state["keep_control"]:
                (home / "gateway.sock").unlink()

    def identify(path, **_kwargs):
        assert path == home
        return None if state["stopped"] else deepcopy(identity)

    def query(path, verb, **_kwargs):
        assert path == home and verb == "status"
        return dict(status)

    def pause(path, **_kwargs):
        calls.append(path)
        return {"pid": 4242, "pausing": True}

    monkeypatch.setattr(lifecycle, "identify_gateway", identify)
    monkeypatch.setattr(lifecycle, "query_gateway_control", query)
    monkeypatch.setattr(lifecycle, "pause_gateway_for_update", pause)
    monkeypatch.setattr(lifecycle, "get_process_start_time", lambda _pid: 10013)
    monkeypatch.setattr(lifecycle.psutil, "Process", lambda _pid: Process())
    return SimpleNamespace(home=home, identity=identity, status=status, request=request, calls=calls, state=state)


def test_verified_renderer_gateway_is_gracefully_stopped(owner):
    expected = lifecycle.inspect_gateway(owner.home, owner.request)
    assert expected["pid"] == 4242 and expected["process_started_at"] == 100.125
    result = lifecycle.stop_gateway(owner.home, {**owner.request, "expected": expected})
    assert result["process_exited"] and result["control_absent"]
    assert owner.calls == [owner.home]


@pytest.mark.parametrize("field,value", [
    ("hermes_home", "/foreign/home"), ("supervisor", "launchd"), ("code_sha", "b" * 40),
    ("start_time", 999), ("kind", "other"), ("api_origin", "https://example.invalid"),
])
def test_changed_or_foreign_identity_never_receives_a_pause(owner, field, value):
    owner.identity[field] = value
    with pytest.raises(RuntimeError):
        lifecycle.stop_gateway(owner.home, owner.request)
    assert owner.calls == []


@pytest.mark.parametrize("change", ["remote", "profile", "instance", "base_url"])
def test_renderer_descriptor_must_match_the_live_owner(owner, change):
    descriptor = owner.request["connection"]
    if change == "remote":
        descriptor["mode"] = "remote"
    elif change == "profile":
        descriptor["gatewayEndpoint"]["profile_id"] = "/other/home"
    elif change == "instance":
        descriptor["gatewayEndpoint"]["instance_id"] = "other"
    else:
        descriptor["baseUrl"] = "http://127.0.0.1:9999"
    with pytest.raises(RuntimeError):
        lifecycle.stop_gateway(owner.home, owner.request)
    assert owner.calls == []


def test_unknown_owner_is_retained_without_signaling(owner, monkeypatch):
    monkeypatch.setattr(lifecycle, "identify_gateway", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        lifecycle.stop_gateway(owner.home, owner.request)
    assert owner.calls == []


def test_owner_change_between_validation_and_pause_is_rejected(owner, monkeypatch):
    reads = []

    def identify(*_args, **_kwargs):
        reads.append(True)
        return {**owner.identity, "instance_id": "owned" if len(reads) == 1 else "replacement"}

    monkeypatch.setattr(lifecycle, "identify_gateway", identify)
    with pytest.raises(RuntimeError, match="owner changed"):
        lifecycle.stop_gateway(owner.home, owner.request)
    assert owner.calls == []


def test_preexisting_gateway_cannot_be_adopted_for_cleanup(owner):
    owner.request["started_after"] = 101
    with pytest.raises(RuntimeError, match="predates"):
        lifecycle.stop_gateway(owner.home, owner.request)
    assert owner.calls == []


@pytest.mark.parametrize("peer_pid,peer_started,accepted", [
    (9999, 100.125, False), (4242, 101, False), (4242, 100.125, True),
])
def test_pause_connection_is_bound_before_any_request(owner, monkeypatch, peer_pid, peer_started, accepted):
    sent = []
    original_transport = control_socket._query_unix_socket

    class ReplacementSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            owner.state["process_started"] = peer_started

        def getsockopt(self, _level, _option, *size):
            return struct.pack("3i", peer_pid, 0, 0) if size else peer_pid

        def sendall(self, data):
            sent.append(data)
            owner.calls.append(owner.home)

        def recv(self, _size):
            return json.dumps({"ok": True, "result": {"pid": 4242, "pausing": True}}).encode() + b"\n"

    monkeypatch.setattr(lifecycle, "pause_gateway_for_update", control_socket.pause_gateway_for_update)
    monkeypatch.setattr(control_socket, "resolve_client_socket_path", lambda _home: owner.home / "gateway.sock")
    monkeypatch.setattr(control_socket.socket, "socket", lambda *_args: ReplacementSocket())
    if accepted:
        result = lifecycle.stop_gateway(owner.home, owner.request)
        assert result["process_exited"] and result["control_absent"]
        assert [json.loads(data) for data in sent] == [{"verb": "pause-for-update", "id": 1, "protocol": 1}]
        assert owner.calls == [owner.home]
    else:
        with pytest.raises(RuntimeError, match="peer"):
            lifecycle.stop_gateway(owner.home, owner.request)
        assert sent == [] and owner.calls == []
    assert control_socket._query_unix_socket is original_transport


def test_unresponsive_control_path_is_not_absence(owner):
    owner.state["keep_control"] = True
    with pytest.raises(RuntimeError, match="control.*exists"):
        lifecycle.stop_gateway(owner.home, owner.request)
