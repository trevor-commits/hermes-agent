"""Process-level contract tests for the packaged atomic macOS recovery CLI."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Optional
import unittest
from unittest import mock


SOURCE_RUNTIME = Path(__file__).resolve().parents[1] / "scripts" / "desktop-update"
PYTHON = "/usr/bin/python3"
RUNTIME_MODES = {
    "posix.sh": 0o500,
    "atomic_macos.py": 0o500,
    "_atomic_macos_transaction.py": 0o400,
    "_atomic_macos_candidate.py": 0o400,
    "_atomic_macos_git.py": 0o400,
    "_atomic_macos_cli.py": 0o400,
    "serve-ui.py": 0o400,
    "ui.html": 0o400,
}
TRUSTED_TEST_BOOTSTRAP = """\
import json
import pathlib
import sys
import types

runtime = pathlib.Path(sys.argv[1])
modes = json.loads(sys.argv[2])
verified = {leaf: (runtime / leaf).read_bytes() for leaf in modes}
module = types.ModuleType("__main__")
namespace = module.__dict__
namespace.update({
    "__file__": str(runtime / "atomic_macos.py"),
    "__name__": "__main__",
    "_HERMES_TRUSTED_RUNTIME_BOOTSTRAP": True,
    "_HERMES_VERIFIED_RUNTIME_BYTES": verified,
    "_HERMES_VERIFIED_RUNTIME_MODES": modes,
    "_HERMES_VERIFIED_RUNTIME_TRANSACTION_ID": runtime.name,
})
sys.modules["__main__"] = module
sys.argv = [namespace["__file__"], *sys.argv[3:]]
exec(compile(verified["atomic_macos.py"], namespace["__file__"], "exec"), namespace, namespace)
"""


def canonical_manifest_bytes(transaction_id: str, runtime: Path) -> bytes:
    files = []
    for leaf, mode in sorted(RUNTIME_MODES.items()):
        payload = (runtime / leaf).read_bytes()
        files.append(
            {
                "mode": mode,
                "path": leaf,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    manifest = {
        "files": files,
        "principal": "hermes-atomic-update",
        "schema_version": 1,
        "transaction_id": transaction_id,
    }
    return (json.dumps(manifest, separators=(",", ":")) + "\n").encode("utf-8")


def publish_real_runtime(root: Path, transaction_id: str) -> tuple:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = root / transaction_id
    runtime.mkdir(mode=0o700)
    for leaf, mode in RUNTIME_MODES.items():
        shutil.copyfile(SOURCE_RUNTIME / leaf, runtime / leaf)
        os.chmod(runtime / leaf, mode)
    manifest_bytes = canonical_manifest_bytes(transaction_id, runtime)
    (runtime / "manifest.json").write_bytes(manifest_bytes)
    os.chmod(runtime / "manifest.json", 0o400)
    runtime_stat = runtime.lstat()
    binding = {
        "runtime_device": str(runtime_stat.st_dev),
        "runtime_inode": str(runtime_stat.st_ino),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    return runtime, binding


def create_original_receipt(
    runtime: Path,
    transactions_root: Path,
    transaction_id: str,
    live: Path,
    candidate: Path,
) -> None:
    probe = """\
import importlib.util
import pathlib
import sys

