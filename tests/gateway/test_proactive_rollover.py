"""Proactive rollover: opt-in soft-threshold session roll at a safe boundary.

Sessions otherwise only reset in crisis (hard-ceiling exhaustion) or via
/new. With ``gateway.proactive_rollover_enabled`` on, a SUCCESSFUL turn
whose real provider-reported prompt usage reaches
``proactive_rollover_threshold_tokens`` rolls to a fresh session carrying a
deterministic digest, with a visible 🔄 notice. Failed turns, queued
sessions, sub-threshold usage, and the default-off config must never roll.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from gateway import run as gateway_run
from gateway.config import Platform
from gateway.session import SessionEntry
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
    runner.session_store.reset_session.return_value = fresh_entry
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

    runner.session_store.reset_session.assert_called_once_with(_SESSION_KEY)
    by_session = _appends_by_session(runner)
    carryover_rows = [
        row
        for row in by_session.get("sess-proactive-fresh", [])
        if row.get("_compressed_summary") is True
    ]
    assert len(carryover_rows) == 1
    assert "CONTEXT CARRYOVER" in carryover_rows[0]["content"]
    assert "sess-dedup" in carryover_rows[0]["content"]
    assert carryover_rows[0].get("display_kind") == "hidden"
    # This turn's own rows went to the OLD session, before the roll.
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

    runner.session_store.reset_session.assert_not_called()


@pytest.mark.asyncio
async def test_under_threshold_never_rolls(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(5_000))

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.reset_session.assert_not_called()


@pytest.mark.asyncio
async def test_failed_turn_never_rolls_proactively(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    failed = _success_result(25_000)
    failed.update({"failed": True, "error": "ReadTimeout: provider"})
    runner._run_agent = AsyncMock(return_value=failed)

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.reset_session.assert_not_called()


@pytest.mark.asyncio
async def test_queued_session_defers_roll(monkeypatch, tmp_path):
    runner = _rollover_runner(monkeypatch, tmp_path, enabled=True)
    runner._run_agent = AsyncMock(return_value=_success_result(25_000))
    runner._pending_messages[_SESSION_KEY] = [object()]

    await runner._handle_message_with_agent(_event(), _source(), _SESSION_KEY, 1)

    runner.session_store.reset_session.assert_not_called()


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
