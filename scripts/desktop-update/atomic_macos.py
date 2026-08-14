#!/usr/bin/env python3
"""Deployable facade for the macOS Hermes atomic-update engine."""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Optional


_RUNTIME_ASSET_MODES = {
    "posix.sh": 0o500,
    "atomic_macos.py": 0o500,
    "_atomic_macos_transaction.py": 0o400,
    "_atomic_macos_candidate.py": 0o400,
    "_atomic_macos_git.py": 0o400,
    "_atomic_macos_cli.py": 0o400,
    "serve-ui.py": 0o400,
    "ui.html": 0o400,
}
_DECIMAL_RE = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_MAX_RUNTIME_MEMBER_BYTES = 16 * 1024 * 1024
_VERIFIED_RUNTIME_BYTES = None
_VERIFIED_RUNTIME_TRANSACTION_ID = None


class _RuntimeValidationError(RuntimeError):
    pass


def _required_argument(arguments, option):
    positions = [index for index, value in enumerate(arguments) if value == option]
    if len(positions) != 1:
        raise _RuntimeValidationError("runtime binding argument is missing or repeated")
    position = positions[0]
    if position + 1 >= len(arguments) or arguments[position + 1].startswith("--"):
        raise _RuntimeValidationError("runtime binding argument has no value")
    return arguments[position + 1]


