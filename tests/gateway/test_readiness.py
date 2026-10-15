from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from gateway.readiness import collect_runtime_readiness


def test_collect_runtime_readiness_reports_healthy_local_runtime(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  model: test/model\n",
        encoding="utf-8",
    )
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "gateway.readiness.shutil.disk_usage",
        lambda path: SimpleNamespace(total=1000, used=500, free=500),
    )

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {"telegram": {"state": "connected"}},
            "updated_at": "2026-07-09T00:00:00Z",
        },
        active_api_runs=2,
    )

    assert result["status"] == "ok"
    assert result["checks"]["state_db"]["status"] == "ok"
    assert result["checks"]["session_store"]["status"] == "ok"
    assert result["checks"]["config"]["status"] == "ok"
    assert result["checks"]["model"]["status"] == "ok"
    assert result["checks"]["gateway"]["status"] == "ok"
    assert result["checks"]["background_queues"]["active_api_runs"] == 2
    assert result["checks"]["disk"] == {
        "status": "ok", "used_percent": 50.0, "free_bytes": 500
    }


@pytest.mark.parametrize(
    ("used", "expected_status", "expected_percent"),
    [(899, "ok", 89.9), (900, "degraded", 90.0), (1000, "degraded", 100.0)],
)
def test_readiness_reports_disk_pressure_in_overall_status(
    tmp_path, monkeypatch, used, expected_status, expected_percent
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def disk_usage(path):
        assert path == tmp_path
        return SimpleNamespace(total=1000, used=used, free=1000 - used)

    monkeypatch.setattr("gateway.readiness.shutil.disk_usage", disk_usage)
    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={"gateway_state": "running"},
    )

    assert result["status"] == expected_status
    assert result["checks"]["disk"] == {
        "status": expected_status,
        "used_percent": expected_percent,
        "free_bytes": 1000 - used,
    }
    assert all(
        check["status"] == "ok"
        for name, check in result["checks"].items()
        if name != "disk"
    )


def test_readiness_degrades_when_disk_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def disk_usage(path):
        raise OSError(f"private filesystem failure at {path}")

    monkeypatch.setattr("gateway.readiness.shutil.disk_usage", disk_usage)
    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={"gateway_state": "running"},
    )

    assert result["status"] == "degraded"
    assert result["checks"]["disk"] == {"status": "degraded", "detail": "OSError"}


def test_collect_runtime_readiness_degrades_on_invalid_config_and_stopped_gateway(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: [unterminated", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="",
        runtime_status={"gateway_state": "stopped", "platforms": {}},
    )

    assert result["status"] == "degraded"
    assert result["checks"]["config"]["status"] == "degraded"
    assert result["checks"]["model"]["status"] == "degraded"
    assert result["checks"]["gateway"]["status"] == "degraded"
    # Readiness is diagnostic data, not an exception or a destructive repair.
    assert (home / "config.yaml").read_text(encoding="utf-8") == "model: [unterminated"


def test_readiness_uses_running_session_store_state_over_independent_probe(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    monkeypatch.setenv("HERMES_HOME", str(home))

    unavailable = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {},
            "session_store": {"status": "unavailable"},
        },
    )

    assert unavailable["checks"]["state_db"]["status"] == "ok"
    assert unavailable["checks"]["session_store"] == {"status": "unavailable"}
    assert unavailable["status"] == "degraded"

    recovered = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {},
            "session_store": {"status": "ok"},
        },
    )
    assert recovered["checks"]["session_store"] == {"status": "ok"}


