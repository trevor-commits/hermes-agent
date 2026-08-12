from types import SimpleNamespace

from agent.tool_executor import _budget_for_agent
from tools.budget_config import DEFAULT_BUDGET
from tools.tool_result_storage import maybe_persist_tool_result


def test_job_scoped_budget_caps_large_tool_results_and_each_turn():
    agent = SimpleNamespace(
        tool_result_max_chars=4_000,
        context_compressor=SimpleNamespace(context_length=200_000),
    )

    budget = _budget_for_agent(agent)

    assert budget.resolve_threshold("execute_code") == 4_000
    assert budget.resolve_threshold("terminal") == 4_000
    assert budget.turn_budget == 12_000
    assert budget.preview_size == 1_500

    bounded = maybe_persist_tool_result(
        content="x" * 50_872,
        tool_name="execute_code",
        tool_use_id="live-research-regression",
        env=None,
        config=budget,
    )
    assert len(bounded) < 2_000
    assert "tool response was 50,872 chars" in bounded


def test_missing_job_scoped_budget_preserves_default_large_model_budget():
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(context_length=200_000),
    )

    assert _budget_for_agent(agent) == DEFAULT_BUDGET
