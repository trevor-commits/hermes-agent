"""Gateway contract for fail-closed context-ceiling results."""

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


_STRUCTURED_FIELDS = {
    "compression_exhausted",
    "compression_deferred",
    "hard_context_ceiling_blocked",
    "estimated_context_tokens",
    "hard_context_ceiling_tokens",
    "compression_block_reason",
    "continuity_preserved",
    "rollover_safe",
}


def test_gateway_context_ceiling_projection_preserves_every_structured_field():
    project = getattr(gateway_run, "_gateway_context_ceiling_result_fields", None)
    assert callable(project), "gateway needs one shared structured-result adapter"
    source = {name: f"value:{name}" for name in _STRUCTURED_FIELDS}
    assert project(source) == source


def test_rollover_requires_explicit_authoritative_continuity():
    authorize = getattr(gateway_run, "_gateway_rollover_is_authorized", None)
    assert callable(authorize), "gateway needs a single rollover authorization predicate"
    assert authorize({}) is False
    assert authorize({"compression_exhausted": True}) is False
    assert authorize({"compression_exhausted": True, "rollover_safe": True}) is False
    assert authorize(
        {
            "compression_exhausted": True,
            "continuity_preserved": True,
            "rollover_safe": False,
        }
    ) is False
    assert authorize(
        {
            "compression_exhausted": True,
            "continuity_preserved": True,
            "rollover_safe": True,
        }
    ) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rollover_fields", "should_reset"),
    [
        ({"compression_exhausted": True}, False),
        (
            {
                "compression_exhausted": True,
                "continuity_preserved": True,
                "rollover_safe": "true",
            },
            False,
        ),
        (
            {
                "compression_exhausted": True,
                "continuity_preserved": True,
                "rollover_safe": True,
                "compression_deferred": True,
            },
            False,
        ),
        (
            {
                "compression_exhausted": True,
                "continuity_preserved": True,
                "rollover_safe": True,
            },
            True,
        ),
    ],
)
async def test_gateway_resets_and_resyncs_only_for_authoritative_rollover(
    monkeypatch,
    tmp_path,
    rollover_fields,
    should_reset,
):
    runner = _bootstrap(monkeypatch, tmp_path)
    session_key = "agent:main:telegram:group:-1001:12345"
    fresh_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-after-authoritative-reset",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.reset_session.return_value = fresh_entry
    runner._sync_telegram_topic_binding = MagicMock()
    runner._run_agent = AsyncMock(
        return_value={
            "failed": True,
            "completed": False,
            "final_response": "hard ceiling blocked",
            "error": "hard_context_ceiling_blocked:compression_stalled",
            "messages": [{"role": "user", "content": "hello world"}],
            "history_offset": 0,
            "agent_persisted": True,
            "last_prompt_tokens": 2_000,
            **rollover_fields,
        }
    )

    await runner._handle_message_with_agent(_event(), _source(), session_key, 1)

    if should_reset:
        runner.session_store.reset_session.assert_called_once_with(session_key)
        runner._sync_telegram_topic_binding.assert_called_once_with(
            _source(),
            fresh_entry,
            reason="compression-exhausted-reset",
        )
    else:
        runner.session_store.reset_session.assert_not_called()
        runner._sync_telegram_topic_binding.assert_not_called()
