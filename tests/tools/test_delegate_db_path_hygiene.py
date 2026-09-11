#!/usr/bin/env python3
"""
Regression: a mocked parent ``_session_db`` must never materialize a database.

keeper publish gate 2026-09-10: delegate tests holding
``agent._session_db = MagicMock()`` reached ``_open_child_session_db``, whose
getattr chain handed the mock straight to ``hermes_state_registry.acquire``.
``os.fspath(MagicMock())`` evaluates to ``MagicMock/mock._session_db.db_path``
— a CWD-relative path — and the registry created REAL SQLite files there,
dirtying the candidate worktree mid-gate (publish refused after a fully green
gate+smoke run).

Run with:  python -m pytest tests/tools/test_delegate_db_path_hygiene.py -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from tools.delegate_tool import _open_child_session_db  # noqa: E402


class TestMockedParentSessionDb(unittest.TestCase):
    """The incident shape: mocked parent agent, cwd must stay untouched."""

    def test_mock_db_path_never_creates_files(self):
        """No patching: the real registry guard must refuse the mock's db_path
        BEFORE any filesystem I/O, and _quiet must degrade to None."""
        agent = MagicMock()
        with tempfile.TemporaryDirectory() as td:
            old = os.getcwd()
            os.chdir(td)
            try:
                db = _open_child_session_db(agent)
                self.assertIsNone(db)
                self.assertEqual(
                    os.listdir(td), [],
                    "a mocked _session_db materialized files in the worktree cwd",
                )
            finally:
                os.chdir(old)

    def test_real_path_still_reaches_registry_acquire(self):
        """A real db_path must keep flowing through to the registry."""
        agent = MagicMock()
        real_path = Path(tempfile.gettempdir()) / "delegate_hygiene_parent.db"
        agent._session_db.db_path = real_path
        with patch(
            "hermes_state_registry.acquire", return_value="SENTINEL"
        ) as acquire:
            db = _open_child_session_db(agent)
        self.assertEqual(db, "SENTINEL")
        acquire.assert_called_once_with(real_path)

    def test_missing_session_db_returns_none(self):
        self.assertIsNone(_open_child_session_db(MagicMap()))


class MagicMap:
    """Agent lookalike with no _session_db attribute at all."""

    _session_db = None


if __name__ == "__main__":
    unittest.main()
