"""Identify and gracefully retire only a packaged lifecycle test's isolated gateway."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import time
from unittest.mock import patch
from urllib.parse import urlsplit

import psutil

from gateway import control_socket
from gateway.control_socket import identify_gateway, pause_gateway_for_update, query_gateway_control
from gateway.status import get_process_start_time


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _test_home(home: Path, sandbox: str) -> Path:
    home, root = home.resolve(strict=True), Path(sandbox).resolve(strict=True)
    _require(root.name.startswith("hermes-desktop-fresh-install-lifecycle-"), "not a lifecycle sandbox")
    _require(home == root / "hermes-home", "gateway home is outside the lifecycle sandbox")
    return home


def _loopback_origin(value: str) -> str:
    origin = urlsplit(value)
    _require(origin.scheme == "http" and origin.hostname in {"127.0.0.1", "::1"}
             and origin.port and not origin.username and not origin.password
             and not origin.path and not origin.query and not origin.fragment,
             "gateway API origin is not a plain loopback HTTP origin")
    return value


def inspect_gateway(home: Path, request: dict) -> dict:
    """Prove live control identity against the fresh home and optional renderer descriptor."""
    home = _test_home(home, request["sandbox"])
    _require(re.fullmatch(r"[0-9a-f]{40}", request.get("code_sha", "")), "backend revision is missing")
    identity = identify_gateway(home, timeout=3)
    _require(isinstance(identity, dict), "isolated gateway identity is unavailable")
    _require(identity.get("kind") == "hermes-gateway" and identity.get("hermes_home") == str(home)
             and identity.get("supervisor") == "none", "gateway is not this sandbox's unsupervised owner")
    _require(identity.get("code_sha") == request["code_sha"] and identity.get("runtime_protocol") == 1
             and identity.get("instance_id") and type(identity.get("authority_epoch")) is int
             and identity["authority_epoch"] > 0, "gateway code or instance identity mismatch")
    pid, started = identity.get("pid"), identity.get("start_time")
    _require(type(pid) is int and pid > 1 and type(started) is int and started > 0,
             "gateway PID/start identity is missing")
    _require(get_process_start_time(pid) == started, "gateway PID was reused")
    process = psutil.Process(pid)
    process_started = process.create_time()
    _require(process_started >= request["started_after"], "gateway predates this lifecycle run")
    _require(Path(process.environ().get("HERMES_HOME", "")).resolve() == home,
             "gateway process environment names another home")
    if hasattr(os, "getuid"):
        _require(process.uids().real == os.getuid(), "gateway belongs to another user")  # windows-footgun: ok — guarded POSIX call
    status = query_gateway_control(home, "status", timeout=3)
    _require(status and status.get("answering_pid") == pid and status.get("hermes_home") == str(home)
             and status.get("start_time") == started, "gateway status identity mismatch")
    _require(status.get("active_agents") == 0 and not status.get("active_work"),
             "isolated gateway unexpectedly has active work")
    origin = _loopback_origin(identity.get("api_origin", ""))
    owned = {key: identity[key] for key in (
        "pid", "start_time", "hermes_home", "supervisor", "instance_id", "api_origin", "code_sha",
        "authority_epoch", "runtime_protocol",
    )}
    owned["process_started_at"] = process_started
    owned["executable"] = process.exe()
    control_path = control_socket.resolve_client_socket_path(home)
    _require(control_path is not None, "gateway control path is missing")
    owned["control_socket"] = str(control_path)
    expected = request.get("expected")
    if expected is not None:
        _require(owned == expected, "isolated gateway owner changed")
    connection = request.get("connection")
    if connection is not None:
        endpoint = connection.get("gatewayEndpoint") or {}
        _require(connection.get("mode") == "local" and connection.get("source") == "local"
                 and connection.get("baseUrl") == origin, "renderer did not attach to the local gateway")
        _require(endpoint.get("profile_id") == str(home) and endpoint.get("control_home") in (None, str(home)),
                 "renderer gateway profile is outside the sandbox")
        _require(all(endpoint.get(key) == identity.get(key) for key in (
            "instance_id", "authority_epoch", "runtime_protocol", "api_origin", "code_sha", "supervisor",
        )), "renderer and live gateway identities differ")
        _require(identity.get("state") == "ready" and status.get("gateway_state") == "running",
                 "renderer gateway is not ready")
    return owned


def _pause_owned(home: Path, owned: dict) -> dict | None:
    """Bind the native one-request transport to the kernel-authenticated owner before sending."""
    _require(sys.platform in {"darwin", "linux"}, "identity-bound cleanup requires macOS or Linux")

    def bound_query(_home, request_bytes, timeout):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(timeout)
            peer.connect(owned["control_socket"])
            if sys.platform == "darwin":
                # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2.
                pid = peer.getsockopt(0, 2)
            else:
                pid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
            _require(pid == owned["pid"], "connected gateway peer changed")
            _require(psutil.Process(pid).create_time() == owned["process_started_at"]
                     and get_process_start_time(pid) == owned["start_time"], "gateway peer PID was reused")
            peer.sendall(request_bytes)
            return control_socket._read_response_line(lambda: peer.recv(65536), time.monotonic() + timeout)

    # Reuse native framing and response validation; only the one pause transport is bound.
    with patch.object(control_socket, "_query_unix_socket", bound_query):
        return pause_gateway_for_update(home, timeout=5)


def stop_gateway(home: Path, request: dict) -> dict:
    """Revalidate the exact owner, use its native drain, and prove that incarnation exited."""
    owned = inspect_gateway(home, request)
    process = psutil.Process(owned["pid"])
    _require(process.create_time() == owned["process_started_at"], "gateway PID changed before cleanup")
    # Recheck after the process/status reads; a changed owner must never inherit a stop.
    inspect_gateway(home, {**request, "expected": owned, "connection": None})
    ack = _pause_owned(home, owned)
    _require(ack and ack.get("pid") == owned["pid"] and (ack.get("pausing") or ack.get("already_stopping")),
             "isolated gateway peer did not acknowledge its native pause")
    process.wait(timeout=20)
    _require(not any(os.path.lexists(path) for path in (
        owned["control_socket"], home / "gateway.sock", home / "gateway.sock.path",
    )), "gateway control path still exists after cleanup")
    return {**owned, "native_pause_ack": ack, "process_exited": True, "control_absent": True}


def main() -> None:
    operation, raw_home = sys.argv[1:]
    _require(operation in {"inspect", "stop"}, "unsupported lifecycle operation")
    request = json.load(sys.stdin)
    result = (inspect_gateway if operation == "inspect" else stop_gateway)(Path(raw_home), request)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
