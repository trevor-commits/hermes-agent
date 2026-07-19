"""Regression tests for compression continuation session metadata.

Compression rotation creates a child session with ``parent_session_id``.  The
runtime path must preserve cwd/git metadata so desktop project grouping and
session routing do not lose the working directory at the compaction boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def test_compression_child_can_inherit_parent_cwd_and_git_metadata(db: SessionDB) -> None:
    """Exercise the DB writes used by compression rotation.

    The compression code now reads the parent row, passes ``cwd`` into
    ``create_session`` for the child, then best-effort copies branch/repo-root via
    ``update_session_cwd``.  This catches regressions where either write stops
    persisting the inherited metadata.
    """

    db.create_session("parent", "cli", model="m")
    db.update_session_cwd(
        "parent",
        "/repo/worktree",
        git_branch="feature/compaction",
        git_repo_root="/repo",
    )

    parent = db.get_session("parent")
    assert parent is not None

    db.end_session("parent", "compression")
    db.create_session(
        "child",
        "cli",
        model="m",
        parent_session_id="parent",
        cwd=parent.get("cwd"),
    )
    db.update_session_cwd(
        "child",
        parent.get("cwd") or "",
        git_branch=parent.get("git_branch"),
        git_repo_root=parent.get("git_repo_root"),
    )

    child = db.get_session("child")
    assert child is not None
    assert child["parent_session_id"] == "parent"
    assert child["cwd"] == "/repo/worktree"
    assert child["git_branch"] == "feature/compaction"
    assert child["git_repo_root"] == "/repo"
