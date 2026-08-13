#!/usr/bin/env python3
"""Deployable facade for the macOS Hermes atomic-update engine."""

from __future__ import annotations

import ctypes
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional


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
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


_transaction = _load_sibling("_atomic_macos_transaction")
_candidate = _load_sibling("_atomic_macos_candidate")
_git = _load_sibling("_atomic_macos_git")

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
    raise SystemExit("atomic coordinator command interface is not implemented yet")
