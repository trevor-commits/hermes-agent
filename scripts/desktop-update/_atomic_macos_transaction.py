"""Durable receipts and atomic exchange/recovery for the macOS updater."""

from __future__ import annotations

import ctypes
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Dict, Optional, Union
import uuid


PathLike = Union[str, os.PathLike]

_RENAME_SWAP = 0x00000002
_RECEIPT_NAME = "receipt.json"
_TRANSACTION_LOCK_NAME = ".transaction.lock"
_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "failed_unchanged",
        "failed_rolled_back",
        "manual_recovery_required",
    }
)
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@"
)
_URL_QUERY_VALUE_RE = re.compile(r"(?P<prefix>[?&][^=?&#\s]+)=([^&#\s]*)")
_URL_FRAGMENT_RE = re.compile(r"#[^\s]*")
_BEARER_TOKEN_RE = re.compile(r"(?i)(?P<prefix>\bbearer\s+)[^\s,;]+")
_SCHEMELESS_PASSWORD_RE = re.compile(
    r"(?P<prefix>(?:^|[\s'\"(]))"
    r"(?P<username>[^/@\s:]+):(?P<secret>[^@/\s]+)@"
    r"(?P<host>[^/\s]+)"
)
_SCP_USERINFO_RE = re.compile(
    r"(?P<prefix>(?:^|[\s'\"(]))"
    r"(?P<userinfo>[A-Za-z0-9._%+-]+)@"
    r"(?P<host>[A-Za-z0-9.-]+:)"
)