module_path = pathlib.Path(sys.argv[1])
transactions_root = pathlib.Path(sys.argv[2])
transaction_id = sys.argv[3]
live = pathlib.Path(sys.argv[4])
candidate = pathlib.Path(sys.argv[5])
spec = importlib.util.spec_from_file_location("cli_fixture_atomic", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
receipt = module.create_transaction(transactions_root, transaction_id)
module.atomic_exchange(
    live,
    candidate,
    receipt=receipt,
    resource="source",
    finish_on_success=False,
)
"""
    result = subprocess.run(
        [
            PYTHON,
            "-c",
            probe,
            str(runtime / "atomic_macos.py"),
            str(transactions_root),
            transaction_id,
            str(live),
            str(candidate),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


def create_multi_resource_receipt(
    runtime: Path,
    transactions_root: Path,
    transaction_id: str,
    exchanges: list,
) -> None:
    probe = """\
import importlib.util
import json
import pathlib
import sys

module_path = pathlib.Path(sys.argv[1])
transactions_root = pathlib.Path(sys.argv[2])
transaction_id = sys.argv[3]
exchanges = json.loads(sys.argv[4])
spec = importlib.util.spec_from_file_location("cli_multi_fixture_atomic", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
receipt = module.create_transaction(transactions_root, transaction_id)
for resource, live, candidate in exchanges:
    module.atomic_exchange(
        pathlib.Path(live),
        pathlib.Path(candidate),
        receipt=receipt,
        resource=resource,
        finish_on_success=False,
    )
"""
    result = subprocess.run(
        [
            PYTHON,
            "-c",
            probe,
            str(runtime / "atomic_macos.py"),
            str(transactions_root),
            transaction_id,
            json.dumps(exchanges),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


def replace_manifest(runtime: Path, binding: dict, mutate) -> None:
    manifest_path = runtime / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    raw_manifest = (json.dumps(manifest, separators=(",", ":")) + "\n").encode("utf-8")
    os.chmod(manifest_path, 0o600)
    manifest_path.write_bytes(raw_manifest)
    os.chmod(manifest_path, 0o400)
    binding["manifest_sha256"] = hashlib.sha256(raw_manifest).hexdigest()


def run_cli(
    runtime: Path,
    binding: dict,
    transaction_id: str,
    transactions_root: Path,
    *extra: str,
    environment: Optional[dict] = None,
    timeout: Optional[float] = None,
    trusted: bool = True,
) -> subprocess.CompletedProcess:
    arguments = [
        "recover",
        "--transaction",
        transaction_id,
        "--runtime-device",
        binding["runtime_device"],
        "--runtime-inode",
        binding["runtime_inode"],
        "--manifest-sha256",
        binding["manifest_sha256"],
        "--transactions-root",
        str(transactions_root),
        *extra,
    ]
    if trusted:
        command = trusted_cli_command(runtime, *arguments)
    else:
        command = [PYTHON, str(runtime / "atomic_macos.py"), *arguments]
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(environment)
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=process_environment,
        timeout=timeout,
    )


def trusted_cli_command(runtime: Path, *arguments: str) -> list:
    return [
        PYTHON,
        "-I",
        "-S",
        "-c",
        TRUSTED_TEST_BOOTSTRAP,
        str(runtime),
        json.dumps(RUNTIME_MODES, sort_keys=True),
        *arguments,
    ]


class AtomicMacosRecoveryCliTests(unittest.TestCase):
    def test_source_runtime_contains_the_focused_cli_module(self):
        self.assertTrue(
            (SOURCE_RUNTIME / "_atomic_macos_cli.py").is_file(),
            "canonical runtime is missing _atomic_macos_cli.py",
        )

    def test_capabilities_are_machine_readable_without_a_receipt(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            runtime, binding = publish_real_runtime(root, "tx-capabilities")
            result = subprocess.run(
                [
                    PYTHON,
                    str(runtime / "atomic_macos.py"),
                    "--capabilities",
                    "--runtime-device",
                    binding["runtime_device"],
                    "--runtime-inode",
                    binding["runtime_inode"],
                    "--manifest-sha256",
                    binding["manifest_sha256"],
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload,
                {"commands": ["recover"], "schema_version": 1},
            )
            self.assertNotIn("/Users/", result.stdout)
            self.assertNotIn("gillettes", result.stdout)

    def test_recovery_requires_an_explicit_caller_selected_transactions_root(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            runtime, binding = publish_real_runtime(root, "tx-test-root-gate")
            command = trusted_cli_command(
                runtime,
                "recover",
                "--transaction",
                "tx-test-root-gate",
                "--runtime-device",
                binding["runtime_device"],
                "--runtime-inode",
                binding["runtime_inode"],
                "--manifest-sha256",
                binding["manifest_sha256"],
                "--transactions-root",
                str(root / "transactions"),
            )

            selected_root = subprocess.run(command, check=False, capture_output=True, text=True)
            missing_root = subprocess.run(
                command[:-2],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(selected_root.returncode, 75)
            self.assertEqual(json.loads(selected_root.stdout)["failure_code"], "unsafe_transactions_root")
            self.assertEqual(missing_root.returncode, 64)
            self.assertEqual(json.loads(missing_root.stdout)["failure_code"], "invalid_arguments")

    def test_selected_transactions_root_must_be_canonical_owner_only_and_real(self):
        for mutation in ("mode", "symlink", "noncanonical"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(os.path.realpath(raw_tmp))
                runtime, binding = publish_real_runtime(root / "published", "tx-root-trust")
                real_root = root / "transactions"
                real_root.mkdir(mode=0o700)
                requested_root = real_root
                if mutation == "mode":
                    real_root.chmod(0o750)
                elif mutation == "symlink":
                    requested_root = root / "transactions-link"
                    requested_root.symlink_to(real_root, target_is_directory=True)
                else:
                    requested_root = real_root / ".." / "transactions"

                result = run_cli(
                    runtime,
                    binding,
                    "tx-root-trust",
                    requested_root,
                )

                self.assertEqual(result.returncode, 75, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)["failure_code"],
                    "unsafe_transactions_root",
                )
                self.assertEqual(result.stderr, "")

    def test_transactions_root_binding_rejects_an_ancestor_swap(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            active_parent = root / "active"
            alternate_parent = root / "alternate"
            active_parent.mkdir(mode=0o700)
            alternate_parent.mkdir(mode=0o700)
            requested_root = active_parent / "transactions"
            alternate_root = alternate_parent / "transactions"
            module_path = SOURCE_RUNTIME / "atomic_macos.py"
            spec = importlib.util.spec_from_file_location("cli_root_binding_atomic", module_path)
            atomic_macos = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = atomic_macos
            spec.loader.exec_module(atomic_macos)
            cli_module = atomic_macos._load_sibling("_atomic_macos_cli")
            transaction_id = "tx-ancestor-swap"
            intended = atomic_macos.create_transaction(requested_root, transaction_id)
            intended.finish("succeeded", phase="intended_terminal")

            live = root / "alternate-live"
            candidate = root / "alternate-candidate"
            live.mkdir()
            candidate.mkdir()
            (live / "version.txt").write_text("old", encoding="utf-8")
            (candidate / "version.txt").write_text("new", encoding="utf-8")
            alternate = atomic_macos.create_transaction(alternate_root, transaction_id)
            atomic_macos.atomic_exchange(
                live,
                candidate,
                receipt=alternate,
                resource="source",
                finish_on_success=False,
            )
            intended_before = intended.path.read_bytes()
            alternate_before = alternate.path.read_bytes()
            live_before = (live.stat().st_ino, (live / "version.txt").read_bytes())
            candidate_before = (candidate.stat().st_ino, (candidate / "version.txt").read_bytes())
            preserved_parent = root / "preserved-active"
            real_validate_transaction = cli_module._validate_transaction_directory
            swapped = False

            def swap_ancestor_then_validate(*args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.rename(active_parent, preserved_parent)
                    os.rename(alternate_parent, active_parent)
                return real_validate_transaction(*args, **kwargs)

            emitted = {}
            with mock.patch.object(
                cli_module,
                "_validate_transaction_directory",
                side_effect=swap_ancestor_then_validate,
            ), mock.patch.object(
                cli_module,
                "_emit",
                side_effect=lambda **payload: emitted.update(payload),
            ):
                result = cli_module.main(
                    atomic_macos,
                    [
                        "recover",
                        "--transaction",
                        transaction_id,
                        "--runtime-device",
                        "1",
                        "--runtime-inode",
                        "1",
                        "--manifest-sha256",
                        "0" * 64,
                        "--transactions-root",
                        str(requested_root),
                    ],
                    runtime_transaction_id=transaction_id,
                )

            self.assertEqual(result, 75)
            self.assertEqual(emitted["failure_code"], "unsafe_transactions_root")
            self.assertEqual((preserved_parent / "transactions" / transaction_id / "receipt.json").read_bytes(), intended_before)
            self.assertEqual((active_parent / "transactions" / transaction_id / "receipt.json").read_bytes(), alternate_before)
            self.assertEqual((live.stat().st_ino, (live / "version.txt").read_bytes()), live_before)
            self.assertEqual((candidate.stat().st_ino, (candidate / "version.txt").read_bytes()), candidate_before)

    def test_runtime_validation_happens_before_any_receipt_read(self):
        mutations = ("missing", "corrupt", "symlink", "digest", "inode")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(os.path.realpath(raw_tmp))
                transaction_id = "tx-runtime-{}".format(mutation)
                runtime, binding = publish_real_runtime(root, transaction_id)
                transactions_root = root / "missing-transactions"
                target = runtime / "_atomic_macos_transaction.py"
                if mutation == "missing":
                    target.unlink()
                elif mutation == "corrupt":
                    target.chmod(0o600)
                    target.write_text("corrupt\n", encoding="utf-8")
                    target.chmod(0o400)
                elif mutation == "symlink":
                    target.unlink()
                    target.symlink_to(runtime / "_atomic_macos_git.py")
                elif mutation == "digest":
                    binding["manifest_sha256"] = "0" * 64
                else:
                    binding["runtime_inode"] = str(int(binding["runtime_inode"]) + 1)

                result = run_cli(
                    runtime,
                    binding,
                    transaction_id,
                    transactions_root,
                    trusted=False,
                )

                self.assertEqual(result.returncode, 64, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["failure_code"], "runtime_validation_failed")
                self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)
                self.assertEqual(result.stderr, "")
                self.assertFalse(transactions_root.exists())

    def test_runtime_validation_requires_exact_canonical_identity_and_manifest(self):
        mutations = (
            "directory_mode",
            "parent_directory_mode",
            "leading_decimal",
            "transaction",
            "manifest_key",
            "member_mode",
            "extra_asset",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(os.path.realpath(raw_tmp))
                transaction_id = "tx-exact-{}".format(mutation)
                runtime, binding = publish_real_runtime(root / "runtime", transaction_id)
                transactions_root = root / "missing-transactions"
                if mutation == "directory_mode":
                    os.chmod(runtime, 0o755)
                elif mutation == "parent_directory_mode":
                    os.chmod(runtime.parent, 0o755)
                elif mutation == "leading_decimal":
                    binding["runtime_inode"] = "0" + binding["runtime_inode"]
                elif mutation == "transaction":
                    replace_manifest(runtime, binding, lambda manifest: manifest.update(transaction_id="other-transaction"))
                elif mutation == "manifest_key":
                    replace_manifest(runtime, binding, lambda manifest: manifest.update(unexpected=True))
                elif mutation == "member_mode":
                    def change_mode(manifest):
                        manifest["files"][0]["mode"] = 0o600
                    replace_manifest(runtime, binding, change_mode)
                else:
                    (runtime / "unexpected.py").write_text("unexpected\n", encoding="utf-8")

                result = run_cli(
                    runtime,
                    binding,
                    transaction_id,
                    transactions_root,
                    trusted=False,
                )

                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertEqual(json.loads(result.stdout)["failure_code"], "runtime_validation_failed")
                self.assertEqual(result.stderr, "")
                self.assertFalse(transactions_root.exists())

    def test_rejects_unsafe_transaction_leaf_before_receipt_access(self):
        for transaction_id in ("../unsafe", "other-safe-transaction"):
            with self.subTest(transaction_id=transaction_id), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(os.path.realpath(raw_tmp))
                runtime, binding = publish_real_runtime(root, "tx-safe-runtime")
                transactions_root = root / "missing-transactions"

                result = run_cli(runtime, binding, transaction_id, transactions_root)

                self.assertEqual(result.returncode, 64)
                self.assertEqual(json.loads(result.stdout)["failure_code"], "invalid_transaction")
                self.assertFalse(transactions_root.exists())

    def test_direct_pathname_recovery_is_explicitly_unauthenticated(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            transaction_id = "tx-direct-unauthenticated"
            runtime, binding = publish_real_runtime(root / "runtime", transaction_id)
            transactions_root = root / "transactions"
            live = root / "live"
            candidate = root / "candidate"
            live.mkdir()
            candidate.mkdir()
            (live / "version.txt").write_text("old", encoding="utf-8")
            (candidate / "version.txt").write_text("new", encoding="utf-8")
            create_original_receipt(
                runtime,
                transactions_root,
                transaction_id,
                live,
                candidate,
            )
            receipt_path = transactions_root / transaction_id / "receipt.json"
            receipt_before = receipt_path.read_bytes()
            live_before = (live.stat().st_ino, (live / "version.txt").read_bytes())
            candidate_before = (
                candidate.stat().st_ino,
                (candidate / "version.txt").read_bytes(),
            )

            result = run_cli(
                runtime,
                binding,
                transaction_id,
                transactions_root,
                trusted=False,
            )

            self.assertEqual(result.returncode, 64, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                json.loads(result.stdout)["failure_code"],
                "unauthenticated_entrypoint",
            )
            self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)
            self.assertEqual(receipt_path.read_bytes(), receipt_before)
            self.assertEqual(
                (live.stat().st_ino, (live / "version.txt").read_bytes()),
                live_before,
            )
            self.assertEqual(
                (candidate.stat().st_ino, (candidate / "version.txt").read_bytes()),
                candidate_before,
            )

    def test_successful_noop_recovery_emits_a_bounded_terminal_result(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            transaction_id = "tx-cli-noop"
            runtime, binding = publish_real_runtime(root / "runtime", transaction_id)
            transactions_root = root / "transactions"
            live = root / "live"
            candidate = root / "candidate"
            live.mkdir()
            candidate.mkdir()
            (live / "version.txt").write_text("old", encoding="utf-8")
            (candidate / "version.txt").write_text("new", encoding="utf-8")
            create_original_receipt(runtime, transactions_root, transaction_id, live, candidate)
            os.rename(live, root / "swapped-live")
            os.rename(candidate, live)
            os.rename(root / "swapped-live", candidate)

            result = run_cli(runtime, binding, transaction_id, transactions_root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "failed_rolled_back")
            self.assertNotIn("failure_code", payload)
            self.assertEqual(payload["resources"], ["source"])
            self.assertFalse(payload["manual_recovery_required"])
            self.assertEqual((live / "version.txt").read_text(), "old")
            self.assertEqual((candidate / "version.txt").read_text(), "new")

            terminal_receipt = transactions_root / transaction_id / "receipt.json"
            before_repeat = terminal_receipt.read_bytes()
            repeated = run_cli(runtime, binding, transaction_id, transactions_root)

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            repeated_payload = json.loads(repeated.stdout)
            self.assertEqual(repeated_payload["status"], "failed_rolled_back")
            self.assertNotIn("failure_code", repeated_payload)
            self.assertEqual(terminal_receipt.read_bytes(), before_repeat)

            os.chmod(terminal_receipt.parent, 0o500)
            try:
                unsafe_repeat = run_cli(
                    runtime,
                    binding,
                    transaction_id,
                    transactions_root,
                )
            finally:
                os.chmod(terminal_receipt.parent, 0o700)

            self.assertEqual(unsafe_repeat.returncode, 75, unsafe_repeat.stderr)
            self.assertEqual(
                json.loads(unsafe_repeat.stdout)["failure_code"],
                "unsafe_transaction_directory",
            )
            self.assertEqual(terminal_receipt.read_bytes(), before_repeat)

    def test_rejects_transaction_directories_without_exact_mode_before_receipt_access(self):
        for mode in (0o710, 0o750, 0o701):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(os.path.realpath(raw_tmp))
                transaction_id = "tx-dir-mode-{:o}".format(mode)
                runtime, binding = publish_real_runtime(root / "runtime", transaction_id)
                transactions_root = root / "transactions"
                transaction_dir = transactions_root / transaction_id
                transactions_root.mkdir(mode=0o700)
                transaction_dir.mkdir(mode=0o700)
                receipt_path = transaction_dir / "receipt.json"
                receipt_path.write_text("not valid JSON and must not be read\n", encoding="utf-8")
                os.chmod(receipt_path, 0o600)
                os.chmod(transaction_dir, mode)

                try:
                    result = run_cli(runtime, binding, transaction_id, transactions_root)
                finally:
                    os.chmod(transaction_dir, 0o700)

                self.assertEqual(result.returncode, 75, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout)["failure_code"],
                    "unsafe_transaction_directory",
                )
                self.assertEqual(result.stderr, "")

    def test_held_transaction_lock_returns_retryable_bounded_failure(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            transaction_id = "tx-cli-lock-held"
            runtime, binding = publish_real_runtime(root / "runtime", transaction_id)
            transactions_root = root / "transactions"
            live = root / "live"
            candidate = root / "candidate"
            live.mkdir()
            candidate.mkdir()
            create_original_receipt(
                runtime,
                transactions_root,
                transaction_id,
                live,
                candidate,
            )
            receipt_path = transactions_root / transaction_id / "receipt.json"
            receipt_before = receipt_path.read_bytes()
            lock_path = transactions_root / transaction_id / ".transaction.lock"
            locker = subprocess.Popen(
                [
                    PYTHON,
                    "-c",
                    "import fcntl, os, sys; fd=os.open(sys.argv[1], os.O_RDWR); "
                    "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); "
                    "sys.stdin.readline()",
                    str(lock_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(locker.stdout.readline().strip(), "locked")
            started = time.monotonic()
            try:
                try:
                    result = run_cli(
                        runtime,
                        binding,
                        transaction_id,
                        transactions_root,
                        timeout=3.0,
                    )
                except subprocess.TimeoutExpired:
                    self.fail("published recovery CLI blocked on the transaction lock")
            finally:
                locker.stdin.write("release\n")
                locker.stdin.flush()
                locker.wait(timeout=2)
                locker.stdin.close()
                locker.stdout.close()
                locker.stderr.close()
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 2.5)
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["failure_code"], "transaction_lock_unavailable")
            self.assertEqual(payload["status"], "unrecovered")
            self.assertFalse(payload["manual_recovery_required"])
            self.assertEqual(receipt_path.read_bytes(), receipt_before)

            retried = run_cli(runtime, binding, transaction_id, transactions_root)
            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertEqual(json.loads(retried.stdout)["status"], "failed_rolled_back")

    def test_queued_repeat_after_another_cli_finishes_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            transaction_id = "tx-cli-queued-repeat"
            runtime, binding = publish_real_runtime(root / "runtime", transaction_id)
            transactions_root = root / "transactions"
            resources = {}
            exchanges = []
            for resource in ("source", "app"):
                live = root / "{}-live".format(resource)
                candidate = root / "{}-candidate".format(resource)
                live.mkdir()
                candidate.mkdir()
                (live / "version.txt").write_text("old", encoding="utf-8")
                (candidate / "version.txt").write_text("new", encoding="utf-8")
                resources[resource] = (live, candidate)
                exchanges.append((resource, str(live), str(candidate)))
            create_multi_resource_receipt(
                runtime,
                transactions_root,
                transaction_id,
                exchanges,
            )

            lock_path = transactions_root / transaction_id / ".transaction.lock"
            locker = subprocess.Popen(
                [
                    PYTHON,
                    "-c",
                    "import fcntl, os, sys; fd=os.open(sys.argv[1], os.O_RDWR); "
                    "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); "
                    "sys.stdin.readline()",
                    str(lock_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(locker.stdout.readline().strip(), "locked")
            command = trusted_cli_command(
                runtime,
                "recover",
                "--transaction",
                transaction_id,
                "--runtime-device",
                binding["runtime_device"],
                "--runtime-inode",
                binding["runtime_inode"],
                "--manifest-sha256",
                binding["manifest_sha256"],
                "--transactions-root",
                str(transactions_root),
            )
            environment = os.environ.copy()
            queued = [
                subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                for _ in range(2)
            ]
            time.sleep(0.4)
            locker.stdin.write("release\n")
            locker.stdin.flush()
            locker.wait(timeout=2)
            locker.stdin.close()
            locker.stdout.close()
            locker.stderr.close()
            completed = []
            for process in queued:
                stdout, stderr = process.communicate(timeout=3)
                completed.append((process.returncode, stdout, stderr))

            self.assertEqual([returncode for returncode, _, _ in completed], [0, 0])
            payloads = [json.loads(stdout) for _, stdout, _ in completed]
            self.assertTrue(all(stderr == "" for _, _, stderr in completed))
            self.assertTrue(all(payload["status"] == "failed_rolled_back" for payload in payloads))
            self.assertTrue(all("failure_code" not in payload for payload in payloads))
            self.assertCountEqual(
                [tuple(payload["resources"]) for payload in payloads],
                [("app", "source"), ()],
            )
            for live, candidate in resources.values():
                self.assertEqual((live / "version.txt").read_text(), "old")
                self.assertEqual((candidate / "version.txt").read_text(), "new")

    def test_recovery_processes_recorded_exchanges_app_then_source(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            transaction_id = "tx-cli-multi"
            runtime, binding = publish_real_runtime(root / "runtime", transaction_id)
            transactions_root = root / "transactions"
            resources = {}
            exchanges = []
            for resource in ("source", "app"):
                live = root / "{}-live".format(resource)
                candidate = root / "{}-candidate".format(resource)
                live.mkdir()
                candidate.mkdir()
                (live / "version.txt").write_text("{}-old".format(resource), encoding="utf-8")
                (candidate / "version.txt").write_text("{}-new".format(resource), encoding="utf-8")
                resources[resource] = (live, candidate)
                exchanges.append((resource, str(live), str(candidate)))
            create_multi_resource_receipt(runtime, transactions_root, transaction_id, exchanges)

            result = run_cli(runtime, binding, transaction_id, transactions_root)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["resources"], ["app", "source"])
            self.assertEqual(payload["status"], "failed_rolled_back")
            self.assertNotIn("failure_code", payload)
            for resource, (live, candidate) in resources.items():
                self.assertEqual((live / "version.txt").read_text(), "{}-old".format(resource))
                self.assertEqual((candidate / "version.txt").read_text(), "{}-new".format(resource))

    def test_ambiguous_recovery_is_nonzero_and_honestly_manual(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            transaction_id = "tx-cli-ambiguous"
            runtime, binding = publish_real_runtime(root / "runtime", transaction_id)
            transactions_root = root / "transactions"
            live = root / "live"
            candidate = root / "candidate"
            live.mkdir()
            candidate.mkdir()
            (live / "version.txt").write_text("old", encoding="utf-8")
            (candidate / "version.txt").write_text("new", encoding="utf-8")
            create_original_receipt(runtime, transactions_root, transaction_id, live, candidate)
            preserved = root / "preserved"
            unexpected = root / "unexpected"
            unexpected.mkdir()
            os.rename(live, preserved)
            os.rename(unexpected, live)

            result = run_cli(runtime, binding, transaction_id, transactions_root)

            self.assertEqual(result.returncode, 75, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "manual_recovery_required")
            self.assertTrue(payload["manual_recovery_required"])
            self.assertEqual(payload["failure_code"], "ambiguous_exchange_mapping")
            self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)
            self.assertEqual(result.stderr, "")
            self.assertTrue(preserved.is_dir())


if __name__ == "__main__":
    unittest.main()
