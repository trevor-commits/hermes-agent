"""WAL-safe SQLite snapshots. Direct execution needs only the standard library.

Desktop invokes this file before stopping its backend, even when application
imports cannot load. Full and quick backups use the same SQLite copy operation.
"""
import json
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_T7_ROOT = Path("/Volumes/T7")
_DISK_GUARD = "/Users/gillettes/.local/bin/disk-guard"
_T7_RESERVE_BYTES = 200 * 1024**3
_LOCAL_RESERVE_BYTES = 15 * 1024**3
_BACKUP_SCAN_MAX_ENTRIES = 250_000
_BACKUP_SCAN_SECONDS = 15.0


class BackupCapacityError(RuntimeError):
    """Backup admission refused; existing recovery files must remain untouched."""


def _verify_backup_volume(device: int) -> None:
    """Reuse the installed exact-volume verifier without environment overrides."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("DISK_GUARD_", "DG_"))}
    try:
        result = subprocess.run([_DISK_GUARD, "t7-verify", "--json"],
                                capture_output=True, text=True, timeout=15, check=False,
                                env=environment)
        proof = json.loads(result.stdout)
        if (result.returncode or not isinstance(proof, dict)
                or proof.get("verified") is not True or proof.get("root") != str(_T7_ROOT)
                or proof.get("offload") != str(_T7_ROOT / "Offload")
                or _T7_ROOT.stat().st_dev != device):
            raise BackupCapacityError("exact T7 backup destination unavailable")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise BackupCapacityError("exact T7 backup destination unavailable") from exc


def require_backup_capacity(writes: list[tuple[Path, int]]) -> None:
    """Aggregate raw projected usage by destination device before any write."""
    groups = {}
    try:
        local_device = Path.home().stat().st_dev
        for path, estimate in writes:
            resolved = path.expanduser().resolve()
            lexical_t7 = path == _T7_ROOT or _T7_ROOT in path.parents
            resolved_t7 = resolved == _T7_ROOT or _T7_ROOT in resolved.parents
            if lexical_t7 and not resolved_t7:
                raise BackupCapacityError("T7 backup destination escaped its volume")
            while not resolved.exists():
                resolved = resolved.parent
            device = resolved.stat().st_dev
            anchor, total, on_t7 = groups.get(device, (resolved, 0, False))
            groups[device] = (anchor, total + estimate, on_t7 or resolved_t7)
        for device, (anchor, total, on_t7) in groups.items():
            if on_t7:
                _verify_backup_volume(device)
            floor = _T7_RESERVE_BYTES if on_t7 else _LOCAL_RESERVE_BYTES if device == local_device else 0
            if shutil.disk_usage(anchor).free - total < floor:
                raise BackupCapacityError(f"backup held: projected raw free space below {floor // 1024**3} GiB reserve")
    except OSError as exc:
        raise BackupCapacityError("backup capacity unavailable") from exc


def _bounded_backup_names(names, deadline):
    for name in names:
        if time.monotonic() >= deadline:
            raise BackupCapacityError("backup metadata scan exceeded its finite limit")
        yield name


def bounded_backup_walk(root: Path):
    """Prunable scandir walk with finite metadata work and no ignored read errors."""
    pending, count = [root], 0
    deadline = time.monotonic() + _BACKUP_SCAN_SECONDS
    try:
        while pending:
            directory = pending.pop()
            directories, files = [], []
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > _BACKUP_SCAN_MAX_ENTRIES or time.monotonic() >= deadline:
                        raise BackupCapacityError("backup metadata scan exceeded its finite limit")
                    (directories if entry.is_dir(follow_symlinks=False) else files).append(entry.name)
            if time.monotonic() >= deadline:
                raise BackupCapacityError("backup metadata scan exceeded its finite limit")
            yield directory, directories, _bounded_backup_names(files, deadline)
            pending.extend(directory / name for name in reversed(directories))
    except OSError as exc:
        raise BackupCapacityError("backup metadata scan incomplete") from exc


def _selected_backup_files(source: Path, quick: bool):
    try:
        info = source.stat()
    except FileNotFoundError:
        if quick:  # Absent optional quick-state paths are normal; selected full files are not.
            return
        raise
    if stat.S_ISDIR(info.st_mode):
        for directory, dirs, files in bounded_backup_walk(source):
            if quick:
                dirs[:] = [name for name in dirs if name not in ("workspaces", "attachments")]
            yield from (directory / name for name in files)
    else:
        yield source


def estimate_backup_bytes(paths, *, archive: bool = False, max_file_size=None, quick: bool = False) -> int:
    """Bound selected copies plus WAL, SQLite staging, and archive overhead."""
    total = largest_db = count = 0
    deadline = time.monotonic() + _BACKUP_SCAN_SECONDS
    try:
        for source in paths:
            count += 1
            if count > _BACKUP_SCAN_MAX_ENTRIES or time.monotonic() >= deadline:
                raise BackupCapacityError("backup size estimate exceeded its finite limit")
            for path in _selected_backup_files(source, quick):
                count += 1
                if count > _BACKUP_SCAN_MAX_ENTRIES or time.monotonic() >= deadline:
                    raise BackupCapacityError("backup size estimate exceeded its finite limit")
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                size = info.st_size
                if max_file_size is not None and size > max_file_size:
                    continue
                if path.suffix == ".db":
                    wal = path.with_name(path.name + "-wal")
                    with suppress(FileNotFoundError):
                        size += wal.stat().st_size
                    size = size * 115 // 100 + 64 * 1024**2
                    largest_db = max(largest_db, size)
                total += size + 1024
    except OSError as exc:
        raise BackupCapacityError("backup source size unavailable") from exc
    if time.monotonic() >= deadline:
        raise BackupCapacityError("backup size estimate exceeded its finite limit")
    return total * 101 // 100 + (largest_db if archive else 0) + 16 * 1024**2


class BackupInProgressError(RuntimeError):
    """Raised when another process already owns the Hermes backup slot."""


@contextmanager
def _backup_operation_lock(hermes_home: Path, timeout_seconds: float = 0.25):
    """Acquire one cross-process backup slot for full and quick snapshots."""
    lock_path = hermes_home / ".backup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    try:
        if os.name == "nt":
            import msvcrt
            if lock_path.stat().st_size == 0:
                handle.write(b" ")
                handle.flush()
            def _lock_op(flag: int) -> None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), flag, 1)
            lock_flag, unlock_flag = msvcrt.LK_NBLCK, msvcrt.LK_UNLCK
        else:
            import fcntl
            def _lock_op(flag: int) -> None:
                fcntl.flock(handle.fileno(), flag)
            lock_flag, unlock_flag = fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN
        while not acquired:
            try:
                _lock_op(lock_flag)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise BackupInProgressError("another Hermes backup is already running")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            with suppress(OSError):
                _lock_op(unlock_flag)
        handle.close()



class _SQLiteBackupTimeout(RuntimeError):
    """Raised when a SQLite snapshot remains busy past its deadline."""


def _close_quietly(conn: Optional[sqlite3.Connection]) -> None:
    if conn is not None:
        with suppress(Exception):
            conn.close()


def _safe_copy_db(src: Path, dst: Path, *, timeout_seconds: float = 10.0) -> bool:
    """Copy a SQLite database with the backup() API (WAL-safe consistent snapshot).

    Fails closed when no consistent snapshot can be made: copying only the main file loses WAL data.
    """
    conn = backup_conn = None
    try:
        # sqlite3.connect() creates a missing destination with the process
        # umask, which is commonly 0022 (0644).  Snapshot databases contain
        # session and tool state, so create the inode owner-only before SQLite
        # writes its first byte.  O_NOFOLLOW also refuses a planted symlink on
        # platforms that support it.  Tighten an existing internal staging
        # file as well (NamedTemporaryFile callers already create it 0600).
        if os.name != "nt":
            open_flags = os.O_WRONLY | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                open_flags |= os.O_NOFOLLOW
            secure_fd = os.open(dst, open_flags, 0o600)
            try:
                os.fchmod(secure_fd, 0o600)
            finally:
                os.close(secure_fd)
        # timeout=0.0 disables sqlite3's implicit busy wait so the progress callback owns the
        # full locked-source deadline instead of adding the default timeout before each callback.
        conn = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0)
        backup_conn = sqlite3.connect(str(dst))
        busy_deadline = time.monotonic() + max(0.0, timeout_seconds)

        def _check_backup_progress(status: int, _remaining: int, _total: int) -> None:
            nonlocal busy_deadline
            now = time.monotonic()
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                if now >= busy_deadline:
                    raise _SQLiteBackupTimeout(f"database remained locked for {timeout_seconds:g} seconds")
            else:
                busy_deadline = now + max(0.0, timeout_seconds)

        conn.backup(backup_conn, pages=256, progress=_check_backup_progress, sleep=0.1)
        return True
    except Exception as exc:
        logger.warning("SQLite safe copy failed for %s: %s", src, exc)
        # Windows won't remove the partial destination while SQLite still has it open.
        _close_quietly(backup_conn)
        backup_conn = None
        with suppress(OSError):
            dst.unlink(missing_ok=True)
        return False
    finally:
        _close_quietly(backup_conn)
        _close_quietly(conn)


def preflight_state_db(home: Path) -> dict:
    """Publish an emergency snapshot; do not prune recovery files on failure."""
    source = home / "state.db"
    if not source.exists():
        return {"path": None, "message": "state.db not found (fresh install?)"}
    require_backup_capacity([(home, estimate_backup_bytes([source]))])
    with _backup_operation_lock(home):
        require_backup_capacity([(home, estimate_backup_bytes([source]))])
        return _preflight_state_db_locked(home)


def _preflight_state_db_locked(home: Path) -> dict:
    source = home / "state.db"
    prefix = "state.db.pre-update-emergency-"
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    destination = home / f"{prefix}{stamp}-{os.getpid()}.bak"
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".partial", dir=home)
    os.close(fd)
    staged = Path(name)
    try:
        if not _safe_copy_db(source, staged):
            raise RuntimeError("SQLite safe copy failed; previous emergency snapshots were retained")
        connection = sqlite3.connect(str(staged))
        try:
            result = connection.execute("PRAGMA quick_check").fetchall()
            if result != [("ok",)]:
                raise RuntimeError(f"SQLite snapshot integrity check failed: {result}")
        finally:
            connection.close()
        size = staged.stat().st_size
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    for old in sorted(home.glob(f"{prefix}*.bak"), reverse=True)[2:]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("Could not prune emergency snapshot %s: %s", old, exc)
    return {"path": str(destination), "bytes": size}


if __name__ == "__main__":
    print(json.dumps(preflight_state_db(Path(sys.argv[1]))))
