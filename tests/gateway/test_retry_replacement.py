"""Regression tests for /retry replacement semantics."""

import asyncio
from datetime import datetime

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore
from gateway.turn_lease import SessionTurnLeaseRegistry
from tests.gateway.test_42039_duplicate_user_message import _bootstrap


@pytest.mark.asyncio
async def test_gateway_retry_replaces_last_user_turn_in_transcript(tmp_path, monkeypatch):
    # Pin DEFAULT_DB_PATH so SessionDB() doesn't write to the real ~/.hermes/state.db.
    # (Module-level constant snapshot, see test_load_transcript_db_only.)
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")

    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path, config=config)

    session_id = "retry_session"
    store._db.create_session(session_id=session_id, source="test")
    for msg in [
        {"role": "session_meta", "tools": []},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "retry me"},
        {"role": "assistant", "content": "old answer"},
    ]:
        store.append_to_transcript(session_id, msg)

    gw = GatewayRunner.__new__(GatewayRunner)
    gw.config = config
    gw.session_store = store

    session_entry = MagicMock(session_id=session_id)
    session_entry.last_prompt_tokens = 111
    gw.session_store.get_or_create_session = MagicMock(return_value=session_entry)

    async def fake_handle_message(event):
        assert event.text == "retry me"
        transcript_before = store.load_transcript(session_id)
        assert [m.get("content") for m in transcript_before if m.get("role") == "user"] == [
            "first question"
        ]
        store.append_to_transcript(session_id, {"role": "user", "content": event.text})
        store.append_to_transcript(session_id, {"role": "assistant", "content": "new answer"})
        return "new answer"

    gw._handle_message = AsyncMock(side_effect=fake_handle_message)

    retry_event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=MagicMock(),
    )
    assert await gw._prepare_retry_turn(retry_event, session_entry) is None
    result = await gw._handle_message(retry_event)

    assert result == "new answer"
    transcript_after = store.load_transcript(session_id)
    assert [m.get("content") for m in transcript_after if m.get("role") == "user"] == [
        "first question",
        "retry me",
    ]
    assert [m.get("content") for m in transcript_after if m.get("role") == "assistant"] == [
        "first answer",
        "new answer",
    ]


@pytest.mark.asyncio
async def test_gateway_retry_preserves_archived_compaction_rows_when_probe_fails(
    tmp_path, monkeypatch
):
    """/retry must not DELETE archives when an existence probe would fail.

    With compression.in_place (the default, #38763) archive_and_compact()
    keeps the pre-compaction transcript on disk as active=0/compacted=1 rows
    under the same session id. /retry used to persist its truncation via a
    bare rewrite_transcript(), whose replace_messages(active_only=False)
    DELETEs every row for the session and reinserts only the truncated live
    tail, wiping the archived history permanently (same class as #61145;
    #57803 named this call site as a residual gap). /retry never intends to
    purge archived history, so it must pass active_only=True unconditionally:
    a separate existence probe can fail open or race with the rewrite.
    """
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")

    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path, config=config)

    session_id = "retry_archived_session"
    store._db.create_session(session_id=session_id, source="test")
    store._db.append_message(session_id=session_id, role="user", content="old question")
    store._db.append_message(session_id=session_id, role="assistant", content="old answer")
    # In-place compaction: the two rows above are soft-archived and the
    # compacted transcript becomes the live set under the same id.
    store._db.archive_and_compact(
        session_id,
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "retry me"},
            {"role": "assistant", "content": "old answer"},
        ],
    )
    assert store._db.has_archived_messages(session_id) is True

    # A failed preflight lookup must not turn this data-preservation path back
    # into a destructive full-history rewrite. The write itself still works.
    archived_probe = MagicMock(side_effect=OSError("transient archive lookup failure"))
    monkeypatch.setattr(store._db, "has_archived_messages", archived_probe)

    gw = GatewayRunner.__new__(GatewayRunner)
    gw.config = config
    gw.session_store = store

    session_entry = MagicMock(session_id=session_id)
    session_entry.last_prompt_tokens = 111
    gw.session_store.get_or_create_session = MagicMock(return_value=session_entry)

    async def fake_handle_message(event):
        assert event.text == "retry me"
        store.append_to_transcript(session_id, {"role": "user", "content": event.text})
        store.append_to_transcript(session_id, {"role": "assistant", "content": "new answer"})
        return "new answer"

    gw._handle_message = AsyncMock(side_effect=fake_handle_message)

    retry_event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=MagicMock(),
    )
    assert await gw._prepare_retry_turn(retry_event, session_entry) is None
    result = await gw._handle_message(retry_event)

    assert result == "new answer"
    archived_probe.assert_not_called()
    # The archived pre-compaction rows survive the rewrite untouched.
    archived = [
        m for m in store._db.get_messages(session_id, include_inactive=True)
        if not m["active"]
    ]
    assert [(m["role"], m["content"]) for m in archived] == [
        ("user", "old question"),
        ("assistant", "old answer"),
    ]
    assert all(m["compacted"] == 1 for m in archived)
    # The live set reflects the truncation plus the retried exchange.
    transcript_after = store.load_transcript(session_id)
    assert [m.get("content") for m in transcript_after if m.get("role") == "user"] == [
        "first question",
        "retry me",
    ]


