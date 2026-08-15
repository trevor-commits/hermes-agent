"""Deterministic Telegram source-card intake routing.

The trusted channel binding is a control-plane contract. Bound URL turns must
dispatch one durable worker without allowing the parent model or parent tools
to interpret the router prompt.
"""

import json
import os
import subprocess
import threading
from types import SimpleNamespace
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


def _write_worker_environment(home):
    cards_root = home / "cards"
    cards_root.mkdir()
    writer = home / "scripts" / "hermes-research-decisions"
    writer.parent.mkdir()
    writer.write_text("#!/bin/sh\n", encoding="utf-8")
    writer.chmod(0o755)
    x_lookup = home / "scripts" / "x-lookup"
    x_lookup.write_text("#!/bin/sh\n", encoding="utf-8")
    x_lookup.chmod(0o755)
    transcript_db = home / "state.db"
    transcript_db.write_bytes(b"sqlite fixture")
    config = home / "state" / "research-decision-config.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cards_root": str(cards_root),
                "transcript_db": str(transcript_db),
            }
        ),
        encoding="utf-8",
    )
    references = home / "references"
    references.mkdir()
    reference_bodies = {
        "research-method.md": "research method body",
        "card-schema.md": "card schema body",
        "receipts-and-ledger.md": "receipt contract body",
    }
    for name, body in reference_bodies.items():
        (references / name).write_text(body, encoding="utf-8")
    return {
        "cards_root": cards_root,
        "writer": writer,
        "x_lookup": x_lookup,
        "transcript_db": transcript_db,
        "reference_bodies": reference_bodies,
    }


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


def test_missing_first_tool_does_not_overwrite_an_existing_worker_failure():
    from gateway.run import _normalize_source_card_guarded_worker_result

    provider_failure = _normalize_source_card_guarded_worker_result(
        {
            "final_response": "",
            "error": "provider authentication failed",
            "failed": True,
            "api_calls": 1,
        },
        first_tool_validated=False,
        duration_seconds=1.0,
        worker_model="test-model",
    )
    false_success = _normalize_source_card_guarded_worker_result(
        {"final_response": "card complete", "api_calls": 1},
        first_tool_validated=False,
        duration_seconds=1.0,
        worker_model="test-model",
    )

    assert provider_failure["status"] == "error"
    assert provider_failure["error"] == "provider authentication failed"
    assert false_success["status"] == "error"
    assert "first_tool_contract_violated" in false_success["error"]


def test_worker_tool_surface_is_minimal_and_fails_closed_when_required_tools_are_off():
    from gateway.run import _source_card_worker_toolsets

    assert _source_card_worker_toolsets(
        ["browser", "file", "memory", "terminal", "web"]
    ) == ["terminal", "file", "web"]
    with pytest.raises(RuntimeError, match="file"):
        _source_card_worker_toolsets(["terminal", "web"])


def test_worker_environment_resolves_exact_paths_and_origin(tmp_path):
    from gateway.run import _resolve_source_card_worker_environment

    fixture = _write_worker_environment(tmp_path)
    source = _source()
    source.thread_id = "topic-77"
    session_entry = MagicMock(
        session_id="parent-session-9",
        session_key="agent:main:telegram:group:-1001:12345",
    )

    environment = _resolve_source_card_worker_environment(
        _event(), source, session_entry, hermes_home=tmp_path
    )

    assert environment == {
        "cards_root": str(fixture["cards_root"].resolve()),
        "decision_writer": str(fixture["writer"].resolve()),
        "x_lookup": str(fixture["x_lookup"].resolve()),
        "transcript_db": str(fixture["transcript_db"].resolve()),
        "source_chat_id": "-1001",
        "source_thread_id": "topic-77",
        "parent_session_id": "parent-session-9",
        "platform_message_id": "msg-source-42",
    }


def test_worker_environment_rejects_symlinked_managed_paths(tmp_path):
    from gateway.run import _resolve_source_card_worker_environment

    fixture = _write_worker_environment(tmp_path)
    outside = tmp_path / "outside-writer"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    fixture["writer"].unlink()
    os.symlink(outside, fixture["writer"])

    with pytest.raises(RuntimeError, match="decision writer"):
        _resolve_source_card_worker_environment(
            _event(),
            _source(),
            MagicMock(session_id="parent-session-9"),
            hermes_home=tmp_path,
        )


