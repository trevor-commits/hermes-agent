"""Unit coverage for the background-review aux-model selector + routed digest.

Covers the two behaviors this change adds:
  • _resolve_review_runtime — auto/same-model → not routed (main model, warm
    cache); a configured different model → routed with resolved credentials.
  • _digest_history — compact replay used ONLY on the routed path (recent tail
    verbatim + a digest of older turns), preserving role alternation.

Pure-function / config-driven; no live model calls.
"""
from typing import Any
from unittest.mock import patch
from pathlib import Path
import json

from agent import background_review as br


def _msg(role, content, tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


# ---------------------------------------------------------------------------
# _resolve_review_runtime — the aux-model selector
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, provider="openai-codex", model="gpt-5.5"):
        self.provider = provider
        self.model = model
        self._credential_pool: Any = None
        self.request_overrides = {}
        self.max_tokens: int | None = None

    def _current_main_runtime(self):
        return {
            "api_key": "parent-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_app_server",
        }


def test_routing_auto_inherits_parent_and_downgrades_codex_app_server():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {"provider": "auto", "model": ""}}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"
    assert rt["model"] == "gpt-5.5"
    assert rt["api_mode"] == "codex_responses"  # downgraded so agent-loop tools dispatch


def test_routing_to_different_model_marks_routed_and_resolves_credentials():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    fake_rp = {
        "provider": "openrouter", "api_key": "or-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
        "credential_pool": "routed-pool",
        "request_overrides": {"extra_body": {"store": False}},
        "max_output_tokens": 2048,
    }
    with patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_rp):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is True
    assert rt["provider"] == "openrouter"
    assert rt["model"] == "google/gemini-3-flash-preview"
    assert rt["api_key"] == "or-key"
    assert rt["credential_pool"] == "routed-pool"
    assert rt["request_overrides"] == {"extra_body": {"store": False}}
    assert rt["max_tokens"] == 2048


def test_unrouted_runtime_keeps_parent_pool_and_overrides():
    agent = _FakeAgent()
    agent._credential_pool = "parent-pool"
    agent.request_overrides = {"service_tier": "priority"}
    agent.max_tokens = 4096
    with patch("hermes_cli.config.load_config", return_value={}):
        rt = br._resolve_review_runtime(agent)
    assert rt["credential_pool"] == "parent-pool"
    assert rt["request_overrides"] == {"service_tier": "priority"}
    assert rt["max_tokens"] == 4096


def test_routing_same_model_as_parent_is_not_routed():
    agent = _FakeAgent(provider="openrouter", model="anthropic/claude-opus-4.8")
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "anthropic/claude-opus-4.8",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False  # same model/provider → keep full-replay path


def test_routing_resolution_failure_is_degraded_without_parent_fallback():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=RuntimeError("boom")):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is True
    assert rt["available"] is False
    assert rt["requested_provider"] == "openrouter"
    assert rt["requested_model"] == "google/gemini-3-flash-preview"
    assert "boom" in rt["degraded_reason"]


def test_completed_turn_without_learning_signal_does_not_qualify():
    result = br.qualify_background_review_turn(
        original_user_message="What time is the meeting?",
        completed=True,
        memory_due=True,
        skills_due=False,
        memory_available=True,
        skills_available=True,
    )
    assert result == {
        "review_memory": False,
        "review_skills": False,
        "reasons": [],
    }


def test_explicit_preference_qualifies_memory_immediately():
    result = br.qualify_background_review_turn(
        original_user_message="I prefer concise status updates; remember that.",
        completed=True,
        memory_due=False,
        skills_due=False,
        memory_available=True,
        skills_available=True,
    )
    assert result["review_memory"] is True
    assert "memory_signal" in result["reasons"]


def test_user_correction_qualifies_skill_review_immediately():
    result = br.qualify_background_review_turn(
        original_user_message="Stop using that format; next time use a table instead.",
        completed=True,
        memory_due=False,
        skills_due=False,
        memory_available=True,
        skills_available=True,
    )
    assert result["review_skills"] is True
    assert "workflow_correction" in result["reasons"]


