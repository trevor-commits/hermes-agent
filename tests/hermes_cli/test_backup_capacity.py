"""T7 admission must hold every backup writer without rotating recovery state."""
import json
import os
import sqlite3
import subprocess
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import backup
from hermes_cli import backup_sqlite as storage


@pytest.fixture
def destination(tmp_path, monkeypatch):
    volume = tmp_path / "T7"
    home = volume / "Offload" / "hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model: fixture\n")
    with sqlite3.connect(home / "state.db") as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('preserved')")
    state = SimpleNamespace(home=home, volume=volume, free=storage._T7_RESERVE_BYTES + 2 * 1024**3,
                            verified=True, root=str(volume), calls=[])

    def verify(command, **kwargs):
        state.calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "verified": state.verified, "root": state.root, "offload": str(volume / "Offload")}))

    monkeypatch.setattr(storage, "_T7_ROOT", volume)
    monkeypatch.setattr(storage.subprocess, "run", verify)
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=state.free))
    monkeypatch.setattr(backup, "get_hermes_home", lambda: home)
    monkeypatch.setattr(backup, "get_default_hermes_root", lambda: home)
    monkeypatch.setattr(backup, "_collect_external_entries", lambda: ([], []))
    monkeypatch.setattr(backup, "_sibling_profile_homes", lambda home: [])
    monkeypatch.setenv("HERMES_HOME", str(home))
    return state


def invoke(kind, home):
    if kind == "quick":
        return backup.create_quick_snapshot(hermes_home=home)
    if kind == "pre-update":
        return backup.create_pre_update_backup(hermes_home=home)
    if kind == "pre-migration":
        return backup.create_pre_migration_backup(hermes_home=home)
    if kind == "preflight":
        return storage.preflight_state_db(home)
    return backup.run_backup(Namespace(output=str(home / "output" / "full.zip")))


def held(kind, home):
    if kind == "full":
        with pytest.raises(SystemExit) as error:
            invoke(kind, home)
        assert error.value.code == 75
    elif kind == "preflight":
        with pytest.raises(storage.BackupCapacityError):
            invoke(kind, home)
    else:
        assert invoke(kind, home) is None


@pytest.mark.parametrize("kind", ["quick", "full", "pre-update", "pre-migration", "preflight"])
@pytest.mark.parametrize("reason", ["below-reserve", "unverified-volume", "wrong-volume"])
def test_hold_precedes_all_writes_and_pruning(destination, kind, reason):
    state = destination
    if reason == "below-reserve":
        state.free = storage._T7_RESERVE_BYTES - 1
    elif reason == "unverified-volume":
        state.verified = False
    else:
        state.root = "/wrong-volume"
    preserved = state.home / "backups"
    preserved.mkdir()
    for prefix in ("pre-update", "pre-migration"):
        for stamp in ("old", "older"):
            (preserved / f"{prefix}-{stamp}.incomplete.zip").write_bytes(b"salvage")
    before = {str(p.relative_to(state.home)): p.read_bytes() for p in state.home.rglob("*") if p.is_file()}
    held(kind, state.home)
    after = {str(p.relative_to(state.home)): p.read_bytes() for p in state.home.rglob("*") if p.is_file()}
    assert before == after
    assert not (state.home / ".backup.lock").exists()
    assert not (state.home / "state-snapshots").exists()
    assert not (state.home / "output").exists()


@pytest.mark.parametrize("kind", ["quick", "full", "pre-update", "pre-migration", "preflight"])
def test_adequate_capacity_allows_selected_backup(destination, kind):
    assert invoke(kind, destination.home)
    assert destination.calls
    assert (destination.home / ".backup.lock").exists()


