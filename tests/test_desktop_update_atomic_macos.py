"""Regression tests for the macOS atomic desktop-update coordinator."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
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

            with mock.patch.object(
                os,
                "rename",
                side_effect=AssertionError("sequential rename is not atomic"),
            ):
                atomic_macos.atomic_exchange(live, candidate)

            self.assertTrue(live.is_dir())
            self.assertTrue(candidate.is_dir())
            self.assertEqual((live / "version.txt").read_text(), "new")
            self.assertEqual((candidate / "version.txt").read_text(), "old")
            self.assertEqual(live.stat().st_ino, candidate_inode)
            self.assertEqual(candidate.stat().st_ino, live_inode)

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


if __name__ == "__main__":
    unittest.main()
