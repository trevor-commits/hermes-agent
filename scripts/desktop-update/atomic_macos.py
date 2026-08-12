#!/usr/bin/env python3
"""On-demand atomic coordinator for the macOS Hermes desktop updater."""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Dict, Optional, Union
import uuid


PathLike = Union[str, os.PathLike]

_RENAME_SWAP = 0x00000002
_RECEIPT_NAME = "receipt.json"
_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "failed_unchanged",
        "failed_rolled_back",
        "manual_recovery_required",
    }
)
_EXACT_COMMIT_RE = re.compile(r"\A(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_GIT_TIMEOUT_SECONDS = 600


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


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


def _open_verified_directory(
    path: Path,
    *,
    owner_only: bool = False,
    expected_uid: Optional[int] = None,
) -> int:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"directory must not be a symlink: {path}")
    owner = os.geteuid() if expected_uid is None else expected_uid
    if before.st_uid != owner:
        raise PermissionError(f"directory has unexpected owner: {path}")
    if owner_only and before.st_mode & 0o077:
        raise PermissionError(f"transaction directory is not owner-only: {path}")

    descriptor = os.open(str(path), _directory_open_flags())
    try:
        after = os.fstat(descriptor)
        if _identity_tuple(before) != _identity_tuple(after):
            raise RuntimeError(f"directory identity changed while opening: {path}")
        if not stat.S_ISDIR(after.st_mode):
            raise ValueError(f"opened path is not a directory: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


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


class TransactionReceipt:
    """Durable schema-v1 state for one updater transaction."""

    def __init__(self, transaction_dir: PathLike, data: Dict[str, Any]) -> None:
        self.transaction_dir = Path(transaction_dir)
        self.path = self.transaction_dir / _RECEIPT_NAME
        self.data = data

    @property
    def is_terminal(self) -> bool:
        return self.data.get("status") in _TERMINAL_STATUSES

    def record_phase(self, phase: str, **updates: Any) -> None:
        if self.is_terminal:
            raise RuntimeError("terminal transaction receipts are immutable")
        if not isinstance(phase, str) or not phase:
            raise ValueError("receipt phase must be a non-empty string")
        self.data.update(updates)
        self.data["phase"] = phase
        self.data["updated_at"] = _utc_now()
        _atomic_json_write(self.transaction_dir, self.data)

    def finish(
        self,
        status: str,
        *,
        phase: str,
        failure_code: Optional[str] = None,
        failure_message: Optional[str] = None,
        **updates: Any,
    ) -> None:
        if self.is_terminal:
            raise RuntimeError("terminal transaction receipts are immutable")
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"unknown terminal status: {status}")
        if not isinstance(phase, str) or not phase:
            raise ValueError("receipt phase must be a non-empty string")

        now = _utc_now()
        self.data.update(updates)
        self.data.update(
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
        _atomic_json_write(self.transaction_dir, self.data)


def create_transaction(
    transactions_root: PathLike,
    transaction_id: Optional[str] = None,
) -> TransactionReceipt:
    """Create an owner-only transaction directory and initial receipt."""
    root = Path(transactions_root)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"transactions root must be a real directory: {root}")

    leaf = validate_generated_leaf(
        transaction_id or "txn-{}".format(uuid.uuid4().hex)
    )
    transaction_dir = root / leaf
    os.mkdir(str(transaction_dir), 0o700)
    os.chmod(str(transaction_dir), 0o700, follow_symlinks=False)

    now = _utc_now()
    data: Dict[str, Any] = {
        "schema_version": 1,
        "transaction_id": leaf,
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
    _atomic_json_write(transaction_dir, data)
    return TransactionReceipt(transaction_dir, data)


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
        with os.fdopen(receipt_fd, "r", encoding="utf-8") as handle:
            receipt_fd = None
            data = json.load(handle)
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
    return TransactionReceipt(directory, data)


class KeeperConflictError(RuntimeError):
    """The first conflicting keeper commit stopped candidate preparation."""

    def __init__(self, commit: str, subject: str, unmerged_paths: list) -> None:
        self.commit = commit
        self.subject = subject
        self.unmerged_paths = tuple(unmerged_paths)
        super().__init__(
            "keeper commit {} ({}) conflicts in: {}".format(
                commit,
                subject,
                ", ".join(unmerged_paths),
            )
        )


def _git_environment() -> Dict[str, str]:
    environment = os.environ.copy()
    environment.pop("HERMES_HOME", None)
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_COMMITTER_NAME": "Hermes Safe Updater",
            "GIT_COMMITTER_EMAIL": "hermes-updater@localhost.invalid",
        }
    )
    return environment


