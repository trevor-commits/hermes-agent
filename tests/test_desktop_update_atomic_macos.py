"""Regression tests for the macOS atomic desktop-update coordinator."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "desktop-update"
    / "atomic_macos.py"
)


def load_atomic_macos():
    """Load the deployable helper from its external-runtime source path."""
    assert MODULE_PATH.is_file(), "the atomic macOS coordinator does not exist"
    spec = importlib.util.spec_from_file_location("hermes_atomic_macos", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def altered_stat(file_stat, *, device=None, inode=None, owner=None, mode=None):
    """Return a stat_result with selected identity fields changed."""
    values = list(file_stat)
    if mode is not None:
        values[0] = mode
    if inode is not None:
        values[1] = inode
    if device is not None:
        values[2] = device
    if owner is not None:
        values[4] = owner
    return os.stat_result(values)


def run_git(repository, *arguments, check=True):
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Hermes Test",
            "GIT_AUTHOR_EMAIL": "hermes-test@example.invalid",
            "GIT_COMMITTER_NAME": "Hermes Test",
            "GIT_COMMITTER_EMAIL": "hermes-test@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    command = ["git"]
    if repository is not None:
        command.extend(["-C", str(repository)])
    command.extend(arguments)
    return subprocess.run(
        command,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )


def commit_file(repository, relative_path, contents, subject):
    path = repository / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    run_git(repository, "add", "--", relative_path)
    run_git(repository, "commit", "-m", subject)
    return run_git(repository, "rev-parse", "HEAD").stdout.strip()


def checkout_fingerprint(repository):
    head = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    status_output = run_git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout
    digest = hashlib.sha256()
    for path in sorted(
        (
            entry
            for entry in repository.rglob("*")
            if ".git" not in entry.relative_to(repository).parts
        ),
        key=lambda entry: os.fsencode(str(entry.relative_to(repository))),
    ):
        relative = os.fsencode(str(path.relative_to(repository)))
        path_stat = path.lstat()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(stat.S_IFMT(path_stat.st_mode).to_bytes(8, "big"))
        digest.update(stat.S_IMODE(path_stat.st_mode).to_bytes(8, "big"))
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(path.read_bytes())
    return {
        "head": head,
        "status": status_output,
        "tree_digest": digest.hexdigest(),
    }


class TransactionReceiptTests(unittest.TestCase):
    def test_generated_leaf_validation_rejects_unsafe_names(self):
        atomic_macos = load_atomic_macos()

        for leaf in ("", ".", "..", "/absolute", "nested/name", "nested\\name", "bad\0name"):
            with self.subTest(leaf=leaf):
                with self.assertRaises((TypeError, ValueError)):
                    atomic_macos.validate_generated_leaf(leaf)

        with self.assertRaises((TypeError, ValueError)):
            atomic_macos.validate_generated_leaf(None)

    def test_receipt_writes_schema_v1_atomically_in_owner_only_directory(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            transactions_root = Path(raw_tmp) / "transactions"
            receipt = atomic_macos.create_transaction(
                transactions_root,
                transaction_id="txn-fixed",
            )
            initial_inode = receipt.path.stat().st_ino

            self.assertEqual(receipt.transaction_dir.name, "txn-fixed")
            self.assertEqual(receipt.transaction_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(receipt.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(receipt.data["schema_version"], 1)
            self.assertEqual(receipt.data["status"], "in_progress")
            self.assertEqual(receipt.data["phase"], "created")
            self.assertFalse(receipt.data["ok"])
            self.assertFalse(receipt.data["switched"])
            self.assertFalse(receipt.data["rolled_back"])
            self.assertTrue(receipt.data["no_live_mutation"])
            self.assertEqual(receipt.data["identities"], {})
            self.assertIsNone(receipt.data["failure_code"])
            self.assertIsNone(receipt.data["failure_message"])
            self.assertIsNone(receipt.data["completed_at"])
            self.assertTrue(receipt.data["created_at"].endswith("Z"))
            self.assertTrue(receipt.data["updated_at"].endswith("Z"))

            receipt.finish(
                "succeeded",
                phase="exchange_verified",
                switched=True,
                rolled_back=False,
                no_live_mutation=False,
            )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "succeeded")
            self.assertTrue(persisted.data["ok"])
            self.assertTrue(persisted.data["switched"])
            self.assertFalse(persisted.data["rolled_back"])
            self.assertFalse(persisted.data["no_live_mutation"])
            self.assertTrue(persisted.data["completed_at"].endswith("Z"))
            self.assertNotEqual(persisted.path.stat().st_ino, initial_inode)
            self.assertEqual(list(receipt.transaction_dir.glob("*.tmp")), [])

            with self.assertRaisesRegex(RuntimeError, "terminal"):
                persisted.finish(
                    "failed_unchanged",
                    phase="too_late",
                    failure_code="late_write",
                    failure_message="terminal receipts are immutable",
                )

    def test_receipt_rejects_unknown_terminal_status(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            receipt = atomic_macos.create_transaction(Path(raw_tmp), "txn-fixed")

            with self.assertRaisesRegex(ValueError, "terminal status"):
                receipt.finish("almost_done", phase="invalid")


@unittest.skipUnless(sys.platform == "darwin", "macOS renameatx_np contract")
class AtomicExchangeTests(unittest.TestCase):
    def test_exchange_swaps_two_directories_without_sequential_rename(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "live"
            candidate = root / "candidate"
            live.mkdir()
            candidate.mkdir()
            (live / "version.txt").write_text("old", encoding="utf-8")
            (candidate / "version.txt").write_text("new", encoding="utf-8")
            live_inode = live.stat().st_ino
            candidate_inode = candidate.stat().st_ino
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-swap")

            with mock.patch.object(
                os,
                "rename",
                side_effect=AssertionError("sequential rename is not atomic"),
            ):
                returned_receipt = atomic_macos.atomic_exchange(
                    live,
                    candidate,
                    receipt=receipt,
                )

            self.assertIs(returned_receipt, receipt)
            self.assertTrue(live.is_dir())
            self.assertTrue(candidate.is_dir())
            self.assertEqual((live / "version.txt").read_text(), "new")
            self.assertEqual((candidate / "version.txt").read_text(), "old")
            self.assertEqual(live.stat().st_ino, candidate_inode)
            self.assertEqual(candidate.stat().st_ino, live_inode)
            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(persisted["status"], "succeeded")
            self.assertEqual(persisted["phase"], "exchange_verified")
            self.assertTrue(persisted["switched"])
            self.assertEqual(
                persisted["identities"]["before"]["left"]["st_ino"],
                live_inode,
            )
            self.assertEqual(
                persisted["identities"]["after"]["left"]["st_ino"],
                candidate_inode,
            )

    def test_exchange_can_record_verified_step_without_terminalizing_shared_receipt(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_live = root / "source-live"
            source_candidate = root / "source-candidate"
            app_live = root / "app-live"
            app_candidate = root / "app-candidate"
            for path, contents in (
                (source_live, "source-old"),
                (source_candidate, "source-new"),
                (app_live, "app-old"),
                (app_candidate, "app-new"),
            ):
                path.mkdir()
                (path / "version.txt").write_text(contents, encoding="utf-8")
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-multi")

            atomic_macos.atomic_exchange(
                source_live,
                source_candidate,
                receipt=receipt,
                resource="source",
                finish_on_success=False,
            )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "in_progress")
            self.assertEqual(persisted.data["phase"], "source_exchange_verified")
            self.assertFalse(persisted.data["ok"])
            self.assertTrue(persisted.data["switched"])
            self.assertIn("source", persisted.data["exchanges"])
            self.assertTrue(persisted.data["exchanges"]["source"]["verified"])

            atomic_macos.atomic_exchange(
                app_live,
                app_candidate,
                receipt=persisted,
                resource="app",
                finish_on_success=False,
            )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "in_progress")
            self.assertEqual(persisted.data["phase"], "app_exchange_verified")
            self.assertEqual(set(persisted.data["exchanges"]), {"source", "app"})
            self.assertEqual((source_live / "version.txt").read_text(), "source-new")
            self.assertEqual((app_live / "version.txt").read_text(), "app-new")

            persisted.finish(
                "succeeded",
                phase="all_exchanges_verified",
                switched=True,
                rolled_back=False,
                no_live_mutation=False,
            )
            terminal = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(terminal.data["status"], "succeeded")
            self.assertTrue(terminal.data["ok"])

    def test_exchange_rejects_a_symlink_endpoint(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "live"
            candidate_real = root / "candidate-real"
            candidate_link = root / "candidate"
            live.mkdir()
            candidate_real.mkdir()
            candidate_link.symlink_to(candidate_real, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                atomic_macos.atomic_exchange(live, candidate_link)

            self.assertTrue(live.is_dir())
            self.assertTrue(candidate_link.is_symlink())

    def test_exchange_rejects_a_non_directory_endpoint(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "live"
            candidate = root / "candidate"
            live.mkdir()
            candidate.write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "directory"):
                atomic_macos.atomic_exchange(live, candidate)

            self.assertTrue(live.is_dir())
            self.assertTrue(candidate.is_file())

    def test_exchange_rejects_a_symlink_parent(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            real_parent = root / "real-parent"
            linked_parent = root / "linked-parent"
            real_parent.mkdir()
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            (real_parent / "live").mkdir()
            (real_parent / "candidate").mkdir()

            with self.assertRaisesRegex(ValueError, "symlink"):
                atomic_macos.atomic_exchange(
                    linked_parent / "live",
                    linked_parent / "candidate",
                )

            self.assertTrue((real_parent / "live").is_dir())
            self.assertTrue((real_parent / "candidate").is_dir())

    def test_opened_endpoint_identity_must_match_lstat(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            endpoint = Path(raw_tmp) / "endpoint"
            endpoint.mkdir()
            observed = endpoint.lstat()
            changed = altered_stat(observed, inode=observed.st_ino + 1)

            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                atomic_macos._validated_endpoint_identity(
                    observed,
                    changed,
                    expected_uid=os.geteuid(),
                    label="endpoint",
                )

    def test_endpoint_owner_must_match_expected_owner(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            endpoint = Path(raw_tmp) / "endpoint"
            endpoint.mkdir()
            observed = endpoint.lstat()
            wrong_owner = altered_stat(observed, owner=os.geteuid() + 1)

            with self.assertRaisesRegex(PermissionError, "owner"):
                atomic_macos._validated_endpoint_identity(
                    wrong_owner,
                    wrong_owner,
                    expected_uid=os.geteuid(),
                    label="endpoint",
                )

    def test_exchange_endpoints_must_be_on_the_same_device(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            endpoint = Path(raw_tmp) / "endpoint"
            endpoint.mkdir()
            observed = endpoint.lstat()
            other_device = altered_stat(observed, device=observed.st_dev + 1)

            with self.assertRaisesRegex(OSError, "same device") as raised:
                atomic_macos._require_same_device(observed, other_device)

            self.assertEqual(raised.exception.errno, errno.EXDEV)

    def test_missing_renameatx_symbol_fails_closed(self):
        atomic_macos = load_atomic_macos()

        with mock.patch.object(atomic_macos.ctypes, "CDLL", return_value=object()):
            with self.assertRaisesRegex(RuntimeError, "renameatx_np"):
                atomic_macos._load_rename_swap()

    def test_kernel_error_has_durable_intent_and_no_sequential_fallback(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "live"
            candidate = root / "candidate"
            live.mkdir()
            candidate.mkdir()
            live_inode = live.stat().st_ino
            candidate_inode = candidate.stat().st_ino
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-error")

            def fail_exchange(*_args):
                persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
                self.assertEqual(persisted["phase"], "standalone_exchange_intent")
                self.assertFalse(persisted["switched"])
                self.assertEqual(
                    persisted["identities"]["before"]["left"]["st_ino"],
                    live_inode,
                )
                raise OSError(errno.EXDEV, "forced cross-device failure")

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=fail_exchange,
            ), mock.patch.object(
                os,
                "rename",
                side_effect=AssertionError("sequential rename fallback forbidden"),
            ):
                with self.assertRaisesRegex(OSError, "forced cross-device"):
                    atomic_macos.atomic_exchange(live, candidate, receipt=receipt)

            self.assertEqual(live.stat().st_ino, live_inode)
            self.assertEqual(candidate.stat().st_ino, candidate_inode)
            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(persisted["status"], "failed_unchanged")
            self.assertEqual(persisted["failure_code"], "exchange_error")
            self.assertFalse(persisted["switched"])
            self.assertTrue(persisted["no_live_mutation"])

    def test_later_resource_failure_keeps_shared_receipt_recoverable(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_live = root / "source-live"
            source_candidate = root / "source-candidate"
            app_live = root / "app-live"
            app_candidate = root / "app-candidate"
            for path, contents in (
                (source_live, "source-old"),
                (source_candidate, "source-new"),
                (app_live, "app-old"),
                (app_candidate, "app-new"),
            ):
                path.mkdir()
                (path / "version.txt").write_text(contents, encoding="utf-8")
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-later-failure")
            atomic_macos.atomic_exchange(
                source_live,
                source_candidate,
                receipt=receipt,
                resource="source",
                finish_on_success=False,
            )

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=OSError(errno.ENOTSUP, "forced unsupported exchange"),
            ):
                with self.assertRaisesRegex(OSError, "forced unsupported"):
                    atomic_macos.atomic_exchange(
                        app_live,
                        app_candidate,
                        receipt=receipt,
                        resource="app",
                        finish_on_success=False,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "in_progress")
            self.assertEqual(persisted.data["phase"], "app_exchange_failed_unchanged")
            self.assertTrue(persisted.data["switched"])
            self.assertFalse(persisted.data["no_live_mutation"])
            self.assertTrue(persisted.data["exchanges"]["source"]["verified"])
            self.assertEqual(
                persisted.data["exchanges"]["app"]["failure_code"],
                "exchange_error",
            )
            recovered = atomic_macos.recover_exchange(
                receipt.transaction_dir,
                resource="source",
                finish=False,
            )
            self.assertEqual((source_live / "version.txt").read_text(), "source-old")
            self.assertEqual((app_live / "version.txt").read_text(), "app-old")
            self.assertTrue(recovered.data["exchanges"]["source"]["rolled_back"])


@unittest.skipUnless(sys.platform == "darwin", "macOS renameatx_np contract")
class ExchangeRecoveryTests(unittest.TestCase):
    def _exchange_paths(self, root):
        live = root / "live"
        candidate = root / "candidate"
        live.mkdir()
        candidate.mkdir()
        (live / "version.txt").write_text("old", encoding="utf-8")
        (candidate / "version.txt").write_text("new", encoding="utf-8")
        return live, candidate

    def test_original_mapping_is_a_no_op_verified_rollback(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live, candidate = self._exchange_paths(root)
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-original")

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=SystemExit("simulated crash before kernel call"),
            ):
                with self.assertRaisesRegex(SystemExit, "before kernel"):
                    atomic_macos.atomic_exchange(live, candidate, receipt=receipt)

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=AssertionError("original mapping must not be exchanged"),
            ):
                recovered = atomic_macos.recover_exchange(receipt.transaction_dir)

            self.assertEqual((live / "version.txt").read_text(), "old")
            self.assertEqual((candidate / "version.txt").read_text(), "new")
            self.assertEqual(recovered.data["status"], "failed_rolled_back")
            self.assertTrue(recovered.data["rolled_back"])
            self.assertFalse(recovered.data["switched"])
            self.assertTrue(recovered.data["no_live_mutation"])
            self.assertEqual(recovered.data["phase"], "recovery_verified_original")

    def test_exact_transposition_is_exchanged_once_back_and_verified(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live, candidate = self._exchange_paths(root)
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-transposed")
            real_swap = atomic_macos._rename_swap_at

            def swap_then_crash(*args):
                real_swap(*args)
                raise SystemExit("simulated crash after kernel call")

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=swap_then_crash,
            ):
                with self.assertRaisesRegex(SystemExit, "after kernel"):
                    atomic_macos.atomic_exchange(live, candidate, receipt=receipt)

            self.assertEqual((live / "version.txt").read_text(), "new")
            self.assertEqual((candidate / "version.txt").read_text(), "old")

            recovered = atomic_macos.recover_exchange(receipt.transaction_dir)

            self.assertEqual((live / "version.txt").read_text(), "old")
            self.assertEqual((candidate / "version.txt").read_text(), "new")
            self.assertEqual(recovered.data["status"], "failed_rolled_back")
            self.assertTrue(recovered.data["switched"])
            self.assertTrue(recovered.data["rolled_back"])
            self.assertFalse(recovered.data["no_live_mutation"])
            self.assertEqual(recovered.data["phase"], "recovery_verified_rollback")

    def test_ambiguous_mapping_requires_manual_recovery_and_preserves_artifacts(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live, candidate = self._exchange_paths(root)
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-ambiguous")

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=SystemExit("simulated crash before kernel call"),
            ):
                with self.assertRaises(SystemExit):
                    atomic_macos.atomic_exchange(live, candidate, receipt=receipt)

            preserved_live = root / "preserved-live"
            unexpected = root / "unexpected"
            unexpected.mkdir()
            (unexpected / "version.txt").write_text("unexpected", encoding="utf-8")
            os.rename(live, preserved_live)
            os.rename(unexpected, live)

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=AssertionError("ambiguous mappings must never be guessed"),
            ):
                recovered = atomic_macos.recover_exchange(receipt.transaction_dir)

            self.assertEqual((live / "version.txt").read_text(), "unexpected")
            self.assertEqual((candidate / "version.txt").read_text(), "new")
            self.assertEqual((preserved_live / "version.txt").read_text(), "old")
            self.assertEqual(recovered.data["status"], "manual_recovery_required")
            self.assertFalse(recovered.data["rolled_back"])
            self.assertEqual(recovered.data["failure_code"], "ambiguous_exchange_mapping")
            self.assertIn("recovery_observed", recovered.data["identities"])

    def test_recovery_can_roll_back_recorded_resources_in_reverse_order(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_live = root / "source-live"
            source_candidate = root / "source-candidate"
            app_live = root / "app-live"
            app_candidate = root / "app-candidate"
            for path, contents in (
                (source_live, "source-old"),
                (source_candidate, "source-new"),
                (app_live, "app-old"),
                (app_candidate, "app-new"),
            ):
                path.mkdir()
                (path / "version.txt").write_text(contents, encoding="utf-8")
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-resources")
            atomic_macos.atomic_exchange(
                source_live,
                source_candidate,
                receipt=receipt,
                resource="source",
                finish_on_success=False,
            )
            atomic_macos.atomic_exchange(
                app_live,
                app_candidate,
                receipt=receipt,
                resource="app",
                finish_on_success=False,
            )

            after_app = atomic_macos.recover_exchange(
                receipt.transaction_dir,
                resource="app",
                finish=False,
            )

            self.assertEqual((source_live / "version.txt").read_text(), "source-new")
            self.assertEqual((app_live / "version.txt").read_text(), "app-old")
            self.assertEqual(after_app.data["status"], "in_progress")
            self.assertEqual(after_app.data["phase"], "app_recovery_verified_rollback")
            self.assertTrue(after_app.data["exchanges"]["app"]["rolled_back"])
            self.assertFalse(
                after_app.data["exchanges"]["source"].get("rolled_back", False)
            )

            after_source = atomic_macos.recover_exchange(
                receipt.transaction_dir,
                resource="source",
                finish=False,
            )

            self.assertEqual((source_live / "version.txt").read_text(), "source-old")
            self.assertEqual((source_candidate / "version.txt").read_text(), "source-new")
            self.assertEqual((app_live / "version.txt").read_text(), "app-old")
            self.assertEqual((app_candidate / "version.txt").read_text(), "app-new")
            self.assertEqual(after_source.data["status"], "in_progress")
            self.assertEqual(
                after_source.data["phase"],
                "source_recovery_verified_rollback",
            )
            self.assertTrue(after_source.data["exchanges"]["source"]["rolled_back"])
            self.assertTrue(after_source.data["exchanges"]["app"]["rolled_back"])

    def test_ambiguous_resource_recovery_preserves_other_exchange_records(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_live = root / "source-live"
            source_candidate = root / "source-candidate"
            app_live = root / "app-live"
            app_candidate = root / "app-candidate"
            for path, contents in (
                (source_live, "source-old"),
                (source_candidate, "source-new"),
                (app_live, "app-old"),
                (app_candidate, "app-new"),
            ):
                path.mkdir()
                (path / "version.txt").write_text(contents, encoding="utf-8")
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-ambiguous-resource")
            atomic_macos.atomic_exchange(
                source_live,
                source_candidate,
                receipt=receipt,
                resource="source",
                finish_on_success=False,
            )
            atomic_macos.atomic_exchange(
                app_live,
                app_candidate,
                receipt=receipt,
                resource="app",
                finish_on_success=False,
            )
            preserved_source_candidate = root / "preserved-source-candidate"
            unexpected = root / "unexpected-source-candidate"
            unexpected.mkdir()
            (unexpected / "version.txt").write_text("unexpected", encoding="utf-8")
            os.rename(source_candidate, preserved_source_candidate)
            os.rename(unexpected, source_candidate)

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=AssertionError("ambiguous mapping must not be exchanged"),
            ):
                recovered = atomic_macos.recover_exchange(
                    receipt.transaction_dir,
                    resource="source",
                    finish=False,
                )

            self.assertEqual(recovered.data["status"], "manual_recovery_required")
            self.assertEqual(recovered.data["failure_code"], "ambiguous_exchange_mapping")
            self.assertEqual(set(recovered.data["exchanges"]), {"source", "app"})
            self.assertEqual((source_live / "version.txt").read_text(), "source-new")
            self.assertEqual((source_candidate / "version.txt").read_text(), "unexpected")
            self.assertEqual((app_live / "version.txt").read_text(), "app-new")
            self.assertEqual((app_candidate / "version.txt").read_text(), "app-old")

    def test_recovery_requires_an_exact_recorded_resource_name(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live, candidate = self._exchange_paths(root)
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-selection")
            atomic_macos.atomic_exchange(
                live,
                candidate,
                receipt=receipt,
                resource="source",
                finish_on_success=False,
            )

            with self.assertRaisesRegex(ValueError, "recorded exchange"):
                atomic_macos.recover_exchange(
                    receipt.transaction_dir,
                    resource="app",
                    finish=False,
                )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "in_progress")
            self.assertEqual(set(persisted.data["exchanges"]), {"source"})
            self.assertEqual((live / "version.txt").read_text(), "new")


class KeeperPreparationTests(unittest.TestCase):
    def _make_upstream_and_live(self, root):
        upstream = root / "official-upstream"
        live = root / "live"
        run_git(None, "init", "-b", "main", str(upstream))
        run_git(upstream, "config", "user.name", "Hermes Test")
        run_git(upstream, "config", "user.email", "hermes-test@example.invalid")
        commit_file(upstream, "base.txt", "base\n", "upstream base")
        run_git(None, "clone", "--no-local", str(upstream), str(live))
        run_git(live, "config", "user.name", "Hermes Test")
        run_git(live, "config", "user.email", "hermes-test@example.invalid")
        return upstream, live

    def test_candidate_is_independent_clone_pinned_to_exact_target_with_full_keeper_rebase(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root)
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            later_upstream = commit_file(
                upstream,
                "later.txt",
                "must not be selected\n",
                "later upstream commit",
            )
            keeper_one = commit_file(live, "keeper-one.txt", "one\n", "keeper one")
            keeper_two = commit_file(live, "keeper-two.txt", "two\n", "keeper two")
            before = checkout_fingerprint(live)
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-keeper")

            candidate = atomic_macos.prepare_keeper_candidate(
                live,
                upstream,
                target,
                receipt=receipt,
            )

            self.assertTrue(candidate.is_dir())
            self.assertTrue((candidate / ".git").is_dir())
            self.assertFalse((candidate / ".git").is_file())
            self.assertEqual(
                run_git(
                    candidate,
                    "rev-parse",
                    "refs/hermes-update/target^{commit}",
                ).stdout.strip(),
                target,
            )
            self.assertEqual(
                run_git(
                    candidate,
                    "rev-list",
                    "--count",
                    "{}..HEAD".format(target),
                ).stdout.strip(),
                "2",
            )
            rebased_subjects = run_git(
                candidate,
                "log",
                "--reverse",
                "--format=%s",
                "{}..HEAD".format(target),
            ).stdout.splitlines()
            self.assertEqual(rebased_subjects, ["keeper one", "keeper two"])
            self.assertEqual(
                run_git(
                    candidate,
                    "merge-base",
                    "--is-ancestor",
                    target,
                    "HEAD",
                    check=False,
                ).returncode,
                0,
            )
            self.assertNotEqual(
                run_git(
                    candidate,
                    "merge-base",
                    "--is-ancestor",
                    later_upstream,
                    "HEAD",
                    check=False,
                ).returncode,
                0,
            )
            self.assertEqual(checkout_fingerprint(live), before)
            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(persisted["status"], "in_progress")
            self.assertEqual(persisted["phase"], "candidate_prepared")
            self.assertEqual(persisted["keeper"]["original_commits"], [keeper_one, keeper_two])
            self.assertEqual(persisted["keeper"]["target_commit"], target)
            self.assertEqual(persisted["live_before"], persisted["live_after"])

    def test_conflict_records_first_keeper_and_leaves_live_checkout_byte_identical(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root)
            commit_file(upstream, "shared.txt", "base\n", "shared base")
            run_git(live, "fetch", "origin")
            run_git(live, "reset", "--hard", "origin/main")
            target = commit_file(
                upstream,
                "shared.txt",
                "upstream version\n",
                "upstream conflicting target",
            )
            conflict_commit = commit_file(
                live,
                "shared.txt",
                "keeper version\n",
                "keeper conflicting change",
            )
            commit_file(live, "after.txt", "after\n", "keeper after conflict")
            (live / "after.txt").write_text("dirty after\n", encoding="utf-8")
            (live / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            before = checkout_fingerprint(live)
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-conflict")
            shared_hermes_home = root / "shared-hermes-home"
            shared_hermes_home.mkdir()
            sentinel = shared_hermes_home / "sentinel.txt"
            sentinel.write_text("untouched\n", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {"HERMES_HOME": str(shared_hermes_home)},
                clear=False,
            ):
                with self.assertRaises(atomic_macos.KeeperConflictError):
                    atomic_macos.prepare_keeper_candidate(
                        live,
                        upstream,
                        target,
                        receipt=receipt,
                    )

            self.assertEqual(checkout_fingerprint(live), before)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")
            self.assertEqual(list(shared_hermes_home.iterdir()), [sentinel])
            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(persisted["status"], "failed_unchanged")
            self.assertEqual(persisted["phase"], "keeper_conflict")
            self.assertEqual(persisted["failure_code"], "keeper_conflict")
            self.assertFalse(persisted["switched"])
            self.assertTrue(persisted["no_live_mutation"])
            self.assertEqual(
                persisted["keeper_conflict"]["commit"],
                conflict_commit,
            )
            self.assertEqual(
                persisted["keeper_conflict"]["subject"],
                "keeper conflicting change",
            )
            self.assertEqual(
                persisted["keeper_conflict"]["unmerged_paths"],
                ["shared.txt"],
            )
            self.assertEqual(persisted["live_before"], persisted["live_after"])
            candidate = Path(persisted["candidate_path"])
            self.assertTrue((candidate / ".git").is_dir())
            self.assertFalse((candidate / ".git" / "rebase-merge").exists())
            self.assertEqual(
                run_git(candidate, "status", "--porcelain=v1").stdout,
                "",
            )

    def test_target_must_be_an_exact_commit_id(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root)
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-ref")

            with self.assertRaisesRegex(ValueError, "exact commit"):
                atomic_macos.prepare_keeper_candidate(
                    live,
                    upstream,
                    "main",
                    receipt=receipt,
                )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(persisted["status"], "failed_unchanged")
            self.assertEqual(persisted["failure_code"], "invalid_target_commit")


if __name__ == "__main__":
    unittest.main()
