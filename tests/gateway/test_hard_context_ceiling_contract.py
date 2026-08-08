"""Gateway contract for fail-closed context-ceiling results."""

from __future__ import annotations

import ast
import inspect
import textwrap

from gateway import run as gateway_run


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


def test_both_agent_result_return_branches_use_the_shared_projection():
    tree = ast.parse(textwrap.dedent(inspect.getsource(gateway_run.TurnRunner.run_sync)))
    projected_expansions = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if key is not None:
                continue
            if isinstance(value, ast.Name) and value.id == "_context_ceiling_fields":
                projected_expansions += 1
    assert projected_expansions == 2, (
        "the empty- and non-empty-response gateway returns must expand the "
        "same structured hard-ceiling result projection"
    )


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


def test_reset_branch_uses_the_authoritative_rollover_predicate():
    tree = ast.parse(
        textwrap.dedent(
            inspect.getsource(gateway_run.GatewayRunner._handle_message_with_agent)
        )
    )
    authorized_reset_blocks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        calls = [
            sub
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "reset_session"
        ]
        if calls:
            guard_calls = [
                sub.func.id
                for sub in ast.walk(node.test)
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            ]
            if "_gateway_rollover_is_authorized" in guard_calls:
                authorized_reset_blocks.append(node)
    assert len(authorized_reset_blocks) == 1
