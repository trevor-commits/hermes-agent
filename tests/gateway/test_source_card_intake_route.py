"""Deterministic Telegram source-card intake routing.

The trusted channel binding is a control-plane contract. Bound URL turns must
dispatch one durable worker without allowing the parent model or parent tools
to interpret the router prompt.
"""

import json
import os
import sqlite3
import subprocess
import threading
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from tests.gateway.test_42039_duplicate_user_message import (
    _bootstrap,
    _source,
)


def _event(*, text="https://github.com/example/repo", route="source-card-intake"):
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
    new_source_card = home / "scripts" / "new-source-card"
    new_source_card.write_text(
        "#!/bin/sh\nprintf '# gateway/source-card\\n\\n- url: TODO\\n- owner/name: TODO\\n- by: TODO\\n\\n## Decision manifest (ER-278)\\n- decision-key: TODO\\n'\n",
        encoding="utf-8",
    )
    new_source_card.chmod(0o755)
    validator = home / "scripts" / "validate-touched-source-cards"
    validator.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    validator.chmod(0o755)
    source_card_prefetch = home / "scripts" / "source-card-prefetch"
    source_card_prefetch.write_text(
        "#!/bin/sh\n"
        "printf 'repo: %s\\nurl: https://github.com/%s\\nhead: fixture-head\\n"
        "license: fixture-license\\nsignal: fixture signal\\n' \"$1\" \"$1\"\n",
        encoding="utf-8",
    )
    source_card_prefetch.chmod(0o755)
    transcript_db = home / "state.db"
    with sqlite3.connect(transcript_db) as connection:
        connection.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                platform_message_id TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO messages(id, session_id, role, platform_message_id) "
            "VALUES (?, ?, 'user', 'msg-source-42')",
            ((41, "sess-dedup"), (42, "parent-session-9")),
        )
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
        "new_source_card": new_source_card,
        "validator": validator,
        "source_card_prefetch": source_card_prefetch,
        "transcript_db": transcript_db,
        "reference_bodies": reference_bodies,
    }


def test_route_requires_trusted_telegram_binding_url_and_external_event():
    from gateway.run import _is_source_card_intake_event

    assert _is_source_card_intake_event(_event(), _source())
    assert not _is_source_card_intake_event(_event(route=None), _source())
    assert not _is_source_card_intake_event(_event(text="please research this"), _source())
    assert not _is_source_card_intake_event(
        _event(text="https://example.com/general-web-page"), _source()
    )

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


def test_legacy_tool_withholding_does_not_override_a_completed_no_tool_draft():
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

    assert normalized["status"] == "completed"
    assert normalized["summary"] == "Card complete."
    assert normalized["error"] is None
    assert normalized["tool_results_withheld"] == 2


def test_no_tool_normalizer_preserves_an_existing_worker_failure():
    from gateway.run import _normalize_source_card_worker_result

    provider_failure = _normalize_source_card_worker_result(
        {
            "final_response": "",
            "error": "provider authentication failed",
            "failed": True,
            "api_calls": 1,
        },
        duration_seconds=1.0,
        worker_model="test-model",
    )

    assert provider_failure["status"] == "error"
    assert provider_failure["error"] == "provider authentication failed"


def test_worker_tool_surface_is_empty_regardless_of_configured_toolsets():
    from gateway.run import _source_card_worker_toolsets

    assert _source_card_worker_toolsets(
        ["browser", "file", "memory", "terminal", "web"]
    ) == []
    assert _source_card_worker_toolsets([]) == []


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
        "new_source_card": str(fixture["new_source_card"].resolve()),
        "source_card_validator": str(fixture["validator"].resolve()),
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


def test_duplicate_lookup_ignores_nested_intake_records(tmp_path):
    from gateway.run import _source_card_duplicate_lookup

    cards_root = tmp_path / "researched-repos"
    nested = cards_root / "_intake" / "external-source-intake.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("2088626767669981398\n", encoding="utf-8")

    matches, arguments = _source_card_duplicate_lookup(
        "https://x.com/i/status/2088626767669981398",
        cards_root,
    )

    assert matches == []
    assert arguments[:3] == ["rg", "-l", "-F"]