def _run_git(
    arguments: list,
    *,
    repository: Optional[Path] = None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    command = ["git", "-c", "core.hooksPath=/dev/null"]
    if repository is not None:
        command.extend(["-C", str(repository)])
    command.extend(arguments)
    return subprocess.run(
        command,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        env=_git_environment(),
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _git_output(repository: Path, *arguments: str) -> str:
    return _run_git(list(arguments), repository=repository).stdout.strip()


def _worktree_digest(repository: Path) -> str:
    listed = _run_git(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        repository=repository,
        text=False,
    ).stdout
    digest = hashlib.sha256()
    for encoded_relative in sorted(part for part in listed.split(b"\0") if part):
        relative = os.fsdecode(encoded_relative)
        path = repository / relative
        digest.update(len(encoded_relative).to_bytes(8, "big"))
        digest.update(encoded_relative)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            digest.update(b"missing")
            continue
        digest.update(stat.S_IFMT(file_stat.st_mode).to_bytes(8, "big"))
        digest.update(stat.S_IMODE(file_stat.st_mode).to_bytes(8, "big"))
        if stat.S_ISLNK(file_stat.st_mode):
            digest.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(file_stat.st_mode):
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        else:
            digest.update(b"non-regular")
    return digest.hexdigest()


def _checkout_snapshot(repository: Path) -> Dict[str, Any]:
    root_stat = repository.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("live checkout must be a real directory")
    head = _git_output(repository, "rev-parse", "HEAD^{commit}")
    tree = _git_output(repository, "rev-parse", "HEAD^{tree}")
    status_output = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        repository=repository,
    ).stdout
    return {
        "path": str(repository),
        "st_dev": root_stat.st_dev,
        "st_ino": root_stat.st_ino,
        "st_uid": root_stat.st_uid,
        "head": head,
        "status": status_output,
        "status_digest": hashlib.sha256(status_output.encode("utf-8")).hexdigest(),
        "tree_digest": tree,
        "worktree_digest": _worktree_digest(repository),
    }


def _record_candidate_failure(
    receipt: TransactionReceipt,
    *,
    phase: str,
    code: str,
    message: str,
    live_before: Optional[Dict[str, Any]],
    live_after: Optional[Dict[str, Any]],
    candidate_path: Path,
    **updates: Any,
) -> None:
    unchanged = live_before is not None and live_before == live_after
    status = "failed_unchanged" if unchanged else "manual_recovery_required"
    receipt.finish(
        status,
        phase=phase,
        switched=False,
        rolled_back=False,
        no_live_mutation=unchanged,
        failure_code=code,
        failure_message=message,
        live_before=live_before,
        live_after=live_after,
        candidate_path=str(candidate_path),
        **updates,
    )


def prepare_keeper_candidate(
    live_checkout: PathLike,
    official_upstream: PathLike,
    target_commit: str,
    *,
    receipt: TransactionReceipt,
) -> Path:
    """Clone and rebase keepers without using the live checkout as a workspace."""
    if receipt.is_terminal:
        raise RuntimeError("cannot prepare a candidate using a terminal receipt")

    live = Path(os.path.abspath(os.fspath(live_checkout)))
    candidate = receipt.transaction_dir / validate_generated_leaf("candidate")
    live_before: Optional[Dict[str, Any]] = None
    live_after: Optional[Dict[str, Any]] = None

    if not isinstance(target_commit, str) or not _EXACT_COMMIT_RE.fullmatch(
        target_commit
    ):
        try:
            live_before = _checkout_snapshot(live)
            live_after = _checkout_snapshot(live)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        _record_candidate_failure(
            receipt,
            phase="candidate_target_invalid",
            code="invalid_target_commit",
            message="target must be an exact 40- or 64-hex commit id",
            live_before=live_before,
            live_after=live_after,
            candidate_path=candidate,
        )
        raise ValueError("target must be an exact commit id")

    target = target_commit.lower()
    try:
        live_before = _checkout_snapshot(live)
        receipt.record_phase(
            "candidate_clone_intent",
            live_before=live_before,
            candidate_path=str(candidate),
            target_commit=target,
        )
        _run_git(
            ["clone", "--no-local", "--", str(live), str(candidate)],
        )
        os.chmod(str(candidate), 0o700, follow_symlinks=False)
        if not (candidate / ".git").is_dir():
            raise RuntimeError("candidate clone is not an independent Git repository")
        if candidate.stat().st_dev != live.stat().st_dev:
            raise OSError(
                errno.EXDEV,
                "candidate clone and live checkout are not on the same device",
            )

        receipt.record_phase(
            "candidate_cloned",
            live_before=live_before,
            candidate_path=str(candidate),
            target_commit=target,
        )
        _run_git(
            [
                "fetch",
                "--no-tags",
                "--force",
                "--",
                str(official_upstream),
                "{}:refs/hermes-update/target".format(target),
            ],
            repository=candidate,
        )
        fetched_target = _git_output(
            candidate,
            "rev-parse",
            "refs/hermes-update/target^{commit}",
        ).lower()
        if fetched_target != target:
            raise RuntimeError(
                "official upstream fetch resolved to {}, expected {}".format(
                    fetched_target,
                    target,
                )
            )

        original_head = live_before["head"]
        keeper_base = _git_output(
            candidate,
            "merge-base",
            original_head,
            target,
        )
        original_commits_output = _git_output(
            candidate,
            "rev-list",
            "--reverse",
            "{}..{}".format(keeper_base, original_head),
        )
        original_commits = (
            original_commits_output.splitlines() if original_commits_output else []
        )
        original_subjects = [
            _git_output(candidate, "show", "-s", "--format=%s", commit)
            for commit in original_commits
        ]
        keeper = {
            "base_commit": keeper_base,
            "target_commit": target,
            "official_upstream": str(official_upstream),
            "original_commits": original_commits,
            "original_subjects": original_subjects,
        }
        receipt.record_phase(
            "keeper_rebase_intent",
            live_before=live_before,
            candidate_path=str(candidate),
            keeper=keeper,
        )

        rebase = _run_git(
            [
                "rebase",
                "--rebase-merges",
                "--reapply-cherry-picks",
                "--empty=keep",
                "--onto",
                target,
                keeper_base,
            ],
            repository=candidate,
            check=False,
        )
        if rebase.returncode != 0:
            conflict_commit = _git_output(
                candidate,
                "rev-parse",
                "REBASE_HEAD^{commit}",
            )
            conflict_subject = _git_output(
                candidate,
                "show",
                "-s",
                "--format=%s",
                conflict_commit,
            )
            unmerged_output = _run_git(
                ["diff", "--name-only", "--diff-filter=U", "-z"],
                repository=candidate,
            ).stdout
            unmerged_paths = sorted(
                path for path in unmerged_output.split("\0") if path
            )
            _run_git(["rebase", "--abort"], repository=candidate)
            if _git_output(candidate, "rev-parse", "HEAD^{commit}") != original_head:
                raise RuntimeError("candidate rebase abort did not restore its original HEAD")
            if _run_git(
                ["diff", "--name-only", "--diff-filter=U"],
                repository=candidate,
            ).stdout:
                raise RuntimeError("candidate rebase abort left unmerged paths")

            live_after = _checkout_snapshot(live)
            conflict = {
                "commit": conflict_commit,
                "subject": conflict_subject,
                "unmerged_paths": unmerged_paths,
            }
            _record_candidate_failure(
                receipt,
                phase="keeper_conflict",
                code="keeper_conflict",
                message="keeper commit conflicts with the exact upstream target",
                live_before=live_before,
                live_after=live_after,
                candidate_path=candidate,
                keeper=keeper,
                keeper_conflict=conflict,
            )
            raise KeeperConflictError(
                conflict_commit,
                conflict_subject,
                unmerged_paths,
            )

        rebased_output = _git_output(
            candidate,
            "rev-list",
            "--reverse",
            "{}..HEAD".format(target),
        )
        rebased_commits = rebased_output.splitlines() if rebased_output else []
        rebased_subjects = [
            _git_output(candidate, "show", "-s", "--format=%s", commit)
            for commit in rebased_commits
        ]
        if len(rebased_commits) != len(original_commits):
            raise RuntimeError("keeper rebase did not preserve every keeper commit")
        if rebased_subjects != original_subjects:
            raise RuntimeError("keeper rebase changed keeper order or subjects")
        keeper.update(
            {
                "rebased_commits": rebased_commits,
                "rebased_subjects": rebased_subjects,
            }
        )

        live_after = _checkout_snapshot(live)
        if live_after != live_before:
            _record_candidate_failure(
                receipt,
                phase="live_checkout_changed",
                code="live_checkout_changed",
                message="live checkout changed during isolated candidate preparation",
                live_before=live_before,
                live_after=live_after,
                candidate_path=candidate,
                keeper=keeper,
            )
            raise RuntimeError("live checkout changed during candidate preparation")

        receipt.record_phase(
            "candidate_prepared",
            live_before=live_before,
            live_after=live_after,
            candidate_path=str(candidate),
            keeper=keeper,
        )
        return candidate
    except KeeperConflictError:
        raise
    except BaseException as error:
        if not receipt.is_terminal:
            try:
                live_after = _checkout_snapshot(live)
            except (OSError, ValueError, subprocess.SubprocessError):
                live_after = None
            _record_candidate_failure(
                receipt,
                phase="candidate_preparation_failed",
                code="candidate_preparation_failed",
                message=str(error),
                live_before=live_before,
                live_after=live_after,
                candidate_path=candidate,
            )
        raise


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
    receipt.finish(
        "manual_recovery_required",
        phase=phase,
        failure_code=failure_code,
        failure_message=failure_message,
        **updates,
    )
    return receipt


def _aggregate_exchange_state(exchanges: Dict[str, Any]) -> Dict[str, bool]:
    records = [record for record in exchanges.values() if isinstance(record, dict)]
    switched = any(
        bool(record.get("switched")) or bool(record.get("verified"))
        for record in records
    )
    rolled_back = bool(records) and all(
        bool(record.get("rolled_back")) for record in records
    )
    return {
        "switched": switched,
        "rolled_back": rolled_back,
        "no_live_mutation": not switched,
    }


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
            **updates,
        )
    else:
        receipt.record_phase(phase, **updates)
    return receipt


