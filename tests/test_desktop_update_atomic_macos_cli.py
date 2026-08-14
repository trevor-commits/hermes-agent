"""Process-level contract tests for the packaged atomic macOS recovery CLI."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Optional
import unittest


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
) -> subprocess.CompletedProcess:
    command = [
        PYTHON,
        str(runtime / "atomic_macos.py"),
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
        "--allow-test-root",
        *extra,
    ]
    process_environment = os.environ.copy()
    process_environment["HERMES_ATOMIC_TEST_ROOT"] = "1"
    if environment:
        process_environment.update(environment)
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=process_environment,
    )


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
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["commands"], ["recover"])
            self.assertEqual(payload["default_transactions_root"], "/Users/gillettes/.local/share/.hermes-update-transactions")

    def test_test_root_override_requires_both_flag_and_environment_sentinel(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(os.path.realpath(raw_tmp))
            runtime, binding = publish_real_runtime(root, "tx-test-root-gate")
            command = [
                PYTHON,
                str(runtime / "atomic_macos.py"),
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
            ]

            missing_flag = subprocess.run(command, check=False, capture_output=True, text=True)
            with_flag = subprocess.run(
                command + ["--allow-test-root"],
                check=False,
                capture_output=True,
                text=True,
                env={key: value for key, value in os.environ.items() if key != "HERMES_ATOMIC_TEST_ROOT"},
            )

            self.assertEqual(missing_flag.returncode, 64)
            self.assertEqual(with_flag.returncode, 64)
            self.assertEqual(json.loads(missing_flag.stdout)["failure_code"], "unsafe_transactions_root")
            self.assertEqual(json.loads(with_flag.stdout)["failure_code"], "unsafe_transactions_root")

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

                result = run_cli(runtime, binding, transaction_id, transactions_root)

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

                result = run_cli(runtime, binding, transaction_id, transactions_root)

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
            self.assertEqual(payload["resources"], ["source"])
            self.assertFalse(payload["manual_recovery_required"])
            self.assertEqual((live / "version.txt").read_text(), "old")
            self.assertEqual((candidate / "version.txt").read_text(), "new")

            terminal_receipt = transactions_root / transaction_id / "receipt.json"
            before_repeat = terminal_receipt.read_bytes()
            repeated = run_cli(runtime, binding, transaction_id, transactions_root)

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["status"], "failed_rolled_back")
            self.assertEqual(terminal_receipt.read_bytes(), before_repeat)

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
