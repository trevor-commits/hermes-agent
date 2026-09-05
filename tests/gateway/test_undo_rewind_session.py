"""Tests for SessionStore.rewind_session — the gateway /undo [N] primitive.

The gateway /undo backs up N user turns by soft-deleting the truncated rows
in state.db (active=0, kept for audit, hidden from re-prompts/search) via
SessionDB.rewind_to_message, rather than the old hard rewrite_transcript.
load_transcript returns only the active view. See issue #21910.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_state import SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from gateway.turn_lease import SessionTurnLeaseRegistry


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = SessionDB(db_path=tmp_path / "state.db")
    s = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    s._db = db  # use the same DB instance the fixture seeds
    return s


def _seed(store, sid, source="telegram", turns=3):
    store._db.create_session(sid, source=source)
    for i in range(1, turns + 1):
        store._db.append_message(sid, "user", f"q{i}")
        store._db.append_message(sid, "assistant", f"a{i}")
    return sid


def test_rewind_default_one_turn(store):
    sid = _seed(store, "gw-1")
    res = store.rewind_session(sid)
    assert res["turns_undone"] == 1
    assert res["target_text"] == "q3"
    assert res["rewound_count"] == 2  # q3 + a3
    active = store.load_transcript(sid)
    assert [m["role"] for m in active] == ["user", "assistant", "user", "assistant"]


def test_rewind_n_turns(store):
    sid = _seed(store, "gw-2")
    res = store.rewind_session(sid, 2)
    assert res["turns_undone"] == 2
    assert res["target_text"] == "q2"
    assert res["rewound_count"] == 4  # q2,a2,q3,a3
    assert len(store.load_transcript(sid)) == 2  # q1,a1


@pytest.mark.asyncio
async def test_gateway_undo_waits_for_alias_rollover_and_rewinds_only_child(
    store,
):
    """A stale alias /undo shares the turn lease and follows rollover."""
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="u1",
    )
    parent = store.get_or_create_session(source)
    store._db.append_message(parent.session_id, "user", "parent question")
    store._db.append_message(parent.session_id, "assistant", "parent answer")
    alias = dataclasses.replace(
        parent,
        session_key=f"{parent.session_key}:undo-alias",
    )
    with store._lock:
        store._entries[alias.session_key] = alias
        store._save()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = store.config
    runner.session_store = store
    runner._turn_leases = SessionTurnLeaseRegistry()
    runner._begin_session_run_generation = MagicMock(return_value=17)
    runner._evict_cached_agent = MagicMock()
    runner._async_session_store = None
    store.get_or_create_session = MagicMock(return_value=alias)
    holder = await runner._turn_leases.acquire(
        parent.session_id,
        owner_key=parent.session_key,
        generation=16,
        timeout=1,
    )
    event = MessageEvent(
        text="/undo",
        message_type=MessageType.TEXT,
        source=source,
    )

    undo_task = asyncio.create_task(runner._handle_undo_command(event))
    for _ in range(100):
        if runner._turn_leases.has_waiters(parent.session_id) or undo_task.done():
            break
        await asyncio.sleep(0)
    assert runner._turn_leases.has_waiters(parent.session_id) is True

    child = store.rollover_session_with_carryover(
        parent.session_key,
        parent.session_id,
        {
            "role": "user",
            "content": "[CONTEXT CARRYOVER] summary",
            "_compressed_summary": True,
            "display_kind": "hidden",
            "timestamp": 1234.5,
        },
    )
    assert child is not None
    store._db.append_message(child.session_id, "user", "child question")
    store._db.append_message(child.session_id, "assistant", "child answer")
    assert runner._turn_leases.release(holder) is True

    response = await undo_task

    assert "child question" in response
    assert [
        message["content"]
        for message in store._db.get_messages(parent.session_id)
    ] == ["parent question", "parent answer"]
    child_rows = store._db.get_messages(
        child.session_id,
        include_inactive=True,
    )
    assert [
        (message["content"], message["active"])
        for message in child_rows
        if message["content"] in {"child question", "child answer"}
    ] == [("child question", 0), ("child answer", 0)]


def test_rewind_pins_raw_active_ids_when_projection_hides_review_harness(store):
    sid = _seed(store, "gw-review-harness", turns=2)
    store._db.append_message(
        sid,
        "user",
        "Review the conversation above and update the skill library safely",
    )
    store._db.append_message(sid, "assistant", "curator-only reply")

    # Legacy background-review rows are intentionally absent from replay, but
    # they remain physical active rows that the rewind CAS must pin.
    assert [message["content"] for message in store.load_transcript(sid)] == [
        "q1",
        "a1",
        "q2",
        "a2",
    ]

    result = store.rewind_session(sid)

    assert result is not None
    assert result["target_text"] == "q2"
    assert result["rewound_count"] == 4
    assert [message["content"] for message in store.load_transcript(sid)] == [
        "q1",
        "a1",
    ]


def test_rewind_fails_closed_when_transcript_changes_after_snapshot(
    store, monkeypatch
):
    sid = _seed(store, "gw-cas", turns=2)
    sibling = SessionDB(db_path=store._db.db_path)
    original_rewind = store._db.rewind_to_message

    def _append_then_rewind(*args, **kwargs):
        sibling.append_message(sid, "assistant", "concurrent tail")
        return original_rewind(*args, **kwargs)

    monkeypatch.setattr(store._db, "rewind_to_message", _append_then_rewind)

    assert store.rewind_session(sid) is None

    rows = store._db._conn.execute(
        "SELECT content, active FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("q1", 1),
        ("a1", 1),
        ("q2", 1),
        ("a2", 1),
        ("concurrent tail", 1),
    ]
    sibling.close()


def test_rewind_fails_closed_when_new_turn_lands_after_id_snapshot(
    store, monkeypatch
):
    sid = _seed(store, "gw-snapshot-order", turns=2)
    sibling = SessionDB(db_path=store._db.db_path)
    original_load = store._db.get_messages_as_conversation

    def _load_then_append(*args, **kwargs):
        snapshot = original_load(*args, **kwargs)
        sibling.append_message(sid, "user", "q3-from-other-process")
        sibling.append_message(sid, "assistant", "a3-from-other-process")
        return snapshot

    monkeypatch.setattr(store._db, "get_messages_as_conversation", _load_then_append)

    assert store.rewind_session(sid) is None

    rows = store._db._conn.execute(
        "SELECT content, active FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("q1", 1),
        ("a1", 1),
        ("q2", 1),
        ("a2", 1),
        ("q3-from-other-process", 1),
        ("a3-from-other-process", 1),
    ]
    sibling.close()
