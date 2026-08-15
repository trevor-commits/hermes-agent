"""Proactive rollover: opt-in soft-threshold session roll at a safe boundary.

Sessions otherwise only reset in crisis (hard-ceiling exhaustion) or via
/new. With ``gateway.proactive_rollover_enabled`` on, a SUCCESSFUL turn
whose real provider-reported prompt usage reaches
``proactive_rollover_threshold_tokens`` rolls to a fresh session carrying a
deterministic digest, with a visible 🔄 notice. Failed turns, queued
sessions, sub-threshold usage, and the default-off config must never roll.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from gateway import run as gateway_run
from gateway.config import Platform
from gateway.session import SessionEntry, SessionSource, SessionStore
from gateway.turn_lease import SessionTurnLeaseRegistry
from hermes_state import CompressionSessionClosedError, SessionDB
from tests.gateway.test_42039_duplicate_user_message import (
    _bootstrap,
    _event,
    _source,
)

_SESSION_KEY = "agent:main:telegram:group:-1001:12345"


def _success_result(last_prompt_tokens: int) -> dict:
    return {
        "final_response": "all done",
        "messages": [
            {"role": "user", "content": "hello world", "_db_persisted": True},
            {"role": "assistant", "content": "all done"},
        ],
        "tools": [],
        "history_offset": 0,
        "agent_persisted": True,
        "last_prompt_tokens": last_prompt_tokens,
    }


def _rollover_runner(monkeypatch, tmp_path, *, enabled: bool):
    runner = _bootstrap(monkeypatch, tmp_path)
    runner.config.proactive_rollover_enabled = enabled
    runner.config.proactive_rollover_threshold_tokens = 24_000
    fresh_entry = SessionEntry(
        session_key=_SESSION_KEY,
        session_id="sess-proactive-fresh",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.rollover_session_with_carryover.return_value = fresh_entry
    runner.session_store.has_dirty_transcript.return_value = False
    runner.session_store._has_active_processes_safe.return_value = False
    runner._sync_telegram_topic_binding = MagicMock()
    return runner


def _appends_by_session(runner):
    by_session = {}
    for call in runner.session_store.append_to_transcript.call_args_list:
        if len(call.args) >= 2 and isinstance(call.args[1], dict):
            by_session.setdefault(call.args[0], []).append(call.args[1])
    return by_session


@pytest.mark.asyncio
async def test_enabled_over_threshold_rolls_with_carryover(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))

    response = await runner._handle_message_with_agent(
        _event(), _source(), _SESSION_KEY, 1
    )

    rollover_call = runner.session_store.rollover_session_with_carryover.call_args
    assert rollover_call.args[:2] == (_SESSION_KEY, "sess-dedup")
    carryover = rollover_call.args[2]
    assert "CONTEXT CARRYOVER" in carryover["content"]
    assert "sess-dedup" in carryover["content"]
    assert carryover.get("display_kind") == "hidden"
    # This turn's own rows went to the OLD session, before the roll.
    by_session = _appends_by_session(runner)
    assert any(
        row.get("role") == "assistant"
        for row in by_session.get("sess-dedup", [])
    )
    assert isinstance(response, str) and "🔄" in response
    runner._sync_telegram_topic_binding.assert_called_once()


@pytest.mark.asyncio
async def test_disabled_config_never_rolls(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=False)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.rollover_session_with_carryover.assert_not_called()


@pytest.mark.asyncio
async def test_under_threshold_never_rolls(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(5_000))

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.rollover_session_with_carryover.assert_not_called()


@pytest.mark.asyncio
async def test_failed_turn_never_rolls_proactively(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    failed = _success_result(25_000)
    failed.update({"failed": True, "error": "ReadTimeout: provider"})
    runner._run_agent = AsyncMock(return_value=failed)

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.rollover_session_with_carryover.assert_not_called()


@pytest.mark.asyncio
async def test_adapter_pending_message_defers_roll(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))
    adapter = MagicMock()
    adapter._pending_messages = {_SESSION_KEY: object()}
    adapter.send = AsyncMock()
    adapter.send_private_notice = AsyncMock()
    runner._adapter_for_source = MagicMock(return_value=adapter)

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.rollover_session_with_carryover.assert_not_called()


@pytest.mark.asyncio
async def test_runner_queued_event_defers_roll(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))
    runner._queued_events[_SESSION_KEY] = [object()]

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.rollover_session_with_carryover.assert_not_called()


@pytest.mark.asyncio
async def test_live_background_worker_defers_roll(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))
    monkeypatch.setattr(
        "tools.async_delegation.has_live_for_session",
        lambda **_kwargs: True,
    )

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.rollover_session_with_carryover.assert_not_called()


@pytest.mark.asyncio
async def test_live_registered_process_defers_roll(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))
    runner.session_store._has_active_processes_safe.return_value = True

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.rollover_session_with_carryover.assert_not_called()
    runner.session_store._has_active_processes_safe.assert_called_once_with(
        _SESSION_KEY,
        context="proactive_rollover",
    )


@pytest.mark.asyncio
async def test_dirty_transcript_defers_roll(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))
    runner.session_store.has_dirty_transcript.return_value = True

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.rollover_session_with_carryover.assert_not_called()


@pytest.mark.asyncio
async def test_pending_work_probe_exception_defers_roll(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))
    runner.session_store.has_dirty_transcript.side_effect = RuntimeError("probe failed")

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.rollover_session_with_carryover.assert_not_called()


@pytest.mark.asyncio
async def test_topic_sync_failure_keeps_committed_rollover(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))
    runner._sync_telegram_topic_binding.side_effect = OSError("topic sync failed")

    response = await runner._handle_message_with_agent(
        _event(), _source(), _SESSION_KEY, 1
    )

    runner.session_store.rollover_session_with_carryover.assert_called_once()
    assert isinstance(response, str) and "🔄" in response


def test_proactive_topic_sync_can_propagate_failure(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._is_telegram_topic_lane = MagicMock(return_value=True)
    runner._record_telegram_topic_binding = MagicMock(
        side_effect=OSError("topic sync failed")
    )

    with pytest.raises(OSError, match="topic sync failed"):
        gateway_run.GatewayRunner._sync_telegram_topic_binding(
            runner,
            _source(),
            runner.session_store.rollover_session_with_carryover.return_value,
            reason="proactive-rollover",
            raise_on_error=True,
        )


def _real_store(tmp_path: Path) -> tuple[SessionStore, SessionEntry]:
    store = SessionStore(tmp_path / "sessions", gateway_run.GatewayConfig())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )
    entry = store.get_or_create_session(source)
    return store, entry


def _carryover() -> dict:
    return {
        "role": "user",
        "content": "[CONTEXT CARRYOVER] exact durable summary",
        "_compressed_summary": True,
        "display_kind": "hidden",
        "timestamp": 1234.5,
    }


def _add_alias(store: SessionStore, entry: SessionEntry, suffix: str) -> SessionEntry:
    alias = dataclasses.replace(
        entry,
        session_key=f"{entry.session_key}:{suffix}",
    )
    with store._lock:
        store._entries[alias.session_key] = alias
        store._save()
    return alias


@pytest.mark.asyncio
async def test_two_alias_waiter_defers_proactive_rollover(monkeypatch, tmp_path):
    store, old_entry = _real_store(tmp_path)
    alias = _add_alias(store, old_entry, "alias")
    registry = SessionTurnLeaseRegistry()
    holder = await registry.acquire(
        old_entry.session_id,
        owner_key=old_entry.session_key,
        generation=1,
        timeout=1,
    )
    waiter_task = asyncio.create_task(
        registry.acquire(
            old_entry.session_id,
            owner_key=alias.session_key,
            generation=2,
            timeout=1,
        )
    )
    await asyncio.sleep(0)

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = store
    runner._async_session_store = None
    runner._turn_leases = registry
    runner._queued_events = {}
    runner._adapter_for_source = MagicMock(return_value=None)
    monkeypatch.setattr(
        "tools.async_delegation.has_live_for_session",
        lambda **_kwargs: False,
    )

    try:
        assert registry.has_waiters(old_entry.session_id) is True
        assert await runner._proactive_rollover_must_defer(
            old_entry.origin,
            old_entry.session_key,
            old_entry.session_id,
        ) is True
        assert store._db.get_session(old_entry.session_id)["end_reason"] is None
    finally:
        assert registry.release(holder) is True
        waiter = await waiter_task
        assert registry.release(waiter) is True


@pytest.mark.asyncio
async def test_two_alias_waiter_refreshes_to_committed_rollover_child(tmp_path):
    store, old_entry = _real_store(tmp_path)
    alias = _add_alias(store, old_entry, "alias")
    registry = SessionTurnLeaseRegistry()
    holder = await registry.acquire(
        old_entry.session_id,
        owner_key=old_entry.session_key,
        generation=1,
        timeout=1,
    )
    waiter_task = asyncio.create_task(
        registry.acquire(
            old_entry.session_id,
            owner_key=alias.session_key,
            generation=2,
            timeout=1,
        )
    )
    await asyncio.sleep(0)
    assert registry.has_waiters(old_entry.session_id) is True

    child = store.rollover_session_with_carryover(
        old_entry.session_key,
        old_entry.session_id,
        _carryover(),
    )
    assert child is not None

    assert registry.release(holder) is True
    waiter = await waiter_task
    try:
        assert waiter.contended is True
        refreshed = store.resolve_session_after_turn_lease_wait(
            alias.session_key,
            old_entry.session_id,
        )
        assert refreshed is not None
        assert refreshed.session_id == child.session_id
        assert registry.rebind(waiter, child.session_id) is True

        with pytest.raises(CompressionSessionClosedError):
            store._db.append_message(
                old_entry.session_id,
                role="user",
                content="must not land on ended parent",
            )
        with pytest.raises(CompressionSessionClosedError):
            store._db.append_messages_batch(
                old_entry.session_id,
                [{"role": "user", "content": "batch must not land either"}],
            )
        store.append_to_transcript(
            old_entry.session_id,
            {"role": "user", "content": "safely rerouted"},
        )
        assert any(
            message["content"] == "safely rerouted"
            for message in store._db.get_messages(child.session_id)
        )

        restarted = SessionStore(
            tmp_path / "sessions",
            gateway_run.GatewayConfig(),
        )
        restarted_alias = restarted.lookup_by_session_key(alias.session_key)
        assert restarted_alias is not None
        assert restarted_alias.session_id == child.session_id
    finally:
        assert registry.release(waiter) is True


@pytest.mark.parametrize(
    "step_method",
    [
        "_insert_session_row_tx",
        "_insert_gateway_rollover_carryover_tx",
        "_end_gateway_rollover_parent_tx",
        "_update_gateway_rollover_route_tx",
    ],
)
def test_atomic_db_rollover_failure_at_each_step_rolls_back(
    monkeypatch, tmp_path, step_method
):
    store, old_entry = _real_store(tmp_path)
    db: SessionDB = store._db
    original = getattr(db, step_method)

    def fail(*args, **kwargs):
        raise RuntimeError(f"injected {step_method}")

    monkeypatch.setattr(db, step_method, fail)
    new_entry = SessionEntry(
        session_key=old_entry.session_key,
        session_id="sess-proactive-fresh",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=old_entry.origin,
        platform=old_entry.platform,
        chat_type=old_entry.chat_type,
    )

    with pytest.raises(RuntimeError, match="injected"):
        db.atomic_gateway_rollover(
            scope=store._routing_scope(),
            session_key=old_entry.session_key,
            expected_session_id=old_entry.session_id,
            new_entry_json=json.dumps(new_entry.to_dict()),
            source="telegram",
            session_kwargs={
                "session_id": new_entry.session_id,
                "parent_session_id": old_entry.session_id,
                "session_key": old_entry.session_key,
            },
            carryover_message=_carryover(),
        )

    monkeypatch.setattr(db, step_method, original)
    assert db.get_session(new_entry.session_id) is None
    assert db.get_session(old_entry.session_id)["end_reason"] is None
    route = db.load_gateway_routing_entries(scope=store._routing_scope())
    assert json.loads(route[old_entry.session_key])["session_id"] == old_entry.session_id


def test_atomic_db_rollover_route_race_is_safe_noop(tmp_path):
    store, old_entry = _real_store(tmp_path)
    db: SessionDB = store._db
    route = db.load_gateway_routing_entries(scope=store._routing_scope())
    moved = json.loads(route[old_entry.session_key])
    moved["session_id"] = "another-session"
    db.save_gateway_routing_entry(
        old_entry.session_key,
        json.dumps(moved),
        scope=store._routing_scope(),
    )

    result = store.rollover_session_with_carryover(
        old_entry.session_key,
        old_entry.session_id,
        _carryover(),
    )

    assert result is None
    assert db.get_session(old_entry.session_id)["end_reason"] is None
    assert json.loads(
        db.load_gateway_routing_entries(scope=store._routing_scope())[
            old_entry.session_key
        ]
    )["session_id"] == "another-session"


def test_dirty_store_transcript_refuses_atomic_rollover(tmp_path):
    store, old_entry = _real_store(tmp_path)
    store._dirty_transcripts[old_entry.session_id] = [
        {"role": "assistant", "content": "not committed"}
    ]

    result = store.rollover_session_with_carryover(
        old_entry.session_key,
        old_entry.session_id,
        _carryover(),
    )

    assert result is None
    assert store._db.get_session(old_entry.session_id)["end_reason"] is None
    route = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert json.loads(route[old_entry.session_key])["session_id"] == old_entry.session_id


def test_successful_rollover_commits_lineage_carryover_end_and_route(tmp_path):
    store, old_entry = _real_store(tmp_path)

    new_entry = store.rollover_session_with_carryover(
        old_entry.session_key,
        old_entry.session_id,
        _carryover(),
    )

    assert new_entry is not None
    old_row = store._db.get_session(old_entry.session_id)
    child_row = store._db.get_session(new_entry.session_id)
    assert old_row["end_reason"] == "proactive_rollover"
    assert child_row["parent_session_id"] == old_entry.session_id
    messages = store._db.get_messages(new_entry.session_id)
    assert len(messages) == 1
    assert messages[0]["display_kind"] == "hidden"
    assert "exact durable summary" in messages[0]["content"]
    route = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert json.loads(route[old_entry.session_key])["session_id"] == new_entry.session_id
    assert store._db.get_compression_tip(old_entry.session_id) == new_entry.session_id


def test_proactive_tip_rejects_child_without_matching_marker(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("roll-parent", "telegram")
    db.create_session(
        "unmarked-child",
        "telegram",
        parent_session_id="roll-parent",
    )
    db.end_session("roll-parent", "proactive_rollover")

    assert db.get_compression_tip("roll-parent") == "roll-parent"

    db.create_session(
        "marked-child",
        "telegram",
        parent_session_id="roll-parent",
        model_config={
            "_proactive_rollover": True,
            "_reset_from": "roll-parent",
        },
    )
    assert db.get_compression_tip("roll-parent") == "marked-child"


def test_post_commit_mirror_failure_keeps_database_route(monkeypatch, tmp_path):
    store, old_entry = _real_store(tmp_path)
    monkeypatch.setattr(
        store,
        "_save_sessions_json",
        MagicMock(side_effect=OSError("mirror unavailable")),
    )

    new_entry = store.rollover_session_with_carryover(
        old_entry.session_key,
        old_entry.session_id,
        _carryover(),
    )

    assert new_entry is not None
    route = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert json.loads(route[old_entry.session_key])["session_id"] == new_entry.session_id


def test_restart_reconstructs_committed_rollover_route(tmp_path):
    store, old_entry = _real_store(tmp_path)
    new_entry = store.rollover_session_with_carryover(
        old_entry.session_key,
        old_entry.session_id,
        _carryover(),
    )
    assert new_entry is not None

    restarted = SessionStore(tmp_path / "sessions", gateway_run.GatewayConfig())
    restarted_entry = restarted.get_or_create_session(old_entry.origin)

    assert restarted_entry.session_id == new_entry.session_id


def test_carryover_digest_prefers_compacted_summary():
    digest = gateway_run._build_proactive_rollover_carryover(
        [
            {"role": "user", "content": "first ask"},
            {
                "role": "user",
                "content": "[CONTEXT COMPACTION] the compact story so far",
                "_compressed_summary": True,
            },
            {"role": "user", "content": "latest ask"},
            {"role": "assistant", "content": "latest answer"},
        ],
        old_session_id="sess-old-123",
    )
    assert "sess-old-123" in digest
    assert "the compact story so far" in digest
    assert "latest ask" in digest
    assert "latest answer" in digest
    assert len(digest) <= 3500