@pytest.mark.asyncio
async def test_gateway_retry_waits_for_alias_rollover_then_rewrites_only_child(
    monkeypatch,
    tmp_path,
):
    """The retry truncation belongs inside the resolved session turn lease."""
    runner = _bootstrap(monkeypatch, tmp_path)
    registry = SessionTurnLeaseRegistry()
    runner._turn_leases = registry
    quick_key = "agent:main:telegram:group:-1001:12345:retry-alias"
    parent_id = "retry-rollover-parent"
    child_id = "retry-rollover-child"
    now = datetime.now()
    parent = SessionEntry(
        session_key=quick_key,
        session_id=parent_id,
        created_at=now,
        updated_at=now,
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    child = SessionEntry(
        session_key=quick_key,
        session_id=child_id,
        created_at=now,
        updated_at=now,
        platform=parent.platform,
        chat_type="group",
        is_fresh_reset=True,
        auto_skill_pending=True,
    )
    runner.session_store.get_or_create_session.return_value = parent
    runner.session_store.resolve_session_after_turn_lease_wait.return_value = child
    retry_history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "retry me"},
        {"role": "assistant", "content": "old answer"},
    ]
    runner.session_store.load_transcript.side_effect = [
        retry_history,
        retry_history[:2],
    ]

    def rewrite_only_after_child_rebind(session_id, messages, active_only=False):
        assert session_id == child_id
        assert messages == retry_history[:2]
        assert active_only is True
        lease = registry._leases[child_id]
        assert lease.holder is not None
        assert lease.holder.owner_key == quick_key
        return True

    runner.session_store.rewrite_transcript.side_effect = rewrite_only_after_child_rebind

    async def run_retried_turn(*_args, **_kwargs):
        assert retry_event.text == "retry me"
        return {
            "failed": True,
            "final_response": None,
            "error": "stop after retry preparation",
            "messages": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }

    runner._run_agent = AsyncMock(side_effect=run_retried_turn)
    holder = await registry.acquire(
        parent_id,
        owner_key="rollover-alias",
        generation=9,
        timeout=1,
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )
    retry_event = MessageEvent(
        text="/retry",
        message_type=MessageType.TEXT,
        source=source,
    )
    retry_event._gateway_retry_requested = True

    retry_task = asyncio.create_task(
        runner._handle_message_with_agent(
            retry_event,
            source,
            quick_key,
            1,
        )
    )
    for _ in range(100):
        if registry.has_waiters(parent_id) or retry_task.done():
            break
        await asyncio.sleep(0)
    assert registry.has_waiters(parent_id) is True
    assert registry.release(holder) is True

    await retry_task
    runner.session_store.resolve_session_after_turn_lease_wait.assert_called_once_with(
        quick_key,
        parent_id,
    )
    runner.session_store.rewrite_transcript.assert_called_once()
    assert runner._release_turn_lease(quick_key, 1) is True