class TransactionLockUnavailableError(RuntimeError):
    """The root transaction lock stayed busy for the caller's bounded wait."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _redact_known_secret_forms(value: str) -> str:
    def redact_userinfo(match: re.Match) -> str:
        userinfo = match.group("userinfo")
        if ":" in userinfo:
            username = userinfo.split(":", 1)[0]
            redacted = "{}:***".format(username)
        else:
            redacted = "***"
        return "{}{}@".format(match.group("scheme"), redacted)

    def redact_schemeless_password(match: re.Match) -> str:
        return "{}{}:***@{}".format(
            match.group("prefix"),
            match.group("username"),
            match.group("host"),
        )

    redacted = _URL_USERINFO_RE.sub(redact_userinfo, value)
    redacted = _SCHEMELESS_PASSWORD_RE.sub(redact_schemeless_password, redacted)
    redacted = _SCP_USERINFO_RE.sub(
        r"\g<prefix>***@\g<host>",
        redacted,
    )
    redacted = _URL_QUERY_VALUE_RE.sub(r"\g<prefix>=***", redacted)
    redacted = _URL_FRAGMENT_RE.sub("#***", redacted)
    return _BEARER_TOKEN_RE.sub(r"\g<prefix>***", redacted)


def _redact_sensitive_text(
    value: str,
    sensitive_values: tuple = (),
) -> str:
    for sensitive in sensitive_values:
        if not sensitive:
            continue
        safe_value = _redact_known_secret_forms(sensitive)
        if safe_value == sensitive:
            safe_value = "<redacted-upstream>"
        value = value.replace(sensitive, safe_value)
    return _redact_known_secret_forms(value)


def _sanitize_receipt_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if isinstance(value, dict):
        return {
            key: _sanitize_receipt_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_receipt_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_receipt_value(item) for item in value)
    return value


def validate_generated_leaf(leaf: str) -> str:
    """Reject anything except one caller-generated filesystem leaf."""
    if not isinstance(leaf, str):
        raise TypeError("generated leaf must be a string")
    if (
        not leaf
        or leaf in (".", "..")
        or "/" in leaf
        or "\\" in leaf
        or "\0" in leaf
        or os.path.isabs(leaf)
    ):
        raise ValueError("generated leaf must be one safe path component")
    return leaf


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _identity_tuple(file_stat: os.stat_result) -> tuple:
    return (file_stat.st_dev, file_stat.st_ino)


def _validated_directory_identity(
    path_stat: os.stat_result,
    opened_stat: os.stat_result,
    *,
    expected_uid: int,
    label: str,
) -> os.stat_result:
    if stat.S_ISLNK(path_stat.st_mode):
        raise ValueError("{} must not be a symlink".format(label))
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError("{} must be a directory".format(label))
    if path_stat.st_uid != expected_uid:
        raise PermissionError("{} has unexpected owner".format(label))
    if _identity_tuple(path_stat) != _identity_tuple(opened_stat):
        raise RuntimeError("{} identity changed while opening".format(label))
    if not stat.S_ISDIR(opened_stat.st_mode):
        raise ValueError("opened {} must be a directory".format(label))
    if opened_stat.st_uid != expected_uid:
        raise PermissionError("opened {} has unexpected owner".format(label))
    return path_stat


def _open_verified_directory(
    path: Path,
    *,
    owner_only: bool = False,
    expected_uid: Optional[int] = None,
) -> int:
    before = path.lstat()
    owner = os.geteuid() if expected_uid is None else expected_uid
    _validated_directory_identity(
        before,
        before,
        expected_uid=owner,
        label="directory {}".format(path),
    )
    if owner_only and stat.S_IMODE(before.st_mode) != 0o700:
        raise PermissionError(f"transaction directory must have mode 0700: {path}")

    descriptor = os.open(str(path), _directory_open_flags())
    try:
        after = os.fstat(descriptor)
        _validated_directory_identity(
            before,
            after,
            expected_uid=owner,
            label="directory {}".format(path),
        )
        if owner_only and stat.S_IMODE(after.st_mode) != 0o700:
            raise PermissionError(
                f"opened transaction directory must have mode 0700: {path}"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _canonical_transactions_root(path: PathLike) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _receipt_lock_transaction_id(data: Dict[str, Any]) -> str:
    lock_transaction_id = data.get("lock_transaction_id")
    if lock_transaction_id is None:
        recovery_of = data.get("recovery_of")
        if isinstance(recovery_of, dict):
            lock_transaction_id = recovery_of.get("transaction_id")
    if lock_transaction_id is None:
        lock_transaction_id = data.get("transaction_id")
    if not isinstance(lock_transaction_id, str):
        raise ValueError("transaction lock identity is invalid")
    return validate_generated_leaf(lock_transaction_id)


class _TransactionGuard:
    """One held advisory lock for a root transaction and its descendants."""

    def __init__(
        self,
        transactions_root: Path,
        transaction_id: str,
        descriptor: int,
    ) -> None:
        self.transactions_root = transactions_root
        self.transaction_id = transaction_id
        self.descriptor = descriptor
        self._active = True

    def require(self, transactions_root: PathLike, transaction_id: str) -> None:
        if not self._active:
            raise RuntimeError("transaction guard is no longer active")
        expected_root = _canonical_transactions_root(transactions_root)
        expected_id = validate_generated_leaf(transaction_id)
        if (
            expected_root != self.transactions_root
            or expected_id != self.transaction_id
        ):
            raise RuntimeError("transaction guard does not match the receipt lock root")

    def release(self) -> None:
        self._active = False


def _validate_transaction_lock(
    descriptor: int,
    directory_fd: int,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
        raise PermissionError("transaction lock must be an owner-owned regular file")
    if stat.S_IMODE(opened.st_mode) != 0o600:
        raise PermissionError("transaction lock must have mode 0600")
    current = os.stat(
        _TRANSACTION_LOCK_NAME,
        dir_fd=directory_fd,
        follow_symlinks=False,
    )
    if _identity_tuple(current) != _identity_tuple(opened):
        raise RuntimeError("transaction lock identity changed while opening")
    return opened


@contextmanager
def _transaction_guard(
    transactions_root: PathLike,
    transaction_id: str,
    *,
    lock_timeout_seconds: Optional[float] = None,
):
    if lock_timeout_seconds is not None:
        if isinstance(lock_timeout_seconds, bool):
            raise TypeError("transaction lock timeout must be a number")
        timeout = float(lock_timeout_seconds)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("transaction lock timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
    else:
        deadline = None
    root = _canonical_transactions_root(transactions_root)
    lock_transaction_id = validate_generated_leaf(transaction_id)
    transaction_dir = root / lock_transaction_id
    directory_fd = _open_verified_directory(transaction_dir, owner_only=True)
    base_flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    lock_fd: Optional[int] = None
    created = False
    locked = False
    guard: Optional[_TransactionGuard] = None
    try:
        try:
            lock_fd = os.open(
                _TRANSACTION_LOCK_NAME,
                base_flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            created = True
        except FileExistsError:
            lock_fd = os.open(
                _TRANSACTION_LOCK_NAME,
                base_flags,
                dir_fd=directory_fd,
            )
        _validate_transaction_lock(lock_fd, directory_fd)
        if created:
            os.fsync(lock_fd)
            os.fsync(directory_fd)
        attempted = False
        while True:
            if attempted and deadline is not None and time.monotonic() >= deadline:
                raise TransactionLockUnavailableError(
                    "transaction lock is temporarily unavailable"
                )
            attempted = True
            try:
                operation = fcntl.LOCK_EX
                if deadline is not None:
                    operation |= fcntl.LOCK_NB
                fcntl.flock(lock_fd, operation)
                locked = True
                break
            except InterruptedError:
                continue
            except OSError as error:
                if deadline is None or error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransactionLockUnavailableError(
                        "transaction lock is temporarily unavailable"
                    ) from error
                time.sleep(min(0.01, remaining))
        _validate_transaction_lock(lock_fd, directory_fd)
        guard = _TransactionGuard(root, lock_transaction_id, lock_fd)
        yield guard
    finally:
        if guard is not None:
            guard.release()
        try:
            if lock_fd is not None:
                try:
                    if locked:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        finally:
            os.close(directory_fd)


def _atomic_json_write(directory: Path, payload: Dict[str, Any]) -> Path:
    directory_fd = _open_verified_directory(directory, owner_only=True)
    temporary_leaf = ".receipt-{}.tmp".format(uuid.uuid4().hex)
    validate_generated_leaf(temporary_leaf)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    temporary_fd: Optional[int] = None
    try:
        temporary_fd = os.open(temporary_leaf, flags, 0o600, dir_fd=directory_fd)
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        view = memoryview(encoded)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise OSError("short write while persisting transaction receipt")
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(
            temporary_leaf,
            _RECEIPT_NAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except BaseException:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_leaf, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_fd)
    return directory / _RECEIPT_NAME


@contextmanager
def _receipt_write_guard(
    receipt,
    guard: Optional[_TransactionGuard] = None,
):
    transactions_root = receipt.transaction_dir.parent
    lock_transaction_id = _receipt_lock_transaction_id(receipt.data)
    if guard is not None:
        guard.require(transactions_root, lock_transaction_id)
        yield guard
        return
    with _transaction_guard(transactions_root, lock_transaction_id) as acquired:
        yield acquired


class TransactionReceipt:
    """Durable schema-v1 state for one updater transaction."""

    def __init__(
        self,
        transaction_dir: PathLike,
        data: Dict[str, Any],
        *,
        receipt_identity: Optional[tuple] = None,
        receipt_digest: Optional[str] = None,
    ) -> None:
        self.transaction_dir = Path(transaction_dir)
        self.path = self.transaction_dir / _RECEIPT_NAME
        self.data = data
        self._receipt_identity = receipt_identity
        self._receipt_digest = receipt_digest

    def _refresh_metadata(self) -> None:
        refreshed = load_transaction(self.transaction_dir)
        self._receipt_identity = refreshed._receipt_identity
        self._receipt_digest = refreshed._receipt_digest

    def _ensure_writable(self, guard: _TransactionGuard) -> None:
        guard.require(
            self.transaction_dir.parent,
            _receipt_lock_transaction_id(self.data),
        )
        current = load_transaction(self.transaction_dir)
        guard.require(
            current.transaction_dir.parent,
            _receipt_lock_transaction_id(current.data),
        )
        if current.is_terminal:
            raise RuntimeError("terminal transaction receipts are immutable")
        if (
            self._receipt_identity != current._receipt_identity
            or self._receipt_digest != current._receipt_digest
        ):
            raise RuntimeError("transaction receipt changed through another handle")

    @property
    def is_terminal(self) -> bool:
        return self.data.get("status") in _TERMINAL_STATUSES

    def record_phase(
        self,
        phase: str,
        *,
        _guard: Optional[_TransactionGuard] = None,
        **updates: Any,
    ) -> None:
        if not isinstance(phase, str) or not phase:
            raise ValueError("receipt phase must be a non-empty string")
        with _receipt_write_guard(self, _guard) as guard:
            if self.is_terminal:
                raise RuntimeError("terminal transaction receipts are immutable")
            self._ensure_writable(guard)
            proposed = dict(self.data)
            proposed.update(_sanitize_receipt_value(updates))
            proposed["phase"] = phase
            proposed["updated_at"] = _utc_now()
            proposed = _sanitize_receipt_value(proposed)
            guard.require(
                self.transaction_dir.parent,
                _receipt_lock_transaction_id(proposed),
            )
            _atomic_json_write(self.transaction_dir, proposed)
            self.data = proposed
            self._refresh_metadata()

    def finish(
        self,
        status: str,
        *,
        phase: str,
        failure_code: Optional[str] = None,
        failure_message: Optional[str] = None,
        _guard: Optional[_TransactionGuard] = None,
        **updates: Any,
    ) -> None:
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"unknown terminal status: {status}")
        if not isinstance(phase, str) or not phase:
            raise ValueError("receipt phase must be a non-empty string")
        with _receipt_write_guard(self, _guard) as guard:
            if self.is_terminal:
                raise RuntimeError("terminal transaction receipts are immutable")
            self._ensure_writable(guard)
            now = _utc_now()
            proposed = dict(self.data)
            proposed.update(_sanitize_receipt_value(updates))
            proposed.update(
                {
                    "status": status,
                    "phase": phase,
                    "ok": status == "succeeded",
                    "failure_code": failure_code,
                    "failure_message": failure_message,
                    "updated_at": now,
                    "completed_at": now,
                }
            )
            proposed = _sanitize_receipt_value(proposed)
            guard.require(
                self.transaction_dir.parent,
                _receipt_lock_transaction_id(proposed),
            )
            _atomic_json_write(self.transaction_dir, proposed)
            self.data = proposed
            self._refresh_metadata()


def create_transaction(
    transactions_root: PathLike,
    transaction_id: Optional[str] = None,
    *,
    lock_transaction_id: Optional[str] = None,
    _guard: Optional[_TransactionGuard] = None,
) -> TransactionReceipt:
    """Create an owner-only transaction directory and initial receipt."""
    root = _canonical_transactions_root(transactions_root)
    root.mkdir(parents=True, exist_ok=True)
    leaf = validate_generated_leaf(
        transaction_id or "txn-{}".format(uuid.uuid4().hex)
    )
    lock_leaf = validate_generated_leaf(lock_transaction_id or leaf)
    transaction_dir = root / leaf
    root_fd = _open_verified_directory(root)
    try:
        os.mkdir(leaf, 0o700, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)

    now = _utc_now()
    data: Dict[str, Any] = {
        "schema_version": 1,
        "transaction_id": leaf,
        "lock_transaction_id": lock_leaf,
        "status": "in_progress",
        "phase": "created",
        "ok": False,
        "switched": False,
        "rolled_back": False,
        "no_live_mutation": True,
        "identities": {},
        "failure_code": None,
        "failure_message": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    if _guard is not None:
        _guard.require(root, lock_leaf)
        _atomic_json_write(transaction_dir, data)
    else:
        with _transaction_guard(root, lock_leaf):
            _atomic_json_write(transaction_dir, data)
    return load_transaction(transaction_dir)


def load_transaction(transaction_dir: PathLike) -> TransactionReceipt:
    """Load and validate an existing schema-v1 transaction receipt."""
    directory = Path(transaction_dir)
    directory_fd = _open_verified_directory(directory, owner_only=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    receipt_fd: Optional[int] = None
    try:
        before = os.stat(
            _RECEIPT_NAME,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            raise PermissionError("transaction receipt must be an owner-owned regular file")
        if before.st_mode & 0o077:
            raise PermissionError("transaction receipt must be owner-only")
        receipt_fd = os.open(_RECEIPT_NAME, flags, dir_fd=directory_fd)
        after = os.fstat(receipt_fd)
        if _identity_tuple(before) != _identity_tuple(after):
            raise RuntimeError("transaction receipt identity changed while opening")
        with os.fdopen(receipt_fd, "rb") as handle:
            receipt_fd = None
            encoded = handle.read()
            data = json.loads(encoded.decode("utf-8"))
    finally:
        if receipt_fd is not None:
            os.close(receipt_fd)
        os.close(directory_fd)

    if data.get("schema_version") != 1:
        raise ValueError("unsupported transaction receipt schema")
    required = {
        "transaction_id",
        "status",
        "phase",
        "ok",
        "switched",
        "rolled_back",
        "no_live_mutation",
        "identities",
        "failure_code",
        "failure_message",
        "created_at",
        "updated_at",
        "completed_at",
    }
    missing = sorted(required.difference(data))
    if missing:
        raise ValueError("transaction receipt is missing fields: {}".format(missing))
    return TransactionReceipt(
        directory,
        data,
        receipt_identity=_identity_tuple(after),
        receipt_digest=hashlib.sha256(encoded).hexdigest(),
    )


def _validated_endpoint_identity(
    path_stat: os.stat_result,
    opened_stat: os.stat_result,
    *,
    expected_uid: int,
    label: str,
) -> os.stat_result:
    if stat.S_ISLNK(path_stat.st_mode):
        raise ValueError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError(f"{label} must be a directory")
    if path_stat.st_uid != expected_uid:
        raise PermissionError(f"{label} has unexpected owner")
    if _identity_tuple(path_stat) != _identity_tuple(opened_stat):
        raise RuntimeError(f"{label} identity changed while opening")
    if not stat.S_ISDIR(opened_stat.st_mode):
        raise ValueError(f"opened {label} must be a directory")
    if opened_stat.st_uid != expected_uid:
        raise PermissionError(f"opened {label} has unexpected owner")
    return path_stat


def _require_same_device(
    left_stat: os.stat_result,
    right_stat: os.stat_result,
) -> None:
    if left_stat.st_dev != right_stat.st_dev:
        raise OSError(
            errno.EXDEV,
            "atomic exchange endpoints must be on the same device",
        )


def _stat_identity(
    file_stat: os.stat_result,
    *,
    path: Optional[Path] = None,
    leaf: Optional[str] = None,
) -> Dict[str, Any]:
    identity: Dict[str, Any] = {
        "st_dev": file_stat.st_dev,
        "st_ino": file_stat.st_ino,
        "st_uid": file_stat.st_uid,
        "st_mode": stat.S_IMODE(file_stat.st_mode),
        "type": "directory" if stat.S_ISDIR(file_stat.st_mode) else "other",
    }
    if path is not None:
        identity["path"] = str(path)
    if leaf is not None:
        identity["leaf"] = leaf
    return identity


def _open_endpoint(
    parent_fd: int,
    leaf: str,
    *,
    expected_uid: int,
    label: str,
) -> tuple:
    validate_generated_leaf(leaf)
    before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    _validated_endpoint_identity(
        before,
        before,
        expected_uid=expected_uid,
        label=label,
    )
    descriptor = os.open(leaf, _directory_open_flags(), dir_fd=parent_fd)
    try:
        after = os.fstat(descriptor)
        _validated_endpoint_identity(
            before,
            after,
            expected_uid=expected_uid,
            label=label,
        )
        return descriptor, before
    except BaseException:
        os.close(descriptor)
        raise


def _observe_endpoint(
    parent_fd: int,
    leaf: str,
    *,
    expected_uid: int,
    label: str,
) -> os.stat_result:
    descriptor, observed = _open_endpoint(
        parent_fd,
        leaf,
        expected_uid=expected_uid,
        label=label,
    )
    os.close(descriptor)
    return observed


def _load_rename_swap():
    if sys.platform != "darwin":
        raise OSError("atomic path exchange is supported only on macOS")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        raise RuntimeError("renameatx_np is unavailable; refusing non-atomic fallback")
    renameatx_np.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameatx_np.restype = ctypes.c_int
    return renameatx_np


def _rename_swap_at(
    left_parent_fd: int,
    left_leaf: str,
    right_parent_fd: int,
    right_leaf: str,
) -> None:
    renameatx_np = _load_rename_swap()
    ctypes.set_errno(0)
    result = renameatx_np(
        left_parent_fd,
        os.fsencode(validate_generated_leaf(left_leaf)),
        right_parent_fd,
        os.fsencode(validate_generated_leaf(right_leaf)),
        _RENAME_SWAP,
    )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(
            error_number,
            "atomic rename swap failed: {}".format(os.strerror(error_number)),
        )


def _mapping_kind(
    left_current: os.stat_result,
    right_current: os.stat_result,
    left_original: os.stat_result,
    right_original: os.stat_result,
) -> str:
    current = (_identity_tuple(left_current), _identity_tuple(right_current))
    original = (_identity_tuple(left_original), _identity_tuple(right_original))
    transposed = (original[1], original[0])
    if current == original:
        return "original"
    if current == transposed:
        return "transposed"
    return "ambiguous"


def _fsync_parents(*parent_fds: int) -> None:
    seen = set()
    for parent_fd in parent_fds:
        identity = _identity_tuple(os.fstat(parent_fd))
        if identity in seen:
            continue
        os.fsync(parent_fd)
        seen.add(identity)


def _default_receipt(left_parent: Path) -> TransactionReceipt:
    root = left_parent / ".hermes-update-transactions"
    return create_transaction(root)


def _expected_owner_pair(
    owners: Optional[tuple],
    *,
    default_uid: int,
    label: str,
) -> tuple:
    if owners is None:
        return (default_uid, default_uid)
    if (
        not isinstance(owners, tuple)
        or len(owners) != 2
        or not all(isinstance(owner, int) and owner >= 0 for owner in owners)
    ):
        raise ValueError("{} must be a pair of non-negative user ids".format(label))
    return owners


def _finish_preflight_failure(
    receipt: Optional[TransactionReceipt],
    error: BaseException,
) -> None:
    if receipt is None or receipt.is_terminal:
        return
    receipt.finish(
        "failed_unchanged",
        phase="exchange_preflight_failed",
        switched=False,
        rolled_back=False,
        no_live_mutation=True,
        failure_code="exchange_precondition",
        failure_message=str(error),
    )


def _stored_identity(identity: Dict[str, Any]) -> tuple:
    return (int(identity["st_dev"]), int(identity["st_ino"]))


def _finish_manual_recovery(
    receipt: TransactionReceipt,
    *,
    phase: str,
    failure_code: str,
    failure_message: str,
    identities: Optional[Dict[str, Any]] = None,
    exchanges: Optional[Dict[str, Any]] = None,
    finish: bool = True,
    _guard: Optional[_TransactionGuard] = None,
) -> TransactionReceipt:
    updates: Dict[str, Any] = {
        "switched": bool(receipt.data.get("switched")),
        "rolled_back": False,
        "no_live_mutation": False,
    }
    if identities is not None:
        updates["identities"] = identities
    if exchanges is not None:
        updates["exchanges"] = exchanges
    if finish:
        receipt.finish(
            "manual_recovery_required",
            phase=phase,
            failure_code=failure_code,
            failure_message=failure_message,
            _guard=_guard,
            **updates,
        )
    else:
        receipt.record_phase(
            phase,
            failure_code=failure_code,
            failure_message=failure_message,
            _guard=_guard,
            **updates,
        )
    return receipt


def _aggregate_exchange_state(exchanges: Dict[str, Any]) -> Dict[str, bool]:
    records = [record for record in exchanges.values() if isinstance(record, dict)]
    mutated = [
        record
        for record in records
        if bool(record.get("switched")) or bool(record.get("verified"))
    ]
    unresolved = [
        record
        for record in records
        if bool(record.get("manual_recovery_required"))
    ]
    switched = bool(mutated)
    rolled_back = not unresolved and (
        (
            bool(mutated)
            and all(bool(record.get("rolled_back")) for record in mutated)
        )
        or (
            not mutated
            and any(bool(record.get("rolled_back")) for record in records)
        )
    )
    return {
        "switched": switched,
        "rolled_back": rolled_back,
        "no_live_mutation": not switched and not unresolved,
    }


def _persist_exchange_failure(
    receipt: TransactionReceipt,
    *,
    resource: str,
    exchanges: Dict[str, Any],
    phase: str,
    failure_code: str,
    failure_message: str,
    manual_recovery_required: bool,
    finish: bool,
    _guard: Optional[_TransactionGuard] = None,
) -> None:
    selected = dict(exchanges.get(resource, {}))
    selected.update(
        {
            "failure_code": failure_code,
            "failure_message": failure_message,
            "manual_recovery_required": manual_recovery_required,
        }
    )
    exchanges[resource] = selected
    aggregate = _aggregate_exchange_state(exchanges)
    if finish:
        receipt.finish(
            "manual_recovery_required",
            phase=phase,
            failure_code=failure_code,
            failure_message=failure_message,
            exchanges=exchanges,
            _guard=_guard,
            **aggregate,
        )
    else:
        receipt.record_phase(
            phase,
            failure_code=failure_code,
            failure_message=failure_message,
            exchanges=exchanges,
            _guard=_guard,
            **aggregate,
        )


def _recovery_phase(resource: str, suffix: str, finish: bool) -> str:
    if finish and resource == "standalone":
        return "recovery_{}".format(suffix)
    return "{}_recovery_{}".format(resource, suffix)


def _persist_verified_recovery(
    receipt: TransactionReceipt,
    *,
    resource: str,
    exchanges: Dict[str, Any],
    identities: Dict[str, Any],
    suffix: str,
    failure_code: str,
    failure_message: str,
    finish: bool,
    _guard: Optional[_TransactionGuard] = None,
) -> TransactionReceipt:
    aggregate = _aggregate_exchange_state(exchanges)
    phase = _recovery_phase(resource, suffix, finish)
    updates: Dict[str, Any] = {
        "exchanges": exchanges,
        "identities": identities,
        "switched": aggregate["switched"],
        "rolled_back": aggregate["rolled_back"],
        "no_live_mutation": aggregate["no_live_mutation"],
    }
    if finish:
        receipt.finish(
            "failed_rolled_back",
            phase=phase,
            failure_code=failure_code,
            failure_message=failure_message,
            _guard=_guard,
            **updates,
        )
    else:
        receipt.record_phase(phase, _guard=_guard, **updates)
    return receipt


def _receipt_reference(receipt: TransactionReceipt) -> Dict[str, Any]:
    if receipt._receipt_identity is None or receipt._receipt_digest is None:
        raise RuntimeError("transaction receipt identity is unavailable")
    return {
        "transaction_id": receipt.data["transaction_id"],
        "receipt_path": str(receipt.path),
        "receipt_st_dev": receipt._receipt_identity[0],
        "receipt_st_ino": receipt._receipt_identity[1],
        "receipt_sha256": receipt._receipt_digest,
    }


def _validate_original_receipt_reference(
    receipt: TransactionReceipt,
    reference: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        transaction_id = validate_generated_leaf(str(reference["transaction_id"]))
        receipt_path = Path(reference["receipt_path"])
        expected_identity = (
            int(reference["receipt_st_dev"]),
            int(reference["receipt_st_ino"]),
        )
        expected_digest = str(reference["receipt_sha256"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("original recovery receipt reference is invalid") from error
    expected_path = receipt.transaction_dir.parent / transaction_id / _RECEIPT_NAME
    if receipt_path != expected_path:
        raise ValueError("original recovery receipt path leaves the transaction root")
    original = load_transaction(receipt_path.parent)
    if original.data["transaction_id"] != transaction_id:
        raise RuntimeError("original recovery transaction identity changed")
    if original._receipt_identity != expected_identity:
        raise RuntimeError("original recovery receipt identity changed")
    if original._receipt_digest != expected_digest:
        raise RuntimeError("original recovery receipt contents changed")
    return dict(reference)


def _validated_recorded_exchanges(
    receipt: TransactionReceipt,
    resource: str,
) -> Dict[str, Any]:
    exchanges = receipt.data.get("exchanges")
    if not isinstance(exchanges, dict) or resource not in exchanges:
        raise ValueError("receipt has no recorded exchange named {!r}".format(resource))
    if not isinstance(exchanges[resource], dict):
        raise ValueError("recorded exchange {!r} is invalid".format(resource))
    return exchanges


def recover_recorded_exchanges(
    transaction_dir: PathLike,
    resources,
    *,
    lock_timeout_seconds: Optional[float] = None,
    _rename_swap_command=None,
    _mapping_kind_command=None,
):
    """Recover ordered receipt resources while holding one root transaction guard."""
    if not isinstance(resources, (list, tuple)) or not resources:
        raise ValueError("recovery resources must be a non-empty ordered sequence")
    ordered_resources = [validate_generated_leaf(resource) for resource in resources]
    if len(set(ordered_resources)) != len(ordered_resources):
        raise ValueError("recovery resources must not contain duplicates")

    preliminary = load_transaction(transaction_dir)
    lock_transaction_id = _receipt_lock_transaction_id(preliminary.data)
    current_dir = preliminary.transaction_dir
    processed = []
    receipt = preliminary
    with _transaction_guard(
        preliminary.transaction_dir.parent,
        lock_transaction_id,
        lock_timeout_seconds=lock_timeout_seconds,
    ) as guard:
        for index, resource in enumerate(ordered_resources):
            receipt = _recover_exchange_locked(
                current_dir,
                resource=resource,
                finish=index == len(ordered_resources) - 1,
                _rename_swap_command=_rename_swap_command,
                _mapping_kind_command=_mapping_kind_command,
                _guard=guard,
            )
            current_dir = receipt.transaction_dir
            processed.append(resource)
            if receipt.data.get("status") == "manual_recovery_required":
                break
            if receipt.is_terminal and index != len(ordered_resources) - 1:
                break
    return receipt, processed


def recover_exchange(
    transaction_dir: PathLike,
    *,
    resource: str = "standalone",
    finish: bool = True,
    lock_timeout_seconds: Optional[float] = None,
    _rename_swap_command=None,
    _mapping_kind_command=None,
) -> TransactionReceipt:
    """Recover one exact receipt-recorded exchange without guessing."""
    preliminary = load_transaction(transaction_dir)
    lock_transaction_id = _receipt_lock_transaction_id(preliminary.data)
    with _transaction_guard(
        preliminary.transaction_dir.parent,
        lock_transaction_id,
        lock_timeout_seconds=lock_timeout_seconds,
    ) as guard:
        return _recover_exchange_locked(
            transaction_dir,
            resource=resource,
            finish=finish,
            _rename_swap_command=_rename_swap_command,
            _mapping_kind_command=_mapping_kind_command,
            _guard=guard,
        )


def _recover_exchange_locked(
    transaction_dir: PathLike,
    *,
    resource: str,
    finish: bool,
    _rename_swap_command,
    _mapping_kind_command,
    _guard: _TransactionGuard,
) -> TransactionReceipt:
    rename_swap = (
        _rename_swap_at if _rename_swap_command is None else _rename_swap_command
    )
    mapping_kind = (
        _mapping_kind if _mapping_kind_command is None else _mapping_kind_command
    )
    receipt = load_transaction(transaction_dir)
    _guard.require(
        receipt.transaction_dir.parent,
        _receipt_lock_transaction_id(receipt.data),
    )
    resource = validate_generated_leaf(resource)
    recorded_root_reference = receipt.data.get("recovery_of")
    if recorded_root_reference is not None:
        if not isinstance(recorded_root_reference, dict):
            raise ValueError("original recovery receipt reference is invalid")
        _validate_original_receipt_reference(receipt, recorded_root_reference)
    if receipt.is_terminal:
        if receipt.data.get("status") != "manual_recovery_required":
            return receipt
        original_exchanges = _validated_recorded_exchanges(receipt, resource)
        original = receipt
        parent_reference = _receipt_reference(original)
        if recorded_root_reference is None:
            recovery_of = dict(parent_reference)
        elif isinstance(recorded_root_reference, dict):
            recovery_of = _validate_original_receipt_reference(
                original,
                recorded_root_reference,
            )
        else:
            raise ValueError("original recovery receipt reference is invalid")
        attempt = create_transaction(
            original.transaction_dir.parent,
            "recovery-{}".format(uuid.uuid4().hex),
            lock_transaction_id=_guard.transaction_id,
            _guard=_guard,
        )
        attempt.record_phase(
            "recovery_attempt_created",
            exchanges=copy.deepcopy(original_exchanges),
            identities={},
            switched=bool(original.data.get("switched")),
            rolled_back=False,
            no_live_mutation=False,
            recovery_of=recovery_of,
            recovery_parent=parent_reference,
            _guard=_guard,
        )
        receipt = attempt

    exchanges = _validated_recorded_exchanges(receipt, resource)
    selected = exchanges[resource]
    identities = {
        "parents": selected.get("parents"),
        "before": selected.get("before"),
        "after": selected.get("after", {}),
    }
    parents = identities["parents"]
    before = identities["before"]
    if not isinstance(parents, dict) or not isinstance(before, dict):
        return _finish_manual_recovery(
            receipt,
            phase=_recovery_phase(resource, "receipt_invalid", finish),
            failure_code="invalid_recovery_receipt",
            failure_message="receipt identity map is incomplete",
            identities=identities,
            exchanges=exchanges,
            _guard=_guard,
        )

    try:
        left_parent_record = parents["left"]
        right_parent_record = parents["right"]
        left_original_record = before["left"]
        right_original_record = before["right"]
        left_parent_path = Path(left_parent_record["path"])
        right_parent_path = Path(right_parent_record["path"])
        left_leaf = validate_generated_leaf(left_original_record["leaf"])
        right_leaf = validate_generated_leaf(right_original_record["leaf"])
        left_parent_owner = int(left_parent_record["st_uid"])
        right_parent_owner = int(right_parent_record["st_uid"])
        left_endpoint_owner = int(left_original_record["st_uid"])
        right_endpoint_owner = int(right_original_record["st_uid"])
        if Path(left_original_record["path"]) != left_parent_path / left_leaf:
            raise ValueError("left recovery role does not match its parent and leaf")
        if Path(right_original_record["path"]) != right_parent_path / right_leaf:
            raise ValueError("right recovery role does not match its parent and leaf")
    except (KeyError, TypeError, ValueError) as error:
        return _finish_manual_recovery(
            receipt,
            phase=_recovery_phase(resource, "receipt_invalid", finish),
            failure_code="invalid_recovery_receipt",
            failure_message=str(error),
            identities=identities,
            exchanges=exchanges,
            _guard=_guard,
        )

    descriptors = []
    try:
        left_parent_fd = _open_verified_directory(
            left_parent_path,
            expected_uid=left_parent_owner,
        )
        descriptors.append(left_parent_fd)
        right_parent_fd = _open_verified_directory(
            right_parent_path,
            expected_uid=right_parent_owner,
        )
        descriptors.append(right_parent_fd)
        if _identity_tuple(os.fstat(left_parent_fd)) != _stored_identity(
            left_parent_record
        ):
            raise RuntimeError("left parent identity changed since exchange intent")
        if _identity_tuple(os.fstat(right_parent_fd)) != _stored_identity(
            right_parent_record
        ):
            raise RuntimeError("right parent identity changed since exchange intent")

        left_current = _observe_endpoint(
            left_parent_fd,
            left_leaf,
            expected_uid=left_endpoint_owner,
            label="left recovery endpoint",
        )
        right_current = _observe_endpoint(
            right_parent_fd,
            right_leaf,
            expected_uid=right_endpoint_owner,
            label="right recovery endpoint",
        )
    except (OSError, RuntimeError, ValueError) as error:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not finish:
            _persist_exchange_failure(
                receipt,
                resource=resource,
                exchanges=exchanges,
                phase=_recovery_phase(resource, "identity_unavailable", finish),
                failure_code="recovery_identity_unavailable",
                failure_message=str(error),
                manual_recovery_required=True,
                finish=False,
                _guard=_guard,
            )
            return receipt
        return _finish_manual_recovery(
            receipt,
            phase=_recovery_phase(resource, "identity_unavailable", finish),
            failure_code="recovery_identity_unavailable",
            failure_message=str(error),
            identities=identities,
            exchanges=exchanges,
            _guard=_guard,
        )

    try:
        left_original = os.stat_result(
            (
                stat.S_IFDIR | int(left_original_record["st_mode"]),
                int(left_original_record["st_ino"]),
                int(left_original_record["st_dev"]),
                1,
                int(left_original_record["st_uid"]),
                0,
                0,
                0,
                0,
                0,
            )
        )
        right_original = os.stat_result(
            (
                stat.S_IFDIR | int(right_original_record["st_mode"]),
                int(right_original_record["st_ino"]),
                int(right_original_record["st_dev"]),
                1,
                int(right_original_record["st_uid"]),
                0,
                0,
                0,
                0,
                0,
            )
        )
        mapping = mapping_kind(
            left_current,
            right_current,
            left_original,
            right_original,
        )
        identities["recovery_observed"] = {
            "left": _stat_identity(
                left_current,
                path=left_parent_path / left_leaf,
                leaf=left_leaf,
            ),
            "right": _stat_identity(
                right_current,
                path=right_parent_path / right_leaf,
                leaf=right_leaf,
            ),
        }
        selected = dict(selected)
        selected["recovery_observed"] = identities["recovery_observed"]

        if mapping == "original":
            selected.update(
                {
                    "recovery_mapping": "original",
                    "rolled_back": True,
                    "switched": bool(selected.get("verified")),
                    "manual_recovery_required": False,
                }
            )
            exchanges[resource] = selected
            return _persist_verified_recovery(
                receipt,
                resource=resource,
                exchanges=exchanges,
                identities=identities,
                suffix="verified_original",
                failure_code="recovered_original_mapping",
                failure_message="original endpoint mapping already present",
                finish=finish,
                _guard=_guard,
            )

        if mapping != "transposed":
            selected.update(
                {
                    "recovery_mapping": "ambiguous",
                    "rolled_back": False,
                }
            )
            exchanges[resource] = selected
            return _finish_manual_recovery(
                receipt,
                phase=_recovery_phase(resource, "mapping_ambiguous", finish),
                failure_code="ambiguous_exchange_mapping",
                failure_message="endpoint identities match neither known mapping",
                identities=identities,
                exchanges=exchanges,
                _guard=_guard,
            )

        selected.update(
            {
                "recovery_mapping": "transposed",
                "switched": True,
                "rolled_back": False,
            }
        )
        exchanges[resource] = selected
        aggregate = _aggregate_exchange_state(exchanges)
        receipt.record_phase(
            _recovery_phase(resource, "intent", False),
            switched=aggregate["switched"],
            rolled_back=aggregate["rolled_back"],
            no_live_mutation=aggregate["no_live_mutation"],
            identities=identities,
            exchanges=exchanges,
            _guard=_guard,
        )
        try:
            rename_swap(
                left_parent_fd,
                left_leaf,
                right_parent_fd,
                right_leaf,
            )
        except OSError as error:
            if not finish:
                _persist_exchange_failure(
                    receipt,
                    resource=resource,
                    exchanges=exchanges,
                    phase=_recovery_phase(resource, "exchange_failed", finish),
                    failure_code="recovery_exchange_failed",
                    failure_message=str(error),
                    manual_recovery_required=True,
                    finish=False,
                    _guard=_guard,
                )
                return receipt
            return _finish_manual_recovery(
                receipt,
                phase=_recovery_phase(resource, "exchange_failed", finish),
                failure_code="recovery_exchange_failed",
                failure_message=str(error),
                identities=identities,
                exchanges=exchanges,
                _guard=_guard,
            )

        left_restored = _observe_endpoint(
            left_parent_fd,
            left_leaf,
            expected_uid=left_endpoint_owner,
            label="left recovery endpoint",
        )
        right_restored = _observe_endpoint(
            right_parent_fd,
            right_leaf,
            expected_uid=right_endpoint_owner,
            label="right recovery endpoint",
        )
        identities["recovery_after"] = {
            "left": _stat_identity(
                left_restored,
                path=left_parent_path / left_leaf,
                leaf=left_leaf,
            ),
            "right": _stat_identity(
                right_restored,
                path=right_parent_path / right_leaf,
                leaf=right_leaf,
            ),
        }
        selected["recovery_after"] = identities["recovery_after"]
        exchanges[resource] = selected
        if (
            mapping_kind(
                left_restored,
                right_restored,
                left_original,
                right_original,
            )
            != "original"
        ):
            if not finish:
                _persist_exchange_failure(
                    receipt,
                    resource=resource,
                    exchanges=exchanges,
                    phase=_recovery_phase(resource, "rollback_unverified", finish),
                    failure_code="rollback_verification_failed",
                    failure_message="rollback did not restore the exact original mapping",
                    manual_recovery_required=True,
                    finish=False,
                    _guard=_guard,
                )
                return receipt
            return _finish_manual_recovery(
                receipt,
                phase=_recovery_phase(resource, "rollback_unverified", finish),
                failure_code="rollback_verification_failed",
                failure_message="rollback did not restore the exact original mapping",
                identities=identities,
                exchanges=exchanges,
                _guard=_guard,
            )

        _fsync_parents(left_parent_fd, right_parent_fd)
        selected.update(
            {
                "rolled_back": True,
                "switched": True,
                "manual_recovery_required": False,
            }
        )
        exchanges[resource] = selected
        return _persist_verified_recovery(
            receipt,
            resource=resource,
            exchanges=exchanges,
            identities=identities,
            suffix="verified_rollback",
            failure_code="recovered_transposed_mapping",
            failure_message="exact transposition was exchanged once back",
            finish=finish,
            _guard=_guard,
        )
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def atomic_exchange(
    left: PathLike,
    right: PathLike,
    *,
    receipt: Optional[TransactionReceipt] = None,
    expected_uid: Optional[int] = None,
    expected_parent_uids: Optional[tuple] = None,
    expected_endpoint_uids: Optional[tuple] = None,
    resource: str = "standalone",
    finish_on_success: bool = True,
    _rename_swap_command=None,
    _mapping_kind_command=None,
) -> TransactionReceipt:
    """Safely exchange two directories and durably verify the transposition."""
    if sys.platform != "darwin":
        raise OSError("atomic path exchange is supported only on macOS")

    rename_swap = (
        _rename_swap_at if _rename_swap_command is None else _rename_swap_command
    )
    mapping_kind = (
        _mapping_kind if _mapping_kind_command is None else _mapping_kind_command
    )

    owner = os.geteuid() if expected_uid is None else expected_uid
    parent_owners = _expected_owner_pair(
        expected_parent_uids,
        default_uid=owner,
        label="expected_parent_uids",
    )
    endpoint_owners = _expected_owner_pair(
        expected_endpoint_uids,
        default_uid=owner,
        label="expected_endpoint_uids",
    )
    left_path = Path(os.path.abspath(os.fspath(left)))
    right_path = Path(os.path.abspath(os.fspath(right)))
    left_leaf = validate_generated_leaf(left_path.name)
    right_leaf = validate_generated_leaf(right_path.name)
    resource = validate_generated_leaf(resource)
    if receipt is not None and receipt.is_terminal:
        raise RuntimeError("cannot exchange using a terminal transaction receipt")

    descriptors = []
    try:
        left_parent_fd = _open_verified_directory(
            left_path.parent,
            expected_uid=parent_owners[0],
        )
        descriptors.append(left_parent_fd)
        right_parent_fd = _open_verified_directory(
            right_path.parent,
            expected_uid=parent_owners[1],
        )
        descriptors.append(right_parent_fd)
        left_endpoint_fd, left_original = _open_endpoint(
            left_parent_fd,
            left_leaf,
            expected_uid=endpoint_owners[0],
            label="left exchange endpoint",
        )
        descriptors.append(left_endpoint_fd)
        right_endpoint_fd, right_original = _open_endpoint(
            right_parent_fd,
            right_leaf,
            expected_uid=endpoint_owners[1],
            label="right exchange endpoint",
        )
        descriptors.append(right_endpoint_fd)
        _require_same_device(left_original, right_original)
        _require_same_device(os.fstat(left_parent_fd), left_original)
        _require_same_device(os.fstat(right_parent_fd), right_original)
    except BaseException as error:
        if receipt is not None and not finish_on_success:
            exchanges = dict(receipt.data.get("exchanges", {}))
            exchanges[resource] = {
                "verified": False,
                "switched": False,
                "rolled_back": False,
                "parents": {},
                "before": {},
                "after": {},
                "roles": {
                    "left": str(left_path),
                    "right": str(right_path),
                },
            }
            _persist_exchange_failure(
                receipt,
                resource=resource,
                exchanges=exchanges,
                phase="{}_exchange_preflight_failed".format(resource),
                failure_code="exchange_precondition",
                failure_message=str(error),
                manual_recovery_required=False,
                finish=False,
            )
        else:
            _finish_preflight_failure(receipt, error)
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise

    guard_context = None
    guard_entered = False
    transaction_guard: Optional[_TransactionGuard] = None
    try:
        if receipt is None:
            receipt = _default_receipt(left_path.parent)
        guard_context = _receipt_write_guard(receipt)
        transaction_guard = guard_context.__enter__()
        guard_entered = True

        parent_identities = {
            "left": _stat_identity(os.fstat(left_parent_fd), path=left_path.parent),
            "right": _stat_identity(os.fstat(right_parent_fd), path=right_path.parent),
        }
        before_identities = {
            "left": _stat_identity(left_original, path=left_path, leaf=left_leaf),
            "right": _stat_identity(right_original, path=right_path, leaf=right_leaf),
        }
        exchanges = dict(receipt.data.get("exchanges", {}))
        if resource in exchanges:
            raise ValueError(
                "receipt already has a recorded exchange named {!r}".format(resource)
            )
        exchanges[resource] = {
            "verified": False,
            "switched": False,
            "rolled_back": False,
            "parents": parent_identities,
            "before": before_identities,
            "after": {},
        }
        aggregate = _aggregate_exchange_state(exchanges)
        receipt.record_phase(
            "{}_exchange_intent".format(resource),
            exchanges=exchanges,
            identities={
                "parents": parent_identities,
                "before": before_identities,
                "after": {},
            },
            _guard=transaction_guard,
            **aggregate,
        )

        try:
            rename_swap(
                left_parent_fd,
                left_leaf,
                right_parent_fd,
                right_leaf,
            )
        except Exception as error:
            left_current = _observe_endpoint(
                left_parent_fd,
                left_leaf,
                expected_uid=endpoint_owners[0],
                label="left exchange endpoint",
            )
            right_current = _observe_endpoint(
                right_parent_fd,
                right_leaf,
                expected_uid=endpoint_owners[1],
                label="right exchange endpoint",
            )
            mapping = mapping_kind(
                left_current,
                right_current,
                left_original,
                right_original,
            )
            if mapping == "original":
                resource_exchange = dict(exchanges[resource])
                resource_exchange.update(
                    {
                        "failure_code": "exchange_error",
                        "failure_message": str(error),
                        "observed_mapping": "original",
                    }
                )
                exchanges[resource] = resource_exchange
                aggregate = _aggregate_exchange_state(exchanges)
                if finish_on_success:
                    receipt.finish(
                        "failed_unchanged",
                        phase="exchange_failed_unchanged",
                        failure_code="exchange_error",
                        failure_message=str(error),
                        exchanges=exchanges,
                        _guard=transaction_guard,
                        **aggregate,
                    )
                else:
                    receipt.record_phase(
                        "{}_exchange_failed_unchanged".format(resource),
                        failure_code="exchange_error",
                        failure_message=str(error),
                        exchanges=exchanges,
                        _guard=transaction_guard,
                        **aggregate,
                    )
                raise
            if mapping == "transposed":
                resource_exchange = dict(exchanges[resource])
                resource_exchange.update(
                    {
                        "switched": True,
                        "observed_mapping": "transposed",
                    }
                )
                exchanges[resource] = resource_exchange
                aggregate = _aggregate_exchange_state(exchanges)
                receipt.record_phase(
                    "{}_exchange_rollback_intent".format(resource),
                    failure_code="exchange_error",
                    failure_message=str(error),
                    exchanges=exchanges,
                    _guard=transaction_guard,
                    **aggregate,
                )
                try:
                    rename_swap(
                        left_parent_fd,
                        left_leaf,
                        right_parent_fd,
                        right_leaf,
                    )
                except Exception as rollback_error:
                    resource_exchange.update(
                        {
                            "switched": True,
                            "rolled_back": False,
                            "rollback_error": str(rollback_error),
                        }
                    )
                    exchanges[resource] = resource_exchange
                    _persist_exchange_failure(
                        receipt,
                        resource=resource,
                        exchanges=exchanges,
                        phase="{}_exchange_rollback_failed".format(resource),
                        failure_code="exchange_rollback_failed",
                        failure_message=str(rollback_error),
                        manual_recovery_required=True,
                        finish=finish_on_success,
                        _guard=transaction_guard,
                    )
                    raise RuntimeError(
                        "atomic exchange rollback failed; manual recovery required"
                    ) from rollback_error
                left_restored = _observe_endpoint(
                    left_parent_fd,
                    left_leaf,
                    expected_uid=endpoint_owners[0],
                    label="left exchange endpoint",
                )
                right_restored = _observe_endpoint(
                    right_parent_fd,
                    right_leaf,
                    expected_uid=endpoint_owners[1],
                    label="right exchange endpoint",
                )
                if (
                    mapping_kind(
                        left_restored,
                        right_restored,
                        left_original,
                        right_original,
                    )
                    != "original"
                ):
                    resource_exchange.update(
                        {
                            "switched": True,
                            "rolled_back": False,
                            "rollback_verified": False,
                        }
                    )
                    exchanges[resource] = resource_exchange
                    _persist_exchange_failure(
                        receipt,
                        resource=resource,
                        exchanges=exchanges,
                        phase="{}_exchange_rollback_unverified".format(resource),
                        failure_code="rollback_verification_failed",
                        failure_message=str(error),
                        manual_recovery_required=True,
                        finish=finish_on_success,
                        _guard=transaction_guard,
                    )
                    raise RuntimeError("atomic exchange rollback could not be verified")
                _fsync_parents(left_parent_fd, right_parent_fd)
                resource_exchange.update(
                    {
                        "rolled_back": True,
                        "rollback_verified": True,
                        "failure_code": "exchange_error",
                        "failure_message": str(error),
                    }
                )
                exchanges[resource] = resource_exchange
                aggregate = _aggregate_exchange_state(exchanges)
                if finish_on_success:
                    receipt.finish(
                        "failed_rolled_back",
                        phase="exchange_rolled_back",
                        failure_code="exchange_error",
                        failure_message=str(error),
                        exchanges=exchanges,
                        _guard=transaction_guard,
                        **aggregate,
                    )
                else:
                    receipt.record_phase(
                        "{}_exchange_rolled_back".format(resource),
                        failure_code="exchange_error",
                        failure_message=str(error),
                        exchanges=exchanges,
                        _guard=transaction_guard,
                        **aggregate,
                    )
                raise

            resource_exchange = dict(exchanges[resource])
            resource_exchange.update(
                {
                    "observed_mapping": "ambiguous",
                    "failure_code": "ambiguous_exchange_mapping",
                    "failure_message": str(error),
                }
            )
            exchanges[resource] = resource_exchange
            _persist_exchange_failure(
                receipt,
                resource=resource,
                exchanges=exchanges,
                phase="{}_exchange_mapping_ambiguous".format(resource),
                failure_code="ambiguous_exchange_mapping",
                failure_message=str(error),
                manual_recovery_required=True,
                finish=finish_on_success,
                _guard=transaction_guard,
            )
            raise RuntimeError("exchange mapping is ambiguous; manual recovery required")

        left_current = _observe_endpoint(
            left_parent_fd,
            left_leaf,
            expected_uid=endpoint_owners[0],
            label="left exchange endpoint",
        )
        right_current = _observe_endpoint(
            right_parent_fd,
            right_leaf,
            expected_uid=endpoint_owners[1],
            label="right exchange endpoint",
        )
        if (
            mapping_kind(
                left_current,
                right_current,
                left_original,
                right_original,
            )
            != "transposed"
        ):
            exchanges = dict(receipt.data.get("exchanges", {}))
            resource_exchange = dict(exchanges[resource])
            resource_exchange.update(
                {
                    "switched": True,
                    "observed_mapping": "ambiguous",
                }
            )
            exchanges[resource] = resource_exchange
            _persist_exchange_failure(
                receipt,
                resource=resource,
                exchanges=exchanges,
                phase="{}_exchange_transposition_unverified".format(resource),
                failure_code="transposition_verification_failed",
                failure_message="endpoint identities are not an exact transposition",
                manual_recovery_required=True,
                finish=finish_on_success,
                _guard=transaction_guard,
            )
            raise RuntimeError("atomic exchange did not produce an exact transposition")

        _fsync_parents(left_parent_fd, right_parent_fd)
        identities = receipt.data["identities"]
        identities["after"] = {
            "left": _stat_identity(left_current, path=left_path, leaf=left_leaf),
            "right": _stat_identity(right_current, path=right_path, leaf=right_leaf),
        }
        exchanges = dict(receipt.data.get("exchanges", {}))
        resource_exchange = dict(exchanges[resource])
        resource_exchange.update(
            {
                "verified": True,
                "switched": True,
                "rolled_back": False,
                "after": identities["after"],
            }
        )
        exchanges[resource] = resource_exchange
        if finish_on_success:
            receipt.finish(
                "succeeded",
                phase="exchange_verified",
                switched=True,
                rolled_back=False,
                no_live_mutation=False,
                identities=identities,
                exchanges=exchanges,
                _guard=transaction_guard,
            )
        else:
            receipt.record_phase(
                "{}_exchange_verified".format(resource),
                switched=True,
                rolled_back=False,
                no_live_mutation=False,
                identities=identities,
                exchanges=exchanges,
                _guard=transaction_guard,
            )
        return receipt
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if guard_entered and guard_context is not None:
            guard_context.__exit__(None, None, None)