@pytest.mark.parametrize("kind", ["quick", "full", "pre-update", "pre-migration", "preflight"])
def test_capacity_is_rechecked_after_lock(destination, monkeypatch, kind):
    original = storage._backup_operation_lock

    @contextmanager
    def lose_space(home, *args, **kwargs):
        with original(home, *args, **kwargs):
            destination.free = storage._T7_RESERVE_BYTES - 1
            yield

    monkeypatch.setattr(backup, "_backup_operation_lock", lose_space)
    monkeypatch.setattr(storage, "_backup_operation_lock", lose_space)
    held(kind, destination.home)
    assert not (destination.home / "state-snapshots").exists()
    assert not (destination.home / "backups").exists()
    assert not (destination.home / "output").exists()
    assert not list(destination.home.glob("*.partial"))
    assert not list(destination.home.glob("*.bak"))


def test_external_paths_are_discovered_under_lock_and_admitted_before_output(destination, monkeypatch):
    home = destination.home
    output = home / "output" / "full.zip"
    main_files = [path for path, _ in backup._iter_backup_files(home, output)]
    destination.free = storage._T7_RESERVE_BYTES + storage.estimate_backup_bytes(main_files, archive=True)
    external = destination.volume / "provider-state.json"
    external.write_bytes(b"x" * 100_000)
    calls = []

    def discover():
        assert (home / ".backup.lock").exists()
        calls.append(True)
        return [(external, "_external/provider-state.json")], []

    monkeypatch.setattr(backup, "_collect_external_entries", discover)
    held("full", home)
    assert calls == [True]
    assert not output.parent.exists()


def test_projected_aggregate_reserve_and_boundary(destination):
    destination.free = storage._T7_RESERVE_BYTES + 100
    storage.require_backup_capacity([(destination.home, 100)])
    with pytest.raises(storage.BackupCapacityError):
        storage.require_backup_capacity([(destination.home / "a", 60), (destination.home / "b", 60)])


def test_profile_fleet_is_admitted_before_first_snapshot(destination, monkeypatch):
    sibling = destination.volume / "Offload" / "sibling"
    sibling.mkdir()
    (sibling / "config.yaml").write_text("model: fixture\n")
    monkeypatch.setattr(backup, "_sibling_profile_homes", lambda home: [("sibling", sibling)])
    estimate = storage.estimate_backup_bytes([destination.home / rel for rel in backup._QUICK_STATE_FILES], quick=True)
    destination.free = storage._T7_RESERVE_BYTES + estimate + 1
    assert backup.create_quick_snapshot(label="pre-update") is None
    assert not (destination.home / ".backup.lock").exists()
    assert not (sibling / ".backup.lock").exists()


def test_volume_device_and_verifier_failures_hold(destination, monkeypatch):
    with pytest.raises(storage.BackupCapacityError):
        storage._verify_backup_volume(-1)

    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("disk-guard", 15)

    monkeypatch.setattr(storage.subprocess, "run", unavailable)
    held("quick", destination.home)
    assert not (destination.home / ".backup.lock").exists()


@pytest.mark.platforms("posix")
def test_volume_aliases_and_environment_overrides(destination, monkeypatch, tmp_path):
    alias = tmp_path / "home-alias"
    alias.symlink_to(destination.home, target_is_directory=True)
    monkeypatch.setenv("DISK_GUARD_T7_FREE_WARN_GIB", "0")
    monkeypatch.setenv("DG_T7_ROOT", str(tmp_path))
    storage.require_backup_capacity([(alias / "future", 1)])
    environment = destination.calls[-1][1]["env"]
    assert "DISK_GUARD_T7_FREE_WARN_GIB" not in environment
    assert "DG_T7_ROOT" not in environment
    escaped = destination.volume / "escape"
    escaped.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(storage.BackupCapacityError):
        storage.require_backup_capacity([(escaped / "future", 0)])


def test_wal_and_archive_staging_are_budgeted(destination):
    source = destination.home / "state.db"
    plain = storage.estimate_backup_bytes([source])
    wal = source.with_name(source.name + "-wal")
    wal.write_bytes(b"x" * 1024)
    with_wal = storage.estimate_backup_bytes([source])
    assert with_wal > plain + wal.stat().st_size
    assert storage.estimate_backup_bytes([source], archive=True) > 2 * with_wal - 32 * 1024**2