def test_failed_or_interrupted_turn_never_qualifies():
    result = br.qualify_background_review_turn(
        original_user_message="Remember that I prefer tables.",
        completed=False,
        memory_due=True,
        skills_due=True,
        memory_available=True,
        skills_available=True,
    )
    assert result["review_memory"] is False
    assert result["review_skills"] is False


def test_unavailable_optional_route_does_not_construct_primary_review_agent():
    agent = _FakeAgent()
    agent.session_id = "session-route-degraded"
    agent.root_task_budget = None
    captured = []
    unavailable = {
        "available": False,
        "routed": True,
        "requested_provider": "openrouter",
        "requested_model": "cheap-review-model",
        "degraded_reason": "quota exhausted",
    }

    with patch.object(br, "_resolve_review_runtime", return_value=unavailable), \
         patch("run_agent.AIAgent") as review_agent_cls, \
         patch.object(
             br,
             "_write_background_review_receipt",
             side_effect=lambda _agent, receipt: captured.append(dict(receipt)),
         ):
        br._run_review_in_thread(
            agent,
            [{"role": "user", "content": "Remember my preference."}],
            "review memory",
            review_memory=True,
        )

    review_agent_cls.assert_not_called()
    assert captured[-1]["status"] == "degraded"
    assert captured[-1]["reason"] == "optional_route_unavailable"
    assert captured[-1]["route"]["requested_provider"] == "openrouter"


def test_review_receipt_writes_archive_and_atomic_last_pointer(tmp_path):
    payload = {
        "schema_version": 1,
        "receipt_id": "receipt-test",
        "session_id": "session-test",
        "status": "no_action",
    }
    agent = _FakeAgent()

    with patch("hermes_constants.get_hermes_home", return_value=Path(tmp_path)):
        archive = br._write_background_review_receipt(agent, payload)

    assert archive is not None
    archive_path = Path(archive)
    last_path = Path(tmp_path) / "audits" / "background-review-last.json"
    assert json.loads(archive_path.read_text()) == payload
    assert json.loads(last_path.read_text()) == payload
    assert archive_path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# _digest_history — routed-path compact replay
# ---------------------------------------------------------------------------

def test_digest_under_tail_returns_full():
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    assert br._digest_history(msgs, tail=24) == msgs


def test_digest_collapses_old_keeps_tail_verbatim():
    msgs = []
    for i in range(60):
        msgs.append(_msg("user", f"u{i} " + "x" * 50))
        msgs.append(_msg("assistant", f"a{i} " + "y" * 50))
    out = br._digest_history(msgs, tail=10)
    # First message is the synthetic digest (user role → alternation preserved).
    assert out[0]["role"] == "user"
    assert out[0]["content"].startswith("[Earlier conversation digest")
    # Recent tail preserved verbatim.
    assert out[-1] == msgs[-1]
    assert len(out) == 11  # 1 digest + 10 tail


def test_digest_does_not_open_tail_on_a_tool_message():
    msgs = []
    for i in range(40):
        msgs.append(_msg("user", "u" + "x" * 50))
        msgs.append(_msg("assistant", "", tool_calls=[
            {"function": {"name": "terminal", "arguments": "{}"}}]))
        msgs.append({"role": "tool", "content": "result " + "w" * 50})
    out = br._digest_history(msgs, tail=2)
    # The verbatim tail (after the digest) must not begin on a bare tool message.
    assert out[1]["role"] != "tool"


def test_digest_records_tool_names_in_arc():
    old = [
        _msg("user", "do the thing"),
        _msg("assistant", "", tool_calls=[
            {"function": {"name": "skill_view", "arguments": "{}"}},
            {"function": {"name": "patch", "arguments": "{}"}}]),
    ]
    msgs = old + [_msg("user", f"tail{i}") for i in range(30)]
    out = br._digest_history(msgs, tail=10)
    digest = out[0]["content"]
    assert "USER: do the thing" in digest
    assert "tools: skill_view, patch" in digest
