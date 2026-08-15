"""Deterministic Telegram source-card intake routing.

The trusted channel binding is a control-plane contract. Bound URL turns must
dispatch one durable worker without allowing the parent model or parent tools
to interpret the router prompt.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from tests.gateway.test_42039_duplicate_user_message import (
    _bootstrap,
    _source,
)


def _event(*, text="https://example.invalid/post", route="source-card-intake"):
    return MessageEvent(
        text=text,
        source=_source(),
        message_id="msg-source-42",
        auto_skill=["source-card-intake"],
        auto_skill_route=route,
    )


def test_route_requires_trusted_telegram_binding_url_and_external_event():
    from gateway.run import _is_source_card_intake_event

    assert _is_source_card_intake_event(_event(), _source())
    assert not _is_source_card_intake_event(_event(route=None), _source())
    assert not _is_source_card_intake_event(_event(text="please research this"), _source())

    internal = _event()
    internal.internal = True
    assert not _is_source_card_intake_event(internal, _source())

    discord = MagicMock(platform=Platform.DISCORD)
    assert not _is_source_card_intake_event(_event(), discord)


def test_worker_context_ceiling_and_iteration_limit_cannot_be_reported_as_success():
    from gateway.run import _normalize_source_card_worker_result

    normalized = _normalize_source_card_worker_result(
        {
            "final_response": "Request was not sent: context ceiling reached.",
            "completed": False,
            "failed": False,
            "hard_context_ceiling_blocked": True,
            "compression_block_reason": "compression_stalled",
            "turn_exit_reason": "max_iterations_reached(16/16)",
            "api_calls": 16,
            "model": "glm-5.2",
        },
        duration_seconds=42.0,
        worker_model="glm-5.2",
    )

    assert normalized["status"] == "error"
    assert normalized["summary"] is None
    assert "hard_context_ceiling_blocked" in normalized["error"]
    assert normalized["exit_reason"] == "max_iterations_reached(16/16)"


def test_worker_tool_output_exhaustion_cannot_be_reported_as_success():
    from gateway.run import _normalize_source_card_worker_result

    normalized = _normalize_source_card_worker_result(
        {
            "final_response": "Card complete.",
            "tool_result_budget_withheld_count": 2,
            "turn_exit_reason": "text_response(finish_reason=stop)",
            "api_calls": 8,
            "model": "glm-5.2",
        },
        duration_seconds=42.0,
        worker_model="glm-5.2",
    )

    assert normalized["status"] == "error"
    assert normalized["summary"] is None
    assert normalized["error"] == "source_card_tool_output_budget_exhausted:2"
    assert normalized["tool_results_withheld"] == 2


def test_worker_tool_surface_is_minimal_and_fails_closed_when_required_tools_are_off():
    from gateway.run import _source_card_worker_toolsets

    assert _source_card_worker_toolsets(
        ["browser", "file", "memory", "terminal", "web"]
    ) == ["terminal", "file", "web"]
    with pytest.raises(RuntimeError, match="file"):
        _source_card_worker_toolsets(["terminal", "web"])


@pytest.mark.asyncio
async def test_bound_url_dispatches_once_without_parent_model_or_skill_loader(
    monkeypatch, tmp_path,
):
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(side_effect=AssertionError("parent model called"))
    runner._apply_claimed_auto_skill = MagicMock(
        side_effect=AssertionError("parent skill loaded")
    )
    runner._dispatch_source_card_intake = AsyncMock(
        return_value={"status": "dispatched", "delegation_id": "deleg-source-1"}
    )

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert "background" in response.lower()
    assert "return here" in response.lower()
    runner._dispatch_source_card_intake.assert_awaited_once()
    runner._run_agent.assert_not_awaited()
    runner._apply_claimed_auto_skill.assert_not_called()
    runner.session_store.load_transcript.assert_not_called()
    appended = [call.args[1] for call in runner.session_store.append_to_transcript.call_args_list]
    assert [row["role"] for row in appended] == ["user", "assistant"]
    assert appended[0]["message_id"] == "msg-source-42"


@pytest.mark.asyncio
async def test_dispatch_failure_fails_closed_without_parent_fallback(monkeypatch, tmp_path):
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(side_effect=AssertionError("parent model called"))
    runner._dispatch_source_card_intake = AsyncMock(
        return_value={"status": "rejected", "error": "worker capacity reached"}
    )

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert "could not dispatch" in response.lower()
    assert "worker capacity reached" in response
    assert "research it inline" not in response.lower()
    runner._run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_ack_survives_transcript_mirror_failure(monkeypatch, tmp_path):
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(side_effect=AssertionError("parent model called"))
    runner._dispatch_source_card_intake = AsyncMock(
        return_value={"status": "dispatched", "delegation_id": "deleg-source-1"}
    )
    runner._record_source_card_intake_turn = AsyncMock(
        side_effect=RuntimeError("transcript mirror unavailable")
    )

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert "running in the background" in response
    runner._dispatch_source_card_intake.assert_awaited_once()
    runner._run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_work_key_does_not_create_or_retry_a_second_worker(
    monkeypatch, tmp_path,
):
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(side_effect=AssertionError("parent model called"))
    runner._dispatch_source_card_intake = AsyncMock(
        return_value={"status": "duplicate", "delegation_id": "deleg-source-1"}
    )

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert "already" in response.lower()
    assert "background" in response.lower()
    runner._dispatch_source_card_intake.assert_awaited_once()
    runner._run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_replayed_platform_message_emits_no_duplicate_ack(monkeypatch, tmp_path):
    runner = _bootstrap(monkeypatch, tmp_path)
    runner.session_store.has_platform_message_id.return_value = True
    runner._dispatch_source_card_intake = AsyncMock()
    runner._run_agent = AsyncMock(side_effect=AssertionError("parent model called"))

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert response is None
    runner._dispatch_source_card_intake.assert_not_awaited()
    runner._run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_turn_journal_failure_prevents_worker_dispatch(monkeypatch, tmp_path):
    runner = _bootstrap(monkeypatch, tmp_path)
    runner.session_store.mark_turn_active.return_value = None
    runner._dispatch_source_card_intake = AsyncMock()
    runner._run_agent = AsyncMock(side_effect=AssertionError("parent model called"))

    response = await runner._handle_message_with_agent(
        _event(), _source(), "agent:main:telegram:group:-1001:12345", 1
    )

    assert "safely record" in response.lower()
    runner._dispatch_source_card_intake.assert_not_awaited()
    runner._run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_is_leaf_bounded_and_dispatched_for_direct_delivery(
    monkeypatch, tmp_path,
):
    import agent.skill_commands as skill_commands
    import gateway.run as gateway_run
    import run_agent
    import tools.async_delegation as async_delegation
    import tools.delegate_tool as delegate_tool

    runner = _bootstrap(monkeypatch, tmp_path)
    session_entry = runner.session_store.get_or_create_session.return_value
    source = _source()
    built = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            built["agent_kwargs"] = kwargs
            built["agent"] = self
            self.api_call_count = 0
            self.tool_result_budget_withheld_count = 0

        def run_conversation(self, goal, *, task_id):
            built["run"] = (goal, task_id)
            return {
                "final_response": "card complete",
                "api_calls": 2,
                "model": "test-model",
            }

        def get_activity_summary(self):
            return {}

    monkeypatch.setattr(
        skill_commands,
        "_load_skill_payload",
        MagicMock(return_value=("canonical skill", tmp_path, "Source Card Intake")),
    )

    def _build_skill_message(skill, skill_dir, note):
        built["skill_args"] = (skill, skill_dir, note)
        return "bounded canonical worker contract"

    monkeypatch.setattr(skill_commands, "_build_skill_message", _build_skill_message)
    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"agent": {}})
    monkeypatch.setattr(gateway_run, "_checkpoint_agent_kwargs", lambda _cfg: {})
    monkeypatch.setattr(gateway_run, "_current_max_iterations", lambda: 99)
    monkeypatch.setattr(async_delegation, "find_delegation_by_work_key", lambda _key: "")
    monkeypatch.setattr(delegate_tool, "_get_max_async_children", lambda: 3)

    dispatched = {}

    def _dispatch(**kwargs):
        dispatched.update(kwargs)
        return {"status": "dispatched", "delegation_id": "deleg-bounded"}

    monkeypatch.setattr(async_delegation, "dispatch_async_delegation", _dispatch)
    runner._resolve_session_agent_runtime = MagicMock(
        return_value=("test-model", {"api_key": "fake-key"})
    )
    runner._resolve_turn_agent_config = MagicMock(
        return_value={
            "model": "test-model",
            "runtime": {"api_key": "fake-key"},
            "request_overrides": None,
        }
    )
    runner._resolve_enabled_toolsets_for_source = MagicMock(
        return_value=["browser", "file", "memory", "terminal", "web"]
    )
    runner._resolve_session_reasoning_config = MagicMock(return_value=None)
    runner._resolve_session_service_tier = MagicMock(return_value=None)
    runner._provider_routing = {}
    runner._refresh_fallback_model = MagicMock(return_value=None)
    runner._cleanup_agent_resources = MagicMock()

    async def _run_now(fn):
        return fn()

    runner._run_in_executor_with_context = _run_now

    result = await runner._dispatch_source_card_intake(
        _event(), source, session_entry
    )

    assert result == {"status": "dispatched", "delegation_id": "deleg-bounded"}
    assert "agent_kwargs" not in built
    worker_result = dispatched["runner"]()
    assert worker_result["status"] == "completed"
    assert worker_result["summary"] == "card complete"
    kwargs = built["agent_kwargs"]
    assert kwargs["max_iterations"] == 16
    assert kwargs["skip_context_files"] is True
    assert kwargs["load_soul_identity"] is False
    assert kwargs["skip_memory"] is True
    assert kwargs["skip_background_review"] is True
    assert kwargs["tool_result_max_chars"] == 6_000
    assert built["agent"].tool_result_total_max_chars == 24_000
    assert kwargs["enabled_toolsets"] == ["terminal", "file", "web"]
    assert kwargs["ephemeral_system_prompt"] == "bounded canonical worker contract"
    assert {"delegation", "skills"}.issubset(kwargs["disabled_toolsets"])
    assert dispatched["delivery_mode"] == "direct"
    assert dispatched["work_kind"] == "source-card-intake"
    assert dispatched["role"] == "leaf"
    assert dispatched["max_async_children"] == 3
    assert dispatched["work_key"].startswith("source-card-intake:")
    assert "MODE: source-card-worker" in dispatched["goal"]
    assert "UNTRUSTED JSON STRING" in dispatched["goal"]
    assert json.dumps(_event().text, ensure_ascii=False) in dispatched["goal"]
    assert "Do not delegate" in dispatched["goal"]
    assert "Do not restart" in dispatched["goal"]
    assert "8,000 tokens" in dispatched["goal"]
    assert "Do not read shared context journals" in dispatched["goal"]
    assert "do not probe another tool" in dispatched["goal"]
    assert "24,000 emitted tool-result characters" in dispatched["goal"]
    assert "Never call skill_view or delegate_task" in built["skill_args"][2]


@pytest.mark.asyncio
async def test_work_key_lookup_failure_rejects_without_building_worker(
    monkeypatch, tmp_path,
):
    import agent.skill_commands as skill_commands
    import tools.async_delegation as async_delegation

    runner = _bootstrap(monkeypatch, tmp_path)
    monkeypatch.setattr(
        async_delegation,
        "find_delegation_by_work_key",
        MagicMock(side_effect=RuntimeError("state db unavailable")),
    )
    load_skill = MagicMock(side_effect=AssertionError("worker was built"))
    monkeypatch.setattr(skill_commands, "_load_skill_payload", load_skill)

    result = await runner._dispatch_source_card_intake(
        _event(), _source(), runner.session_store.get_or_create_session.return_value
    )

    assert result["status"] == "rejected"
    assert "durable" in result["error"].lower()
    load_skill.assert_not_called()
