"""Receipt-bound source-candidate paths for the macOS atomic updater."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Dict, Optional

from _atomic_macos_transaction import (
    PathLike,
    TransactionReceipt,
    _directory_open_flags,
    _identity_tuple,
    _open_verified_directory,
    _validated_directory_identity,
    validate_generated_leaf,
)


_CANDIDATE_MARKER_PREFIX = "hermes-source-candidate-v1:"
_CANDIDATE_SCHEMA_VERSION = 1
_MAX_CANDIDATE_COLLISIONS = 32


class CandidatePathError(RuntimeError):
    """A stable, receipt-safe candidate path validation failure."""

    def __init__(
        self,
        failure_code: str,
        failure_phase: str,
        message: str,
        *,
        candidate_path: Optional[Path] = None,
        candidate_artifact: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.failure_code = failure_code
        self.failure_phase = failure_phase
        self.candidate_path = candidate_path
        self.candidate_artifact = candidate_artifact
        super().__init__(message)


def _candidate_leaf(byte_length: int) -> str:
    """Generate one hidden ASCII leaf with exactly ``byte_length`` bytes."""
    prefix = ".hu-"
    if byte_length < len(prefix) + 4:
        raise ValueError("live checkout leaf is too short for a safe candidate name")
    suffix_length = byte_length - len(prefix)
    suffix = secrets.token_hex((suffix_length + 1) // 2)[:suffix_length]
    leaf = validate_generated_leaf(prefix + suffix)
    if len(os.fsencode(leaf)) != byte_length:
        raise RuntimeError("generated candidate leaf has the wrong byte length")
    return leaf


def _validate_live_root(live: Path, *, expected_uid: int) -> os.stat_result:
    observed = live.lstat()
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise ValueError("live checkout must be a real directory")
    if observed.st_uid != expected_uid:
        raise PermissionError("live checkout has unexpected owner")
    if observed.st_mode & 0o022:
        raise PermissionError("live checkout is writable by another account")
    return observed


def _validate_live_snapshot(
    live: Path,
    live_stat: os.stat_result,
    live_before: Any,
) -> None:
    if not isinstance(live_before, dict):
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "receipt has no live checkout identity",
        )
    try:
        expected_path = live_before["path"]
        expected_identity = (
            int(live_before["st_dev"]),
            int(live_before["st_ino"]),
        )
        expected_uid = int(live_before["st_uid"])
        expected_mode = int(live_before["st_mode"])
    except (KeyError, TypeError, ValueError):
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "receipt live checkout identity is incomplete",
        ) from None
    if (
        expected_path != str(live)
        or _identity_tuple(live_stat) != expected_identity
        or live_stat.st_uid != expected_uid
        or (stat.S_IFMT(live_stat.st_mode) | stat.S_IMODE(live_stat.st_mode))
        != expected_mode
    ):
        raise CandidatePathError(
            "live_checkout_changed",
            "candidate_identity_invalid",
            "live checkout identity changed after its recorded snapshot",
        )


def _candidate_binding(
    live: Path,
    candidate: Path,
    candidate_stat: os.stat_result,
    parent_stat: os.stat_result,
    transaction_id: str,
) -> Dict[str, Any]:
    return {
        "schema_version": _CANDIDATE_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "transaction_marker": _CANDIDATE_MARKER_PREFIX + transaction_id,
        "path": str(candidate),
        "future_live_path": str(live),
        "leaf": candidate.name,
        "leaf_byte_length": len(os.fsencode(candidate.name)),
        "st_dev": candidate_stat.st_dev,
        "st_ino": candidate_stat.st_ino,
        "st_uid": candidate_stat.st_uid,
        "mode": stat.S_IMODE(candidate_stat.st_mode),
        "parent": {
            "path": str(live.parent),
            "st_dev": parent_stat.st_dev,
            "st_ino": parent_stat.st_ino,
            "st_uid": parent_stat.st_uid,
        },
    }


def _candidate_artifact(
    candidate: Path,
    transaction_id: str,
) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {
        "path": str(candidate),
        "transaction_id": transaction_id,
        "transaction_marker": _CANDIDATE_MARKER_PREFIX + transaction_id,
    }
    try:
        observed = candidate.lstat()
    except OSError:
        evidence["identity_available"] = False
        return evidence
    evidence.update(
        {
            "identity_available": True,
            "st_dev": observed.st_dev,
            "st_ino": observed.st_ino,
            "st_uid": observed.st_uid,
            "mode": stat.S_IMODE(observed.st_mode),
            "kind": stat.S_IFMT(observed.st_mode),
        }
    )
    return evidence


def reserve_source_candidate(
    live_checkout: PathLike,
    *,
    receipt: TransactionReceipt,
    live_before: Dict[str, Any],
) -> Path:
    """Atomically reserve an owner-only, same-length sibling of ``live``."""
    if receipt.is_terminal:
        raise RuntimeError("cannot reserve a candidate using a terminal receipt")

    transaction_id = validate_generated_leaf(receipt.data.get("transaction_id"))
    live = Path(os.path.abspath(os.fspath(live_checkout)))
    owner = os.geteuid()
    live_stat = _validate_live_root(live, expected_uid=owner)
    leaf_byte_length = len(os.fsencode(live.name))
    parent_fd = _open_verified_directory(live.parent, expected_uid=owner)
    try:
        parent_stat = os.fstat(parent_fd)
        if parent_stat.st_mode & 0o022:
            raise PermissionError(
                "source candidate parent is writable by another account"
            )
        if live_stat.st_dev != parent_stat.st_dev:
            raise OSError(
                errno.EXDEV,
                "live checkout and its candidate parent are on different devices",
            )
        _validate_live_snapshot(live, live_stat, live_before)

        for _attempt in range(_MAX_CANDIDATE_COLLISIONS):
            leaf = validate_generated_leaf(_candidate_leaf(leaf_byte_length))
            if len(os.fsencode(leaf)) != leaf_byte_length:
                raise CandidatePathError(
                    "candidate_reservation_failed",
                    "candidate_reservation_failed",
                    "generated candidate leaf has the wrong byte length",
                )
            if leaf == live.name:
                continue
            candidate = live.parent / leaf
            try:
                os.mkdir(leaf, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue

            try:
                os.fsync(parent_fd)
                candidate_fd = None
                try:
                    before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                    candidate_fd = os.open(
                        leaf,
                        _directory_open_flags(),
                        dir_fd=parent_fd,
                    )
                    opened = os.fstat(candidate_fd)
                    candidate_stat = _validated_directory_identity(
                        before,
                        opened,
                        expected_uid=owner,
                        label="source candidate",
                    )
                    if stat.S_IMODE(candidate_stat.st_mode) != 0o700:
                        raise PermissionError("source candidate must be owner-only")
                    if candidate_stat.st_dev != live_stat.st_dev:
                        raise OSError(
                            errno.EXDEV,
                            "source candidate and live checkout are on different devices",
                        )
                finally:
                    if candidate_fd is not None:
                        os.close(candidate_fd)

                binding = _candidate_binding(
                    live,
                    candidate,
                    candidate_stat,
                    parent_stat,
                    transaction_id,
                )
                receipt.record_phase(
                    "candidate_reserved",
                    live_before=live_before,
                    candidate_path=str(candidate),
                    source_candidate=binding,
                )
                return candidate
            except BaseException as error:
                if isinstance(error, CandidatePathError):
                    raise
                raise CandidatePathError(
                    "candidate_reservation_failed",
                    "candidate_reservation_failed",
                    "source candidate reservation failed: {}".format(error),
                    candidate_path=candidate,
                    candidate_artifact=_candidate_artifact(
                        candidate,
                        transaction_id,
                    ),
                ) from error

        raise CandidatePathError(
            "candidate_reservation_failed",
            "candidate_reservation_failed",
            "could not reserve a collision-free source candidate",
        )
    finally:
        os.close(parent_fd)


def _require_binding_field(binding: Dict[str, Any], key: str) -> Any:
    if key not in binding:
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "source candidate receipt is missing {!r}".format(key),
        )
    return binding[key]


def validate_reserved_candidate(
    live_checkout: PathLike,
    *,
    receipt: TransactionReceipt,
) -> Path:
    """Revalidate the receipt-bound candidate through no-follow descriptors."""
    live = Path(os.path.abspath(os.fspath(live_checkout)))
    owner = os.geteuid()
    live_stat = _validate_live_root(live, expected_uid=owner)
    binding = receipt.data.get("source_candidate")
    if not isinstance(binding, dict):
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "receipt has no source candidate binding",
        )

    transaction_id = validate_generated_leaf(receipt.data.get("transaction_id"))
    if binding.get("schema_version") != _CANDIDATE_SCHEMA_VERSION:
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "source candidate receipt has an unsupported schema",
        )
    if binding.get("transaction_id") != transaction_id or binding.get(
        "transaction_marker"
    ) != _CANDIDATE_MARKER_PREFIX + transaction_id:
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "source candidate transaction marker does not match",
        )

    leaf = validate_generated_leaf(_require_binding_field(binding, "leaf"))
    candidate = Path(_require_binding_field(binding, "path"))
    expected_candidate = live.parent / leaf
    if candidate != expected_candidate:
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "source candidate path does not match its recorded parent and leaf",
        )
    if binding.get("future_live_path") != str(live):
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "source candidate future live path does not match",
        )
    if len(os.fsencode(leaf)) != len(os.fsencode(live.name)):
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "source candidate and live checkout names have different byte lengths",
        )

    parent_binding = binding.get("parent")
    if not isinstance(parent_binding, dict) or parent_binding.get("path") != str(
        live.parent
    ):
        raise CandidatePathError(
            "candidate_receipt_invalid",
            "candidate_identity_invalid",
            "source candidate parent binding is invalid",
        )

    parent_fd = _open_verified_directory(live.parent, expected_uid=owner)
    candidate_fd = None
    try:
        parent_stat = os.fstat(parent_fd)
        if parent_stat.st_mode & 0o022:
            raise CandidatePathError(
                "candidate_identity_changed",
                "candidate_identity_invalid",
                "source candidate parent became writable by another account",
            )
        expected_parent_identity = (
            int(_require_binding_field(parent_binding, "st_dev")),
            int(_require_binding_field(parent_binding, "st_ino")),
        )
        if _identity_tuple(parent_stat) != expected_parent_identity:
            raise CandidatePathError(
                "candidate_identity_changed",
                "candidate_identity_invalid",
                "source candidate parent identity changed",
            )
        if parent_stat.st_uid != int(_require_binding_field(parent_binding, "st_uid")):
            raise CandidatePathError(
                "candidate_identity_changed",
                "candidate_identity_invalid",
                "source candidate parent owner changed",
            )
        if live_stat.st_dev != parent_stat.st_dev:
            raise OSError(
                errno.EXDEV,
                "live checkout and source candidate parent are on different devices",
            )
        _validate_live_snapshot(live, live_stat, receipt.data.get("live_before"))

        before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        candidate_fd = os.open(
            leaf,
            _directory_open_flags(),
            dir_fd=parent_fd,
        )
        opened = os.fstat(candidate_fd)
        candidate_stat = _validated_directory_identity(
            before,
            opened,
            expected_uid=owner,
            label="source candidate",
        )
        expected_identity = (
            int(_require_binding_field(binding, "st_dev")),
            int(_require_binding_field(binding, "st_ino")),
        )
        if _identity_tuple(candidate_stat) != expected_identity:
            raise CandidatePathError(
                "candidate_identity_changed",
                "candidate_identity_invalid",
                "source candidate identity changed",
            )
        if candidate_stat.st_uid != int(_require_binding_field(binding, "st_uid")):
            raise CandidatePathError(
                "candidate_identity_changed",
                "candidate_identity_invalid",
                "source candidate owner changed",
            )
        if stat.S_IMODE(candidate_stat.st_mode) != 0o700:
            raise CandidatePathError(
                "candidate_identity_changed",
                "candidate_identity_invalid",
                "source candidate mode changed",
            )
        if candidate_stat.st_dev != live_stat.st_dev:
            raise OSError(
                errno.EXDEV,
                "source candidate and live checkout are on different devices",
            )
        return candidate
    except FileNotFoundError as error:
        raise CandidatePathError(
            "candidate_identity_changed",
            "candidate_identity_invalid",
            "source candidate identity changed: {}".format(error),
        ) from None
    finally:
        if candidate_fd is not None:
            os.close(candidate_fd)
        os.close(parent_fd)
