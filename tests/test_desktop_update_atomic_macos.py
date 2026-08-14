"""Regression tests for the macOS atomic desktop-update coordinator."""

from __future__ import annotations

import errno
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


def wait_for_paths(paths, timeout=10):
    """Wait for bounded subprocess readiness markers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(path.exists() for path in paths):
            return
        time.sleep(0.01)
    missing = [str(path) for path in paths if not path.exists()]
    raise AssertionError("timed out waiting for paths: {}".format(missing))


def finish_processes(processes, timeout=15):
    """Collect bounded subprocess results and fail with their stderr."""
    completed = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=timeout)
            completed.append((process.returncode, stdout, stderr))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    return completed


class FacadePackagingTests(unittest.TestCase):
    _IMPORT_PROBE = (
        "import importlib.util, pathlib, sys; "
        "path = pathlib.Path(sys.argv[1]); "
        "spec = importlib.util.spec_from_file_location('packaged_atomic_macos', path); "
        "module = importlib.util.module_from_spec(spec); "
        "sys.modules[spec.name] = module; "
        "spec.loader.exec_module(module); "
        "assert callable(module.atomic_exchange); "
        "assert callable(module.recover_exchange); "
        "assert callable(module.prepare_keeper_candidate); "
        "print('facade-api-ok')"
    )

    def test_packaged_facade_imports_with_all_sibling_modules(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp)
            for name in (
                "atomic_macos.py",
                "_atomic_macos_transaction.py",
                "_atomic_macos_candidate.py",
                "_atomic_macos_git.py",
            ):
                shutil.copy2(MODULE_PATH.with_name(name), runtime / name)

            result = subprocess.run(
                [sys.executable, "-c", self._IMPORT_PROBE, str(runtime / "atomic_macos.py")],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "facade-api-ok")

    def test_packaged_facade_fails_honestly_without_transaction_sibling(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp)
            shutil.copy2(MODULE_PATH, runtime / "atomic_macos.py")
            shutil.copy2(
                MODULE_PATH.with_name("_atomic_macos_candidate.py"),
                runtime / "_atomic_macos_candidate.py",
            )
            shutil.copy2(
                MODULE_PATH.with_name("_atomic_macos_git.py"),
                runtime / "_atomic_macos_git.py",
            )

            result = subprocess.run(
                [sys.executable, "-c", self._IMPORT_PROBE, str(runtime / "atomic_macos.py")],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("_atomic_macos_transaction.py", result.stderr)

    def test_packaged_facade_fails_honestly_without_git_sibling(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp)
            shutil.copy2(MODULE_PATH, runtime / "atomic_macos.py")
            shutil.copy2(
                MODULE_PATH.with_name("_atomic_macos_transaction.py"),
                runtime / "_atomic_macos_transaction.py",
            )
            shutil.copy2(
                MODULE_PATH.with_name("_atomic_macos_candidate.py"),
                runtime / "_atomic_macos_candidate.py",
            )

            result = subprocess.run(
                [sys.executable, "-c", self._IMPORT_PROBE, str(runtime / "atomic_macos.py")],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("_atomic_macos_git.py", result.stderr)

    def test_packaged_facade_fails_honestly_without_candidate_sibling(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            runtime = Path(raw_tmp)
            shutil.copy2(MODULE_PATH, runtime / "atomic_macos.py")
            shutil.copy2(
                MODULE_PATH.with_name("_atomic_macos_transaction.py"),
                runtime / "_atomic_macos_transaction.py",
            )
            shutil.copy2(
                MODULE_PATH.with_name("_atomic_macos_git.py"),
                runtime / "_atomic_macos_git.py",
            )

            result = subprocess.run(
                [sys.executable, "-c", self._IMPORT_PROBE, str(runtime / "atomic_macos.py")],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("_atomic_macos_candidate.py", result.stderr)


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
            self.assertEqual(receipt.data["lock_transaction_id"], "txn-fixed")
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
            lock_stat = (receipt.transaction_dir / ".transaction.lock").lstat()
            self.assertTrue(stat.S_ISREG(lock_stat.st_mode))
            self.assertEqual(lock_stat.st_uid, os.geteuid())
            self.assertEqual(stat.S_IMODE(lock_stat.st_mode), 0o600)

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

    def test_transaction_directory_mode_must_be_exactly_0700(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            receipt = atomic_macos.create_transaction(Path(raw_tmp), "txn-exact-mode")
            for mode in (0o500, 0o710, 0o750, 0o701):
                with self.subTest(mode=oct(mode)):
                    os.chmod(receipt.transaction_dir, mode)
                    try:
                        with self.assertRaisesRegex(PermissionError, "mode 0700"):
                            atomic_macos.load_transaction(receipt.transaction_dir)
                    finally:
                        os.chmod(receipt.transaction_dir, 0o700)

    def test_stale_handle_cannot_overwrite_terminal_receipt(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            receipt = atomic_macos.create_transaction(Path(raw_tmp), "txn-stale")
            stale = atomic_macos.load_transaction(receipt.transaction_dir)
            receipt.finish(
                "manual_recovery_required",
                phase="manual",
                failure_code="forced",
                failure_message="forced terminal state",
            )
            terminal_bytes = receipt.path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "terminal"):
                stale.record_phase("stale-write")

            self.assertEqual(receipt.path.read_bytes(), terminal_bytes)

    def test_receipt_cannot_rekey_its_root_transaction_lock(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            receipt = atomic_macos.create_transaction(Path(raw_tmp), "txn-lock-root")
            original_bytes = receipt.path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "guard"):
                receipt.record_phase(
                    "invalid-lock-rekey",
                    lock_transaction_id="different-root",
                )

            self.assertEqual(receipt.path.read_bytes(), original_bytes)

    def test_nested_guarded_process_writes_do_not_deadlock(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            receipt = atomic_macos.create_transaction(root, "txn-nested-guard")
            worker = root / "nested-guard-worker.py"
            worker.write_text(
                """\
import importlib.util
import pathlib
import sys