def _runtime_stat_identity(observed):
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_uid,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _read_verified_runtime_member(directory_fd, leaf, mode, *, maximum_size):
    try:
        before = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise _RuntimeValidationError("runtime member is unavailable") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size < 0
        or before.st_size > maximum_size
    ):
        raise _RuntimeValidationError("runtime member identity is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = None
    try:
        descriptor = os.open(leaf, flags, dir_fd=directory_fd)
        opened = os.fstat(descriptor)
        if _runtime_stat_identity(before) != _runtime_stat_identity(opened):
            raise _RuntimeValidationError("runtime member changed while opening")
        chunks = []
        remaining = maximum_size + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) > maximum_size
            or len(payload) != before.st_size
            or _runtime_stat_identity(opened) != _runtime_stat_identity(after)
        ):
            raise _RuntimeValidationError("runtime member changed while reading")
        return payload
    except OSError as error:
        raise _RuntimeValidationError("runtime member could not be read") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _parse_canonical_manifest(raw_manifest, runtime_transaction_id):
    try:
        manifest = json.loads(
            raw_manifest.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_json_object(pairs),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("non-finite JSON value")
            ),
        )
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise _RuntimeValidationError("runtime manifest is invalid") from error
    if not isinstance(manifest, dict):
        raise _RuntimeValidationError("runtime manifest schema is invalid")
    if list(manifest) != ["files", "principal", "schema_version", "transaction_id"]:
        raise _RuntimeValidationError("runtime manifest schema is invalid")
    if (
        manifest.get("principal") != "hermes-atomic-update"
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("transaction_id") != runtime_transaction_id
        or not isinstance(manifest.get("files"), list)
    ):
        raise _RuntimeValidationError("runtime manifest identity is invalid")
    canonical = (json.dumps(manifest, separators=(",", ":")) + "\n").encode("utf-8")
    if canonical != raw_manifest:
        raise _RuntimeValidationError("runtime manifest encoding is not canonical")
    return manifest


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _validate_external_runtime(arguments):
    runtime_device = _required_argument(arguments, "--runtime-device")
    runtime_inode = _required_argument(arguments, "--runtime-inode")
    manifest_sha256 = _required_argument(arguments, "--manifest-sha256")
    if (
        not _DECIMAL_RE.fullmatch(runtime_device)
        or not _DECIMAL_RE.fullmatch(runtime_inode)
        or not _SHA256_RE.fullmatch(manifest_sha256)
    ):
        raise _RuntimeValidationError("runtime binding format is invalid")

    runtime_directory = os.path.abspath(os.path.dirname(__file__))
    runtime_parent = os.path.dirname(runtime_directory)
    runtime_leaf = os.path.basename(runtime_directory)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    parent_fd = None
    directory_fd = None
    try:
        parent_before = os.lstat(runtime_parent)
        if (
            stat.S_ISLNK(parent_before.st_mode)
            or not stat.S_ISDIR(parent_before.st_mode)
            or parent_before.st_uid != os.geteuid()
            or stat.S_IMODE(parent_before.st_mode) != 0o700
        ):
            raise _RuntimeValidationError("runtime parent directory identity is unsafe")
        parent_fd = os.open(runtime_parent, flags)
        parent_opened = os.fstat(parent_fd)
        if _runtime_stat_identity(parent_before) != _runtime_stat_identity(parent_opened):
            raise _RuntimeValidationError("runtime parent changed while opening")

        before = os.stat(runtime_leaf, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o700
        ):
            raise _RuntimeValidationError("runtime directory identity is unsafe")
        directory_fd = os.open(runtime_leaf, flags, dir_fd=parent_fd)
        opened = os.fstat(directory_fd)
        if (
            _runtime_stat_identity(before) != _runtime_stat_identity(opened)
            or str(opened.st_dev) != runtime_device
            or str(opened.st_ino) != runtime_inode
        ):
            raise _RuntimeValidationError("runtime directory does not match its binding")
        runtime_transaction_id = os.path.basename(runtime_directory)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", runtime_transaction_id):
            raise _RuntimeValidationError("runtime transaction identity is invalid")
        expected_names = set(_RUNTIME_ASSET_MODES)
        expected_names.add("manifest.json")
        if set(os.listdir(directory_fd)) != expected_names:
            raise _RuntimeValidationError("runtime asset set is invalid")

        raw_manifest = _read_verified_runtime_member(
            directory_fd,
            "manifest.json",
            0o400,
            maximum_size=256 * 1024,
        )
        if hashlib.sha256(raw_manifest).hexdigest() != manifest_sha256:
            raise _RuntimeValidationError("runtime manifest digest does not match")
        manifest = _parse_canonical_manifest(raw_manifest, runtime_transaction_id)
        expected_assets = sorted(_RUNTIME_ASSET_MODES)
        if len(manifest["files"]) != len(expected_assets):
            raise _RuntimeValidationError("runtime manifest asset set is invalid")

        verified_bytes = {}
        for index, leaf in enumerate(expected_assets):
            record = manifest["files"][index]
            if not isinstance(record, dict) or list(record) != ["mode", "path", "sha256", "size"]:
                raise _RuntimeValidationError("runtime manifest member schema is invalid")
            mode = _RUNTIME_ASSET_MODES[leaf]
            if (
                record.get("path") != leaf
                or type(record.get("mode")) is not int
                or record.get("mode") != mode
                or type(record.get("size")) is not int
                or record.get("size") < 0
                or not isinstance(record.get("sha256"), str)
                or not _SHA256_RE.fullmatch(record["sha256"])
            ):
                raise _RuntimeValidationError("runtime manifest member identity is invalid")
            payload = _read_verified_runtime_member(
                directory_fd,
                leaf,
                mode,
                maximum_size=_MAX_RUNTIME_MEMBER_BYTES,
            )
            if len(payload) != record["size"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
                raise _RuntimeValidationError("runtime manifest member digest does not match")
            verified_bytes[leaf] = payload
        if _runtime_stat_identity(opened) != _runtime_stat_identity(os.fstat(directory_fd)):
            raise _RuntimeValidationError("runtime directory changed during validation")
        if _runtime_stat_identity(parent_opened) != _runtime_stat_identity(os.fstat(parent_fd)):
            raise _RuntimeValidationError("runtime parent changed during validation")
        return verified_bytes, runtime_transaction_id
    except OSError as error:
        raise _RuntimeValidationError("runtime directory could not be validated") from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _emit_bootstrap_failure():
    encoded = b'{"failure_code":"runtime_validation_failed","manual_recovery_required":false,"ok":false,"schema_version":1,"status":"unsafe"}\n'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        _VERIFIED_RUNTIME_BYTES, _VERIFIED_RUNTIME_TRANSACTION_ID = _validate_external_runtime(
            sys.argv[1:]
        )
    except Exception:
        _emit_bootstrap_failure()
        raise SystemExit(64)


def _load_sibling(module_name: str):
    path = Path(__file__).resolve().with_name(module_name + ".py")
    existing = sys.modules.get(module_name)
    if existing is not None:
        existing_path = Path(getattr(existing, "__file__", "")).resolve()
        if existing_path != path:
            raise ImportError(
                "updater module name collision for {}".format(module_name)
            )
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load updater module {}".format(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        if _VERIFIED_RUNTIME_BYTES is None:
            spec.loader.exec_module(module)
        else:
            leaf = module_name + ".py"
            source = _VERIFIED_RUNTIME_BYTES.get(leaf)
            if source is None:
                raise ImportError("verified updater module is unavailable")
            exec(compile(source, str(path), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


try:
    _transaction = _load_sibling("_atomic_macos_transaction")
    _candidate = _load_sibling("_atomic_macos_candidate")
    _git = _load_sibling("_atomic_macos_git")
except Exception:
    if __name__ == "__main__":
        _emit_bootstrap_failure()
        raise SystemExit(64)
    raise

PathLike = _transaction.PathLike
TransactionReceipt = _transaction.TransactionReceipt
KeeperConflictError = _git.KeeperConflictError
GitCommandError = _git.GitCommandError

validate_generated_leaf = _transaction.validate_generated_leaf
create_transaction = _transaction.create_transaction
load_transaction = _transaction.load_transaction

_validated_directory_identity = _transaction._validated_directory_identity
_validated_endpoint_identity = _transaction._validated_endpoint_identity
_require_same_device = _transaction._require_same_device
_load_rename_swap = _transaction._load_rename_swap
_rename_swap_at = _transaction._rename_swap_at
_mapping_kind = _transaction._mapping_kind
_run_git = _git._run_git
_record_candidate_failure = _git._record_candidate_failure


def _git_output(repository: Path, *arguments: str) -> str:
    return _git._git_output(
        repository,
        *arguments,
        _run_git_command=_run_git,
    )


def _checkout_snapshot(repository: Path):
    return _git._checkout_snapshot(
        repository,
        _run_git_command=_run_git,
    )


def prepare_keeper_candidate(
    live_checkout: PathLike,
    official_upstream: PathLike,
    target_commit: str,
    *,
    receipt: TransactionReceipt,
) -> Path:
    return _git.prepare_keeper_candidate(
        live_checkout,
        official_upstream,
        target_commit,
        receipt=receipt,
        _run_git_command=_run_git,
    )


def recover_exchange(
    transaction_dir: PathLike,
    *,
    resource: str = "standalone",
    finish: bool = True,
) -> TransactionReceipt:
    return _transaction.recover_exchange(
        transaction_dir,
        resource=resource,
        finish=finish,
        _rename_swap_command=_rename_swap_at,
        _mapping_kind_command=_mapping_kind,
    )


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
) -> TransactionReceipt:
    return _transaction.atomic_exchange(
        left,
        right,
        receipt=receipt,
        expected_uid=expected_uid,
        expected_parent_uids=expected_parent_uids,
        expected_endpoint_uids=expected_endpoint_uids,
        resource=resource,
        finish_on_success=finish_on_success,
        _rename_swap_command=_rename_swap_at,
        _mapping_kind_command=_mapping_kind,
    )


if __name__ == "__main__":
    try:
        _cli = _load_sibling("_atomic_macos_cli")
        raise SystemExit(
            _cli.main(
                sys.modules[__name__],
                sys.argv[1:],
                runtime_transaction_id=_VERIFIED_RUNTIME_TRANSACTION_ID,
            )
        )
    except SystemExit:
        raise
    except Exception:
        _emit_bootstrap_failure()
        raise SystemExit(64)
