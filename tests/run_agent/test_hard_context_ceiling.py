"""Provider sends fail closed when context cannot be compacted below its ceiling."""

from __future__ import annotations

import contextlib
from dataclasses import asdict, replace
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


def _config() -> dict:
    return {
        "compression": {
            "enabled": True,
            "threshold": 0.50,
            "target_ratio": 0.20,
            "protect_first_n": 3,
            "protect_last_n": 20,
            "max_attempts": 1,
        },
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(monkeypatch, tmp_path: Path) -> tuple[AIAgent, SessionDB]:
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", _config)
    monkeypatch.setattr(config_mod, "load_config_readonly", _config)
    db = SessionDB(db_path=tmp_path / "state.db")
    with (
        contextlib.redirect_stdout(io.StringIO()),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            base_url="https://openrouter.ai/api/v1",
            api_key="test-key",
            model="test/model",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
            session_db=db,
            session_id="hard-ceiling-e2e",
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    message = SimpleNamespace(
        content="provider should not receive this request",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    agent.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )
    return agent, db


def test_provider_is_not_called_after_compression_stalls_above_ceiling(
    monkeypatch, tmp_path
):
    agent, db = _make_agent(monkeypatch, tmp_path)
    agent.context_compressor.threshold_tokens = 1_000

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
        for i in range(60)
    ]

    def _no_progress(messages, system_message, **_kwargs):
        active_prompt = (
            system_message.get("content", "")
            if isinstance(system_message, dict)
            else agent._cached_system_prompt
        )
        return messages, active_prompt

    with (
        patch(
            "agent.turn_context.estimate_request_tokens_rough",
            return_value=2_000,
        ),
        patch(
            "agent.conversation_loop.estimate_messages_tokens_rough",
            return_value=2_000,
        ),
        patch.object(agent, "_compress_context", side_effect=_no_progress),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("keep this turn", conversation_history=history)

    agent.client.chat.completions.create.assert_not_called()
    assert result["completed"] is False
    assert result["failed"] is True
    assert result["compression_exhausted"] is True
    assert result["hard_context_ceiling_blocked"] is True
    assert result["estimated_context_tokens"] == 2_000
    assert result["hard_context_ceiling_tokens"] == 1_000
    assert result["session_id"] == "hard-ceiling-e2e"
    assert result["continuity_preserved"] is True
    assert result["rollover_safe"] is True
    assert "was not sent" in result["final_response"]

    persisted = db.get_messages("hard-ceiling-e2e")
    assert any(
        row.get("role") == "user" and row.get("content") == "keep this turn"
        for row in persisted
    )
    assert any(
        row.get("role") == "assistant" and "was not sent" in row.get("content", "")
        for row in persisted
    )


def test_pre_api_compression_rebinds_before_next_iteration_hard_ceiling(
    monkeypatch, tmp_path
):
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    agent, db = _make_agent(monkeypatch, tmp_path)
    agent.context_compressor.threshold_tokens = 1_000
    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"history {index}",
        }
        for index in range(8)
    ]

    def _compress_to_index_six(messages, _system_message, **_kwargs):
        active = next(
            message
            for message in messages
            if message.get(CURRENT_TURN_IDENTITY_KEY)
            == agent._persist_user_turn_identity
        )
        compressed = [dict(message) for message in messages[:6]]
        compressed.append(dict(active))
        assert agent._persist_user_message_idx == 8
        assert len(compressed) - 1 == 6
        agent._last_compression_attempt_recorded = True
        agent._last_compression_attempt_in_place = True
        return compressed, agent._cached_system_prompt

    with (
        patch(
            "agent.turn_context.estimate_request_tokens_rough",
            return_value=500,
        ),
        patch(
            "agent.conversation_loop.estimate_messages_tokens_rough",
            return_value=2_000,
        ),
        patch.object(agent, "_compress_context", side_effect=_compress_to_index_six),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "active ask",
            conversation_history=history,
        )

    agent.client.chat.completions.create.assert_not_called()
    assert result["hard_context_ceiling_blocked"] is True
    assert result["continuity_preserved"] is True
    assert result["agent_persisted"] is True
    assert result["rollover_safe"] is True
    assert agent._persist_user_message_idx == 6
    assert any(
        row.get("role") == "user" and row.get("content") == "active ask"
        for row in db.get_messages(agent.session_id)
    )