module_path = pathlib.Path(sys.argv[1])
transaction_dir = pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("nested_guard_transaction", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
receipt = module.load_transaction(transaction_dir)
with module._transaction_guard(
    receipt.transaction_dir.parent,
    receipt.data["lock_transaction_id"],
) as guard:
    receipt.record_phase("nested-one", _guard=guard)
    receipt.record_phase("nested-two", _guard=guard)
""",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(MODULE_PATH.with_name("_atomic_macos_transaction.py")),
                    str(receipt.transaction_dir),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["phase"], "nested-two")

    def test_released_guard_cannot_authorize_a_receipt_write(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            receipt = atomic_macos.create_transaction(Path(raw_tmp), "txn-stale-guard")
            transaction_module = atomic_macos._transaction
            with transaction_module._transaction_guard(
                receipt.transaction_dir.parent,
                receipt.data["lock_transaction_id"],
            ) as released_guard:
                pass

            with self.assertRaisesRegex(RuntimeError, "no longer active"):
                receipt.record_phase("stale-guard-write", _guard=released_guard)

    def test_concurrent_process_cannot_overwrite_terminal_receipt(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            receipt = atomic_macos.create_transaction(root, "txn-process-race")
            go = root / "go"
            worker = root / "receipt-worker.py"
            worker.write_text(
                """\
import importlib.util
import pathlib
import sys
import time

module_path = pathlib.Path(sys.argv[1])
transaction_dir = pathlib.Path(sys.argv[2])
role = sys.argv[3]
ready = pathlib.Path(sys.argv[4])
go = pathlib.Path(sys.argv[5])
spec = importlib.util.spec_from_file_location("receipt_race_" + role, module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
receipt = module.load_transaction(transaction_dir)
real_ensure = module.TransactionReceipt._ensure_writable

def delayed_ensure(self, *args, **kwargs):
    result = real_ensure(self, *args, **kwargs)
    time.sleep(0.25 if role == "terminal" else 0.50)
    return result

module.TransactionReceipt._ensure_writable = delayed_ensure
ready.write_text("ready\\n", encoding="utf-8")
while not go.exists():
    time.sleep(0.01)
if role == "stale":
    time.sleep(0.05)
try:
    if role == "terminal":
        receipt.finish(
            "succeeded",
            phase="terminal-won",
            switched=True,
            rolled_back=False,
            no_live_mutation=False,
        )
    else:
        receipt.record_phase("stale-process-write", stale_writer=True)
except RuntimeError:
    pass
""",
                encoding="utf-8",
            )
            ready_paths = [root / "terminal.ready", root / "stale.ready"]
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(worker),
                        str(MODULE_PATH),
                        str(receipt.transaction_dir),
                        role,
                        str(ready),
                        str(go),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for role, ready in zip(("terminal", "stale"), ready_paths)
            ]
            wait_for_paths(ready_paths)
            go.write_text("go\n", encoding="utf-8")
            completed = finish_processes(processes)

            for returncode, _stdout, stderr in completed:
                self.assertEqual(returncode, 0, stderr)
            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "succeeded")
            self.assertEqual(persisted.data["phase"], "terminal-won")
            self.assertNotIn("stale_writer", persisted.data)

    def test_create_transaction_publishes_directory_relative_to_fsynced_root(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            transactions_root = Path(raw_tmp) / "transactions"
            transactions_root.mkdir(mode=0o700)
            root_identity = (
                transactions_root.stat().st_dev,
                transactions_root.stat().st_ino,
            )
            events = []
            real_mkdir = atomic_macos.os.mkdir
            real_fsync = atomic_macos.os.fsync
            real_chmod = atomic_macos.os.chmod

            def recording_mkdir(path, mode=0o777, *, dir_fd=None):
                identity = None
                if dir_fd is not None:
                    opened = os.fstat(dir_fd)
                    identity = (opened.st_dev, opened.st_ino)
                events.append(("mkdir", os.fspath(path), dir_fd, identity))
                return real_mkdir(path, mode, dir_fd=dir_fd)

            def recording_fsync(descriptor):
                opened = os.fstat(descriptor)
                events.append(("fsync", (opened.st_dev, opened.st_ino)))
                return real_fsync(descriptor)

            def recording_chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
                self.assertNotEqual(
                    os.fspath(path),
                    str(transactions_root / "txn-root-fsync"),
                )
                return real_chmod(
                    path,
                    mode,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            with mock.patch.object(
                atomic_macos.os,
                "mkdir",
                side_effect=recording_mkdir,
            ), mock.patch.object(
                atomic_macos.os,
                "fsync",
                side_effect=recording_fsync,
            ), mock.patch.object(
                atomic_macos.os,
                "chmod",
                side_effect=recording_chmod,
            ):
                receipt = atomic_macos.create_transaction(
                    transactions_root,
                    "txn-root-fsync",
                )

            mkdir_event = next(
                event
                for event in events
                if event[0] == "mkdir" and event[1] == "txn-root-fsync"
            )
            self.assertIsNotNone(mkdir_event[2])
            self.assertEqual(mkdir_event[3], root_identity)
            self.assertEqual(receipt.transaction_dir.stat().st_mode & 0o777, 0o700)
            mkdir_index = events.index(mkdir_event)
            root_fsync_index = events.index(("fsync", root_identity))
            self.assertGreater(root_fsync_index, mkdir_index)
            self.assertTrue(receipt.path.is_file())


@unittest.skipUnless(sys.platform == "darwin", "macOS renameatx_np contract")
class AtomicExchangeTests(unittest.TestCase):
    def _source_switched_shared_transaction(self, atomic_macos, root, transaction_id):
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
        receipt = atomic_macos.create_transaction(
            root / "transactions",
            transaction_id,
        )
        atomic_macos.atomic_exchange(
            source_live,
            source_candidate,
            receipt=receipt,
            resource="source",
            finish_on_success=False,
        )
        return source_live, source_candidate, app_live, app_candidate, receipt

    def _assert_source_can_still_recover(
        self,
        atomic_macos,
        receipt,
        source_live,
        source_candidate,
    ):
        recovered = atomic_macos.recover_exchange(
            receipt.transaction_dir,
            resource="source",
            finish=False,
        )
        self.assertEqual((source_live / "version.txt").read_text(), "source-old")
        self.assertEqual(
            (source_candidate / "version.txt").read_text(),
            "source-new",
        )
        self.assertTrue(recovered.data["exchanges"]["source"]["rolled_back"])
        return recovered

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

    def test_root_owned_app_parent_and_user_owned_endpoint_validate_independently(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            endpoint = Path(raw_tmp) / "Hermes.app"
            endpoint.mkdir()
            observed = endpoint.lstat()
            root_parent = altered_stat(observed, owner=0)
            user_endpoint = altered_stat(observed, owner=os.geteuid())

            atomic_macos._validated_directory_identity(
                root_parent,
                root_parent,
                expected_uid=0,
                label="/Applications parent",
            )
            atomic_macos._validated_endpoint_identity(
                user_endpoint,
                user_endpoint,
                expected_uid=os.geteuid(),
                label="/Applications/Hermes.app endpoint",
            )

    def test_exchange_accepts_separate_parent_and_endpoint_owner_expectations(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "live"
            candidate = root / "candidate"
            live.mkdir()
            candidate.mkdir()
            owner = os.geteuid()

            atomic_macos.atomic_exchange(
                live,
                candidate,
                expected_parent_uids=(owner, owner),
                expected_endpoint_uids=(owner, owner),
            )

            self.assertTrue(live.is_dir())
            self.assertTrue(candidate.is_dir())

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

    def test_ambiguous_exchange_never_claims_no_live_mutation(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "live"
            candidate = root / "candidate"
            live.mkdir()
            candidate.mkdir()
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-ambiguous-mutation-state",
            )

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=OSError(errno.EIO, "forced uncertain exchange"),
            ), mock.patch.object(
                atomic_macos,
                "_mapping_kind",
                return_value="ambiguous",
            ):
                with self.assertRaisesRegex(RuntimeError, "manual recovery"):
                    atomic_macos.atomic_exchange(live, candidate, receipt=receipt)

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(persisted["status"], "manual_recovery_required")
            self.assertEqual(
                persisted["failure_code"],
                "ambiguous_exchange_mapping",
            )
            self.assertFalse(persisted["no_live_mutation"])

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

    def test_later_app_preflight_failure_keeps_source_recoverable(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_live, source_candidate, app_live, app_candidate, receipt = (
                self._source_switched_shared_transaction(
                    atomic_macos,
                    root,
                    "txn-app-preflight",
                )
            )
            (app_candidate / "version.txt").unlink()
            app_candidate.rmdir()
            app_candidate.symlink_to(app_live, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                atomic_macos.atomic_exchange(
                    app_live,
                    app_candidate,
                    receipt=receipt,
                    resource="app",
                    finish_on_success=False,
                )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "in_progress")
            self.assertEqual(
                persisted.data["exchanges"]["app"]["failure_code"],
                "exchange_precondition",
            )
            self.assertFalse(
                persisted.data["exchanges"]["app"]["manual_recovery_required"]
            )
            recovered = self._assert_source_can_still_recover(
                atomic_macos,
                receipt,
                source_live,
                source_candidate,
            )
            self.assertTrue(recovered.data["rolled_back"])

    def test_later_app_ambiguous_mapping_keeps_source_recoverable(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_live, source_candidate, app_live, app_candidate, receipt = (
                self._source_switched_shared_transaction(
                    atomic_macos,
                    root,
                    "txn-app-ambiguous",
                )
            )

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=OSError(errno.EIO, "forced app uncertainty"),
            ), mock.patch.object(
                atomic_macos,
                "_mapping_kind",
                return_value="ambiguous",
            ):
                with self.assertRaisesRegex(RuntimeError, "manual recovery"):
                    atomic_macos.atomic_exchange(
                        app_live,
                        app_candidate,
                        receipt=receipt,
                        resource="app",
                        finish_on_success=False,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "in_progress")
            self.assertTrue(
                persisted.data["exchanges"]["app"]["manual_recovery_required"]
            )
            self.assertEqual(
                persisted.data["exchanges"]["app"]["failure_code"],
                "ambiguous_exchange_mapping",
            )
            recovered = self._assert_source_can_still_recover(
                atomic_macos,
                receipt,
                source_live,
                source_candidate,
            )
            self.assertFalse(recovered.data["rolled_back"])

    def test_later_app_rollback_syscall_failure_keeps_source_recoverable(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_live, source_candidate, app_live, app_candidate, receipt = (
                self._source_switched_shared_transaction(
                    atomic_macos,
                    root,
                    "txn-app-rollback-syscall",
                )
            )
            real_swap = atomic_macos._rename_swap_at
            calls = 0

            def swap_then_fail_rollback(*arguments):
                nonlocal calls
                calls += 1
                if calls == 1:
                    real_swap(*arguments)
                    raise OSError(errno.EIO, "uncertain app exchange result")
                raise OSError(errno.EIO, "forced app rollback failure")

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=swap_then_fail_rollback,
            ):
                with self.assertRaisesRegex(RuntimeError, "manual recovery"):
                    atomic_macos.atomic_exchange(
                        app_live,
                        app_candidate,
                        receipt=receipt,
                        resource="app",
                        finish_on_success=False,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "in_progress")
            self.assertTrue(
                persisted.data["exchanges"]["app"]["manual_recovery_required"]
            )
            self.assertEqual(
                persisted.data["exchanges"]["app"]["failure_code"],
                "exchange_rollback_failed",
            )
            self.assertEqual((app_live / "version.txt").read_text(), "app-new")
            self._assert_source_can_still_recover(
                atomic_macos,
                receipt,
                source_live,
                source_candidate,
            )

    def test_later_app_rollback_verification_failure_keeps_source_recoverable(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_live, source_candidate, app_live, app_candidate, receipt = (
                self._source_switched_shared_transaction(
                    atomic_macos,
                    root,
                    "txn-app-rollback-verify",
                )
            )
            real_swap = atomic_macos._rename_swap_at
            calls = 0

            def uncertain_then_rollback(*arguments):
                nonlocal calls
                calls += 1
                real_swap(*arguments)
                if calls == 1:
                    raise OSError(errno.EIO, "uncertain app exchange result")

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=uncertain_then_rollback,
            ), mock.patch.object(
                atomic_macos,
                "_mapping_kind",
                side_effect=("transposed", "ambiguous"),
            ):
                with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                    atomic_macos.atomic_exchange(
                        app_live,
                        app_candidate,
                        receipt=receipt,
                        resource="app",
                        finish_on_success=False,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "in_progress")
            self.assertTrue(
                persisted.data["exchanges"]["app"]["manual_recovery_required"]
            )
            self.assertEqual(
                persisted.data["exchanges"]["app"]["failure_code"],
                "rollback_verification_failed",
            )
            self.assertEqual((app_live / "version.txt").read_text(), "app-old")
            self._assert_source_can_still_recover(
                atomic_macos,
                receipt,
                source_live,
                source_candidate,
            )


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

    def test_recovery_cannot_overtake_an_in_progress_atomic_exchange(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live, candidate = self._exchange_paths(root)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-initial-recovery-race",
            )
            initial_at_swap = root / "initial-at-swap"
            recovery_mapped = root / "recovery-mapped"
            initial_swapped = root / "initial-swapped"
            initial_worker = root / "initial-exchange-worker.py"
            initial_worker.write_text(
                """\
import importlib.util
import pathlib
import sys
import time

module_path = pathlib.Path(sys.argv[1])
transaction_dir = pathlib.Path(sys.argv[2])
live = pathlib.Path(sys.argv[3])
candidate = pathlib.Path(sys.argv[4])
initial_at_swap = pathlib.Path(sys.argv[5])
recovery_mapped = pathlib.Path(sys.argv[6])
initial_swapped = pathlib.Path(sys.argv[7])
spec = importlib.util.spec_from_file_location("initial_exchange_race", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
real_swap = module._rename_swap_at

def paused_swap(*arguments):
    initial_at_swap.write_text("ready\\n", encoding="utf-8")
    deadline = time.monotonic() + 1.0
    while not recovery_mapped.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    real_swap(*arguments)
    initial_swapped.write_text("swapped\\n", encoding="utf-8")

module._rename_swap_at = paused_swap
try:
    result = module.atomic_exchange(
        live,
        candidate,
        receipt=module.load_transaction(transaction_dir),
    )
    print(result.data["status"])
except Exception as error:
    print("error:" + type(error).__name__)
""",
                encoding="utf-8",
            )
            recovery_worker = root / "recovery-during-exchange-worker.py"
            recovery_worker.write_text(
                """\
import importlib.util
import pathlib
import sys
import time

module_path = pathlib.Path(sys.argv[1])
transaction_dir = pathlib.Path(sys.argv[2])
recovery_mapped = pathlib.Path(sys.argv[3])
initial_swapped = pathlib.Path(sys.argv[4])
spec = importlib.util.spec_from_file_location("recovery_during_exchange", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
real_mapping = module._mapping_kind

def coordinated_mapping(*arguments):
    result = real_mapping(*arguments)
    recovery_mapped.write_text("mapped\\n", encoding="utf-8")
    deadline = time.monotonic() + 3.0
    while not initial_swapped.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return result

module._mapping_kind = coordinated_mapping
result = module.recover_exchange(transaction_dir)
print(result.data["status"])
""",
                encoding="utf-8",
            )
            initial = subprocess.Popen(
                [
                    sys.executable,
                    str(initial_worker),
                    str(MODULE_PATH),
                    str(receipt.transaction_dir),
                    str(live),
                    str(candidate),
                    str(initial_at_swap),
                    str(recovery_mapped),
                    str(initial_swapped),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            wait_for_paths([initial_at_swap])
            recovery = subprocess.Popen(
                [
                    sys.executable,
                    str(recovery_worker),
                    str(MODULE_PATH),
                    str(receipt.transaction_dir),
                    str(recovery_mapped),
                    str(initial_swapped),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            completed = finish_processes([initial, recovery])

            for returncode, _stdout, stderr in completed:
                self.assertEqual(returncode, 0, stderr)
            self.assertEqual(completed[0][1].strip(), "succeeded")
            self.assertEqual(completed[1][1].strip(), "succeeded")
            persisted = atomic_macos.load_transaction(receipt.transaction_dir)
            self.assertEqual(persisted.data["status"], "succeeded")
            self.assertEqual(persisted.data["phase"], "exchange_verified")
            self.assertFalse(persisted.data["no_live_mutation"])
            self.assertEqual((live / "version.txt").read_text(), "new")
            self.assertEqual((candidate / "version.txt").read_text(), "old")

    def test_concurrent_process_recovery_exchanges_transposed_mapping_only_once(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live, candidate = self._exchange_paths(root)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-process-recovery-race",
            )
            real_swap = atomic_macos._rename_swap_at

            def swap_then_crash(*arguments):
                real_swap(*arguments)
                raise SystemExit("simulated crash after exchange")

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=swap_then_crash,
            ):
                with self.assertRaisesRegex(SystemExit, "after exchange"):
                    atomic_macos.atomic_exchange(live, candidate, receipt=receipt)

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=OSError(errno.EIO, "forced first recovery failure"),
            ):
                failed = atomic_macos.recover_exchange(receipt.transaction_dir)

            self.assertEqual(failed.data["status"], "manual_recovery_required")
            self.assertEqual((live / "version.txt").read_text(), "new")
            worker = root / "recovery-worker.py"
            go = root / "go"
            worker.write_text(
                """\
import importlib.util
import pathlib
import sys
import time

module_path = pathlib.Path(sys.argv[1])
transaction_dir = pathlib.Path(sys.argv[2])
ready = pathlib.Path(sys.argv[3])
go = pathlib.Path(sys.argv[4])
spec = importlib.util.spec_from_file_location("recovery_race_" + ready.name, module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
real_mapping = module._mapping_kind

def delayed_mapping(*arguments):
    result = real_mapping(*arguments)
    if result == "transposed":
        time.sleep(0.50)
    return result

module._mapping_kind = delayed_mapping
ready.write_text("ready\\n", encoding="utf-8")
while not go.exists():
    time.sleep(0.01)
recovered = module.recover_exchange(transaction_dir)
print(recovered.data["status"])
""",
                encoding="utf-8",
            )
            ready_paths = [root / "first.ready", root / "second.ready"]
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(worker),
                        str(MODULE_PATH),
                        str(receipt.transaction_dir),
                        str(ready),
                        str(go),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for ready in ready_paths
            ]
            wait_for_paths(ready_paths)
            go.write_text("go\n", encoding="utf-8")
            completed = finish_processes(processes)

            for returncode, stdout, stderr in completed:
                self.assertEqual(returncode, 0, stderr)
                self.assertEqual(stdout.strip(), "failed_rolled_back")
            self.assertEqual((live / "version.txt").read_text(), "old")
            self.assertEqual((candidate / "version.txt").read_text(), "new")

    def test_transient_recovery_error_can_retry_from_terminal_attempt(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live, candidate = self._exchange_paths(root)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-recovery-retry-terminal",
            )
            real_swap = atomic_macos._rename_swap_at

            def swap_then_crash(*arguments):
                real_swap(*arguments)
                raise SystemExit("simulated crash after exchange")

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=swap_then_crash,
            ):
                with self.assertRaises(SystemExit):
                    atomic_macos.atomic_exchange(live, candidate, receipt=receipt)

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=OSError(errno.EIO, "transient recovery failure"),
            ):
                failed_attempt = atomic_macos.recover_exchange(
                    receipt.transaction_dir,
                )

            self.assertEqual(
                failed_attempt.data["status"],
                "manual_recovery_required",
            )
            original_bytes = receipt.path.read_bytes()
            recovered_attempt = atomic_macos.recover_exchange(
                failed_attempt.transaction_dir,
            )

            self.assertEqual((live / "version.txt").read_text(), "old")
            self.assertEqual((candidate / "version.txt").read_text(), "new")
            self.assertEqual(recovered_attempt.data["status"], "failed_rolled_back")
            self.assertNotEqual(
                recovered_attempt.transaction_dir,
                failed_attempt.transaction_dir,
            )
            self.assertEqual(receipt.path.read_bytes(), original_bytes)
            self.assertEqual(recovered_attempt.transaction_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                recovered_attempt.data["recovery_of"]["transaction_id"],
                receipt.data["transaction_id"],
            )
            self.assertEqual(
                failed_attempt.data["lock_transaction_id"],
                receipt.data["transaction_id"],
            )
            self.assertEqual(
                recovered_attempt.data["lock_transaction_id"],
                receipt.data["transaction_id"],
            )

    def test_recovery_attempt_rejects_changed_original_receipt_identity(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live, candidate = self._exchange_paths(root)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-recovery-origin-change",
            )
            real_swap = atomic_macos._rename_swap_at

            def swap_then_crash(*arguments):
                real_swap(*arguments)
                raise SystemExit("simulated crash after exchange")

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=swap_then_crash,
            ):
                with self.assertRaises(SystemExit):
                    atomic_macos.atomic_exchange(live, candidate, receipt=receipt)

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=OSError(errno.EIO, "transient recovery failure"),
            ):
                failed_attempt = atomic_macos.recover_exchange(
                    receipt.transaction_dir,
                )

            failed_attempt_bytes = failed_attempt.path.read_bytes()

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=OSError(errno.EIO, "second transient recovery failure"),
            ):
                terminal_attempt = atomic_macos.recover_exchange(
                    failed_attempt.transaction_dir,
                )
            terminal_attempt_bytes = terminal_attempt.path.read_bytes()

            replacement = receipt.path.with_name("replacement.json")
            replacement.mkdir()
            (replacement / "sentinel").write_text("different identity", encoding="utf-8")
            moved_original = receipt.path.with_name("original-receipt.json")
            os.rename(receipt.path, moved_original)
            os.rename(replacement, receipt.path)

            with self.assertRaisesRegex((OSError, RuntimeError), "receipt"):
                atomic_macos.recover_exchange(terminal_attempt.transaction_dir)

            self.assertEqual(moved_original.read_bytes(), failed_attempt_bytes)
            self.assertEqual(terminal_attempt.path.read_bytes(), terminal_attempt_bytes)
            self.assertEqual((live / "version.txt").read_text(), "new")

    def test_nonterminal_recovery_failure_remains_retryable_in_place(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live, candidate = self._exchange_paths(root)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-recovery-retry-in-place",
            )
            atomic_macos.atomic_exchange(
                live,
                candidate,
                receipt=receipt,
                resource="source",
                finish_on_success=False,
            )

            with mock.patch.object(
                atomic_macos,
                "_rename_swap_at",
                side_effect=OSError(errno.EIO, "transient recovery failure"),
            ):
                failed = atomic_macos.recover_exchange(
                    receipt.transaction_dir,
                    resource="source",
                    finish=False,
                )

            self.assertEqual(failed.data["status"], "in_progress")
            self.assertTrue(
                failed.data["exchanges"]["source"]["manual_recovery_required"]
            )
            recovered = atomic_macos.recover_exchange(
                receipt.transaction_dir,
                resource="source",
                finish=False,
            )
            self.assertEqual(recovered.transaction_dir, receipt.transaction_dir)
            self.assertEqual((live / "version.txt").read_text(), "old")
            self.assertTrue(recovered.data["exchanges"]["source"]["rolled_back"])

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

    def test_multi_resource_recovery_holds_one_root_guard_across_resources(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            endpoints = {}
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-one-recovery-guard",
            )
            for resource in ("source", "app"):
                live = root / "{}-live".format(resource)
                candidate = root / "{}-candidate".format(resource)
                live.mkdir()
                candidate.mkdir()
                (live / "version.txt").write_text("old", encoding="utf-8")
                (candidate / "version.txt").write_text("new", encoding="utf-8")
                atomic_macos.atomic_exchange(
                    live,
                    candidate,
                    receipt=receipt,
                    resource=resource,
                    finish_on_success=False,
                )
                endpoints[resource] = (live, candidate)

            transaction_module = atomic_macos._transaction
            if not hasattr(transaction_module, "recover_recorded_exchanges"):
                self.fail("multi-resource recovery has no single-guard engine API")
            real_recover_locked = transaction_module._recover_exchange_locked
            observed_guards = []

            def recover_with_barrier(*args, **kwargs):
                recovered = real_recover_locked(*args, **kwargs)
                observed_guards.append(kwargs["_guard"])
                if kwargs["resource"] == "app":
                    contender = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "import importlib.util, pathlib, sys; "
                            "p=pathlib.Path(sys.argv[1]); "
                            "s=importlib.util.spec_from_file_location('guard_contender', p); "
                            "m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; "
                            "s.loader.exec_module(m); "
                            "code='acquired'; "
                            "\ntry:\n"
                            " with m._transaction_guard(pathlib.Path(sys.argv[2]), sys.argv[3], lock_timeout_seconds=0.1): pass\n"
                            "except m.TransactionLockUnavailableError: code='unavailable'\n"
                            "print(code)",
                            str(MODULE_PATH.with_name("_atomic_macos_transaction.py")),
                            str(receipt.transaction_dir.parent),
                            receipt.data["lock_transaction_id"],
                        ],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=3,
                    )
                    self.assertEqual(contender.returncode, 0, contender.stderr)
                    self.assertEqual(contender.stdout.strip(), "unavailable")
                return recovered

            with mock.patch.object(
                transaction_module,
                "_recover_exchange_locked",
                side_effect=recover_with_barrier,
            ):
                recovered, processed = transaction_module.recover_recorded_exchanges(
                    receipt.transaction_dir,
                    ("app", "source"),
                    lock_timeout_seconds=1.0,
                    _rename_swap_command=atomic_macos._rename_swap_at,
                    _mapping_kind_command=atomic_macos._mapping_kind,
                )

            self.assertEqual(processed, ["app", "source"])
            self.assertEqual(recovered.data["status"], "failed_rolled_back")
            self.assertEqual(len(observed_guards), 2)
            self.assertIs(observed_guards[0], observed_guards[1])
            for live, candidate in endpoints.values():
                self.assertEqual((live / "version.txt").read_text(), "old")
                self.assertEqual((candidate / "version.txt").read_text(), "new")

    def test_multi_resource_recovery_validates_complete_sequence_before_mutation(self):
        atomic_macos = load_atomic_macos()
        invalid_sequences = (
            ("app", "bogus"),
            ("source", "app"),
            ("app", "app"),
            ("app",),
        )

        for sequence in invalid_sequences:
            with self.subTest(sequence=sequence), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                receipt = atomic_macos.create_transaction(
                    root / "transactions",
                    "txn-invalid-sequence",
                )
                endpoints = []
                for resource in ("source", "app"):
                    live = root / "{}-live".format(resource)
                    candidate = root / "{}-candidate".format(resource)
                    live.mkdir()
                    candidate.mkdir()
                    (live / "version.txt").write_text("old", encoding="utf-8")
                    (candidate / "version.txt").write_text("new", encoding="utf-8")
                    atomic_macos.atomic_exchange(
                        live,
                        candidate,
                        receipt=receipt,
                        resource=resource,
                        finish_on_success=False,
                    )
                    endpoints.append((live, candidate))
                receipt_before = receipt.path.read_bytes()

                with self.assertRaisesRegex(ValueError, "recovery resource sequence"):
                    atomic_macos.recover_recorded_exchanges(
                        receipt.transaction_dir,
                        sequence,
                        lock_timeout_seconds=1.0,
                    )

                self.assertEqual(receipt.path.read_bytes(), receipt_before)
                for live, candidate in endpoints:
                    self.assertEqual((live / "version.txt").read_text(), "new")
                    self.assertEqual((candidate / "version.txt").read_text(), "old")

    def test_multi_resource_recovery_validates_every_plan_before_mutation(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-invalid-later-plan",
            )
            endpoints = {}
            for resource in ("source", "app"):
                live = root / "{}-live".format(resource)
                candidate = root / "{}-candidate".format(resource)
                live.mkdir()
                candidate.mkdir()
                (live / "version.txt").write_text("old", encoding="utf-8")
                (candidate / "version.txt").write_text("new", encoding="utf-8")
                atomic_macos.atomic_exchange(
                    live,
                    candidate,
                    receipt=receipt,
                    resource=resource,
                    finish_on_success=False,
                )
                endpoints[resource] = (live, candidate)

            exchanges = json.loads(json.dumps(receipt.data["exchanges"]))
            exchanges["source"]["parents"] = {"corrupt": True}
            receipt.record_phase("corrupt_later_recovery_plan", exchanges=exchanges)
            receipt_before = receipt.path.read_bytes()
            endpoint_before = {
                path: (path.stat().st_dev, path.stat().st_ino, (path / "version.txt").read_bytes())
                for pair in endpoints.values()
                for path in pair
            }

            with self.assertRaisesRegex(ValueError, "recovery plan"):
                atomic_macos.recover_recorded_exchanges(
                    receipt.transaction_dir,
                    ("app", "source"),
                    lock_timeout_seconds=1.0,
                )

            self.assertEqual(receipt.path.read_bytes(), receipt_before)
            for path, expected in endpoint_before.items():
                observed = path.stat()
                self.assertEqual(
                    (observed.st_dev, observed.st_ino, (path / "version.txt").read_bytes()),
                    expected,
                )

    def test_multi_resource_recovery_rejects_a_stale_later_plan_without_guessing(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-stale-later-plan",
            )
            endpoints = {}
            for resource in ("source", "app"):
                live = root / "{}-live".format(resource)
                candidate = root / "{}-candidate".format(resource)
                live.mkdir()
                candidate.mkdir()
                (live / "version.txt").write_text("old", encoding="utf-8")
                (candidate / "version.txt").write_text("new", encoding="utf-8")
                atomic_macos.atomic_exchange(
                    live,
                    candidate,
                    receipt=receipt,
                    resource=resource,
                    finish_on_success=False,
                )
                endpoints[resource] = (live, candidate)

            transaction_module = atomic_macos._transaction
            real_recover_locked = transaction_module._recover_exchange_locked
            source_live, source_candidate = endpoints["source"]
            preserved_source_live = root / "preserved-source-live"

            def recover_then_replace_later_endpoint(*args, **kwargs):
                recovered = real_recover_locked(*args, **kwargs)
                if kwargs["resource"] == "app":
                    # This replacement occurs only after every resource passed planning.
                    # The earlier app rollback may therefore be retained, but source must
                    # not be exchanged from a stale plan.
                    os.rename(source_live, preserved_source_live)
                    source_live.mkdir()
                    (source_live / "version.txt").write_text("unexpected", encoding="utf-8")
                return recovered

            with mock.patch.object(
                transaction_module,
                "_recover_exchange_locked",
                side_effect=recover_then_replace_later_endpoint,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after recovery planning"):
                    atomic_macos.recover_recorded_exchanges(
                        receipt.transaction_dir,
                        ("app", "source"),
                        lock_timeout_seconds=1.0,
                    )

            app_live, app_candidate = endpoints["app"]
            self.assertEqual((app_live / "version.txt").read_text(), "old")
            self.assertEqual((app_candidate / "version.txt").read_text(), "new")
            self.assertEqual((source_live / "version.txt").read_text(), "unexpected")
            self.assertEqual((preserved_source_live / "version.txt").read_text(), "new")
            self.assertEqual((source_candidate / "version.txt").read_text(), "old")

    def test_multi_resource_recovery_classifies_later_ambiguity_before_mutation(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-later-ambiguous-plan",
            )
            endpoints = {}
            for resource in ("source", "app"):
                live = root / "{}-live".format(resource)
                candidate = root / "{}-candidate".format(resource)
                live.mkdir()
                candidate.mkdir()
                (live / "version.txt").write_text("old", encoding="utf-8")
                (candidate / "version.txt").write_text("new", encoding="utf-8")
                atomic_macos.atomic_exchange(
                    live,
                    candidate,
                    receipt=receipt,
                    resource=resource,
                    finish_on_success=False,
                )
                endpoints[resource] = (live, candidate)

            source_live, source_candidate = endpoints["source"]
            preserved_source_candidate = root / "preserved-source-candidate"
            os.rename(source_candidate, preserved_source_candidate)
            source_candidate.mkdir()
            (source_candidate / "version.txt").write_text("unexpected", encoding="utf-8")

            recovered, processed = atomic_macos.recover_recorded_exchanges(
                receipt.transaction_dir,
                ("app", "source"),
                lock_timeout_seconds=1.0,
            )

            app_live, app_candidate = endpoints["app"]
            self.assertEqual(recovered.data["status"], "manual_recovery_required")
            self.assertEqual(recovered.data["failure_code"], "ambiguous_exchange_mapping")
            self.assertEqual(processed, ["source"])
            self.assertEqual((app_live / "version.txt").read_text(), "new")
            self.assertEqual((app_candidate / "version.txt").read_text(), "old")
            self.assertEqual((source_live / "version.txt").read_text(), "new")
            self.assertEqual((source_candidate / "version.txt").read_text(), "unexpected")
            self.assertEqual((preserved_source_candidate / "version.txt").read_text(), "old")

    def test_transaction_receipt_read_is_bounded(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            receipt = atomic_macos.create_transaction(
                Path(raw_tmp) / "transactions",
                "txn-oversized-receipt",
            )
            with receipt.path.open("ab") as handle:
                handle.write(b"x" * (1024 * 1024 + 1))

            with self.assertRaisesRegex(ValueError, "receipt size limit"):
                atomic_macos.load_transaction(receipt.transaction_dir)

    def test_transaction_receipt_detects_change_while_reading(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            receipt = atomic_macos.create_transaction(
                Path(raw_tmp) / "transactions",
                "txn-changing-receipt",
            )
            transaction_module = atomic_macos._transaction
            real_read = transaction_module.os.read
            changed = False

            def read_then_change(descriptor, size):
                nonlocal changed
                payload = real_read(descriptor, size)
                if payload and not changed:
                    changed = True
                    with receipt.path.open("ab") as handle:
                        handle.write(b" ")
                return payload

            with mock.patch.object(transaction_module.os, "read", side_effect=read_then_change):
                with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                    atomic_macos.load_transaction(receipt.transaction_dir)

    def test_transaction_receipt_detects_same_size_rewrite_while_reading(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            receipt = atomic_macos.create_transaction(
                Path(raw_tmp) / "transactions",
                "txn-same-size-rewrite",
            )
            original = receipt.path.read_bytes()
            replacement = original.replace(
                b'"phase":"created"',
                b'"phase":"altered"',
            )
            self.assertNotEqual(replacement, original)
            self.assertEqual(len(replacement), len(original))
            transaction_module = atomic_macos._transaction
            real_read = transaction_module.os.read
            rewritten = False

            def read_then_rewrite(descriptor, size):
                nonlocal rewritten
                payload = real_read(descriptor, size)
                if payload and not rewritten:
                    rewritten = True
                    before = receipt.path.stat()
                    with receipt.path.open("r+b") as handle:
                        handle.write(replacement)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.utime(
                        receipt.path,
                        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
                    )
                return payload

            with mock.patch.object(transaction_module.os, "read", side_effect=read_then_rewrite):
                with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                    atomic_macos.load_transaction(receipt.transaction_dir)

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
        live = root / "hermes-agent"
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
            self.assertEqual(candidate.parent, live.parent)
            self.assertEqual(
                len(os.fsencode(candidate.name)),
                len(os.fsencode(live.name)),
            )
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
            self.assertEqual(persisted["source_candidate"]["path"], str(candidate))

    def test_success_receipt_redacts_credentials_from_official_upstream_url(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root)
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            commit_file(live, "keeper.txt", "keeper\n", "keeper change")
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-redacted")
            sentinel = "SENTINEL_SECRET_TOKEN"
            credential_url = (
                "https://updater:{}@example.invalid/hermes-agent.git".format(sentinel)
            )
            real_run_git = atomic_macos._run_git

            def redirect_credential_fetch(arguments, **kwargs):
                forwarded = list(arguments)
                if forwarded and forwarded[0] == "fetch":
                    self.assertIn("--no-write-fetch-head", forwarded)
                    self.assertEqual(forwarded[-2], credential_url)
                    forwarded[-2] = str(upstream)
                return real_run_git(forwarded, **kwargs)

            with mock.patch.object(
                atomic_macos,
                "_run_git",
                side_effect=redirect_credential_fetch,
            ):
                atomic_macos.prepare_keeper_candidate(
                    live,
                    credential_url,
                    target,
                    receipt=receipt,
                )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            serialized = json.dumps(persisted, sort_keys=True)
            self.assertNotIn(sentinel, serialized)
            self.assertEqual(
                persisted["keeper"]["official_upstream"],
                "https://updater:***@example.invalid/hermes-agent.git",
            )

    def test_candidate_git_ignores_inherited_index_redirection(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root)
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            commit_file(live, "keeper.txt", "keeper\n", "keeper change")
            outside_index = root / "outside" / "attacker-index"
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-git-environment",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "GIT_INDEX_FILE": str(outside_index),
                    "GIT_DIR": str(root / "attacker-git-dir"),
                    "GIT_WORK_TREE": str(root / "attacker-worktree"),
                    "GIT_OBJECT_DIRECTORY": str(root / "attacker-objects"),
                    "GIT_CONFIG_GLOBAL": str(root / "attacker-gitconfig"),
                    "GIT_SSH_COMMAND": "false inherited-ssh-command",
                },
                clear=False,
            ):
                candidate = atomic_macos.prepare_keeper_candidate(
                    live,
                    upstream,
                    target,
                    receipt=receipt,
                )

            self.assertTrue((candidate / ".git").is_dir())
            self.assertFalse(outside_index.exists())
            self.assertFalse((root / "attacker-git-dir").exists())
            self.assertFalse((root / "attacker-objects").exists())

            environment = atomic_macos._git._git_environment()
            self.assertEqual(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
            self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
            self.assertEqual(
                {key for key in environment if key.startswith("GIT_")},
                {
                    "GIT_COMMITTER_EMAIL",
                    "GIT_COMMITTER_NAME",
                    "GIT_CONFIG_GLOBAL",
                    "GIT_CONFIG_NOSYSTEM",
                    "GIT_OPTIONAL_LOCKS",
                    "GIT_TERMINAL_PROMPT",
                },
            )

    def test_upstream_secret_forms_are_absent_from_failure_and_receipt(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _upstream, live = self._make_upstream_and_live(root)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-redact-all-upstream-forms",
            )
            secrets = {
                "password": "PASS_SENTINEL",
                "oauth": "OAUTH_SENTINEL",
                "auth": "AUTH_SENTINEL",
                "fragment": "FRAGMENT_SENTINEL",
                "bearer": "BEARER_SENTINEL",
                "schemeless": "SCHEMELESS_SENTINEL",
            }
            official_upstream = (
                "https://updater:{password}@example.invalid/repo.git"
                "?oauth_token={oauth}&auth={auth}#token={fragment}"
            ).format(**secrets)
            target = "a" * 40
            real_subprocess_run = atomic_macos.subprocess.run

            def fail_fetch(command, **kwargs):
                if "fetch" in command:
                    raise subprocess.CalledProcessError(
                        128,
                        command,
                        stderr=(
                            "fatal for {} Authorization: Bearer {} mirror "
                            "updater:{}@example.invalid:repo.git"
                        ).format(
                            official_upstream,
                            secrets["bearer"],
                            secrets["schemeless"],
                        ),
                    )
                return real_subprocess_run(command, **kwargs)

            with mock.patch.object(
                atomic_macos.subprocess,
                "run",
                side_effect=fail_fetch,
            ):
                with self.assertRaises(Exception) as raised:
                    atomic_macos.prepare_keeper_candidate(
                        live,
                        official_upstream,
                        target,
                        receipt=receipt,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            public = str(raised.exception) + json.dumps(persisted, sort_keys=True)
            for label, secret in secrets.items():
                with self.subTest(label=label):
                    self.assertNotIn(secret, public)

    def test_git_spawn_error_redacts_opaque_upstream_argument(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root)
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-redact-spawn-error",
            )
            sentinel = "OPAQUE_UPSTREAM_SENTINEL"
            opaque_upstream = "mirror-alias-{}".format(sentinel)
            real_subprocess_run = atomic_macos.subprocess.run

            def fail_fetch_spawn(command, **kwargs):
                if "fetch" in command:
                    raise OSError("transport could not open {}".format(opaque_upstream))
                return real_subprocess_run(command, **kwargs)

            with mock.patch.object(
                atomic_macos.subprocess,
                "run",
                side_effect=fail_fetch_spawn,
            ):
                with self.assertRaises(Exception) as raised:
                    atomic_macos.prepare_keeper_candidate(
                        live,
                        opaque_upstream,
                        target,
                        receipt=receipt,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertNotIn(sentinel, str(raised.exception))
            self.assertNotIn(sentinel, json.dumps(persisted, sort_keys=True))

    def test_fetch_failure_redacts_credentials_from_receipt_and_public_error(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root)
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-fetch-fail")
            sentinel = "SENTINEL_FETCH_PASSWORD"
            credential_url = (
                "https://updater:{}@example.invalid/hermes-agent.git".format(sentinel)
            )
            real_subprocess_run = atomic_macos.subprocess.run

            def fail_only_fetch(command, **kwargs):
                if "fetch" in command:
                    raise subprocess.CalledProcessError(
                        128,
                        command,
                        stderr="fatal: authentication failed for {}".format(
                            credential_url
                        ),
                    )
                return real_subprocess_run(command, **kwargs)

            with mock.patch.object(
                atomic_macos.subprocess,
                "run",
                side_effect=fail_only_fetch,
            ):
                with self.assertRaises(Exception) as raised:
                    atomic_macos.prepare_keeper_candidate(
                        live,
                        credential_url,
                        target,
                        receipt=receipt,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertNotIn(sentinel, str(raised.exception))
            self.assertNotIn(sentinel, json.dumps(persisted, sort_keys=True))
            self.assertIn("***", str(raised.exception))

    def test_live_snapshot_detects_index_change_with_same_status_and_worktree(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _upstream, live = self._make_upstream_and_live(root)
            tracked = live / "base.txt"
            tracked.write_text("index-one\n", encoding="utf-8")
            run_git(live, "add", "--", "base.txt")
            tracked.write_text("same-worktree\n", encoding="utf-8")
            before = atomic_macos._checkout_snapshot(live)

            tracked.write_text("index-two\n", encoding="utf-8")
            run_git(live, "add", "--", "base.txt")
            tracked.write_text("same-worktree\n", encoding="utf-8")
            after = atomic_macos._checkout_snapshot(live)

            self.assertEqual(before["head"], after["head"])
            self.assertEqual(before["status"], after["status"])
            self.assertEqual(before["tree_digest"], after["tree_digest"])
            self.assertEqual(before["worktree_digest"], after["worktree_digest"])
            self.assertNotEqual(before["index_digest"], after["index_digest"])

            receipt = atomic_macos.create_transaction(root / "transactions", "txn-index")
            atomic_macos._record_candidate_failure(
                receipt,
                phase="candidate_preparation_failed",
                code="candidate_preparation_failed",
                message="forced candidate failure",
                live_before=before,
                live_after=after,
                candidate_path=root / "candidate",
            )
            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(persisted["status"], "manual_recovery_required")
            self.assertFalse(persisted["no_live_mutation"])

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
            self.assertEqual(candidate.parent, live.parent)
            self.assertEqual(persisted["source_candidate"]["path"], str(candidate))
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

    def test_invalid_target_with_non_git_live_checkout_terminalizes_fail_closed(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "not-a-git-checkout"
            live.mkdir()
            (live / "sentinel.txt").write_text("unchanged\n", encoding="utf-8")
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-invalid-non-git",
            )

            with self.assertRaisesRegex(ValueError, "exact commit"):
                atomic_macos.prepare_keeper_candidate(
                    live,
                    "https://updater:SECRET@example.invalid/hermes-agent.git",
                    "main",
                    receipt=receipt,
                )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(persisted["status"], "manual_recovery_required")
            self.assertEqual(persisted["phase"], "candidate_target_invalid")
            self.assertEqual(persisted["failure_code"], "invalid_target_commit")
            self.assertEqual(
                persisted["failure_message"],
                "target must be an exact 40- or 64-hex commit id",
            )
            self.assertNotIn("SECRET", json.dumps(persisted, sort_keys=True))
            self.assertIsNotNone(persisted["completed_at"])
            self.assertFalse(persisted["no_live_mutation"])
            self.assertEqual(
                (live / "sentinel.txt").read_text(encoding="utf-8"),
                "unchanged\n",
            )


if __name__ == "__main__":
    unittest.main()
