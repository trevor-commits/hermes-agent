"""Candidate-location tests for the macOS atomic desktop updater."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tests.test_desktop_update_atomic_macos import (
    altered_stat,
    checkout_fingerprint,
    commit_file,
    load_atomic_macos,
    run_git,
)


class _FakeReceipt:
    """Minimal single-owner receipt used to observe reservation writes."""

    def __init__(self, transaction_id="txn-candidate"):
        self.data = {
            "transaction_id": transaction_id,
            "status": "in_progress",
            "phase": "created",
        }

    @property
    def is_terminal(self):
        return False

    def record_phase(self, phase, **updates):
        self.data.update(updates)
        self.data["phase"] = phase


def _live_identity_snapshot(live):
    observed = live.lstat()
    return {
        "path": str(live),
        "st_dev": observed.st_dev,
        "st_ino": observed.st_ino,
        "st_uid": observed.st_uid,
        "st_mode": stat.S_IFMT(observed.st_mode) | stat.S_IMODE(observed.st_mode),
    }


class CandidateReservationTests(unittest.TestCase):
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

    def test_keeper_candidate_is_same_length_sibling_bound_to_receipt(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root / "share")
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            commit_file(live, "keeper.txt", "keeper\n", "keeper change")
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-sibling",
            )
            clone_commands = []
            real_run_git = atomic_macos._run_git

            def recording_git(arguments, **kwargs):
                if arguments and arguments[0] == "clone":
                    clone_commands.append(list(arguments))
                return real_run_git(arguments, **kwargs)

            with mock.patch.object(
                atomic_macos,
                "_run_git",
                side_effect=recording_git,
            ):
                candidate = atomic_macos.prepare_keeper_candidate(
                    live,
                    upstream,
                    target,
                    receipt=receipt,
                )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            binding = persisted["source_candidate"]
            candidate_stat = candidate.lstat()

            self.assertEqual(candidate.parent, live.parent)
            self.assertNotEqual(candidate, live)
            self.assertEqual(
                len(os.fsencode(candidate.name)),
                len(os.fsencode(live.name)),
            )
            self.assertFalse(candidate.is_symlink())
            self.assertTrue((candidate / ".git").is_dir())
            self.assertFalse((candidate / ".git").is_file())
            self.assertEqual(stat.S_IMODE(candidate_stat.st_mode), 0o700)
            self.assertEqual(binding["schema_version"], 1)
            self.assertEqual(binding["transaction_id"], "txn-sibling")
            self.assertEqual(
                binding["transaction_marker"],
                "hermes-source-candidate-v1:txn-sibling",
            )
            self.assertEqual(binding["path"], str(candidate))
            self.assertEqual(binding["future_live_path"], str(live))
            self.assertEqual(binding["leaf"], candidate.name)
            self.assertEqual(binding["st_dev"], candidate_stat.st_dev)
            self.assertEqual(binding["st_ino"], candidate_stat.st_ino)
            self.assertEqual(binding["st_uid"], candidate_stat.st_uid)
            self.assertEqual(binding["parent"]["path"], str(live.parent))
            self.assertEqual(binding["parent"]["st_dev"], live.parent.stat().st_dev)
            self.assertEqual(
                clone_commands,
                [["clone", "--no-local", "--", str(live), str(candidate)]],
            )

    def test_reservation_uses_nofollow_parent_fd_atomic_mkdir_and_parent_fsync(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "hermes-agent"
            live.mkdir()
            receipt = _FakeReceipt("txn-syscalls")
            events = []
            real_open = atomic_macos.os.open
            real_mkdir = atomic_macos.os.mkdir
            real_fsync = atomic_macos.os.fsync

            def recording_open(path, flags, mode=0o777, *, dir_fd=None):
                events.append(("open", os.fspath(path), flags, dir_fd))
                return real_open(path, flags, mode, dir_fd=dir_fd)

            def recording_mkdir(path, mode=0o777, *, dir_fd=None):
                events.append(("mkdir", os.fspath(path), mode, dir_fd))
                return real_mkdir(path, mode, dir_fd=dir_fd)

            def recording_fsync(descriptor):
                opened = os.fstat(descriptor)
                events.append(("fsync", opened.st_dev, opened.st_ino))
                return real_fsync(descriptor)

            with mock.patch.object(
                atomic_macos._candidate,
                "_candidate_leaf",
                return_value=".hu-feedface",
            ), mock.patch.object(
                atomic_macos.os,
                "open",
                side_effect=recording_open,
            ), mock.patch.object(
                atomic_macos.os,
                "mkdir",
                side_effect=recording_mkdir,
            ), mock.patch.object(
                atomic_macos.os,
                "fsync",
                side_effect=recording_fsync,
            ):
                candidate = atomic_macos._candidate.reserve_source_candidate(
                    live,
                    receipt=receipt,
                    live_before=_live_identity_snapshot(live),
                )

            parent_stat = live.parent.stat()
            parent_open = next(
                event
                for event in events
                if event[0] == "open" and event[1] == str(live.parent)
            )
            reservation_mkdir = next(
                event
                for event in events
                if event[0] == "mkdir" and event[1] == candidate.name
            )
            parent_fsync = ("fsync", parent_stat.st_dev, parent_stat.st_ino)

            self.assertTrue(parent_open[2] & getattr(os, "O_NOFOLLOW", 0))
            self.assertTrue(parent_open[2] & getattr(os, "O_DIRECTORY", 0))
            self.assertEqual(reservation_mkdir[2], 0o700)
            self.assertIsNotNone(reservation_mkdir[3])
            self.assertIn(parent_fsync, events)
            self.assertGreater(events.index(parent_fsync), events.index(reservation_mkdir))
            self.assertEqual(receipt.data["phase"], "candidate_reserved")

    def test_collision_retries_without_replacing_existing_symlink(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "hermes-agent"
            live.mkdir()
            sentinel = root / "sentinel"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            occupied = root / ".hu-deadbeef"
            occupied.symlink_to(sentinel)
            occupied_before = occupied.lstat()
            receipt = _FakeReceipt("txn-collision")

            with mock.patch.object(
                atomic_macos._candidate,
                "_candidate_leaf",
                side_effect=[".hu-deadbeef", ".hu-cafebabe"],
            ):
                candidate = atomic_macos._candidate.reserve_source_candidate(
                    live,
                    receipt=receipt,
                    live_before=_live_identity_snapshot(live),
                )

            occupied_after = occupied.lstat()
            self.assertEqual(candidate.name, ".hu-cafebabe")
            self.assertTrue(occupied.is_symlink())
            self.assertEqual(os.readlink(occupied), str(sentinel))
            self.assertEqual(occupied_after.st_ino, occupied_before.st_ino)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")

    def test_live_identity_change_after_snapshot_is_rejected_before_reservation(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "hermes-agent"
            live.mkdir()
            observed = live.lstat()
            receipt = _FakeReceipt("txn-live-swap")
            live_before = {
                "path": str(live),
                "st_dev": observed.st_dev,
                "st_ino": observed.st_ino + 1,
                "st_uid": observed.st_uid,
                "st_mode": stat.S_IFMT(observed.st_mode)
                | stat.S_IMODE(observed.st_mode),
            }

            with self.assertRaisesRegex(RuntimeError, "live checkout identity changed"):
                atomic_macos._candidate.reserve_source_candidate(
                    live,
                    receipt=receipt,
                    live_before=live_before,
                )

            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["hermes-agent"],
            )

    def test_symlinked_parent_is_rejected_before_candidate_creation(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            real_parent = root / "real-share"
            real_parent.mkdir()
            (real_parent / "hermes-agent").mkdir()
            linked_parent = root / "linked-share"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            receipt = _FakeReceipt("txn-linked-parent")

            with self.assertRaisesRegex(ValueError, "symlink"):
                atomic_macos._candidate.reserve_source_candidate(
                    linked_parent / "hermes-agent",
                    receipt=receipt,
                    live_before=_live_identity_snapshot(linked_parent / "hermes-agent"),
                )

            self.assertEqual(
                sorted(path.name for path in real_parent.iterdir()),
                ["hermes-agent"],
            )

    def test_parent_writable_by_another_account_is_rejected(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            share = root / "share"
            share.mkdir()
            live = share / "hermes-agent"
            live.mkdir()
            os.chmod(share, 0o777)
            receipt = _FakeReceipt("txn-writable-parent")

            with self.assertRaisesRegex(PermissionError, "writable by another"):
                atomic_macos._candidate.reserve_source_candidate(
                    live,
                    receipt=receipt,
                    live_before=_live_identity_snapshot(live),
                )

            self.assertEqual(
                sorted(path.name for path in share.iterdir()),
                ["hermes-agent"],
            )

    def test_public_preparation_does_not_claim_permission_change_is_unchanged(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root / "share")
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-live-mode-change",
            )
            real_reserve = atomic_macos._git._reserve_source_candidate

            def make_live_unsafe(*args, **kwargs):
                os.chmod(live, 0o777)
                return real_reserve(*args, **kwargs)

            with mock.patch.object(
                atomic_macos._git,
                "_reserve_source_candidate",
                side_effect=make_live_unsafe,
            ):
                with self.assertRaisesRegex(PermissionError, "writable by another"):
                    atomic_macos.prepare_keeper_candidate(
                        live,
                        upstream,
                        target,
                        receipt=receipt,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(persisted["status"], "manual_recovery_required")
            self.assertFalse(persisted["no_live_mutation"])
            self.assertNotEqual(persisted["live_before"], persisted["live_after"])
            self.assertEqual(stat.S_IMODE(persisted["live_before"]["st_mode"]), 0o755)
            self.assertEqual(stat.S_IMODE(persisted["live_after"]["st_mode"]), 0o777)

    def test_unexpected_parent_owner_is_rejected_before_candidate_creation(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "hermes-agent"
            live.mkdir()
            receipt = _FakeReceipt("txn-parent-owner")
            real_lstat = Path.lstat

            def altered_parent_owner(path):
                observed = real_lstat(path)
                if path == live.parent:
                    return altered_stat(observed, owner=observed.st_uid + 1)
                return observed

            with mock.patch.object(Path, "lstat", altered_parent_owner):
                with self.assertRaisesRegex(PermissionError, "unexpected owner"):
                    atomic_macos._candidate.reserve_source_candidate(
                        live,
                        receipt=receipt,
                        live_before=_live_identity_snapshot(live),
                    )

            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["hermes-agent"],
            )

    def test_transaction_marker_tamper_blocks_candidate_revalidation(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "hermes-agent"
            live.mkdir()
            receipt = _FakeReceipt("txn-marker")
            with mock.patch.object(
                atomic_macos._candidate,
                "_candidate_leaf",
                return_value=".hu-feedface",
            ):
                candidate = atomic_macos._candidate.reserve_source_candidate(
                    live,
                    receipt=receipt,
                    live_before=_live_identity_snapshot(live),
                )

            before = candidate.lstat()
            receipt.data["source_candidate"]["transaction_marker"] = "tampered"
            with self.assertRaisesRegex(RuntimeError, "transaction marker"):
                atomic_macos._candidate.validate_reserved_candidate(
                    live,
                    receipt=receipt,
                )

            after = candidate.lstat()
            self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))

    def test_reservation_failure_after_mkdir_records_and_retains_artifact(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root / "share")
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            before = checkout_fingerprint(live)
            receipt = atomic_macos.create_transaction(
                root / "transactions",
                "txn-reservation-fsync",
            )
            parent_identity = (live.parent.stat().st_dev, live.parent.stat().st_ino)
            real_fsync = atomic_macos.os.fsync
            failed = False

            def fail_reservation_parent_fsync(descriptor):
                nonlocal failed
                observed = os.fstat(descriptor)
                if not failed and (observed.st_dev, observed.st_ino) == parent_identity:
                    failed = True
                    raise OSError(errno.EIO, "forced reservation fsync failure")
                return real_fsync(descriptor)

            with mock.patch.object(
                atomic_macos._candidate,
                "_candidate_leaf",
                return_value=".hu-feedface",
            ), mock.patch.object(
                atomic_macos.os,
                "fsync",
                side_effect=fail_reservation_parent_fsync,
            ):
                with self.assertRaisesRegex(RuntimeError, "forced reservation fsync"):
                    atomic_macos.prepare_keeper_candidate(
                        live,
                        upstream,
                        target,
                        receipt=receipt,
                    )

            artifact = live.parent / ".hu-feedface"
            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertTrue(artifact.is_dir())
            self.assertEqual(checkout_fingerprint(live), before)
            self.assertEqual(persisted["status"], "failed_unchanged")
            self.assertEqual(persisted["failure_code"], "candidate_reservation_failed")
            self.assertEqual(persisted["candidate_path"], str(artifact))
            self.assertEqual(persisted["live_before"], persisted["live_after"])

    def test_target_is_validated_before_any_sibling_artifact(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root / "share")
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-invalid")
            before = sorted(path.name for path in live.parent.iterdir())

            with self.assertRaisesRegex(ValueError, "exact commit"):
                atomic_macos.prepare_keeper_candidate(
                    live,
                    upstream,
                    "main",
                    receipt=receipt,
                )

            after = sorted(path.name for path in live.parent.iterdir())
            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(after, before)
            self.assertNotIn("source_candidate", persisted)
            self.assertEqual(persisted["status"], "failed_unchanged")

    def test_identity_replacement_before_clone_never_reaches_git(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root / "share")
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            before = checkout_fingerprint(live)
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-before-swap")
            real_reserve = atomic_macos._git._reserve_source_candidate
            real_run_git = atomic_macos._run_git
            artifacts = {}
            clone_called = False

            def replace_after_reservation(*args, **kwargs):
                candidate = real_reserve(*args, **kwargs)
                original = candidate.with_name(candidate.name + "-reserved")
                candidate.rename(original)
                candidate.mkdir(mode=0o700)
                artifacts.update(candidate=candidate, original=original)
                return candidate

            def recording_git(arguments, **kwargs):
                nonlocal clone_called
                if arguments and arguments[0] == "clone":
                    clone_called = True
                return real_run_git(arguments, **kwargs)

            with mock.patch.object(
                atomic_macos._git,
                "_reserve_source_candidate",
                side_effect=replace_after_reservation,
            ), mock.patch.object(
                atomic_macos,
                "_run_git",
                side_effect=recording_git,
            ):
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    atomic_macos.prepare_keeper_candidate(
                        live,
                        upstream,
                        target,
                        receipt=receipt,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertFalse(clone_called)
            self.assertEqual(checkout_fingerprint(live), before)
            self.assertEqual(persisted["status"], "failed_unchanged")
            self.assertEqual(persisted["failure_code"], "candidate_identity_changed")
            self.assertEqual(persisted["live_before"], persisted["live_after"])
            self.assertTrue(artifacts["candidate"].is_dir())
            self.assertTrue(artifacts["original"].is_dir())
            self.assertEqual(
                persisted["source_candidate"]["st_ino"],
                artifacts["original"].stat().st_ino,
            )

    def test_identity_replacement_after_clone_is_retained_and_fails_unchanged(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upstream, live = self._make_upstream_and_live(root / "share")
            target = commit_file(upstream, "target.txt", "target\n", "upstream target")
            before = checkout_fingerprint(live)
            receipt = atomic_macos.create_transaction(root / "transactions", "txn-after-swap")
            real_run_git = atomic_macos._run_git
            artifacts = {}

            def replace_after_clone(arguments, **kwargs):
                result = real_run_git(arguments, **kwargs)
                if arguments and arguments[0] == "clone":
                    candidate = Path(arguments[-1])
                    original = candidate.with_name(candidate.name + "-cloned")
                    candidate.rename(original)
                    candidate.mkdir(mode=0o700)
                    (candidate / ".git").mkdir()
                    artifacts.update(candidate=candidate, original=original)
                return result

            with mock.patch.object(
                atomic_macos,
                "_run_git",
                side_effect=replace_after_clone,
            ):
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    atomic_macos.prepare_keeper_candidate(
                        live,
                        upstream,
                        target,
                        receipt=receipt,
                    )

            persisted = atomic_macos.load_transaction(receipt.transaction_dir).data
            self.assertEqual(checkout_fingerprint(live), before)
            self.assertEqual(persisted["status"], "failed_unchanged")
            self.assertEqual(persisted["failure_code"], "candidate_identity_changed")
            self.assertEqual(persisted["live_before"], persisted["live_after"])
            self.assertTrue((artifacts["original"] / ".git").is_dir())
            self.assertTrue((artifacts["candidate"] / ".git").is_dir())
            self.assertEqual(
                persisted["source_candidate"]["st_ino"],
                artifacts["original"].stat().st_ino,
            )

    def test_reserved_candidate_must_remain_on_live_device(self):
        atomic_macos = load_atomic_macos()

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            live = root / "hermes-agent"
            live.mkdir()
            receipt = _FakeReceipt("txn-device")
            with mock.patch.object(
                atomic_macos._candidate,
                "_candidate_leaf",
                return_value=".hu-feedface",
            ):
                atomic_macos._candidate.reserve_source_candidate(
                    live,
                    receipt=receipt,
                    live_before=_live_identity_snapshot(live),
                )

            real_lstat = Path.lstat

            def altered_live_device(path):
                observed = real_lstat(path)
                if path == live:
                    return altered_stat(observed, device=observed.st_dev + 1)
                return observed

            with mock.patch.object(Path, "lstat", altered_live_device):
                with self.assertRaises(OSError) as raised:
                    atomic_macos._candidate.validate_reserved_candidate(
                        live,
                        receipt=receipt,
                    )

            self.assertEqual(raised.exception.errno, errno.EXDEV)


if __name__ == "__main__":
    unittest.main()
