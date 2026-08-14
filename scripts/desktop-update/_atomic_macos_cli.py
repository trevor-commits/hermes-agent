"""Command-line interface for the external atomic macOS updater runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat


_SAFE_TRANSACTION_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_VALUE_OPTIONS = frozenset(
    {
        "--manifest-sha256",
        "--runtime-device",
        "--runtime-inode",
        "--transaction",
        "--transactions-root",
    }
)
_FLAG_OPTIONS = frozenset({"--capabilities"})
_TERMINAL_RECOVERED = frozenset(
    {"succeeded", "failed_unchanged", "failed_rolled_back"}
)
_RECOVERY_LOCK_TIMEOUT_SECONDS = 1.0


class _UsageError(ValueError):
    def __init__(self, failure_code: str):
        self.failure_code = failure_code
        super().__init__(failure_code)


class _RecoveryError(RuntimeError):
    def __init__(self, failure_code: str):
        self.failure_code = failure_code
        super().__init__(failure_code)


def _parse(arguments):
    values = {}
    flags = set()
    positionals = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in _VALUE_OPTIONS:
            if argument in values or index + 1 >= len(arguments):
                raise _UsageError("invalid_arguments")
            value = arguments[index + 1]
            if value.startswith("--"):
                raise _UsageError("invalid_arguments")
            values[argument] = value
            index += 2
            continue
        if argument in _FLAG_OPTIONS:
            if argument in flags:
                raise _UsageError("invalid_arguments")
            flags.add(argument)
            index += 1
            continue
        if argument.startswith("-"):
            raise _UsageError("invalid_arguments")
        positionals.append(argument)
        index += 1
    return values, flags, positionals


def _bounded_payload(*, ok, status, failure_code, resources=()):
    safe_status = status if status in _TERMINAL_RECOVERED | {"manual_recovery_required", "unsafe"} else "unrecovered"
    safe_failure = failure_code if isinstance(failure_code, str) and re.fullmatch(r"[a-z0-9_]{1,128}", failure_code) else "recovery_failed"
    payload = {
        "manual_recovery_required": safe_status == "manual_recovery_required",
        "ok": bool(ok),
        "resources": [resource for resource in resources if resource in ("app", "source")],
        "schema_version": 1,
        "status": safe_status,
    }
    if not ok:
        payload["failure_code"] = safe_failure
    encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > 4096:
        raise RuntimeError("bounded CLI payload unexpectedly exceeded its limit")
    return encoded


def _emit(*, ok, status, failure_code, resources=()):
    os.write(
        1,
        _bounded_payload(
            ok=ok,
            status=status,
            failure_code=failure_code,
            resources=resources,
        ),
    )


def _capabilities():
    payload = {
        "commands": ["recover"],
        "schema_version": 1,
    }
    encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > 4096:
        raise RuntimeError("capabilities payload unexpectedly exceeded its limit")
    os.write(1, encoded)


def _recovery_resources(receipt):
    exchanges = receipt.data.get("exchanges")
    if not isinstance(exchanges, dict) or not exchanges:
        raise RuntimeError("receipt has no recorded exchanges")
    if any(resource not in ("app", "source") for resource in exchanges):
        raise RuntimeError("receipt has an unsupported recorded exchange")
    if any(not isinstance(record, dict) for record in exchanges.values()):
        raise RuntimeError("receipt has an invalid recorded exchange")
    return [resource for resource in ("app", "source") if resource in exchanges]


def _directory_identity(observed):
    return (
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
        stat.S_IMODE(observed.st_mode),
        observed.st_uid,
    )


def _directory_flags():
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_transactions_root_components(canonical):
    descriptor = os.open(os.path.sep, _directory_flags())
    try:
        for component in Path(canonical).parts[1:]:
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise _RecoveryError("unsafe_transactions_root")
            opened_descriptor = os.open(
                component,
                _directory_flags(),
                dir_fd=descriptor,
            )
            opened = os.fstat(opened_descriptor)
            if _directory_identity(before) != _directory_identity(opened):
                os.close(opened_descriptor)
                raise _RecoveryError("unsafe_transactions_root")
            os.close(descriptor)
            descriptor = opened_descriptor
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise _RecoveryError("unsafe_transactions_root")
        return descriptor, _directory_identity(opened)
    except BaseException:
        os.close(descriptor)
        raise


class _TransactionsRootBinding:
    def __init__(self, path, descriptor, identity):
        self.path = Path(path)
        self.descriptor = descriptor
        self.identity = identity

    def require_current(self):
        if _directory_identity(os.fstat(self.descriptor)) != self.identity:
            raise _RecoveryError("unsafe_transactions_root")
        current_descriptor = None
        try:
            current_descriptor, current_identity = _open_transactions_root_components(
                str(self.path)
            )
            if current_identity != self.identity:
                raise _RecoveryError("unsafe_transactions_root")
        finally:
            if current_descriptor is not None:
                os.close(current_descriptor)

    def close(self):
        descriptor = self.descriptor
        self.descriptor = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_transaction_directory(root_binding, transaction_id):
    descriptor = None
    try:
        root_binding.require_current()
        before = os.stat(
            transaction_id,
            dir_fd=root_binding.descriptor,
            follow_symlinks=False,
        )
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o700
        ):
            raise _RecoveryError("unsafe_transaction_directory")
        descriptor = os.open(
            transaction_id,
            _directory_flags(),
            dir_fd=root_binding.descriptor,
        )
        opened = os.fstat(descriptor)
        if _directory_identity(before) != _directory_identity(opened):
            raise _RecoveryError("unsafe_transaction_directory")
        root_binding.require_current()
    except _RecoveryError:
        raise
    except (OSError, ValueError):
        raise _RecoveryError("unsafe_transaction_directory") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_transactions_root(requested_root):
    if not isinstance(requested_root, str) or not requested_root:
        raise _UsageError("invalid_arguments")
    canonical = os.path.abspath(requested_root)
    if not os.path.isabs(requested_root) or os.path.normpath(requested_root) != requested_root:
        raise _RecoveryError("unsafe_transactions_root")

    try:
        descriptor, identity = _open_transactions_root_components(canonical)
    except _RecoveryError:
        raise
    except (OSError, ValueError):
        raise _RecoveryError("unsafe_transactions_root") from None
    return _TransactionsRootBinding(Path(canonical), descriptor, identity)


def _recover(engine, transaction_dir, resources, root_binding):
    return engine.recover_recorded_exchanges(
        transaction_dir,
        resources,
        lock_timeout_seconds=_RECOVERY_LOCK_TIMEOUT_SECONDS,
        _transactions_root_fd=root_binding.descriptor,
        _transactions_root_identity=root_binding.identity,
    )


def main(engine, arguments, *, runtime_transaction_id):
    root_binding = None
    try:
        values, flags, positionals = _parse(arguments)
        if "--capabilities" in flags:
            if positionals or "--transaction" in values or "--transactions-root" in values:
                raise _UsageError("invalid_arguments")
            _capabilities()
            return 0

        if positionals != ["recover"]:
            raise _UsageError("invalid_arguments")
        transaction_id = values.get("--transaction")
        if (
            not isinstance(transaction_id, str)
            or not _SAFE_TRANSACTION_RE.fullmatch(transaction_id)
            or transaction_id != runtime_transaction_id
        ):
            raise _UsageError("invalid_transaction")

        root_binding = _validate_transactions_root(values.get("--transactions-root"))

        transaction_dir = root_binding.path / transaction_id
        _validate_transaction_directory(root_binding, transaction_id)
        receipt = engine.load_transaction(
            transaction_dir,
            _transactions_root_fd=root_binding.descriptor,
            _transactions_root_identity=root_binding.identity,
        )
        if receipt.data.get("transaction_id") != transaction_id:
            raise RuntimeError("receipt transaction identity does not match")
        if receipt.is_terminal and receipt.data.get("status") in _TERMINAL_RECOVERED:
            _emit(
                ok=True,
                status=receipt.data["status"],
                failure_code=receipt.data.get("failure_code"),
            )
            return 0

        resources = _recovery_resources(receipt)
        recovered, processed = _recover(
            engine,
            transaction_dir,
            resources,
            root_binding,
        )
        status = recovered.data.get("status")
        failure_code = recovered.data.get("failure_code")
        if status in _TERMINAL_RECOVERED and (
            len(processed) == len(resources) or not processed
        ):
            _emit(
                ok=True,
                status=status,
                failure_code=failure_code,
                resources=processed,
            )
            return 0
        _emit(
            ok=False,
            status="manual_recovery_required" if status == "manual_recovery_required" else "unrecovered",
            failure_code=failure_code,
            resources=processed,
        )
        return 75
    except _UsageError as error:
        _emit(ok=False, status="unsafe", failure_code=error.failure_code)
        return 64
    except _RecoveryError as error:
        _emit(ok=False, status="unrecovered", failure_code=error.failure_code)
        return 75
    except engine.TransactionLockUnavailableError:
        _emit(
            ok=False,
            status="unrecovered",
            failure_code="transaction_lock_unavailable",
        )
        return 75
    except Exception:
        _emit(ok=False, status="unrecovered", failure_code="recovery_failed")
        return 75
    finally:
        if root_binding is not None:
            root_binding.close()