def test_execution_middleware_cannot_expand_stalled_turn_over_ceiling(
    monkeypatch, tmp_path
):
    agent, db = _make_agent(monkeypatch, tmp_path)
    agent.context_compressor.threshold_tokens = 1_000
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
        for i in range(60)
    ]

    def _no_progress(messages, _system_message, **_kwargs):
        return messages, agent._cached_system_prompt

    def _estimate(messages):
        if messages == [{"role": "user", "content": "middleware expansion"}]:
            return 2_000
        return 500

    def _inflate_request(request, next_call, **_kwargs):
        expanded = dict(request)
        expanded["messages"] = [
            {"role": "user", "content": "middleware expansion"}
        ]
        return next_call(expanded)

    with (
        patch(
            "agent.turn_context.estimate_request_tokens_rough",
            return_value=2_000,
        ),
        patch(
            "agent.conversation_loop.estimate_messages_tokens_rough",
            side_effect=_estimate,
        ),
        patch.object(agent, "_compress_context", side_effect=_no_progress),
        patch(
            "hermes_cli.middleware.run_llm_execution_middleware",
            side_effect=_inflate_request,
        ),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("keep this turn", conversation_history=history)

    agent.client.chat.completions.create.assert_not_called()
    assert result["hard_context_ceiling_blocked"] is True
    assert result["estimated_context_tokens"] == 2_000


def test_swallowed_db_failure_disables_continuity_and_rollover(
    monkeypatch, tmp_path
):
    agent, db = _make_agent(monkeypatch, tmp_path)
    agent.context_compressor.threshold_tokens = 1_000
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
        for i in range(60)
    ]

    with (
        patch(
            "agent.turn_context.estimate_request_tokens_rough",
            return_value=2_000,
        ),
        patch(
            "agent.conversation_loop.estimate_messages_tokens_rough",
            return_value=2_000,
        ),
        patch.object(agent, "_compress_context", side_effect=lambda m, s, **k: (m, s)),
        patch.object(
            db,
            "append_messages_batch",
            side_effect=RuntimeError("simulated sqlite write failure"),
        ),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("must remain durable", conversation_history=history)

    agent.client.chat.completions.create.assert_not_called()
    assert result["hard_context_ceiling_blocked"] is True
    assert result["continuity_preserved"] is False
    assert result["rollover_safe"] is False
    assert result["compression_exhausted"] is False
    assert not any(
        row.get("role") == "user" and row.get("content") == "must remain durable"
        for row in db.get_messages("hard-ceiling-e2e")
    )


def test_boolean_success_without_current_user_durability_is_not_authoritative(
    monkeypatch, tmp_path
):
    agent, _db = _make_agent(monkeypatch, tmp_path)
    messages = [{"role": "user", "content": "not actually written"}]
    agent._persist_user_message_idx = 0

    from agent.conversation_loop import _hard_context_ceiling_result

    with patch.object(agent, "_persist_session", return_value=True):
        result = _hard_context_ceiling_result(
            agent,
            messages,
            [],
            0,
            estimated_tokens=2_000,
            ceiling_tokens=1_000,
            block_reason="compression_stalled",
        )

    assert result["continuity_preserved"] is False
    assert result["rollover_safe"] is False
    assert result["compression_exhausted"] is False


def test_failed_terminal_explanation_persist_disables_rollover_without_user_duplication(
    monkeypatch, tmp_path
):
    agent, db = _make_agent(monkeypatch, tmp_path)
    turn_identity = "hard-ceiling-e2e:current-turn"
    messages = [
        {
            "role": "user",
            "content": "durable user",
            "_db_persisted": True,
            "_current_turn_identity": turn_identity,
        }
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = turn_identity
    agent._ensure_db_session()
    row_id = db.append_message(agent.session_id, "user", content="durable user")
    messages[0]["_row_id"] = row_id
    agent._current_turn_durability_receipt = _durability_receipt(
        agent, row_id, "durable user", turn_identity
    )

    from agent.conversation_loop import _hard_context_ceiling_result

    with patch.object(agent, "_persist_session", side_effect=[True, False]):
        result = _hard_context_ceiling_result(
            agent,
            messages,
            [],
            0,
            estimated_tokens=2_000,
            ceiling_tokens=1_000,
            block_reason="compression_stalled",
        )

    assert result["continuity_preserved"] is False
    assert result["rollover_safe"] is False
    assert result["compression_exhausted"] is False
    assert result["agent_persisted"] is True


def test_single_oversized_current_input_never_calls_provider_or_rolls_session(
    monkeypatch, tmp_path
):
    agent, db = _make_agent(monkeypatch, tmp_path)
    agent.context_compressor.threshold_tokens = 1_000
    history = [
        {"role": "user", "content": "healthy prior question"},
        {"role": "assistant", "content": "healthy prior answer"},
    ]
    original_session_id = agent.session_id
    oversized_input = "x" * 8_000

    with (
        patch.object(agent, "_compress_context", wraps=agent._compress_context) as compress,
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(oversized_input, conversation_history=history)

    agent.client.chat.completions.create.assert_not_called()
    compress.assert_not_called()
    assert agent.session_id == original_session_id
    assert result["hard_context_ceiling_blocked"] is True
    assert result["compression_block_reason"] == "input_too_large"
    assert result["continuity_preserved"] is True
    assert result["rollover_safe"] is False
    assert result["compression_exhausted"] is False
    assert "input is too large" in result["final_response"].lower()
    assert any(
        row.get("role") == "user" and row.get("content") == oversized_input
        for row in db.get_messages(original_session_id)
    )


def test_execution_middleware_cannot_shrink_oversized_input_past_terminal_guard(
    monkeypatch, tmp_path
):
    agent, db = _make_agent(monkeypatch, tmp_path)
    agent.context_compressor.threshold_tokens = 1_000
    oversized_input = "x" * 8_000

    def _replace_with_short_request(request, next_call, **_kwargs):
        shortened = dict(request)
        shortened["messages"] = [{"role": "user", "content": "shortened"}]
        return next_call(shortened)

    with (
        patch(
            "hermes_cli.middleware.run_llm_execution_middleware",
            side_effect=_replace_with_short_request,
        ) as middleware,
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(oversized_input, conversation_history=[])

    middleware.assert_not_called()
    agent.client.chat.completions.create.assert_not_called()
    assert result["api_calls"] == 0
    assert result["hard_context_ceiling_blocked"] is True
    assert result["compression_block_reason"] == "input_too_large"
    assert result["rollover_safe"] is False


def test_prior_durable_user_cannot_authorize_missing_current_turn(
    monkeypatch, tmp_path
):
    agent, _db = _make_agent(monkeypatch, tmp_path)
    messages = [
        {
            "role": "user",
            "content": "durable prior turn",
            "_db_persisted": True,
            "_current_turn_identity": "prior-turn",
        }
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = "missing-current-turn"

    from agent.conversation_loop import _hard_context_ceiling_result

    with patch.object(agent, "_persist_session", return_value=True):
        result = _hard_context_ceiling_result(
            agent,
            messages,
            [],
            0,
            estimated_tokens=2_000,
            ceiling_tokens=1_000,
            block_reason="compression_stalled",
        )

    assert result["continuity_preserved"] is False
    assert result["rollover_safe"] is False
    assert result["compression_exhausted"] is False
    assert result["agent_persisted"] is False
    assert [m["content"] for m in messages if m.get("role") == "user"] == [
        "durable prior turn"
    ]


def test_turn_identity_reanchors_rewritten_compaction_copy():
    from agent.turn_context import (
        CURRENT_TURN_IDENTITY_KEY,
        reanchor_current_turn_user_idx,
    )

    turn_identity = "session:active-turn"
    messages = [
        {
            "role": "user",
            "content": "durable prior turn",
            "_db_persisted": True,
        },
        {
            "role": "user",
            "content": "summary prefix\n\nactive ask",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
        },
    ]

    assert reanchor_current_turn_user_idx(
        messages,
        "active ask",
        turn_identity=turn_identity,
    ) == 1
    assert reanchor_current_turn_user_idx(
        messages[:1],
        "active ask",
        turn_identity=turn_identity,
    ) == -1


def test_committed_compression_rebinds_current_turn_before_hard_ceiling(
    monkeypatch, tmp_path
):
    from agent.conversation_loop import _hard_context_ceiling_result
    from agent.turn_context import (
        CURRENT_TURN_IDENTITY_KEY,
        rebind_turn_after_compression,
    )

    agent, db = _make_agent(monkeypatch, tmp_path)
    turn_identity = "hard-ceiling-e2e:compressed-current-turn"
    original_messages = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"original {index}",
            "_db_persisted": True,
        }
        for index in range(8)
    ]
    original_messages.append(
        {
            "role": "user",
            "content": "active ask",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
            "_db_persisted": True,
        }
    )
    assert len(original_messages) - 1 == 8

    compressed_messages = [dict(message) for message in original_messages[:6]]
    compressed_messages.append(dict(original_messages[8]))
    assert len(compressed_messages) - 1 == 6

    agent._persist_user_message_idx = 8
    agent._persist_user_turn_identity = turn_identity
    agent._ensure_db_session()
    row_id = db.append_message(agent.session_id, "user", content="active ask")
    original_messages[8]["_row_id"] = row_id
    compressed_messages[6]["_row_id"] = row_id
    agent._current_turn_durability_receipt = _durability_receipt(
        agent, row_id, "active ask", turn_identity
    )
    agent._last_compression_attempt_recorded = True
    agent._last_compression_attempt_in_place = True

    conversation_history, current_turn_user_idx = rebind_turn_after_compression(
        agent,
        compressed_messages,
        original_messages,
    )

    assert current_turn_user_idx == 6
    assert agent._persist_user_message_idx == 6
    assert conversation_history == compressed_messages

    result = _hard_context_ceiling_result(
        agent,
        compressed_messages,
        conversation_history,
        0,
        estimated_tokens=2_000,
        ceiling_tokens=1_000,
        block_reason="compression_stalled",
    )

    assert result["continuity_preserved"] is True
    assert result["agent_persisted"] is True
    assert result["rollover_safe"] is True


def test_compression_rebind_missing_identity_remains_fail_closed(
    monkeypatch, tmp_path
):
    from agent.conversation_loop import _hard_context_ceiling_result
    from agent.turn_context import rebind_turn_after_compression

    agent, db = _make_agent(monkeypatch, tmp_path)
    messages = [
        {
            "role": "user",
            "content": "durable prior turn",
            "_db_persisted": True,
        }
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = "missing-current-turn"
    agent._last_compression_attempt_recorded = True
    agent._last_compression_attempt_in_place = True

    conversation_history, current_turn_user_idx = rebind_turn_after_compression(
        agent,
        messages,
        list(messages),
    )

    assert current_turn_user_idx == -1
    assert agent._persist_user_message_idx == -1

    result = _hard_context_ceiling_result(
        agent,
        messages,
        conversation_history,
        0,
        estimated_tokens=2_000,
        ceiling_tokens=1_000,
        block_reason="compression_stalled",
    )

    assert result["continuity_preserved"] is False
    assert result["agent_persisted"] is False
    assert result["rollover_safe"] is False


def test_compaction_user_merge_carries_current_turn_identity():
    from agent.context_compressor import ContextCompressor
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    turn_identity = "session:merged-active-turn"
    merged = ContextCompressor._merge_adjacent_user_turns(
        [
            {"role": "user", "content": "preserved scaffold"},
            {
                "role": "user",
                "content": "active ask",
                CURRENT_TURN_IDENTITY_KEY: turn_identity,
            },
        ]
    )

    assert len(merged) == 1
    assert merged[0][CURRENT_TURN_IDENTITY_KEY] == turn_identity


def test_sequence_repair_cannot_inherit_prior_turn_durability():
    from agent.agent_runtime_helpers import repair_message_sequence
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    turn_identity = "session:unpersisted-active-turn"
    messages = [
        {
            "role": "user",
            "content": "durable prior turn",
            "_db_persisted": True,
        },
        {
            "role": "user",
            "content": "unpersisted active ask",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
        },
    ]

    repair_message_sequence(SimpleNamespace(), messages)

    assert len(messages) == 1
    assert messages[0][CURRENT_TURN_IDENTITY_KEY] == turn_identity
    assert messages[0].get("_db_persisted") is not True


def test_sequence_repair_flushes_current_turn_instead_of_restamping_history(
    monkeypatch, tmp_path
):
    from agent.agent_runtime_helpers import repair_message_sequence
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    agent, db = _make_agent(monkeypatch, tmp_path)
    agent._ensure_db_session()
    db.append_message(agent.session_id, "user", content="durable prior turn")
    history = [
        {
            "role": "user",
            "content": "durable prior turn",
            "_db_persisted": True,
        }
    ]
    turn_identity = "hard-ceiling-e2e:unpersisted-active-turn"
    messages = [
        history[0],
        {
            "role": "user",
            "content": "unpersisted active ask",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
        },
    ]
    agent._persist_user_message_idx = 1
    agent._persist_user_turn_identity = turn_identity

    repair_message_sequence(agent, messages)
    agent._persist_user_message_idx = 0

    assert agent._persist_session(messages, history) is True
    assert agent._current_turn_user_is_durable(messages) is True
    assert any(
        row.get("role") == "user"
        and "unpersisted active ask" in str(row.get("content") or "")
        for row in db.get_messages(agent.session_id)
    )


def test_later_compaction_summary_does_not_revoke_current_turn_durability(
    monkeypatch, tmp_path
):
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    agent, db = _make_agent(monkeypatch, tmp_path)
    turn_identity = "hard-ceiling:active-turn-before-summary"
    messages = [
        {
            "role": "user",
            "content": "active ask",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
            "_db_persisted": True,
        },
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] saved context",
            COMPRESSED_SUMMARY_METADATA_KEY: True,
            "_db_persisted": True,
        },
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = turn_identity
    agent._ensure_db_session()
    row_id = db.append_message(agent.session_id, "user", content="active ask")
    messages[0]["_row_id"] = row_id
    agent._current_turn_durability_receipt = _durability_receipt(
        agent, row_id, "active ask", turn_identity
    )

    assert agent._current_turn_user_is_durable(messages) is True


def _durability_case(kind: str):
    """Build (idx, identity, messages, expected_bool, expected_reason)."""
    turn_identity = "hard-ceiling-e2e:reason-case"
    durable_row = {
        "role": "user",
        "content": "active ask",
        "_current_turn_identity": turn_identity,
        "_db_persisted": True,
    }
    if kind == "ok":
        return 0, turn_identity, [dict(durable_row)], True, "ok"
    if kind == "idx_out_of_range":
        return 5, turn_identity, [dict(durable_row)], False, "idx_out_of_range"
    if kind == "identity_missing":
        return 0, None, [dict(durable_row)], False, "identity_missing"
    if kind == "wrong_role":
        row = dict(durable_row)
        row["role"] = "assistant"
        return 0, turn_identity, [row], False, "wrong_role"
    if kind == "identity_mismatch":
        row = dict(durable_row)
        row["_current_turn_identity"] = "some-other-turn"
        return 0, turn_identity, [row], False, "identity_mismatch"
    if kind == "not_db_persisted":
        row = dict(durable_row)
        row.pop("_db_persisted")
        return 0, turn_identity, [row], False, "not_db_persisted"
    if kind == "later_unmarked_user_row":
        later = {"role": "user", "content": "duplicate copy"}
        return (
            0,
            turn_identity,
            [dict(durable_row), later],
            False,
            "later_unmarked_user_row",
        )
    raise AssertionError(f"unknown case {kind}")


@pytest.mark.parametrize(
    "kind",
    [
        "ok",
        "idx_out_of_range",
        "identity_missing",
        "wrong_role",
        "identity_mismatch",
        "not_db_persisted",
        "later_unmarked_user_row",
    ],
)
def test_current_turn_durability_names_its_failing_predicate(
    monkeypatch, tmp_path, kind
):
    """The durability check must say WHY it refused rollover authority so a
    live hard-ceiling block line is diagnosable (durable_fail=<reason>)."""
    agent, db = _make_agent(monkeypatch, tmp_path)
    idx, identity, messages, expected_bool, expected_reason = _durability_case(kind)
    agent._persist_user_message_idx = idx
    agent._persist_user_turn_identity = identity
    if kind == "ok":
        agent._ensure_db_session()
        row_id = db.append_message(
            agent.session_id, "user", content=messages[idx]["content"]
        )
        messages[idx]["_row_id"] = row_id
        agent._current_turn_durability_receipt = _durability_receipt(
            agent, row_id, messages[idx]["content"], identity
        )

    assert agent._current_turn_user_is_durable(messages) is expected_bool
    assert agent._current_turn_durability_fail_reason == expected_reason


def test_historical_identical_content_cannot_authorize_current_turn(
    monkeypatch, tmp_path
):
    """An older same-content row is not proof that this turn was committed."""
    from agent.conversation_loop import _hard_context_ceiling_result
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    agent, db = _make_agent(monkeypatch, tmp_path)
    agent._ensure_db_session()
    db.append_message(agent.session_id, "user", content="active ask")
    turn_identity = "hard-ceiling-e2e:repair-turn"
    messages = [
        {
            "role": "user",
            "content": "active ask",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
        }
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = turn_identity

    with patch.object(agent, "_persist_session", return_value=True):
        result = _hard_context_ceiling_result(
            agent,
            messages,
            [],
            0,
            estimated_tokens=2_000,
            ceiling_tokens=1_000,
            block_reason="compression_stalled",
        )

    assert messages[0].get("_db_persisted") is not True
    assert result["continuity_preserved"] is False
    assert result["compression_exhausted"] is False
    assert result["rollover_safe"] is False
    assert result["agent_persisted"] is False


def _durability_receipt(agent, row_id, content, turn_identity):
    from agent.turn_context import (
        CurrentTurnDurabilityReceipt,
        stable_message_content_digest,
    )

    return CurrentTurnDurabilityReceipt(
        session_id=agent.session_id,
        row_id=row_id,
        role="user",
        content_digest=stable_message_content_digest(content),
        turn_identity=turn_identity,
    )


def test_exact_current_row_receipt_repairs_lost_marker(monkeypatch, tmp_path):
    from agent.conversation_loop import _hard_context_ceiling_result
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    agent, db = _make_agent(monkeypatch, tmp_path)
    agent._ensure_db_session()
    db.append_message(agent.session_id, "user", content="active ask")
    current_row_id = db.append_message(
        agent.session_id, "user", content="active ask"
    )
    turn_identity = "hard-ceiling-e2e:exact-repair-turn"
    messages = [
        {
            "role": "user",
            "content": "active ask",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
        }
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = turn_identity
    agent._current_turn_durability_receipt = _durability_receipt(
        agent, current_row_id, "active ask", turn_identity
    )

    with patch.object(agent, "_persist_session", return_value=True):
        result = _hard_context_ceiling_result(
            agent,
            messages,
            [],
            0,
            estimated_tokens=2_000,
            ceiling_tokens=1_000,
            block_reason="compression_stalled",
        )

    assert messages[0]["_db_persisted"] is True
    assert messages[0]["_row_id"] == current_row_id
    assert result["continuity_preserved"] is True
    assert result["compression_exhausted"] is True
    assert result["rollover_safe"] is True
    assert result["agent_persisted"] is True


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("session_id", "different-session"),
        ("row_id", -1),
        ("role", "assistant"),
        ("content_digest", "0" * 64),
        ("turn_identity", "different-turn"),
    ],
)
def test_inconsistent_current_turn_receipt_fails_closed(
    monkeypatch, tmp_path, field, bad_value
):
    from agent.conversation_loop import _hard_context_ceiling_result
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    agent, db = _make_agent(monkeypatch, tmp_path)
    agent._ensure_db_session()
    row_id = db.append_message(agent.session_id, "user", content="active ask")
    turn_identity = "hard-ceiling-e2e:receipt-validation"
    messages = [
        {
            "role": "user",
            "content": "active ask",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
        }
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = turn_identity
    receipt = _durability_receipt(agent, row_id, "active ask", turn_identity)
    agent._current_turn_durability_receipt = replace(
        receipt, **{field: bad_value}
    )

    with patch.object(agent, "_persist_session", return_value=True):
        result = _hard_context_ceiling_result(
            agent,
            messages,
            [],
            0,
            estimated_tokens=2_000,
            ceiling_tokens=1_000,
            block_reason="compression_stalled",
        )

    assert result["continuity_preserved"] is False
    assert result["agent_persisted"] is False


def test_batch_persist_captures_exact_current_turn_receipt(monkeypatch, tmp_path):
    from agent.turn_context import (
        CURRENT_TURN_IDENTITY_KEY,
        stable_message_content_digest,
    )

    agent, db = _make_agent(monkeypatch, tmp_path)
    agent._ensure_db_session()
    turn_identity = "hard-ceiling-e2e:captured-receipt"
    messages = [
        {
            "role": "user",
            "content": "persist me",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
        }
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = turn_identity
    agent._current_turn_durability_receipt = None

    assert agent._persist_session(messages, []) is True

    receipt = agent._current_turn_durability_receipt
    row = db.get_messages(agent.session_id)[-1]
    assert asdict(receipt) == {
        "session_id": agent.session_id,
        "row_id": row["id"],
        "role": "user",
        "content_digest": stable_message_content_digest("persist me"),
        "turn_identity": turn_identity,
    }
    assert messages[0]["_row_id"] == row["id"]


def test_lost_marker_without_db_row_stays_fail_closed(monkeypatch, tmp_path):
    from agent.conversation_loop import _hard_context_ceiling_result
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    agent, _db = _make_agent(monkeypatch, tmp_path)
    agent._ensure_db_session()
    turn_identity = "hard-ceiling-e2e:no-db-row"
    messages = [
        {
            "role": "user",
            "content": "never reached storage",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
        }
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = turn_identity

    with patch.object(agent, "_persist_session", return_value=True):
        result = _hard_context_ceiling_result(
            agent,
            messages,
            [],
            0,
            estimated_tokens=2_000,
            ceiling_tokens=1_000,
            block_reason="compression_stalled",
        )

    assert messages[0].get("_db_persisted") is not True
    assert result["continuity_preserved"] is False
    assert result["compression_exhausted"] is False
    assert result["rollover_safe"] is False


def test_later_real_user_row_is_never_repaired(monkeypatch, tmp_path):
    """A genuine later user row means unprocessed input — the DB re-proof must
    not run for that predicate and rollover stays refused."""
    from agent.conversation_loop import _hard_context_ceiling_result
    from agent.turn_context import CURRENT_TURN_IDENTITY_KEY

    agent, db = _make_agent(monkeypatch, tmp_path)
    agent._ensure_db_session()
    db.append_message(agent.session_id, "user", content="active ask")
    db.append_message(agent.session_id, "user", content="a second real ask")
    turn_identity = "hard-ceiling-e2e:later-row"
    messages = [
        {
            "role": "user",
            "content": "active ask",
            CURRENT_TURN_IDENTITY_KEY: turn_identity,
            "_db_persisted": True,
        },
        {"role": "user", "content": "a second real ask"},
    ]
    agent._persist_user_message_idx = 0
    agent._persist_user_turn_identity = turn_identity

    with patch.object(agent, "_persist_session", return_value=True):
        result = _hard_context_ceiling_result(
            agent,
            messages,
            [],
            0,
            estimated_tokens=2_000,
            ceiling_tokens=1_000,
            block_reason="compression_stalled",
        )

    assert result["continuity_preserved"] is False
    assert result["compression_exhausted"] is False
    assert result["rollover_safe"] is False


def _tool_loop_history(tool_chars: int) -> list[dict]:
    return [
        {"role": "user", "content": "please run the tool"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "web_extract", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "x" * tool_chars},
        {"role": "assistant", "content": "tool digested"},
    ]


def _kwargs_content_estimate(request):
    msgs = request.get("messages", []) if isinstance(request, dict) else []
    return sum(len(str(m.get("content") or "")) for m in msgs) // 4


def test_last_mile_trim_converges_small_overshoot(monkeypatch, tmp_path):
    """A block within the deficit window gets one free truncation pass and the
    provider send then succeeds — no model-backed compression spent on it."""
    from agent.context_compressor import LAST_MILE_TRIM_MARKER

    agent, _db = _make_agent(monkeypatch, tmp_path)
    agent.context_compressor.threshold_tokens = 1_000
    history = _tool_loop_history(6_000)

    def _no_progress(messages, system_message, **_kwargs):
        return messages, agent._cached_system_prompt

    with (
        patch(
            "agent.turn_context.estimate_request_tokens_rough",
            return_value=1_500,
        ),
        patch(
            "agent.conversation_loop.estimate_messages_tokens_rough",
            return_value=1_500,
        ),
        patch(
            "agent.conversation_loop._provider_request_tokens_rough",
            side_effect=_kwargs_content_estimate,
        ),
        patch.object(agent, "_compress_context", side_effect=_no_progress),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("continue", conversation_history=history)

    agent.client.chat.completions.create.assert_called_once()
    assert result.get("hard_context_ceiling_blocked") is not True
    assert result["completed"] is True
    assert any(
        m.get("role") == "tool" and LAST_MILE_TRIM_MARKER in str(m.get("content"))
        for m in result["messages"]
    )


def test_last_mile_trim_skips_large_deficit(monkeypatch, tmp_path):
    """Overshoots beyond the deficit window still fail closed untouched."""
    from agent.context_compressor import LAST_MILE_TRIM_MARKER

    agent, _db = _make_agent(monkeypatch, tmp_path)
    agent.context_compressor.threshold_tokens = 1_000
    history = _tool_loop_history(40_000)

    def _no_progress(messages, system_message, **_kwargs):
        return messages, agent._cached_system_prompt

    with (
        patch(
            "agent.turn_context.estimate_request_tokens_rough",
            return_value=10_000,
        ),
        patch(
            "agent.conversation_loop.estimate_messages_tokens_rough",
            return_value=10_000,
        ),
        patch(
            "agent.conversation_loop._provider_request_tokens_rough",
            side_effect=_kwargs_content_estimate,
        ),
        patch.object(agent, "_compress_context", side_effect=_no_progress),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("continue", conversation_history=history)

    agent.client.chat.completions.create.assert_not_called()
    assert result["hard_context_ceiling_blocked"] is True
    assert not any(
        LAST_MILE_TRIM_MARKER in str(m.get("content"))
        for m in result["messages"]
        if m.get("role") == "tool"
    )


def test_truncate_helper_skips_summaries_and_mirrors_twins():
    from agent.context_compressor import (
        COMPRESSED_SUMMARY_METADATA_KEY,
        LAST_MILE_TRIM_MARKER,
        truncate_oversized_tool_results,
    )

    big = "y" * 5_000
    api_row = {"role": "tool", "tool_call_id": "t", "content": big}
    summary = {
        "role": "tool",
        "content": "z" * 5_000,
        COMPRESSED_SUMMARY_METADATA_KEY: True,
    }
    small = {"role": "tool", "content": "tiny"}
    mirror_twin = {"role": "tool", "tool_call_id": "t", "content": big}

    reclaimed = truncate_oversized_tool_results(
        [api_row, summary, small],
        reclaim_chars=1_000,
        mirror=[mirror_twin],
        keep_head_chars=100,
    )

    assert reclaimed == 1_000
    assert LAST_MILE_TRIM_MARKER in api_row["content"]
    assert api_row["content"] == mirror_twin["content"]
    assert summary["content"] == "z" * 5_000
    assert small["content"] == "tiny"
    assert (
        truncate_oversized_tool_results(
            [api_row], reclaim_chars=1_000, keep_head_chars=100
        )
        == 0
    )


def test_one_token_trim_deficit_is_proportional():
    from agent.context_compressor import (
        LAST_MILE_TRIM_MARKER,
        truncate_oversized_tool_results,
    )

    original = "x" * 8_000
    row = {"role": "tool", "content": original}
    reclaimed = truncate_oversized_tool_results([row], reclaim_chars=132)

    assert reclaimed == 132
    assert len(original) - len(row["content"]) == 132
    assert row["content"].endswith(LAST_MILE_TRIM_MARKER)
    assert len(row["content"].split("\n", 1)[0]) >= 1_500


def test_multiple_trim_candidates_stop_at_cumulative_target():
    from agent.context_compressor import truncate_oversized_tool_results

    rows = [
        {"role": "tool", "content": "a" * 2_000},
        {"role": "tool", "content": "b" * 2_000},
    ]

    reclaimed = truncate_oversized_tool_results(
        rows, reclaim_chars=600, keep_head_chars=1_500
    )

    assert reclaimed == 600
    assert sum(2_000 - len(row["content"]) for row in rows) == 600


def test_trim_mirror_uses_tool_identity_not_shared_content():
    from agent.context_compressor import truncate_oversized_tool_results

    shared = "same-result" * 500
    api_rows = [
        {"role": "tool", "tool_call_id": "first", "content": shared},
        {"role": "tool", "tool_call_id": "second", "content": shared},
    ]
    live_rows = [
        {"role": "tool", "tool_call_id": "first", "content": shared},
        {"role": "tool", "tool_call_id": "second", "content": shared},
    ]

    reclaimed = truncate_oversized_tool_results(
        api_rows,
        reclaim_chars=200,
        mirror=live_rows,
        keep_head_chars=100,
    )

    assert reclaimed == 200
    assert api_rows[0]["content"] == live_rows[0]["content"]
    assert api_rows[1]["content"] == shared
    assert live_rows[1]["content"] == shared