def test_no_tool_worker_guard_blocks_full_tree_search_before_execution():
    from gateway.run import _install_source_card_no_tool_guard

    executed = []
    agent = SimpleNamespace(
        _execute_tool_calls=lambda *args, **kwargs: executed.append((args, kwargs))
    )
    _install_source_card_no_tool_guard(agent)
    broad_call = SimpleNamespace(
        function=SimpleNamespace(
            name="terminal",
            arguments=json.dumps({"command": "rg --files /tmp/cards"}),
        )
    )

    with pytest.raises(RuntimeError, match="no_tool_contract_violated"):
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
async def test_transcript_persistence_failure_prevents_dispatch(monkeypatch, tmp_path):
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

    assert "could not safely persist" in response.lower()
    runner._dispatch_source_card_intake.assert_not_awaited()
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
async def test_worker_is_no_tool_bounded_and_dispatched_for_direct_delivery(
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
                for name in (
                    "terminal",
                    "read_file",
                    "write_file",
                    "search_files",
                    "web_extract",
                )
            ]
            self.valid_tool_names = {
                "terminal",
                "read_file",
                "write_file",
                "search_files",
                "web_extract",
            }

        def _execute_tool_calls(self, *_args, **_kwargs):
            built["unrestricted_executor_called"] = True

        def _build_system_prompt(self):
            return "Hermes base system"

        def run_conversation(self, goal, *, task_id):
            built["run"] = (goal, task_id)
            assert self.tools == []
            assert self.valid_tool_names == set()
            self.api_call_count = 1
            return {
                "final_response": json.dumps(
                    {
                        "card_path": str(
                            fixture["cards_root"] / "example-source.md"
                        ),
                        "card_content": _RECORDED_CARD,
                    },
                    ensure_ascii=False,
                ),
                "api_calls": 1,
                "model": "test-model",
                "turn_exit_reason": "text_response(finish_reason=stop)",
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
    write_draft = MagicMock()
    landing = MagicMock(
        return_value={
            "path": "researched-repos/example-source.md",
            "commit": "a" * 40,
            "receipt_results": [],
        }
    )
    monkeypatch.setattr(gateway_run, "_write_source_card_draft", write_draft)
    monkeypatch.setattr(gateway_run, "_land_source_card", landing)
    monkeypatch.setattr(
        async_delegation, "find_delegation_by_work_key", lambda _key: ""
    )
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

    result = await runner._dispatch_source_card_intake(event, source, session_entry)

    assert result == {"status": "dispatched", "delegation_id": "deleg-bounded"}
    assert "agent_kwargs" not in built
    worker_result = dispatched["runner"]()
    assert worker_result["status"] == "completed"
    assert worker_result["summary"] == (
        "✅ Card landed: researched-repos/example-source.md @ " + "a" * 40
    )
    kwargs = built["agent_kwargs"]
    assert kwargs["max_iterations"] == 2
    assert kwargs["skip_context_files"] is True
    assert kwargs["load_soul_identity"] is False
    assert kwargs["skip_memory"] is True
    assert kwargs["skip_background_review"] is True
    assert kwargs["enabled_toolsets"] == []
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
    assert dispatched["goal"].splitlines()[:2] == [
        "MODE: source-card-worker",
        "WORKER PACKET: gateway-prefetched",
    ]
    assert "Make no tool calls" in dispatched["goal"]
    assert "Return exactly one JSON object" in dispatched["goal"]
    assert "SOURCE-CARD TEMPLATE" in dispatched["goal"]
    assert "TRUSTED DUPLICATE LOOKUP RESULT" in dispatched["goal"]
    assert "UNTRUSTED PREFETCHED X POSTS (JSON)" in dispatched["goal"]
    assert "UNTRUSTED PREFETCHED GITHUB REPOSITORIES (JSON)" in dispatched["goal"]
    assert json.dumps(prefetched_post, ensure_ascii=False, sort_keys=True) in dispatched[
        "goal"
    ]
    assert "FIRST tool call" not in dispatched["goal"]
    assert "search_files" not in dispatched["goal"]
    assert "Do not fetch GitHub metadata that is already injected" in dispatched[
        "goal"
    ]
    assert write_draft.call_args.args[0] == (
        fixture["cards_root"] / "example-source.md"
    )
    assert write_draft.call_args.args[1] == _RECORDED_CARD
    landing.assert_called_once()
    assert prefetch.call_count == 1
    assert built["agent"].tools == []
    assert built["agent"].valid_tool_names == set()
    with pytest.raises(RuntimeError, match="no_tool_contract_violated"):
        built["agent"]._execute_tool_calls(
            SimpleNamespace(tool_calls=[]), [], "worker-session", 1
        )
    assert "unrestricted_executor_called" not in built
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


_SOURCE_CARD_FIXTURES = (
    Path(__file__).parent / "../fixtures/source_card_intake"
).resolve()
_REPLAY_X_POST = json.loads(
    (_SOURCE_CARD_FIXTURES / "recorded-x-2087232392209531166.json").read_text(
        encoding="utf-8"
    )
)
_REPLAY_X_STATUS_ID = str(_REPLAY_X_POST["id"])
_REPLAY_GITHUB_COMPACT = (
    _SOURCE_CARD_FIXTURES
    / "recorded-github-igorwarzocha-howaboua-pi-stuff.compact"
).read_text(encoding="utf-8")
_REPLAY_CARD = (
    _SOURCE_CARD_FIXTURES / "recorded-howaboua-pi-shepherdr-card.md"
).read_text(encoding="utf-8")
_REAL_NEW_SOURCE_CARD_OUTPUT = (
    _SOURCE_CARD_FIXTURES / "real-new-source-card-output.md"
).read_text(encoding="utf-8")
_CANONICAL_SKILL_FIXTURE = _SOURCE_CARD_FIXTURES / "canonical-skill"
_RECORDED_CARD = (
    _SOURCE_CARD_FIXTURES / "recorded-mapcn-card.md"
).read_text(encoding="utf-8")

def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_offline_route_fixture(tmp_path: Path) -> dict:
    home = tmp_path / "hermes-home"
    repo = tmp_path / "cards-repo"
    remote = tmp_path / "cards-remote.git"
    cards_root = repo / "researched-repos"
    cards_root.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.name", "Source Card Replay")
    _git(repo, "config", "user.email", "source-card-replay@example.invalid")
    (repo / "README.md").write_text("offline replay\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "test: initialize source-card replay")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    x_payload = json.dumps(_REPLAY_X_POST, ensure_ascii=False)
    _write_executable(
        repo / "scripts" / "x-lookup",
        f"""
        #!/usr/bin/env python3
        import sys
        if sys.argv[1:] != ["--json", "https://x.com/i/status/{_REPLAY_X_STATUS_ID}"]:
            raise SystemExit(3)
        print({x_payload!r})
        """,
    )
    _write_executable(
        repo / "scripts" / "source-card-prefetch",
        f"""
        #!/usr/bin/env python3
        import sys
        if sys.argv[1:] != ["IgorWarzocha/howaboua-pi-stuff", "--compact"]:
            raise SystemExit(3)
        print({_REPLAY_GITHUB_COMPACT!r})
        """,
    )
    _write_executable(
        repo / "scripts" / "new-source-card",
        f"""
        #!/usr/bin/env python3
        import sys
        if sys.argv[1:] != ["gateway/source-card", "--stdout"]:
            raise SystemExit(3)
        print({_REAL_NEW_SOURCE_CARD_OUTPUT!r})
        """,
    )
    validator_log = tmp_path / "validator-argv.json"
    _write_executable(
        repo / "scripts" / "validate-touched-source-cards",
        f"""
        #!/usr/bin/env python3
        import json
        import pathlib
        import sys
        expected = ["--card", "researched-repos/igorwarzocha-howaboua-pi-stuff.md", "--no-full-backlog"]
        pathlib.Path({str(validator_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
        if sys.argv[1:] != expected:
            raise SystemExit(7)
        card = pathlib.Path("researched-repos/igorwarzocha-howaboua-pi-stuff.md").read_text(encoding="utf-8")
        if "## Decision manifest (ER-278)" not in card or "- by:" not in card:
            raise SystemExit(8)
        """,
    )

    decision_log = tmp_path / "decision-writer.jsonl"
    _write_executable(
        home / "scripts" / "hermes-research-decisions",
        f"""
        #!/usr/bin/env python3
        import json
        import pathlib
        import sys
        log = pathlib.Path({str(decision_log)!r})
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sys.argv[1:]) + "\\n")
        print(json.dumps({{"ok": True, "command": sys.argv[1]}}))
        """,
    )
    transcript_db = home / "state.db"
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(transcript_db) as connection:
        connection.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY, session_id TEXT, platform_message_id TEXT, "
            "role TEXT, content TEXT)"
        )
        connection.execute(
            "INSERT INTO messages(id, session_id, platform_message_id, role, content) "
            "VALUES(42, 'sess-dedup', 'msg-source-42', 'user', ?)",
            (f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",),
        )
    config = home / "state" / "research-decision-config.json"
    config.parent.mkdir(parents=True)
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
    return {
        "home": home,
        "repo": repo,
        "remote": remote,
        "cards_root": cards_root,
        "validator_log": validator_log,
        "decision_log": decision_log,
        "decision_writer": home / "scripts" / "hermes-research-decisions",
        "canonical_skill_dir": _CANONICAL_SKILL_FIXTURE,
    }


def test_template_prefetch_neutralizes_real_helper_subject_identity(tmp_path):
    from gateway.run import _prefetch_source_card_template

    fixture = _write_offline_route_fixture(tmp_path)

    template = _prefetch_source_card_template(
        fixture["repo"] / "scripts" / "new-source-card"
    )

    assert "gateway/source-card" not in template
    assert "- url: TODO:" in template
    assert "- owner/name: TODO:" in template


def test_github_prefetch_uses_each_link_once_and_caps_the_batch(tmp_path):
    from gateway.run import _prefetch_source_card_github_repositories

    fixture = _write_offline_route_fixture(tmp_path)
    posts = [
        {
            "links": [
                "https://github.com/IgorWarzocha/howaboua-pi-stuff/tree/main/packages/pi-shepherdr?tab=readme#usage",
                "https://github.com/IgorWarzocha/howaboua-pi-stuff/",
            ]
        }
    ]

    prefetched = _prefetch_source_card_github_repositories(
        posts, fixture["repo"] / "scripts" / "source-card-prefetch"
    )

    assert len(prefetched) == 1
    assert prefetched[0]["owner_name"] == "IgorWarzocha/howaboua-pi-stuff"
    assert prefetched[0]["fields"]["head"] == (
        "8d63d300597488e6fa4c30ccd6a3eb0fed2d4304"
    )
    assert _prefetch_source_card_github_repositories(
        [{"links": ["https://github.com/settings/profile"]}],
        fixture["repo"] / "scripts" / "source-card-prefetch",
    ) == []
    too_many = [
        {"links": [f"https://github.com/example/repo-{index}"]}
        for index in range(5)
    ]
    with pytest.raises(RuntimeError, match="maximum 4"):
        _prefetch_source_card_github_repositories(
            too_many, fixture["repo"] / "scripts" / "source-card-prefetch"
        )


def test_receipt_failure_reports_the_already_contained_card(tmp_path):
    from gateway.run import (
        _SourceCardPostLandingError,
        _format_direct_source_card_completion,
        _land_source_card,
    )

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(_REPLAY_CARD, encoding="utf-8")
    _write_executable(
        fixture["decision_writer"],
        """
        #!/usr/bin/env python3
        import sys
        print("receipt fixture failed", file=sys.stderr)
        raise SystemExit(9)
        """,
    )
    environment = {
        "cards_root": str(fixture["cards_root"]),
        "source_card_validator": str(
            fixture["repo"] / "scripts" / "validate-touched-source-cards"
        ),
        "decision_writer": str(fixture["decision_writer"]),
        "source_chat_id": "-5551733823",
        "source_thread_id": "",
        "parent_session_id": "sess-dedup",
        "platform_message_id": "msg-source-42",
        "transcript_db": str(fixture["home"] / "state.db"),
    }

    with pytest.raises(_SourceCardPostLandingError) as caught:
        _land_source_card(
            card_path=card,
            intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
            environment=environment,
            source_message_row_id=42,
        )

    failure = caught.value
    assert failure.path == "researched-repos/igorwarzocha-howaboua-pi-stuff.md"
    remote_tip = _git(
        fixture["repo"], "ls-remote", "origin", "refs/heads/main"
    ).split()[0]
    assert failure.commit == remote_tip
    assert _git(
        fixture["repo"],
        "show",
        f"{remote_tip}:researched-repos/igorwarzocha-howaboua-pi-stuff.md",
    ) + "\n" == _REPLAY_CARD
    summary = (
        "⚠️ Card landed but receipts incomplete: "
        f"{failure.path} @ {failure.commit}; {failure.step}: {failure.detail}"
    )
    assert _format_direct_source_card_completion(
        {"status": "partial", "summary": summary, "error": str(failure)}
    ) == summary


def test_receipt_preflight_rejects_a_decision_key_without_exact_card_filename(
    tmp_path,
):
    from gateway.run import _SourceCardLandingError, _source_card_fields_and_manifest

    card = tmp_path / "example-card.md"
    card.write_text(
        "# Example\n\n"
        "- url: https://example.com\n"
        "- owner/name: example/card\n\n"
        "## Decision manifest (ER-278)\n\n"
        "- decision-key: card:example-card#adopt\n",
        encoding="utf-8",
    )

    with pytest.raises(
        _SourceCardLandingError,
        match="must match card:<flat-card.md>#<choice>",
    ):
        _source_card_fields_and_manifest(card)


def test_landing_rejects_an_invalid_decision_key_before_push(tmp_path):
    from gateway.run import _SourceCardLandingError, _land_source_card

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "example-card.md"
    card.write_text(
        "# Example\n\n"
        "- url: https://example.com\n"
        "- owner/name: example/card\n"
        "- specific conclusion for this lookup: watch only\n"
        "- disposition: watch-until: evidence changes\n"
        "- latest source signal: fixture evidence\n\n"
        "## Decision manifest (ER-278)\n\n"
        "- decision-key: card:example-card#adopt\n",
        encoding="utf-8",
    )
    _write_executable(
        fixture["repo"] / "scripts" / "validate-touched-source-cards",
        "#!/bin/sh\nexit 0\n",
    )
    before = _git(fixture["repo"], "rev-parse", "origin/main")
    environment = {
        "cards_root": str(fixture["cards_root"]),
        "source_card_validator": str(
            fixture["repo"] / "scripts" / "validate-touched-source-cards"
        ),
        "decision_writer": str(fixture["decision_writer"]),
        "source_chat_id": "-5551733823",
        "source_thread_id": "",
        "parent_session_id": "sess-dedup",
        "platform_message_id": "msg-source-42",
        "transcript_db": str(fixture["home"] / "state.db"),
    }

    with pytest.raises(
        _SourceCardLandingError,
        match="must match card:<flat-card.md>#<choice>",
    ):
        _land_source_card(
            card_path=card,
            intake_text="https://example.com",
            environment=environment,
            source_message_row_id=42,
        )

    assert _git(fixture["repo"], "rev-parse", "origin/main") == before
    assert not fixture["decision_log"].exists()


def test_landing_rejects_a_dirty_tracked_duplicate_without_absorbing_it(tmp_path):
    from gateway.run import _SourceCardLandingError, _land_source_card

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(_REPLAY_CARD, encoding="utf-8")
    _git(fixture["repo"], "add", "--", "researched-repos/igorwarzocha-howaboua-pi-stuff.md")
    _git(fixture["repo"], "commit", "-m", "test: seed tracked source card")
    _git(fixture["repo"], "push", "origin", "main")
    clean_commit = _git(fixture["repo"], "rev-parse", "HEAD")
    operator_edit = _REPLAY_CARD + "\nOperator-owned pending edit.\n"
    card.write_text(operator_edit, encoding="utf-8")
    environment = {
        "cards_root": str(fixture["cards_root"]),
        "source_card_validator": str(
            fixture["repo"] / "scripts" / "validate-touched-source-cards"
        ),
        "decision_writer": str(fixture["decision_writer"]),
        "source_chat_id": "-5551733823",
        "source_thread_id": "",
        "parent_session_id": "sess-dedup",
        "platform_message_id": "msg-source-42",
        "transcript_db": str(fixture["home"] / "state.db"),
    }

    with pytest.raises(_SourceCardLandingError, match="dirty tracked card") as caught:
        _land_source_card(
            card_path=card,
            intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
            environment=environment,
            source_message_row_id=42,
        )

    assert caught.value.step == "git_clean"
    assert card.read_text(encoding="utf-8") == operator_edit
    assert _git(fixture["repo"], "rev-parse", "HEAD") == clean_commit
    assert _git(
        fixture["repo"], "ls-remote", "origin", "refs/heads/main"
    ).split()[0] == clean_commit
    assert not fixture["decision_log"].exists()


@pytest.mark.asyncio
async def test_offline_recorded_route_replay_lands_and_receipts_one_card(
    monkeypatch, tmp_path,
):
    """Recorded X + GitHub evidence traverses the live route into a Git remote."""
    import agent.skill_commands as skill_commands
    import gateway.run as gateway_run
    import run_agent
    import tools.async_delegation as async_delegation
    import tools.delegate_tool as delegate_tool

    fixture = _write_offline_route_fixture(tmp_path)
    unrelated_staged = fixture["cards_root"] / "unrelated-staged.md"
    unrelated_staged.write_text("operator-owned staged change\n", encoding="utf-8")
    _git(fixture["repo"], "add", "--", "researched-repos/unrelated-staged.md")
    runner = _bootstrap(monkeypatch, fixture["home"])
    event = _event(text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}")
    source = _source()
    session_entry = runner.session_store.get_or_create_session.return_value
    captured = {}

    provider_content = json.dumps(
        {
            "card_path": str(
                fixture["cards_root"]
                / "igorwarzocha-howaboua-pi-stuff.md"
            ),
            "card_content": _REPLAY_CARD,
        },
        ensure_ascii=False,
    )
    provider_message = SimpleNamespace(
        content=provider_content,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
        refusal=None,
    )
    provider_response = SimpleNamespace(
        choices=[SimpleNamespace(message=provider_message, finish_reason="stop")],
        model="offline-replay",
        usage=None,
    )
    provider_client = MagicMock()
    provider_client.chat.completions.create.return_value = provider_response
    executor_calls = []
    original_execute = run_agent.AIAgent._execute_tool_calls

    def _tracked_execute(self, *args, **kwargs):
        executor_calls.append((args, kwargs))
        return original_execute(self, *args, **kwargs)

    monkeypatch.setattr(
        skill_commands,
        "_load_skill_payload",
        MagicMock(
            return_value=(
                {
                    "success": True,
                    "name": "source-card-intake",
                    "content": (
                        fixture["canonical_skill_dir"] / "SKILL.md"
                    ).read_text(encoding="utf-8"),
                    "raw_content": (
                        fixture["canonical_skill_dir"] / "SKILL.md"
                    ).read_text(encoding="utf-8"),
                },
                fixture["canonical_skill_dir"],
                "Source Card Intake",
            )
        ),
    )
    monkeypatch.setattr(run_agent, "OpenAI", MagicMock(return_value=provider_client))
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **_kwargs: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(run_agent.AIAgent, "_execute_tool_calls", _tracked_execute)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"agent": {}})
    monkeypatch.setattr(gateway_run, "_checkpoint_agent_kwargs", lambda _cfg: {})
    monkeypatch.setattr(gateway_run, "_current_max_iterations", lambda: 99)
    monkeypatch.setattr(async_delegation, "find_delegation_by_work_key", lambda _key: "")
    monkeypatch.setattr(delegate_tool, "_get_max_async_children", lambda: 3)
    runner._resolve_session_agent_runtime = MagicMock(
        return_value=(
            "openai/gpt-4o-mini",
            {
                "api_key": "fixture-key",
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "chat_completions",
            },
        )
    )
    runner._resolve_turn_agent_config = MagicMock(
        return_value={
            "model": "openai/gpt-4o-mini",
            "runtime": {
                "api_key": "fixture-key",
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "chat_completions",
            },
            "request_overrides": None,
        }
    )
    runner._resolve_enabled_toolsets_for_source = MagicMock(
        return_value=["terminal", "file", "web"]
    )
    runner._resolve_session_reasoning_config = MagicMock(return_value=None)
    runner._resolve_session_service_tier = MagicMock(return_value=None)
    runner._provider_routing = {}
    runner._refresh_fallback_model = MagicMock(return_value=None)
    runner._cleanup_agent_resources = MagicMock()
    runner._run_agent = AsyncMock(side_effect=AssertionError("parent model called"))

    def _dispatch(**kwargs):
        captured["dispatch"] = kwargs
        return {"status": "dispatched", "delegation_id": "deleg-offline-replay"}

    monkeypatch.setattr(async_delegation, "dispatch_async_delegation", _dispatch)

    response = await runner._handle_message_with_agent(
        event, source, session_entry.session_key, 1
    )

    assert response == (
        "Research is running in the background. The completed source card will "
        "return here."
    )
    worker_result = captured["dispatch"]["runner"]()
    assert provider_client.chat.completions.create.call_count == 1
    assert executor_calls == []
    provider_request = provider_client.chat.completions.create.call_args.kwargs
    provider_messages = provider_request["messages"]
    wire_goal = next(
        message["content"]
        for message in reversed(provider_messages)
        if message.get("role") == "user"
    )
    system_content = provider_messages[0]["content"]
    if isinstance(system_content, list):
        system_text = "".join(
            str(item.get("text") or "")
            for item in system_content
            if isinstance(item, dict)
        )
    else:
        system_text = str(system_content)
    replay_metrics = {
        key: worker_result[key]
        for key in (
            "api_calls",
            "worker_goal_chars",
            "worker_goal_bytes",
            "worker_result_chars",
            "worker_result_bytes",
            "worker_system_chars",
            "worker_system_bytes",
            "worker_system_byte_budget",
            "worker_dynamic_chars",
            "worker_dynamic_bytes",
            "worker_total_chars",
            "worker_total_bytes",
            "worker_api_call_budget",
            "worker_goal_byte_budget",
            "worker_result_byte_budget",
            "tool_result_chars",
        )
    }
    replay_metrics.update(
        {
            "wire_provider_calls": provider_client.chat.completions.create.call_count,
            "wire_executor_calls": len(executor_calls),
        }
    )
    metrics_path = tmp_path / "source-card-replay-metrics.json"
    metrics_path.write_text(
        json.dumps(replay_metrics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert json.loads(metrics_path.read_text(encoding="utf-8")) == replay_metrics
    assert worker_result["status"] == "completed"
    assert worker_result["summary"].startswith(
        "✅ Card landed: researched-repos/igorwarzocha-howaboua-pi-stuff.md @ "
    )
    assert worker_result["api_calls"] == 1
    assert worker_result["tool_result_chars"] == 0
    assert worker_result["worker_api_call_budget"] == worker_result["api_calls"] + 1
    assert worker_result["worker_goal_byte_budget"] == 16_384
    assert worker_result["worker_result_byte_budget"] == 16_384
    assert worker_result["worker_system_byte_budget"] == 24_576
    assert (
        worker_result["worker_goal_byte_budget"]
        - worker_result["worker_goal_bytes"]
        >= 4_096
    )
    assert (
        worker_result["worker_result_byte_budget"]
        - worker_result["worker_result_bytes"]
        >= 4_096
    )
    assert (
        worker_result["worker_system_byte_budget"]
        - worker_result["worker_system_bytes"]
        >= 4_096
    )
    agent = runner._cleanup_agent_resources.call_args.args[0]
    assert agent.max_iterations == 2
    assert agent.tools == []
    assert agent.valid_tool_names == set()
    assert worker_result["worker_goal_chars"] == len(wire_goal)
    assert worker_result["worker_goal_bytes"] == len(
        wire_goal.encode("utf-8")
    )
    assert worker_result["worker_system_chars"] == len(system_text)
    assert worker_result["worker_system_bytes"] == len(system_text.encode("utf-8"))
    assert worker_result["worker_dynamic_chars"] == (
        worker_result["worker_goal_chars"] + worker_result["worker_result_chars"]
    )
    assert worker_result["worker_dynamic_bytes"] == (
        worker_result["worker_goal_bytes"] + worker_result["worker_result_bytes"]
    )
    assert worker_result["worker_total_chars"] == (
        worker_result["worker_system_chars"] + worker_result["worker_dynamic_chars"]
    )
    assert worker_result["worker_total_bytes"] == (
        worker_result["worker_system_bytes"] + worker_result["worker_dynamic_bytes"]
    )
    assert '"repo": "IgorWarzocha/howaboua-pi-stuff"' in wire_goal
    assert '"signal": "GitHub metadata showed' in wire_goal
    assert "SOURCE-CARD TEMPLATE" in wire_goal
    assert "gateway/source-card" not in wire_goal
    assert "A Gateway worker makes no tool calls." in system_text
    assert worker_result["worker_result_chars"] == len(provider_content)
    assert worker_result["worker_result_bytes"] == len(
        provider_content.encode("utf-8")
    )

    remote_tip = _git(fixture["repo"], "ls-remote", "origin", "refs/heads/main").split()[0]
    committed = _git(
        fixture["repo"],
        "show",
        f"{remote_tip}:researched-repos/igorwarzocha-howaboua-pi-stuff.md",
    )
    assert committed + "\n" == _REPLAY_CARD
    assert _git(
        fixture["repo"],
        "diff",
        "--cached",
        "--name-only",
    ).splitlines() == ["researched-repos/unrelated-staged.md"]
    assert "researched-repos/unrelated-staged.md" not in _git(
        fixture["repo"],
        "show",
        "--format=",
        "--name-only",
        remote_tip,
    ).splitlines()
    assert json.loads(fixture["validator_log"].read_text(encoding="utf-8")) == [
        "--card",
        "researched-repos/igorwarzocha-howaboua-pi-stuff.md",
        "--no-full-backlog",
    ]
    decision_calls = [
        json.loads(line)
        for line in fixture["decision_log"].read_text(encoding="utf-8").splitlines()
    ]
    assert [call[0] for call in decision_calls] == ["add", "receipt-intake"]
    assert "--commit" in decision_calls[1]
    assert decision_calls[1][decision_calls[1].index("--commit") + 1] == remote_tip
    assert "--card" in decision_calls[1]
    assert decision_calls[1][decision_calls[1].index("--card") + 1] == (
        "igorwarzocha-howaboua-pi-stuff.md"
    )
