"""Contracts for aggregate root-task model-call budgeting and receipts."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
from types import SimpleNamespace

import pytest


def _metadata(**overrides):
    data = {
        "session_id": "session-1",
        "task": "main",
        "role": "parent",
        "provider": "test-provider",
        "model": "test-model",
        "reasoning": {"effort": "high"},
        "fallback_path": "primary",
    }
    data.update(overrides)
    return data


def test_delegate_cannot_spend_parent_closure_reserve():
    from agent.root_task_budget import RootBudgetExhausted, RootTaskBudget

    budget = RootTaskBudget(
        root_turn_id="root-1", max_total=5, closure_reserve=2
    )

    for _ in range(3):
        budget.begin_call(scope="delegate", **_metadata(role="delegate"))
    with pytest.raises(RootBudgetExhausted):
        budget.begin_call(scope="delegate", **_metadata(role="delegate"))

    budget.begin_call(scope="parent", **_metadata())
    budget.begin_call(scope="parent", **_metadata())
    with pytest.raises(RootBudgetExhausted):
        budget.begin_call(scope="parent", **_metadata())

    assert budget.used == 5
    assert budget.remaining == 0


def test_root_budget_is_thread_safe_and_never_overspends():
    from agent.root_task_budget import RootBudgetExhausted, RootTaskBudget

    budget = RootTaskBudget(
        root_turn_id="root-race", max_total=40, closure_reserve=0
    )

    def consume_once(_):
        try:
            budget.begin_call(scope="parent", **_metadata())
            return True
        except RootBudgetExhausted:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(consume_once, range(200)))

    assert sum(outcomes) == 40
    assert budget.used == 40
    assert budget.remaining == 0


def test_success_receipt_has_one_stable_call_id_and_usage():
    from agent.root_task_budget import (
        RootCallContext,
        RootTaskBudget,
        execute_model_call,
    )

    events = []
    budget = RootTaskBudget(
        root_turn_id="root-receipt",
        max_total=3,
        closure_reserve=1,
        receipt_sink=events.append,
    )
    context = RootCallContext(budget=budget, session_id="session-1", role="parent")
    response = SimpleNamespace(
        model="actual-model",
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
        ),
    )

    assert execute_model_call(
        lambda: response,
        context=context,
        scope="parent",
        task="main",
        provider="test-provider",
        model="requested-model",
        reasoning={"effort": "high"},
        fallback_path="primary",
    ) is response

    assert [event["status"] for event in events] == ["started", "succeeded"]
    assert events[0]["call_id"] == events[1]["call_id"]
    assert events[1]["root_turn_id"] == "root-receipt"
    assert events[1]["session_id"] == "session-1"
    assert events[1]["role"] == "parent"
    assert events[1]["task"] == "main"
    assert events[1]["provider"] == "test-provider"
    assert events[1]["model"] == "actual-model"
    assert events[1]["reasoning"] == {"effort": "high"}
    assert events[1]["fallback_path"] == "primary"
    assert events[1]["input_tokens"] == 11
    assert events[1]["output_tokens"] == 7


def test_failed_physical_call_keeps_receipt_and_consumes_budget():
    from agent.root_task_budget import (
        RootCallContext,
        RootTaskBudget,
        execute_model_call,
    )

    events = []
    budget = RootTaskBudget(
        root_turn_id="root-fail",
        max_total=1,
        closure_reserve=0,
        receipt_sink=events.append,
    )
    context = RootCallContext(budget=budget, session_id="session-1", role="parent")

    def fail():
        raise TimeoutError("provider timed out")

    with pytest.raises(TimeoutError):
        execute_model_call(
            fail,
            context=context,
            scope="parent",
            task="main",
            provider="test-provider",
            model="test-model",
        )

    assert budget.used == 1
    assert [event["status"] for event in events] == ["started", "failed"]
    assert events[1]["error_type"] == "TimeoutError"
    assert "provider timed out" in events[1]["error_message"]


def test_async_physical_call_is_charged_once():
    from agent.root_task_budget import (
        RootCallContext,
        RootTaskBudget,
        execute_model_call,
    )

    events = []
    budget = RootTaskBudget(
        root_turn_id="root-async",
        max_total=2,
        closure_reserve=0,
        receipt_sink=events.append,
    )
    context = RootCallContext(budget=budget, session_id="session-1", role="parent")

    async def complete():
        return SimpleNamespace(model="async-model", usage=None)

    result = asyncio.run(
        execute_model_call(
            complete,
            context=context,
            scope="auxiliary",
            task="compression",
            provider="test-provider",
            model="async-model",
        )
    )

    assert result.model == "async-model"
    assert budget.used == 1
    assert [event["status"] for event in events] == ["started", "succeeded"]


def test_child_context_reuses_parent_budget_identity():
    from agent.root_task_budget import RootCallContext, RootTaskBudget

    budget = RootTaskBudget(
        root_turn_id="root-shared", max_total=10, closure_reserve=2
    )
    parent = RootCallContext(budget=budget, session_id="parent", role="parent")
    child = parent.for_child(session_id="child", role="delegate")

    assert child.budget is parent.budget
    assert child.budget.root_turn_id == "root-shared"
    assert child.session_id == "child"
    assert child.role == "delegate"


def test_auto_limits_allow_one_specialist_and_reserve_parent_closure():
    from agent.root_task_budget import resolve_root_budget_limits

    max_total, reserve = resolve_root_budget_limits(
        parent_max_iterations=90,
        config={
            "agent": {
                "root_max_iterations": "auto",
                "root_closure_reserve": "auto",
            },
            "delegation": {"max_iterations": 50},
        },
    )

    assert max_total == 140
    assert reserve == 8


def test_explicit_limits_are_clamped_to_a_usable_parent_budget():
    from agent.root_task_budget import resolve_root_budget_limits

    assert resolve_root_budget_limits(
        parent_max_iterations=10,
        config={
            "agent": {
                "root_max_iterations": 3,
                "root_closure_reserve": 99,
            }
        },
    ) == (3, 2)


def test_auxiliary_client_proxy_charges_each_physical_retry():
    from agent.auxiliary_client import _with_root_call_budget
    from agent.root_task_budget import (
        RootCallContext,
        RootTaskBudget,
        reset_root_call_context,
        set_root_call_context,
    )

    events = []
    budget = RootTaskBudget(
        root_turn_id="root-aux",
        max_total=4,
        closure_reserve=1,
        receipt_sink=events.append,
    )
    response = SimpleNamespace(model="aux-model", usage=None)

    class Completions:
        def create(self, **_kwargs):
            return response

    client = SimpleNamespace(
        base_url="https://example.invalid/v1",
        chat=SimpleNamespace(completions=Completions()),
    )
    wrapped = _with_root_call_budget(
        client,
        task="title_generation",
        provider="test-provider",
        model="aux-model",
        reasoning_config={"effort": "low"},
        fallback_path="primary",
    )
    token = set_root_call_context(
        RootCallContext(budget=budget, session_id="session-1", role="parent")
    )
    try:
        assert wrapped.chat.completions.create(model="aux-model") is response
        assert wrapped.chat.completions.create(model="aux-model") is response
    finally:
        reset_root_call_context(token)

    assert budget.used == 2
    starts = [event for event in events if event["status"] == "started"]
    assert len(starts) == 2
    assert {event["role"] for event in starts} == {"auxiliary:title_generation"}


def test_agent_wrapper_resets_parent_root_but_preserves_inherited_child(monkeypatch):
    import agent.conversation_loop as conversation_loop
    from agent.root_task_budget import (
        RootTaskBudget,
        ScopedRootTaskBudget,
        get_root_call_context,
    )
    from run_agent import AIAgent

    observed = []

    def fake_run(*_args, **_kwargs):
        context = get_root_call_context()
        observed.append((context.budget, context.role))
        return {"final_response": "ok", "messages": []}

    monkeypatch.setattr(conversation_loop, "run_conversation", fake_run)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "agent": {
                "root_max_iterations": "auto",
                "root_closure_reserve": "auto",
            },
            "delegation": {"max_iterations": 4},
        },
    )

    parent = object.__new__(AIAgent)
    parent.session_id = "parent-session"
    parent._parent_session_id = None
    parent._session_db = None
    parent.max_iterations = 6
    parent.root_task_budget = None
    parent._root_task_budget_inherited = False
    parent._root_task_role = "parent"

    AIAgent.run_conversation(parent, "first")
    AIAgent.run_conversation(parent, "second")

    inherited = RootTaskBudget(
        root_turn_id="inherited-root", max_total=10, closure_reserve=2
    )
    child = object.__new__(AIAgent)
    child.session_id = "child-session"
    child._parent_session_id = "parent-session"
    child._session_db = None
    child.max_iterations = 4
    child.root_task_budget = inherited
    child._root_task_budget_inherited = True
    child._root_task_role = "delegate"

    AIAgent.run_conversation(child, "delegated work")

    scoped = ScopedRootTaskBudget(
        inherited, scope_name="background_review", max_calls=4,
    )
    review = object.__new__(AIAgent)
    review.session_id = "review-session"
    review._parent_session_id = "parent-session"
    review._session_db = None
    review.max_iterations = 4
    review.root_task_budget = scoped
    review._root_task_budget_inherited = True
    review._root_task_role = "background_review"

    AIAgent.run_conversation(review, "review work")

    assert observed[0][0] is not observed[1][0]
    assert observed[0][1] == observed[1][1] == "parent"
    assert observed[2] == (inherited, "delegate")
    assert observed[3] == (scoped, "background_review")


def test_scoped_budget_caps_physical_attempts_and_keeps_origin_identity():
    """R1-F04/F05: a review keeps its origin root and four-call physical cap."""
    from agent.root_task_budget import (
        RootBudgetExhausted,
        RootTaskBudget,
        ScopedRootTaskBudget,
    )

    origin = RootTaskBudget(root_turn_id="origin-root", max_total=20, closure_reserve=2)
    scoped = ScopedRootTaskBudget(origin, scope_name="background_review", max_calls=4)

    for _ in range(4):
        scoped.begin_call(scope="background_review", **_metadata(role="background_review"))
    with pytest.raises(RootBudgetExhausted):
        scoped.begin_call(scope="background_review", **_metadata(role="background_review"))

    assert scoped.root_turn_id == "origin-root"
    assert scoped.used == 4
    assert origin.used == 4


def test_stream_receipt_finishes_only_after_stream_consumption():
    """R1-F07: a streaming MoA receipt cannot succeed at iterator creation."""
    from agent.root_task_budget import RootCallContext, RootTaskBudget, execute_model_call

    events = []
    budget = RootTaskBudget(
        root_turn_id="root-stream", max_total=2, closure_reserve=0,
        receipt_sink=events.append,
    )
    context = RootCallContext(budget=budget, session_id="s", role="moa_aggregator")

    def chunks():
        yield SimpleNamespace(model="actual-moa-model", usage=None, value="one")
        raise RuntimeError("stream broke")

    stream = execute_model_call(
        chunks,
        context=context,
        scope="parent",
        task="moa_aggregator",
        provider="auto",
        model="requested",
        defer_stream=True,
    )
    assert [event["status"] for event in events] == ["started"]
    assert next(stream).value == "one"
    with pytest.raises(RuntimeError, match="stream broke"):
        next(stream)
    assert [event["status"] for event in events] == ["started", "failed"]


def test_deferred_stream_preserves_completed_non_iterable_response():
    """R1-F07/F17: adapters may return a completed response on stream paths."""
    from agent.root_task_budget import RootCallContext, RootTaskBudget, execute_model_call

    events = []
    budget = RootTaskBudget(
        root_turn_id="root-completed", max_total=1, closure_reserve=0,
        receipt_sink=events.append,
    )
    context = RootCallContext(budget=budget, session_id="s", role="parent")
    response = SimpleNamespace(choices=[], model="completed-model", usage=None)

    observed = execute_model_call(
        lambda: response,
        context=context,
        scope="parent",
        task="main",
        provider="test",
        model="requested",
        defer_stream=True,
    )

    assert observed is response
    assert [event["status"] for event in events] == ["started", "succeeded"]


def test_terminal_receipt_records_actual_provider_model_and_reasoning():
    """R1-F06: terminal route metadata reflects the billed response."""
    from agent.root_task_budget import RootCallContext, RootTaskBudget, execute_model_call

    events = []
    budget = RootTaskBudget(
        root_turn_id="root-route", max_total=1, closure_reserve=0,
        receipt_sink=events.append,
    )
    context = RootCallContext(budget=budget, session_id="s", role="parent")
    response = SimpleNamespace(
        model="actual-model", provider="actual-provider",
        reasoning={"effort": "medium"}, usage=None,
    )
    execute_model_call(
        lambda: response,
        context=context,
        scope="parent",
        task="main",
        provider="auto",
        model="requested",
        reasoning={"effort": "high"},
    )

    assert events[-1]["provider"] == "actual-provider"
    assert events[-1]["model"] == "actual-model"
    assert events[-1]["reasoning"] == {"effort": "medium"}
