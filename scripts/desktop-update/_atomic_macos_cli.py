"""Command-line interface for the external atomic macOS updater runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re


DEFAULT_TRANSACTIONS_ROOT = "/Users/gillettes/.local/share/.hermes-update-transactions"
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
_FLAG_OPTIONS = frozenset({"--allow-test-root", "--capabilities"})
_TERMINAL_RECOVERED = frozenset(
    {"succeeded", "failed_unchanged", "failed_rolled_back"}
)


class _UsageError(ValueError):
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
        "failure_code": safe_failure,
        "manual_recovery_required": safe_status == "manual_recovery_required",
        "ok": bool(ok),
        "resources": [resource for resource in resources if resource in ("app", "source")],
        "schema_version": 1,
        "status": safe_status,
    }
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
        "default_transactions_root": DEFAULT_TRANSACTIONS_ROOT,
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


def _recover(engine, transaction_dir, resources):
    current_dir = transaction_dir
    recovered = []
    receipt = None
    for index, resource in enumerate(resources):
        receipt = engine.recover_exchange(
            current_dir,
            resource=resource,
            finish=index == len(resources) - 1,
        )
        current_dir = receipt.transaction_dir
        recovered.append(resource)
        if receipt.data.get("status") == "manual_recovery_required":
            break
        if receipt.is_terminal and index != len(resources) - 1:
            break
    if receipt is None:
        raise RuntimeError("no recovery was attempted")
    return receipt, recovered


def main(engine, arguments, *, runtime_transaction_id):
    try:
        values, flags, positionals = _parse(arguments)
        if "--capabilities" in flags:
            if positionals or "--transaction" in values or "--transactions-root" in values or "--allow-test-root" in flags:
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

        requested_root = values.get("--transactions-root", DEFAULT_TRANSACTIONS_ROOT)
        transactions_root = os.path.abspath(requested_root)
        if transactions_root != DEFAULT_TRANSACTIONS_ROOT and (
            "--allow-test-root" not in flags
            or os.environ.get("HERMES_ATOMIC_TEST_ROOT") != "1"
        ):
            raise _UsageError("unsafe_transactions_root")

        transaction_dir = Path(transactions_root) / transaction_id
        receipt = engine.load_transaction(transaction_dir)
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
        recovered, processed = _recover(engine, transaction_dir, resources)
        status = recovered.data.get("status")
        failure_code = recovered.data.get("failure_code")
        if status in _TERMINAL_RECOVERED and len(processed) == len(resources):
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
    except Exception:
        _emit(ok=False, status="unrecovered", failure_code="recovery_failed")
        return 75