def recover_exchange(
    transaction_dir: PathLike,
    *,
    resource: str = "standalone",
    finish: bool = True,
) -> TransactionReceipt:
    """Recover one exact receipt-recorded exchange without guessing."""
    receipt = load_transaction(transaction_dir)
    if receipt.is_terminal:
        return receipt

    resource = validate_generated_leaf(resource)
    exchanges = receipt.data.get("exchanges")
    if not isinstance(exchanges, dict) or resource not in exchanges:
        raise ValueError("receipt has no recorded exchange named {!r}".format(resource))
    selected = exchanges[resource]
    if not isinstance(selected, dict):
        raise ValueError("recorded exchange {!r} is invalid".format(resource))
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
        owner = int(left_original_record["st_uid"])
        if int(right_original_record["st_uid"]) != owner:
            raise ValueError("receipt endpoint owners disagree")
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
        )

    descriptors = []
    try:
        left_parent_fd = _open_verified_directory(
            left_parent_path,
            expected_uid=owner,
        )
        descriptors.append(left_parent_fd)
        right_parent_fd = _open_verified_directory(
            right_parent_path,
            expected_uid=owner,
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
            expected_uid=owner,
            label="left recovery endpoint",
        )
        right_current = _observe_endpoint(
            right_parent_fd,
            right_leaf,
            expected_uid=owner,
            label="right recovery endpoint",
        )
    except (OSError, RuntimeError, ValueError) as error:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        return _finish_manual_recovery(
            receipt,
            phase=_recovery_phase(resource, "identity_unavailable", finish),
            failure_code="recovery_identity_unavailable",
            failure_message=str(error),
            identities=identities,
            exchanges=exchanges,
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
        mapping = _mapping_kind(
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
        )
        try:
            _rename_swap_at(
                left_parent_fd,
                left_leaf,
                right_parent_fd,
                right_leaf,
            )
        except OSError as error:
            return _finish_manual_recovery(
                receipt,
                phase=_recovery_phase(resource, "exchange_failed", finish),
                failure_code="recovery_exchange_failed",
                failure_message=str(error),
                identities=identities,
                exchanges=exchanges,
            )

        left_restored = _observe_endpoint(
            left_parent_fd,
            left_leaf,
            expected_uid=owner,
            label="left recovery endpoint",
        )
        right_restored = _observe_endpoint(
            right_parent_fd,
            right_leaf,
            expected_uid=owner,
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
            _mapping_kind(
                left_restored,
                right_restored,
                left_original,
                right_original,
            )
            != "original"
        ):
            return _finish_manual_recovery(
                receipt,
                phase=_recovery_phase(resource, "rollback_unverified", finish),
                failure_code="rollback_verification_failed",
                failure_message="rollback did not restore the exact original mapping",
                identities=identities,
                exchanges=exchanges,
            )

        _fsync_parents(left_parent_fd, right_parent_fd)
        selected.update(
            {
                "rolled_back": True,
                "switched": True,
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
    resource: str = "standalone",
    finish_on_success: bool = True,
) -> TransactionReceipt:
    """Safely exchange two directories and durably verify the transposition."""
    if sys.platform != "darwin":
        raise OSError("atomic path exchange is supported only on macOS")

    owner = os.geteuid() if expected_uid is None else expected_uid
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
            expected_uid=owner,
        )
        descriptors.append(left_parent_fd)
        right_parent_fd = _open_verified_directory(
            right_path.parent,
            expected_uid=owner,
        )
        descriptors.append(right_parent_fd)
        left_endpoint_fd, left_original = _open_endpoint(
            left_parent_fd,
            left_leaf,
            expected_uid=owner,
            label="left exchange endpoint",
        )
        descriptors.append(left_endpoint_fd)
        right_endpoint_fd, right_original = _open_endpoint(
            right_parent_fd,
            right_leaf,
            expected_uid=owner,
            label="right exchange endpoint",
        )
        descriptors.append(right_endpoint_fd)
        _require_same_device(left_original, right_original)
        _require_same_device(os.fstat(left_parent_fd), left_original)
        _require_same_device(os.fstat(right_parent_fd), right_original)
    except BaseException as error:
        if receipt is not None and not finish_on_success:
            exchanges = dict(receipt.data.get("exchanges", {}))
            aggregate = _aggregate_exchange_state(exchanges)
            receipt.record_phase(
                "{}_exchange_preflight_failed".format(resource),
                failure_code="exchange_precondition",
                failure_message=str(error),
                exchanges=exchanges,
                **aggregate,
            )
        else:
            _finish_preflight_failure(receipt, error)
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise

    try:
        if receipt is None:
            receipt = _default_receipt(left_path.parent)

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
            **aggregate,
        )

        try:
            _rename_swap_at(
                left_parent_fd,
                left_leaf,
                right_parent_fd,
                right_leaf,
            )
        except Exception as error:
            left_current = _observe_endpoint(
                left_parent_fd,
                left_leaf,
                expected_uid=owner,
                label="left exchange endpoint",
            )
            right_current = _observe_endpoint(
                right_parent_fd,
                right_leaf,
                expected_uid=owner,
                label="right exchange endpoint",
            )
            mapping = _mapping_kind(
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
                        **aggregate,
                    )
                else:
                    receipt.record_phase(
                        "{}_exchange_failed_unchanged".format(resource),
                        failure_code="exchange_error",
                        failure_message=str(error),
                        exchanges=exchanges,
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
                    **aggregate,
                )
                try:
                    _rename_swap_at(
                        left_parent_fd,
                        left_leaf,
                        right_parent_fd,
                        right_leaf,
                    )
                except Exception as rollback_error:
                    resource_exchange["rollback_error"] = str(rollback_error)
                    exchanges[resource] = resource_exchange
                    receipt.finish(
                        "manual_recovery_required",
                        phase="{}_exchange_rollback_failed".format(resource),
                        switched=True,
                        rolled_back=False,
                        no_live_mutation=False,
                        failure_code="exchange_rollback_failed",
                        failure_message=str(rollback_error),
                        exchanges=exchanges,
                    )
                    raise RuntimeError(
                        "atomic exchange rollback failed; manual recovery required"
                    ) from rollback_error
                left_restored = _observe_endpoint(
                    left_parent_fd,
                    left_leaf,
                    expected_uid=owner,
                    label="left exchange endpoint",
                )
                right_restored = _observe_endpoint(
                    right_parent_fd,
                    right_leaf,
                    expected_uid=owner,
                    label="right exchange endpoint",
                )
                if (
                    _mapping_kind(
                        left_restored,
                        right_restored,
                        left_original,
                        right_original,
                    )
                    != "original"
                ):
                    resource_exchange["rollback_verified"] = False
                    exchanges[resource] = resource_exchange
                    receipt.finish(
                        "manual_recovery_required",
                        phase="{}_exchange_rollback_unverified".format(resource),
                        switched=True,
                        rolled_back=False,
                        no_live_mutation=False,
                        failure_code="rollback_verification_failed",
                        failure_message=str(error),
                        exchanges=exchanges,
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
                        **aggregate,
                    )
                else:
                    receipt.record_phase(
                        "{}_exchange_rolled_back".format(resource),
                        failure_code="exchange_error",
                        failure_message=str(error),
                        exchanges=exchanges,
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
            receipt.finish(
                "manual_recovery_required",
                phase="{}_exchange_mapping_ambiguous".format(resource),
                switched=_aggregate_exchange_state(exchanges)["switched"],
                rolled_back=False,
                no_live_mutation=False,
                failure_code="ambiguous_exchange_mapping",
                failure_message=str(error),
                exchanges=exchanges,
            )
            raise RuntimeError("exchange mapping is ambiguous; manual recovery required")

        left_current = _observe_endpoint(
            left_parent_fd,
            left_leaf,
            expected_uid=owner,
            label="left exchange endpoint",
        )
        right_current = _observe_endpoint(
            right_parent_fd,
            right_leaf,
            expected_uid=owner,
            label="right exchange endpoint",
        )
        if (
            _mapping_kind(
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
            receipt.finish(
                "manual_recovery_required",
                phase="{}_exchange_transposition_unverified".format(resource),
                switched=True,
                rolled_back=False,
                no_live_mutation=False,
                failure_code="transposition_verification_failed",
                failure_message="endpoint identities are not an exact transposition",
                exchanges=exchanges,
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
            )
        else:
            receipt.record_phase(
                "{}_exchange_verified".format(resource),
                switched=True,
                rolled_back=False,
                no_live_mutation=False,
                identities=identities,
                exchanges=exchanges,
            )
        return receipt
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit("atomic coordinator command interface is not implemented yet")