def test_x_prefetch_runs_each_status_once_and_normalizes_untrusted_json(
    monkeypatch, tmp_path,
):
    import gateway.run as gateway_run

    fixture = _write_worker_environment(tmp_path)
    calls = []

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        status_id = argv[-1].split("/status/")[-1]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "id": status_id,
                    "url": f"https://x.com/i/status/{status_id}",
                    "route": "cache",
                    "author": {
                        "handle": "writer",
                        "name": "Writer",
                        "followers": 10,
                        "ignored": "drop me",
                    },
                    "created_at": "2026-08-15T00:00:00Z",
                    "text": "post body",
                    "stats": {
                        "likes": 3,
                        "retweets": 2,
                        "replies": 1,
                        "views": 99,
                        "ignored": 500,
                    },
                    "links": ["https://example.com"],
                    "media": [],
                    "quote": None,
                    "ignored": "drop me",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(gateway_run.subprocess, "run", _run)
    text = (
        "https://x.com/i/web/status/2088573393121509655 and "
        "https://x.com/author/status/2088626767669981398 plus "
        "https://x.com/i/status/2088573393121509655"
    )

    posts = gateway_run._prefetch_source_card_x_posts(text, fixture["x_lookup"])

    assert [post["status_id"] for post in posts] == [
        "2088573393121509655",
        "2088626767669981398",
    ]
    assert all(set(post) == {
        "status_id", "canonical_url", "author", "text", "created_at",
        "stats", "links", "media", "quote",
    } for post in posts)
    assert all("ignored" not in post["author"] for post in posts)
    assert all("ignored" not in post["stats"] for post in posts)
    assert len(calls) == 2
    for argv, kwargs in calls:
        assert argv[:2] == [str(fixture["x_lookup"].resolve()), "--json"]
        assert "/i/web/status/" not in argv[-1]
        assert argv[-1].startswith("https://x.com/i/status/")
        assert kwargs["timeout"] == 10
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True