def test_local_raw_reserve_and_restore_copy(destination, tmp_path):
    destination.free = 0
    with pytest.raises(storage.BackupCapacityError):
        storage.require_backup_capacity([(tmp_path / "local-output", 100)])
    assert not destination.calls
    assert storage._safe_copy_db(destination.home / "state.db", tmp_path / "restore.db")
    with sqlite3.connect(tmp_path / "restore.db") as connection:
        assert connection.execute("SELECT value FROM sample").fetchall() == [("preserved",)]


@pytest.mark.parametrize("kind", ["quick", "full", "pre-update", "pre-migration", "preflight"])
@pytest.mark.parametrize("adequate", [False, True])
def test_local_writers_use_projected_fifteen_gib_floor(destination, tmp_path, monkeypatch, kind, adequate):
    home = tmp_path / "local-profile"
    home.mkdir()
    (home / "state.db").write_bytes((destination.home / "state.db").read_bytes())
    (home / "config.yaml").write_text("model: fixture\n")
    monkeypatch.setattr(backup, "get_hermes_home", lambda: home)
    monkeypatch.setattr(backup, "get_default_hermes_root", lambda: home)
    # The low case is above the floor, but cannot fit the staged database and reserve.
    destination.free = storage._LOCAL_RESERVE_BYTES + (1024**3 if adequate else 1024)
    if adequate:
        assert invoke(kind, home)
    else:
        held(kind, home)
        assert sorted(p.name for p in home.iterdir()) == ["config.yaml", "state.db"]
    assert not destination.calls


def test_local_aggregate_and_exact_boundary(destination, tmp_path):
    destination.free = storage._LOCAL_RESERVE_BYTES + 100
    storage.require_backup_capacity([(tmp_path / "a", 100)])
    with pytest.raises(storage.BackupCapacityError):
        storage.require_backup_capacity([(tmp_path / "a", 60), (tmp_path / "b", 60)])


@pytest.mark.parametrize("default_output", [False, True])
def test_t7_source_with_local_archive_checks_both_devices(destination, tmp_path, monkeypatch, default_output):
    original_stat = Path.stat

    def device_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == destination.volume or destination.volume in path.parents:
            fields = list(result)
            fields[2] = 700
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", device_stat)
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(
        free=storage._T7_RESERVE_BYTES + 1024**3 if destination.volume in path.parents or path == destination.volume
        else storage._LOCAL_RESERVE_BYTES + 1024))
    local_home = tmp_path / "local-home"
    local_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: local_home)
    output = local_home / "archive.zip"
    with pytest.raises(SystemExit) as error:
        backup.run_backup(Namespace(output=None if default_output else str(output)))
    assert error.value.code == 75
    assert destination.calls  # Exact T7 identity remains required for its lock write.
    assert not (destination.home / ".backup.lock").exists()
    assert list(local_home.iterdir()) == []


@pytest.mark.parametrize("limit", ["entries", "time"])
def test_estimate_limits_hold_before_backup_writes(destination, monkeypatch, limit):
    if limit == "entries":
        monkeypatch.setattr(storage, "_BACKUP_SCAN_MAX_ENTRIES", 1)
    else:
        monkeypatch.setattr(storage, "_BACKUP_SCAN_SECONDS", 0.0)
    assert backup.create_quick_snapshot(hermes_home=destination.home) is None
    assert not (destination.home / ".backup.lock").exists()
    assert not (destination.home / "state-snapshots").exists()


def test_incomplete_walk_and_missing_selected_source_fail_closed(destination, monkeypatch, tmp_path):
    def unreadable(path):
        raise PermissionError("fixture unreadable")

    monkeypatch.setattr(storage.os, "scandir", unreadable)
    with pytest.raises(storage.BackupCapacityError):
        list(backup._iter_backup_files(destination.home, tmp_path / "backup.zip"))
    with pytest.raises(storage.BackupCapacityError):
        storage.estimate_backup_bytes([tmp_path / "missing-selected-file"])
