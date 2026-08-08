"""Provider sends fail closed when context cannot be compacted below its ceiling."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def test_execution_middleware_cannot_expand_stalled_turn_over_ceiling(
    monkeypatch, tmp_path
):
    agent, _db = _make_agent(monkeypatch, tmp_path)
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
    agent, _db = _make_agent(monkeypatch, tmp_path)
    messages = [
        {"role": "user", "content": "durable user", "_db_persisted": True}
    ]
    agent._persist_user_message_idx = 0

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