@pytest.mark.parametrize(
    ("field", "invalid_url", "expected_label"),
    [
        ("links", "file:///Users/example/.ssh/id_ed25519", "links"),
        ("media", "javascript:alert(1)", "media"),
        ("quote", "not a URL", "quote"),
    ],
)
def test_x_prefetch_rejects_non_http_untrusted_urls(
    monkeypatch, tmp_path, field, invalid_url, expected_label,
):
    import gateway.run as gateway_run

    fixture = _write_worker_environment(tmp_path)
    payload = {
        "id": "2088573393121509655",
        "author": {"handle": "writer", "name": "Writer", "followers": 10},
        "text": "post body",
        "created_at": "2026-08-15T00:00:00Z",
        "stats": {},
        "links": ["https://example.com/source"],
        "media": ["https://example.com/media.png"],
        "quote": {
            "author": "quoted",
            "text": "quote",
            "url": "https://example.com/quote",
        },
    }
    if field == "quote":
        payload["quote"]["url"] = invalid_url
    else:
        payload[field] = [invalid_url]
    monkeypatch.setattr(
        gateway_run.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(
        gateway_run._SourceCardPrefetchError,
        match=f"invalid {expected_label} URL",
    ):
        gateway_run._prefetch_source_card_x_posts(
            "https://x.com/i/status/2088573393121509655",
            fixture["x_lookup"],
        )


def test_x_prefetch_runs_distinct_statuses_concurrently(monkeypatch, tmp_path):
    import gateway.run as gateway_run

    fixture = _write_worker_environment(tmp_path)
    rendezvous = threading.Barrier(2, timeout=1.0)

    def _run(argv, **kwargs):
        rendezvous.wait()
        status_id = argv[-1].rsplit("/", 1)[-1]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "id": status_id,
                    "author": {},
                    "text": "post",
                    "stats": {},
                    "links": [],
                    "media": [],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(gateway_run.subprocess, "run", _run)
    posts = gateway_run._prefetch_source_card_x_posts(
        "https://x.com/i/status/2088573393121509655 "
        "https://x.com/i/status/2088626767669981398",
        fixture["x_lookup"],
    )

    assert [post["status_id"] for post in posts] == [
        "2088573393121509655",
        "2088626767669981398",
    ]


def test_x_prefetch_rejects_more_than_bounded_concurrent_limit(monkeypatch, tmp_path):
    import gateway.run as gateway_run

    fixture = _write_worker_environment(tmp_path)
    run = MagicMock(side_effect=AssertionError("lookup should not start"))
    monkeypatch.setattr(gateway_run.subprocess, "run", run)
    intake = " ".join(
        f"https://x.com/i/status/{2088573393121509655 + offset}"
        for offset in range(9)
    )

    with pytest.raises(gateway_run._SourceCardPrefetchError, match="maximum 8"):
        gateway_run._prefetch_source_card_x_posts(intake, fixture["x_lookup"])

    run.assert_not_called()


def test_mixed_intake_duplicate_identifiers_include_x_status_and_other_urls():
    from gateway.run import _source_card_duplicate_identifiers

    assert _source_card_duplicate_identifiers(
        "https://x.com/i/status/2088573393121509655 "
        "https://github.com/example/repo "
        "https://x.com/author/status/2088573393121509655"
    ) == ["2088573393121509655", "https://github.com/example/repo"]


def test_first_worker_tool_guard_blocks_full_tree_search_before_execution():
    from gateway.run import _install_source_card_first_tool_guard

    executed = []
    agent = SimpleNamespace(
        _execute_tool_calls=lambda *args, **kwargs: executed.append((args, kwargs))
    )
    expected = "rg -l -F -e 2088573393121509655 -- /tmp/cards"
    _install_source_card_first_tool_guard(agent, expected)
    broad_call = SimpleNamespace(
        function=SimpleNamespace(
            name="terminal",
            arguments=json.dumps({"command": "rg --files /tmp/cards"}),
        )
    )

    with pytest.raises(RuntimeError, match="first_tool_contract_violated"):
        agent._execute_tool_calls(
            SimpleNamespace(tool_calls=[broad_call]), [], "worker-session", 1
        )

    assert executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit-4", "timeout"])
async def test_real_x_prefetch_failure_starts_no_worker(
    monkeypatch, tmp_path, failure,
):
    import agent.skill_commands as skill_commands
    import gateway.run as gateway_run
    import tools.async_delegation as async_delegation

    runner = _bootstrap(monkeypatch, tmp_path)
    _write_worker_environment(tmp_path)
    monkeypatch.setattr(
        async_delegation, "find_delegation_by_work_key", lambda _key: ""
    )
    dispatch = MagicMock(side_effect=AssertionError("worker was dispatched"))
    monkeypatch.setattr(async_delegation, "dispatch_async_delegation", dispatch)
    if failure == "exit-4":
        outcome = subprocess.CompletedProcess(
            ["x-lookup"], 4, stdout="", stderr="all routes failed\n"
        )
        monkeypatch.setattr(gateway_run.subprocess, "run", lambda *args, **kwargs: outcome)
        expected_error = "exit 4: all routes failed"
    else:
        monkeypatch.setattr(
            gateway_run.subprocess,
            "run",
            MagicMock(side_effect=subprocess.TimeoutExpired(["x-lookup"], 10)),
        )
        expected_error = "timeout after 10 seconds"
    load_skill = MagicMock(side_effect=AssertionError("skill was loaded"))
    monkeypatch.setattr(skill_commands, "_load_skill_payload", load_skill)

    result = await runner._dispatch_source_card_intake(
        _event(text="https://x.com/i/status/2088573393121509655"),
        _source(),
        runner.session_store.get_or_create_session.return_value,
    )

    assert result == {"status": "prefetch_failed", "error": expected_error}
    dispatch.assert_not_called()
    load_skill.assert_not_called()


@pytest.mark.asyncio
async def test_non_x_intake_dispatches_without_x_lookup(monkeypatch, tmp_path):
    import agent.skill_commands as skill_commands
    import gateway.run as gateway_run
    import tools.async_delegation as async_delegation

    runner = _bootstrap(monkeypatch, tmp_path)
    fixture = _write_worker_environment(tmp_path)
    fixture["x_lookup"].unlink()
    monkeypatch.setattr(
        async_delegation, "find_delegation_by_work_key", lambda _key: ""
    )
    monkeypatch.setattr(
        gateway_run,
        "_prefetch_source_card_x_posts",
        MagicMock(side_effect=AssertionError("non-X prefetch was attempted")),
    )
    monkeypatch.setattr(
        skill_commands,
        "_load_skill_payload",
        MagicMock(return_value=("canonical skill", tmp_path, "Source Card Intake")),
    )
    monkeypatch.setattr(
        skill_commands,
        "_build_skill_message",
        MagicMock(return_value="bounded canonical worker contract"),
    )
    dispatch = MagicMock(
        return_value={"status": "dispatched", "delegation_id": "deleg-web"}
    )
    monkeypatch.setattr(async_delegation, "dispatch_async_delegation", dispatch)

    result = await runner._dispatch_source_card_intake(
        _event(text="https://github.com/example/repo"),
        _source(),
        runner.session_store.get_or_create_session.return_value,
    )

    assert result == {"status": "dispatched", "delegation_id": "deleg-web"}
    assert "https://github.com/example/repo" in dispatch.call_args.kwargs["goal"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit-4", "timeout"])
async def test_real_x_prefetch_failure_returns_exact_user_message_without_worker(
    monkeypatch, tmp_path, failure,
):
    import gateway.run as gateway_run
    import tools.async_delegation as async_delegation

    runner = _bootstrap(monkeypatch, tmp_path)
    _write_worker_environment(tmp_path)
    runner._run_agent = AsyncMock(side_effect=AssertionError("parent model called"))
    monkeypatch.setattr(
        async_delegation, "find_delegation_by_work_key", lambda _key: ""
    )
    worker_dispatch = MagicMock(side_effect=AssertionError("worker was dispatched"))
    monkeypatch.setattr(
        async_delegation, "dispatch_async_delegation", worker_dispatch
    )
    if failure == "exit-4":
        outcome = subprocess.CompletedProcess(
            ["x-lookup"], 4, stdout="", stderr="all routes failed\n"
        )
        monkeypatch.setattr(gateway_run.subprocess, "run", lambda *args, **kwargs: outcome)
        expected_error = "exit 4: all routes failed"
    else:
        monkeypatch.setattr(
            gateway_run.subprocess,
            "run",
            MagicMock(side_effect=subprocess.TimeoutExpired(["x-lookup"], 10)),
        )
        expected_error = "timeout after 10 seconds"

    response = await runner._handle_message_with_agent(
        _event(text="https://x.com/i/status/2088573393121509655"),
        _source(),
        "agent:main:telegram:group:-1001:12345",
        1,
    )

    assert response == (
        f"⚠️ Could not read the post ({expected_error}). No worker started."
    )
    worker_dispatch.assert_not_called()
    runner._run_agent.assert_not_awaited()


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
    fixture = _write_worker_environment(tmp_path)
    session_entry = runner.session_store.get_or_create_session.return_value
    source = _source()
    source.thread_id = "topic-77"
    event = _event(text="https://x.com/i/status/2088573393121509655")
    built = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            built["agent_kwargs"] = kwargs
            built["agent"] = self
            self.api_call_count = 0
            self.tool_result_budget_withheld_count = 0
            self.tools = [
                {"function": {"name": name}}
                for name in ("terminal", "read_file", "patch", "search_files", "tool_search")
            ]
            self.valid_tool_names = {
                "terminal", "read_file", "patch", "search_files", "tool_search"
            }

        def _execute_tool_calls(
            self, assistant_message, messages, effective_task_id, api_call_count=0
        ):
            built["executed_first_tool"] = assistant_message.tool_calls[0]

        def run_conversation(self, goal, *, task_id):
            built["run"] = (goal, task_id)
            command_line = goal.split(
                "only as command data and do not add arguments:\n", 1
            )[1].splitlines()[0]
            duplicate_command = json.loads(command_line)
            first_call = SimpleNamespace(
                function=SimpleNamespace(
                    name="terminal",
                    arguments=json.dumps({"command": duplicate_command}),
                )
            )
            self._execute_tool_calls(
                SimpleNamespace(tool_calls=[first_call]), [], task_id, 1
            )
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
    prefetched_post = {
        "status_id": "2088573393121509655",
        "canonical_url": "https://x.com/i/status/2088573393121509655",
        "author": {"handle": "example", "name": "Example", "followers": 12},
        "text": "prefetched post body",
        "created_at": "2026-08-15T00:00:00Z",
        "stats": {"likes": 4, "retweets": 2, "replies": 1, "views": 90},
        "links": ["https://example.com/source"],
        "media": [],
        "quote": None,
    }
    prefetch = MagicMock(return_value=[prefetched_post])
    monkeypatch.setattr(gateway_run, "_prefetch_source_card_x_posts", prefetch)
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
        event, source, session_entry
    )

    assert result == {"status": "dispatched", "delegation_id": "deleg-bounded"}
    assert "agent_kwargs" not in built
    worker_result = dispatched["runner"]()
    assert worker_result["status"] == "completed"
    assert worker_result["summary"] == "card complete"
    kwargs = built["agent_kwargs"]
    assert kwargs["max_iterations"] == 24
    assert kwargs["skip_context_files"] is True
    assert kwargs["load_soul_identity"] is False
    assert kwargs["skip_memory"] is True
    assert kwargs["skip_background_review"] is True
    assert kwargs["tool_result_max_chars"] == 4_000
    assert built["agent"].tool_result_emitted_max_chars == 4_000
    assert built["agent"].tool_result_total_max_chars == 24_000
    assert kwargs["enabled_toolsets"] == ["terminal", "file", "web"]
    worker_system = kwargs["ephemeral_system_prompt"]
    assert worker_system.startswith("bounded canonical worker contract")
    assert "TRUSTED PRELOADED SOURCE-CARD REFERENCES" in worker_system
    for name, body in fixture["reference_bodies"].items():
        assert worker_system.count(f"references/{name}") == 2
        assert worker_system.count(body) == 1
    assert {"delegation", "skills"}.issubset(kwargs["disabled_toolsets"])
    assert dispatched["delivery_mode"] == "direct"
    assert dispatched["work_kind"] == "source-card-intake"
    assert dispatched["role"] == "leaf"
    assert dispatched["max_async_children"] == 3
    assert dispatched["work_key"].startswith("source-card-intake:")
    assert "MODE: source-card-worker" in dispatched["goal"]
    assert dispatched["goal"].splitlines()[:2] == [
        "MODE: source-card-worker",
        "WORKER PACKET: gateway-prefetched",
    ]
    assert "UNTRUSTED JSON STRING" in dispatched["goal"]
    assert json.dumps(event.text, ensure_ascii=False) in dispatched["goal"]
    assert "Do not delegate" in dispatched["goal"]
    assert "Do not restart" in dispatched["goal"]
    assert "8,000 tokens" in dispatched["goal"]
    assert "Do not read shared context journals" in dispatched["goal"]
    assert "do not probe another tool" in dispatched["goal"]
    assert "24,000 emitted tool-result characters" in dispatched["goal"]
    assert "4,000 emitted characters" in dispatched["goal"]
    assert (
        "Do not read the cards-root README or an exemplar card"
        in dispatched["goal"]
    )
    assert "TRUSTED WORKER ENVIRONMENT (JSON)" in dispatched["goal"]
    assert str(fixture["cards_root"].resolve()) in dispatched["goal"]
    assert str(fixture["writer"].resolve()) in dispatched["goal"]
    assert str(fixture["x_lookup"].resolve()) not in dispatched["goal"]
    assert str(fixture["transcript_db"].resolve()) in dispatched["goal"]
    assert '"source_chat_id": "-1001"' in dispatched["goal"]
    assert '"source_thread_id": "topic-77"' in dispatched["goal"]
    assert '"parent_session_id": "sess-dedup"' in dispatched["goal"]
    assert '"platform_message_id": "msg-source-42"' in dispatched["goal"]
    assert "already attached" in dispatched["goal"]
    assert "UNTRUSTED PREFETCHED X POSTS (JSON)" in dispatched["goal"]
    assert json.dumps(prefetched_post, ensure_ascii=False, sort_keys=True) in dispatched[
        "goal"
    ]
    assert "FIRST tool call" in dispatched["goal"]
    assert "rg -l" in dispatched["goal"]
    assert "2088573393121509655" in dispatched["goal"]
    assert "Never call `search_files`" in dispatched["goal"]
    assert "Do not call twitter, the X API, oEmbed, r.jina.ai" in dispatched["goal"]
    assert "publish.twitter.com" not in dispatched["goal"]
    prefetch.assert_called_once()
    assert "search_files" not in built["agent"].valid_tool_names
    assert "tool_search" not in built["agent"].valid_tool_names
    assert built["agent"]._subdirectory_hints.check_tool_call(
        "terminal", {"command": f"rg -l 208857 {fixture['cards_root']}"}
    ) is None
    assert "WORKER PACKET: gateway-prefetched" in built["skill_args"][2]
    assert "Never call skill_view or delegate_task" in built["skill_args"][2]


@pytest.mark.asyncio
async def test_oversized_preloaded_references_reject_before_dispatch(
    monkeypatch, tmp_path,
):
    import agent.skill_commands as skill_commands
    import tools.async_delegation as async_delegation

    runner = _bootstrap(monkeypatch, tmp_path)
    _write_worker_environment(tmp_path)
    (tmp_path / "references" / "research-method.md").write_text(
        "x" * 5_001, encoding="utf-8"
    )
    monkeypatch.setattr(
        skill_commands,
        "_load_skill_payload",
        MagicMock(return_value=("canonical skill", tmp_path, "Source Card Intake")),
    )
    monkeypatch.setattr(
        skill_commands,
        "_build_skill_message",
        MagicMock(return_value="bounded canonical worker contract"),
    )
    dispatch = MagicMock(side_effect=AssertionError("worker was dispatched"))
    monkeypatch.setattr(async_delegation, "dispatch_async_delegation", dispatch)
    monkeypatch.setattr(
        async_delegation, "find_delegation_by_work_key", lambda _key: ""
    )

    result = await runner._dispatch_source_card_intake(
        _event(), _source(), runner.session_store.get_or_create_session.return_value
    )

    assert result["status"] == "rejected"
    assert "reference" in result["error"].lower()
    dispatch.assert_not_called()


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
