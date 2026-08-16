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
                        "card_content": _REPLAY_CARD,
                    },
                    ensure_ascii=False,
                ),
                "api_calls": 1,
                "model": "test-model",
                # A turn that ran must carry provider attestation; the gateway
                # fails the landing closed without it.
                "served_models": [
                    {"call": 1, "requested": "test-model", "served": "test-model"}
                ],
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
    landing = MagicMock(
        return_value={
            "path": "researched-repos/example-source.md",
            "commit": "a" * 40,
            "receipt_results": [],
        }
    )
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
    assert not hasattr(gateway_run, "_write_source_card_draft")
    landing.assert_called_once()
    assert landing.call_args.kwargs["card_path"] == (
        fixture["cards_root"] / "example-source.md"
    )
    finalized_content = landing.call_args.kwargs["card_content"]
    assert "TODO:" not in finalized_content
    assert "# IgorWarzocha/howaboua-pi-stuff" in finalized_content
    assert "## Decision manifest (ER-278)" in finalized_content
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
        if any(
            line.split(":", 1)[1].strip().upper().startswith("TODO:")
            for line in card.splitlines()
            if line.startswith("- ") and ":" in line
        ):
            raise SystemExit(9)
        """,
    )
    _git(repo, "add", "scripts")
    _git(repo, "commit", "-m", "test: install deterministic source-card helpers")
    _git(repo, "push", "origin", "main")

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
    assert "TODO:" not in template
    assert "- url: not verified from gateway-prefetched evidence" in template
    assert "- owner/name: not verified from gateway-prefetched evidence" in template
    assert "- no-decision-reason: watch-only" in template


def test_x_prefetch_rejects_a_multiline_author_handle():
    from gateway.run import _SourceCardPrefetchError, _normalize_source_card_x_post

    payload = {
        "id": "2088554041710145903",
        "author": {
            "handle": "safe_handle\n- owner/name: injected/repository",
            "name": "Safe Name",
            "followers": 1,
        },
        "text": "A post body",
        "created_at": "2026-08-15T00:00:00Z",
        "stats": {"likes": 1, "retweets": 0, "replies": 0, "views": 2},
        "links": [],
        "media": [],
        "quote": None,
    }

    with pytest.raises(_SourceCardPrefetchError, match="author handle"):
        _normalize_source_card_x_post(payload, "2088554041710145903")


def test_worker_draft_finalizer_replaces_validator_rejected_placeholders(tmp_path):
    from gateway.run import _finalize_source_card_worker_draft

    card_path = tmp_path / "alibaba-opensandbox-claim.md"
    draft = _REAL_NEW_SOURCE_CARD_OUTPUT + (
        "\n## Decision manifest (ER-278)\n"
        "- decision-key: TODO: card:<canonical-flat-filename>#<specific-choice>\n"
    )
    prefetched = [
        {
            "status_id": "2088554041710145903",
            "canonical_url": "https://x.com/i/status/2088554041710145903",
            "author": {"handle": "EngMoElgaraihy", "name": "Mo Elgaraihy"},
            "text": "Alibaba released OpenSandbox.",
            "links": [],
            "media": [{"type": "photo", "url": "https://pbs.twimg.com/example"}],
        }
    ]

    finalized = _finalize_source_card_worker_draft(
        card_path=card_path,
        content=draft,
        prefetched_x_posts=prefetched,
        prefetched_github_repositories=[],
    )

    assert "TODO:" not in finalized
    assert "- url: https://x.com/i/status/2088554041710145903" in finalized
    assert (
        "- owner/name: engmoelgaraihy/x-2088554041710145903" in finalized
    )
    assert (
        "- current pinned SHA: n/a (social-source card; stable X status ID "
        "2088554041710145903; no Git revision was prefetched)" in finalized
    )
    assert (
        "- license/use boundary: not verified from gateway-prefetched evidence; "
        "no repository license was available" in finalized
    )
    assert (
        "- downstream learning targets: none: no verified implementation target "
        "was prefetched" in finalized
    )
    assert (
        "- Hermes relevance: none: no verified Hermes integration surface was "
        "prefetched" in finalized
    )
    assert "- no-decision-reason: watch-only" in finalized


def test_worker_draft_finalizer_normalizes_canary_routing_explanations(tmp_path):
    from gateway.run import _finalize_source_card_worker_draft

    draft = _REPLAY_CARD.replace(
        "- downstream learning targets: hermes",
        "- downstream learning targets: hermes: compare the claimed "
        "agent+skill browser-control model with Hermes browser tooling once a "
        "verified primary source exists",
    ).replace(
        "- Hermes relevance: adjacent",
        "- Hermes relevance: adjacent: the post claims an agent+skill-driven "
        "AI browser, conceptually overlapping Hermes agent/skill concepts",
    )

    finalized = _finalize_source_card_worker_draft(
        card_path=tmp_path / "ego-lite-ai-browser.md",
        content=draft,
        prefetched_x_posts=[],
        prefetched_github_repositories=[],
    )

    assert "- downstream learning targets: hermes\n" in finalized
    assert "- Hermes relevance: adjacent\n" in finalized


def test_worker_draft_finalizer_selects_the_repository_named_by_the_worker(tmp_path):
    from gateway.run import _finalize_source_card_worker_draft

    first = {
        "owner_name": "example/first",
        "canonical_url": "https://github.com/example/first",
        "fields": {"head": "a" * 40, "license": "MIT"},
    }
    second = {
        "owner_name": "example/second",
        "canonical_url": "https://github.com/example/second",
        "fields": {"head": "b" * 40, "license": "Apache-2.0"},
    }
    draft = _REPLAY_CARD.replace(
        "IgorWarzocha/howaboua-pi-stuff", "example/second"
    ).replace(
        "https://github.com/IgorWarzocha/howaboua-pi-stuff",
        "https://github.com/example/second",
    ).replace(
        "8d63d300597488e6fa4c30ccd6a3eb0fed2d4304", "b" * 40
    )

    finalized = _finalize_source_card_worker_draft(
        card_path=tmp_path / "example-second.md",
        content=draft,
        prefetched_x_posts=[],
        prefetched_github_repositories=[first, second],
    )

    assert "- owner/name: example/second" in finalized
    assert "- url: https://github.com/example/second" in finalized
    assert f"- current pinned SHA: {'b' * 40}" in finalized
    assert "example/first" not in finalized
    assert "a" * 40 not in finalized


def test_worker_draft_finalizer_selects_the_x_post_named_by_the_worker(tmp_path):
    from gateway.run import _finalize_source_card_worker_draft

    first = {
        "status_id": "1111111111111111111",
        "canonical_url": "https://x.com/i/status/1111111111111111111",
        "author": {"handle": "first_author", "name": "First Author"},
    }
    second = {
        "status_id": "2222222222222222222",
        "canonical_url": "https://x.com/i/status/2222222222222222222",
        "author": {"handle": "second_author", "name": "Second Author"},
    }
    draft = _REPLAY_CARD.replace(
        "https://github.com/IgorWarzocha/howaboua-pi-stuff",
        "https://x.com/i/status/2222222222222222222",
    ).replace(
        "IgorWarzocha/howaboua-pi-stuff",
        "second_author/x-2222222222222222222",
    )

    finalized = _finalize_source_card_worker_draft(
        card_path=tmp_path / "second-x-post.md",
        content=draft,
        prefetched_x_posts=[first, second],
        prefetched_github_repositories=[],
    )

    assert "- url: https://x.com/i/status/2222222222222222222" in finalized
    assert "- owner/name: second_author/x-2222222222222222222" in finalized
    assert "1111111111111111111" not in finalized


def test_worker_draft_finalizer_rejects_quoted_todo_without_mutating_evidence(
    tmp_path,
):
    from gateway.run import _SourceCardLandingError, _finalize_source_card_worker_draft

    quoted_evidence = "> Upstream says: TODO: add a retry boundary."
    draft = _REPLAY_CARD.rstrip() + "\n\n" + quoted_evidence + "\n"

    with pytest.raises(
        _SourceCardLandingError,
        match="retained a forbidden template placeholder",
    ):
        _finalize_source_card_worker_draft(
            card_path=tmp_path / "quoted-evidence.md",
            content=draft,
            prefetched_x_posts=[],
            prefetched_github_repositories=[],
        )

    assert quoted_evidence in draft


def test_worker_draft_path_rejects_a_lexical_parent_traversal(tmp_path):
    from gateway.run import _SourceCardLandingError, _source_card_candidate_path

    cards_root = tmp_path / "cards"
    (cards_root / "nested").mkdir(parents=True)
    disguised = str(cards_root / "nested" / ".." / "example.md")

    with pytest.raises(_SourceCardLandingError, match="directly inside"):
        _source_card_candidate_path(cards_root, disguised)


def test_new_worker_draft_lands_without_publishing_to_shared_checkout(tmp_path):
    from gateway.run import _land_source_card

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
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

    landed = _land_source_card(
        card_path=card,
        card_content=_REPLAY_CARD,
        intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
        environment=environment,
        source_message_row_id=42,
    )

    assert landed["path"] == (
        "researched-repos/igorwarzocha-howaboua-pi-stuff.md"
    )
    assert not card.exists()
    remote_tip = _git(
        fixture["repo"], "ls-remote", "origin", "refs/heads/main"
    ).split()[0]
    _git(fixture["repo"], "fetch", "origin", "main")
    assert _git(
        fixture["repo"],
        "show",
        f"{remote_tip}:researched-repos/igorwarzocha-howaboua-pi-stuff.md",
    ) + "\n" == _REPLAY_CARD


def test_rejected_worker_draft_never_reaches_shared_checkout_or_remote(tmp_path):
    from gateway.run import _SourceCardLandingError, _land_source_card

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    invalid = _REPLAY_CARD.replace(
        "- current pinned SHA: 8d63d300597488e6fa4c30ccd6a3eb0fed2d4304",
        "- current pinned SHA: TODO: verify",
        1,
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
    remote_before = _git(
        fixture["repo"], "ls-remote", "origin", "refs/heads/main"
    ).split()[0]

    with pytest.raises(_SourceCardLandingError, match="validate"):
        _land_source_card(
            card_path=card,
            card_content=invalid,
            intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
            environment=environment,
            source_message_row_id=42,
        )

    assert not card.exists()
    assert _git(
        fixture["repo"], "ls-remote", "origin", "refs/heads/main"
    ).split()[0] == remote_before
    assert not fixture["decision_log"].exists()


def test_landing_error_redacts_validator_temporary_paths():
    from gateway.run import _source_card_safe_landing_detail

    detail = (
        "ERROR /private/var/folders/x1/example/T/"
        "touched-source-cards.rqWHRQ/alibaba-opensandbox-claim.md: "
        "placeholder value remains"
    )

    safe = _source_card_safe_landing_detail(detail)

    assert "/private/" not in safe
    assert "touched-source-cards.rqWHRQ" not in safe
    assert "[temporary card validation]" in safe


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
    _git(fixture["repo"], "fetch", "origin", "main")
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


def test_landing_uses_isolated_origin_main_when_shared_checkout_is_behind_and_dirty(
    tmp_path,
):
    from gateway.run import _land_source_card

    fixture = _write_offline_route_fixture(tmp_path)
    repo = fixture["repo"]
    original_head = _git(repo, "rev-parse", "HEAD")

    remote_peer = tmp_path / "remote-peer"
    subprocess.run(
        [
            "git",
            "clone",
            "--branch",
            "main",
            str(fixture["remote"]),
            str(remote_peer),
        ],
        check=True,
        capture_output=True,
    )
    _git(remote_peer, "config", "user.name", "Concurrent Source Writer")
    _git(remote_peer, "config", "user.email", "source-writer@example.invalid")
    (remote_peer / "remote-only.txt").write_text(
        "remote main advanced\n",
        encoding="utf-8",
    )
    _git(remote_peer, "add", "remote-only.txt")
    _git(remote_peer, "commit", "-m", "test: advance remote main")
    _git(remote_peer, "push", "origin", "main")
    remote_advance = _git(remote_peer, "rev-parse", "HEAD")

    unrelated = fixture["cards_root"] / "operator-owned-pending.md"
    unrelated.write_text("operator-owned pending card\n", encoding="utf-8")
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(_REPLAY_CARD, encoding="utf-8")
    _write_executable(
        fixture["decision_writer"],
        f"""
        #!/usr/bin/env python3
        import json
        import pathlib
        import subprocess
        import sys

        args = sys.argv[1:]
        if args[0] == "receipt-intake":
            cards_root = pathlib.Path(args[args.index("--cards-root") + 1])
            commit = args[args.index("--commit") + 1]
            resolved = subprocess.run(
                ["git", "-C", str(cards_root), "rev-parse", "--verify", f"{{commit}}^{{{{commit}}}}"],
                check=False,
                capture_output=True,
                text=True,
            )
            if resolved.returncode:
                print("receipt commit is absent from cards repository", file=sys.stderr)
                raise SystemExit(12)
        with pathlib.Path({str(fixture["decision_log"])!r}).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(args) + "\\n")
        print(json.dumps({{"ok": True, "command": args[0]}}))
        """,
    )
    environment = {
        "cards_root": str(fixture["cards_root"]),
        "source_card_validator": str(
            repo / "scripts" / "validate-touched-source-cards"
        ),
        "decision_writer": str(fixture["decision_writer"]),
        "source_chat_id": "-5551733823",
        "source_thread_id": "",
        "parent_session_id": "sess-dedup",
        "platform_message_id": "msg-source-42",
        "transcript_db": str(fixture["home"] / "state.db"),
    }

    landed = _land_source_card(
        card_path=card,
        intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
        environment=environment,
        source_message_row_id=42,
    )

    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert unrelated.read_text(encoding="utf-8") == "operator-owned pending card\n"
    assert card.read_text(encoding="utf-8") == _REPLAY_CARD
    remote_tip = _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]
    assert landed["commit"] == remote_tip
    _git(repo, "fetch", "origin", "main")
    assert _git(repo, "merge-base", "--is-ancestor", remote_advance, "origin/main") == ""
    assert _git(repo, "show", "origin/main:remote-only.txt") == "remote main advanced"
    assert (
        _git(
            repo,
            "show",
            "origin/main:researched-repos/igorwarzocha-howaboua-pi-stuff.md",
        )
        + "\n"
        == _REPLAY_CARD
    )
    assert fixture["decision_log"].exists()


def test_landing_receipts_use_the_immutable_landed_card_after_shared_edit(
    tmp_path, monkeypatch
):
    import gateway.run as run_module

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(_REPLAY_CARD, encoding="utf-8")
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
    original_builder = run_module._source_card_receipt_commands
    parsed_paths = []

    def edit_shared_card_then_build(**kwargs):
        card.write_text(
            card.read_text(encoding="utf-8").replace(
                "IgorWarzocha/howaboua-pi-stuff", "operator/changed-after-push"
            ),
            encoding="utf-8",
        )
        parsed_paths.append(kwargs["card_path"])
        return original_builder(**kwargs)

    monkeypatch.setattr(
        run_module, "_source_card_receipt_commands", edit_shared_card_then_build
    )

    landed = run_module._land_source_card(
        card_path=card,
        intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
        environment=environment,
        source_message_row_id=42,
    )

    assert parsed_paths and parsed_paths[0] != card
    assert "operator/changed-after-push" in card.read_text(encoding="utf-8")
    receipt_log = fixture["decision_log"].read_text(encoding="utf-8")
    assert "operator/changed-after-push" not in receipt_log
    remote_tip = _git(
        fixture["repo"], "ls-remote", "origin", "refs/heads/main"
    ).split()[0]
    assert landed["commit"] == remote_tip


def test_landing_recovers_when_push_succeeds_but_reports_a_timeout(
    tmp_path, monkeypatch
):
    import gateway.run as run_module

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(_REPLAY_CARD, encoding="utf-8")
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
    original_run_step = run_module._source_card_run_step

    def accepted_then_timeout(step, arguments, **kwargs):
        result = original_run_step(step, arguments, **kwargs)
        if step == "git_push":
            raise run_module._SourceCardLandingError(
                "git_push", "timeout after 120 seconds"
            )
        return result

    monkeypatch.setattr(run_module, "_source_card_run_step", accepted_then_timeout)

    landed = run_module._land_source_card(
        card_path=card,
        intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
        environment=environment,
        source_message_row_id=42,
    )

    remote_tip = _git(
        fixture["repo"], "ls-remote", "origin", "refs/heads/main"
    ).split()[0]
    assert landed["commit"] == remote_tip
    assert fixture["decision_log"].exists()


def test_landing_reports_unknown_when_push_and_remote_verification_both_fail(
    tmp_path, monkeypatch
):
    import gateway.run as run_module

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(_REPLAY_CARD, encoding="utf-8")
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
    original_run_step = run_module._source_card_run_step

    def fail_push_and_probe(step, arguments, **kwargs):
        if step == "git_push":
            raise run_module._SourceCardLandingError(
                "git_push",
                "exit 128: https://secret-token@example.invalid/repo.git "
                "/tmp/hermes-source-card-landing-private/repo",
            )
        if step == "git_verify":
            raise run_module._SourceCardLandingError(
                "git_verify", "timeout after 60 seconds"
            )
        return original_run_step(step, arguments, **kwargs)

    monkeypatch.setattr(run_module, "_source_card_run_step", fail_push_and_probe)

    with pytest.raises(run_module._SourceCardLandingOutcomeUnknownError) as caught:
        run_module._land_source_card(
            card_path=card,
            intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
            environment=environment,
            source_message_row_id=42,
        )

    failure = caught.value
    assert failure.path == "researched-repos/igorwarzocha-howaboua-pi-stuff.md"
    assert failure.commit
    assert "secret-token" not in str(failure)
    assert "hermes-source-card-landing" not in str(failure)
    assert not fixture["decision_log"].exists()
    summary = (
        "⚠️ Card landing outcome could not be verified: "
        f"{failure.path} @ {failure.commit}; {failure.step}: {failure.detail}"
    )
    assert run_module._format_direct_source_card_completion(
        {"status": "partial", "summary": summary, "error": str(failure)}
    ) == summary


def test_landing_reports_unknown_when_successful_push_cannot_be_verified(
    tmp_path, monkeypatch
):
    import gateway.run as run_module

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(_REPLAY_CARD, encoding="utf-8")
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
    original_run_step = run_module._source_card_run_step

    def push_then_fail_first_probe(step, arguments, **kwargs):
        if step == "git_verify":
            raise run_module._SourceCardLandingError(
                "git_verify", "timeout after 60 seconds"
            )
        return original_run_step(step, arguments, **kwargs)

    monkeypatch.setattr(
        run_module, "_source_card_run_step", push_then_fail_first_probe
    )

    with pytest.raises(run_module._SourceCardLandingOutcomeUnknownError) as caught:
        run_module._land_source_card(
            card_path=card,
            intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
            environment=environment,
            source_message_row_id=42,
        )

    failure = caught.value
    assert failure.step == "git_verify"
    assert failure.path == "researched-repos/igorwarzocha-howaboua-pi-stuff.md"
    assert failure.commit
    assert "remote verification failed" in failure.detail
    assert not fixture["decision_log"].exists()


def test_isolated_landing_rejects_multiple_push_urls(tmp_path):
    from gateway.run import (
        _SourceCardLandingError,
        _source_card_isolated_landing_repository,
    )

    fixture = _write_offline_route_fixture(tmp_path)
    second_remote = tmp_path / "second-remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(second_remote)],
        check=True,
        capture_output=True,
    )
    _git(fixture["repo"], "remote", "set-url", "--add", "--push", "origin", str(fixture["remote"]))
    _git(fixture["repo"], "remote", "set-url", "--add", "--push", "origin", str(second_remote))

    with pytest.raises(_SourceCardLandingError, match="exactly one push URL"):
        with _source_card_isolated_landing_repository(fixture["repo"]):
            pass


def test_isolated_landing_rejects_embedded_remote_credentials(
    tmp_path, monkeypatch
):
    import gateway.run as run_module

    fixture = _write_offline_route_fixture(tmp_path)

    def credential_remote_only(step, arguments, **kwargs):
        if step == "git_remote":
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout="https://secret-token@example.invalid/repo.git\n",
                stderr="",
            )
        raise AssertionError("credential-bearing remote must fail before clone")

    monkeypatch.setattr(run_module, "_source_card_run_step", credential_remote_only)

    with pytest.raises(run_module._SourceCardLandingError) as caught:
        with run_module._source_card_isolated_landing_repository(fixture["repo"]):
            pass
    assert caught.value.step == "git_remote"
    assert "embedded credentials" in caught.value.detail
    assert "secret-token" not in str(caught.value)


def test_landing_rejects_a_clean_tracked_card_absent_from_origin_main(tmp_path):
    from gateway.run import _SourceCardLandingError, _land_source_card

    fixture = _write_offline_route_fixture(tmp_path)
    repo = fixture["repo"]
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(_REPLAY_CARD, encoding="utf-8")
    _git(repo, "add", "--", "researched-repos/igorwarzocha-howaboua-pi-stuff.md")
    _git(repo, "commit", "-m", "test: preserve local-only tracked card")
    local_commit = _git(repo, "rev-parse", "HEAD")
    remote_before = _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]
    environment = {
        "cards_root": str(fixture["cards_root"]),
        "source_card_validator": str(
            repo / "scripts" / "validate-touched-source-cards"
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
        match="tracked card is absent from origin/main",
    ) as caught:
        _land_source_card(
            card_path=card,
            intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
            environment=environment,
            source_message_row_id=42,
        )

    assert caught.value.step == "git_clean"
    assert _git(repo, "rev-parse", "HEAD") == local_commit
    assert _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == remote_before
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
        # A healthy provider reports back the model it was asked for. The
        # attestation gate fails the landing closed when it does not, so this
        # must match the model the runtime resolver hands the worker.
        model="openai/gpt-4o-mini",
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
    assert worker_result["worker_system_bytes"] == 18_787
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
    relevance_rule = (
        "`direct`, `adjacent`, or `upgrade-candidate` Hermes relevance requires "
        "the bare `hermes` token in downstream learning targets; a `none:` "
        "downstream target requires `none:` Hermes relevance."
    )
    assert relevance_rule in wire_goal
    assert relevance_rule in system_text
    assert worker_result["worker_result_chars"] == len(provider_content)
    assert worker_result["worker_result_bytes"] == len(
        provider_content.encode("utf-8")
    )

    remote_tip = _git(fixture["repo"], "ls-remote", "origin", "refs/heads/main").split()[0]
    _git(fixture["repo"], "fetch", "origin", "main")
    committed = _git(
        fixture["repo"],
        "show",
        f"{remote_tip}:researched-repos/igorwarzocha-howaboua-pi-stuff.md",
    )
    assert committed + "\n" == _REPLAY_CARD
    assert not (
        fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    ).exists()
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


# --- deterministic routing-field rendering (typed grammar, every landing path) ---
#
# The validator's routing grammar is closed: `hermes relevance` is `direct`,
# `adjacent`, `upgrade-candidate`, or `none: <reason>`; every downstream target
# is a `[a-z0-9][a-z0-9-]*` slug unless the whole value is `none: <reason>`.
# The previous normalizer was separator-shaped and only stripped prose after
# `:`, ` - `, or `. `, so every other phrasing reached the validator intact and
# failed a live intake.


@pytest.mark.parametrize(
    "raw, expected",
    [
        # shapes the separator-shaped normalizer already handled
        ("adjacent: the post claims…", "adjacent"),
        ("direct — extends the gateway", "direct"),
        ("upgrade-candidate. newer than ours", "upgrade-candidate"),
        # shapes it missed, each one a live failure class
        ("adjacent because the post overlaps Hermes", "adjacent"),
        ("adjacent (overlaps Hermes agent concepts)", "adjacent"),
        ("Adjacent, the post overlaps Hermes", "adjacent"),
        ("direct; extends the gateway route", "direct"),
        # already-valid values pass through untouched
        ("adjacent", "adjacent"),
        ("none: unrelated to any Hermes surface", "none: unrelated to any Hermes surface"),
    ],
)
def test_hermes_relevance_renders_to_the_validator_grammar(raw, expected):
    from gateway.run import _source_card_render_routing_fields

    relevance, _targets = _source_card_render_routing_fields(raw, "hermes")
    assert relevance == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("hermes: compare browser tooling", "hermes"),
        # `hermes-agent` is a valid slug but is not the bare `hermes` target the
        # cross-field rule requires for an enum relevance, so both survive.
        ("hermes-agent: compare browser tooling", "hermes, hermes-agent"),
        ("hermes, codex-cli", "hermes, codex-cli"),
        ("hermes (browser tooling), codex-cli", "hermes, codex-cli"),
        ("Hermes — browser tooling", "hermes"),
        ("none: nothing downstream consumes this", "none: nothing downstream consumes this"),
    ],
)
def test_downstream_targets_render_to_slugs(raw, expected):
    from gateway.run import _source_card_render_routing_fields

    # `none:` relevance is used for the `none:` target case so the cross-field
    # rule does not inject `hermes` into the expectation.
    relevance = "none: n/a" if raw.startswith("none:") else "adjacent"
    _relevance, targets = _source_card_render_routing_fields(relevance, raw)
    assert targets == expected


def test_enum_relevance_forces_the_bare_hermes_target():
    """`adjacent` without `hermes` downstream is the exact 04:08 UTC failure."""
    from gateway.run import _source_card_render_routing_fields

    relevance, targets = _source_card_render_routing_fields(
        "adjacent", "hermes-agent, codex-cli"
    )
    assert relevance == "adjacent"
    assert targets.split(", ")[0] == "hermes"
    assert "hermes" in [item.strip() for item in targets.split(",")]


def test_none_relevance_removes_the_hermes_target():
    from gateway.run import _source_card_render_routing_fields

    relevance, targets = _source_card_render_routing_fields(
        "none: unrelated to any Hermes surface", "hermes, codex-cli"
    )
    assert relevance == "none: unrelated to any Hermes surface"
    assert "hermes" not in [item.strip() for item in targets.split(",")]
    assert targets == "codex-cli"


def test_none_relevance_with_only_hermes_target_renders_none_targets():
    from gateway.run import _source_card_render_routing_fields

    _relevance, targets = _source_card_render_routing_fields(
        "none: unrelated to any Hermes surface", "hermes"
    )
    assert targets.startswith("none:")
    assert targets.strip() != "none:"


def test_unresolvable_relevance_is_left_for_the_validator_not_guessed():
    """Rendering never invents a routing decision it cannot read."""
    from gateway.run import _source_card_render_routing_fields

    relevance, _targets = _source_card_render_routing_fields(
        "probably worth a look someday", "hermes"
    )
    assert relevance == "probably worth a look someday"


# --- rendering runs on EVERY landing path, including the duplicate path -------
#
# When the scoped duplicate lookup matches, the route lands the pre-existing
# file with no model turn: `_build_worker`, `run_conversation`,
# `_parse_source_card_worker_draft` and `_finalize_source_card_worker_draft` are
# all gated behind `if card_path is None:`. A finalizer-only fix therefore never
# runs for a duplicate, and a repair call cannot help because there is no model
# output to repair.


def _routing_environment(fixture):
    return {
        "cards_root": str(fixture["cards_root"]),
        "source_card_validator": str(
            fixture["repo"] / "scripts" / "validate-touched-source-cards"
        ),
        "decision_writer": str(fixture["decision_writer"]),
        "source_chat_id": "-5551733823",
        "source_thread_id": "",
        "parent_session_id": "sess-dup",
        "platform_message_id": "msg-source-77",
        "transcript_db": str(fixture["home"] / "state.db"),
    }


def test_duplicate_landing_path_renders_untracked_card_routing(tmp_path):
    """An untracked card written by an earlier run still gets rendered."""
    from gateway.run import _land_source_card

    fixture = _write_offline_route_fixture(tmp_path)
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    # Exactly the prose shape that the separator-shaped normalizer missed.
    prose = "- downstream learning targets: hermes: compare the claimed pi tooling"
    card.write_text(
        _REPLAY_CARD.replace("- downstream learning targets: hermes", prose),
        encoding="utf-8",
    )
    assert prose in card.read_text(encoding="utf-8")

    landed = _land_source_card(
        card_path=card,
        intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
        environment=_routing_environment(fixture),
        source_message_row_id=77,
    )

    _git(fixture["repo"], "fetch", "origin", "main")
    committed = _git(
        fixture["repo"],
        "show",
        f"{landed['commit']}:researched-repos/igorwarzocha-howaboua-pi-stuff.md",
    )
    assert "- downstream learning targets: hermes\n" in committed + "\n"
    assert "compare the claimed pi tooling" not in committed
    # The shared checkout is never rewritten by landing.
    assert prose in card.read_text(encoding="utf-8")


def test_duplicate_landing_path_fails_closed_on_a_tracked_nonconforming_card(
    tmp_path,
):
    """A committed card that renders differently is an operator conflict."""
    from gateway.run import _land_source_card, _SourceCardLandingError

    fixture = _write_offline_route_fixture(tmp_path)
    repo = fixture["repo"]
    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(
        _REPLAY_CARD.replace(
            "- downstream learning targets: hermes",
            "- downstream learning targets: hermes (compare the claimed pi tooling)",
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "researched-repos/igorwarzocha-howaboua-pi-stuff.md")
    _git(repo, "commit", "-m", "test: land a non-conforming card")
    _git(repo, "push", "origin", "main")

    with pytest.raises(_SourceCardLandingError) as excinfo:
        _land_source_card(
            card_path=card,
            intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
            environment=_routing_environment(fixture),
            source_message_row_id=77,
        )
    assert excinfo.value.step == "validate"
    assert "routing" in excinfo.value.detail


# --- typed analysis payload --------------------------------------------------
#
# Rendering salvages a readable leading token from prose. A value with no
# readable token (`probably worth a look someday`) cannot be salvaged, so the
# worker is asked for the routing decision as typed data instead of as prose
# inside the card body. The typed value wins over whatever the body says.


def test_typed_analysis_overrides_routing_prose_in_the_card_body(tmp_path):
    from gateway.run import _parse_source_card_worker_draft

    cards_root = tmp_path / "researched-repos"
    cards_root.mkdir()
    body = _REPLAY_CARD.replace(
        "- downstream learning targets: hermes",
        "- downstream learning targets: probably worth a look someday",
    )
    response = json.dumps(
        {
            "card_path": str(cards_root / "typed-analysis-card.md"),
            "card_content": body,
            "analysis": {
                "hermes_relevance": "adjacent",
                "downstream_learning_targets": ["hermes", "codex-cli"],
            },
        }
    )

    _path, content = _parse_source_card_worker_draft(response, cards_root)
    assert "- downstream learning targets: hermes, codex-cli\n" in content + "\n"
    assert "probably worth a look someday" not in content


def test_typed_analysis_is_optional_and_absent_payloads_still_parse(tmp_path):
    from gateway.run import _parse_source_card_worker_draft

    cards_root = tmp_path / "researched-repos"
    cards_root.mkdir()
    response = json.dumps(
        {
            "card_path": str(cards_root / "untyped-card.md"),
            "card_content": _REPLAY_CARD,
        }
    )
    _path, content = _parse_source_card_worker_draft(response, cards_root)
    assert "- downstream learning targets: hermes" in content


@pytest.mark.parametrize(
    "analysis",
    [
        {"hermes_relevance": "maybe", "downstream_learning_targets": ["hermes"]},
        {"hermes_relevance": "adjacent", "downstream_learning_targets": "hermes"},
        {"hermes_relevance": "adjacent", "downstream_learning_targets": ["Hermes!"]},
        {"hermes_relevance": "none:", "downstream_learning_targets": ["hermes"]},
        {"downstream_learning_targets": ["hermes"]},
    ],
)
def test_invalid_typed_analysis_is_rejected_not_guessed(tmp_path, analysis):
    from gateway.run import _parse_source_card_worker_draft, _SourceCardLandingError

    cards_root = tmp_path / "researched-repos"
    cards_root.mkdir()
    response = json.dumps(
        {
            "card_path": str(cards_root / "bad-typed-card.md"),
            "card_content": _REPLAY_CARD,
            "analysis": analysis,
        }
    )
    with pytest.raises(_SourceCardLandingError) as excinfo:
        _parse_source_card_worker_draft(response, cards_root)
    assert excinfo.value.step == "worker_output"


def test_finalizer_substitutes_embedded_todo_values(tmp_path):
    """An embedded `TODO:` self-heals instead of failing the whole intake.

    The finalizer only substituted values that START with `TODO:`, so
    `no advisories found - TODO: verify` fell through to the global
    fail-closed guard and cost the user the whole card.
    """
    from gateway.run import _finalize_source_card_worker_draft

    content = _REPLAY_CARD.replace(
        "- risk signal:",
        "- risk signal: no evidence-backed blocker - TODO: verify\n- spare-field:",
        1,
    )
    finalized = _finalize_source_card_worker_draft(
        card_path=tmp_path / "embedded-todo-card.md",
        content=content,
        prefetched_x_posts=[],
        prefetched_github_repositories=[],
    )
    assert "TODO:" not in finalized
    assert "- risk signal: " in finalized


# --- worker model pin --------------------------------------------------------
#
# `_resolve_session_agent_runtime` is a five-layer stack in which the config
# model is the LOWEST input: a session `/model` carrying an api_key returns
# early and discards it, `_resolve_runtime_agent_kwargs()` displaces it with
# `runtime_model` with no session or channel override involved, a channel
# override replaces it again, and `_apply_session_model_override` re-applies on
# top. Pinning `config.yaml` therefore does NOT pin this route. The pin is read
# from `auxiliary.source_card_worker`, the same per-role mechanism the config
# already uses for vision, web_extract, compression and the rest.


def test_source_card_worker_pin_is_read_from_auxiliary_config():
    from gateway.run import _source_card_worker_pin

    pin = _source_card_worker_pin(
        {
            "auxiliary": {
                "source_card_worker": {"provider": "zai", "model": "glm-5.3"},
                "vision": {"provider": "openai-codex", "model": "gpt-5.6-terra"},
            }
        }
    )
    assert pin == {"provider": "zai", "model": "glm-5.3"}


def test_source_card_worker_pin_absent_returns_none():
    from gateway.run import _source_card_worker_pin

    assert _source_card_worker_pin({"auxiliary": {"vision": {"model": "x"}}}) is None
    assert _source_card_worker_pin({}) is None


@pytest.mark.parametrize(
    "block",
    [
        {"provider": "zai"},
        {"model": "glm-5.3"},
        {"provider": "", "model": "glm-5.3"},
        {"provider": "zai", "model": ""},
        "glm-5.3",
    ],
)
def test_incomplete_source_card_worker_pin_fails_closed(block):
    """A half-written pin must not silently fall back to the session model."""
    from gateway.run import _source_card_worker_pin

    with pytest.raises(RuntimeError) as excinfo:
        _source_card_worker_pin({"auxiliary": {"source_card_worker": block}})
    assert "source_card_worker" in str(excinfo.value)


def test_worker_pin_overrides_the_session_resolved_model(monkeypatch):
    """The pin outranks whatever the five-layer session stack produced."""
    import gateway.run as gateway_run
    from gateway.run import _apply_source_card_worker_pin

    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        lambda provider: {"api_key": f"key-for-{provider}", "provider": provider},
    )
    model, runtime, pin = _apply_source_card_worker_pin(
        "session-override-model",
        {"api_key": "session-key", "provider": "openrouter"},
        {"auxiliary": {"source_card_worker": {"provider": "zai", "model": "glm-5.3"}}},
    )
    assert model == "glm-5.3"
    assert runtime["provider"] == "zai"
    assert runtime["api_key"] == "key-for-zai"
    assert pin == {"provider": "zai", "model": "glm-5.3"}


def test_absent_worker_pin_leaves_the_session_runtime_untouched():
    from gateway.run import _apply_source_card_worker_pin

    runtime = {"api_key": "session-key", "provider": "openrouter"}
    model, resolved, pin = _apply_source_card_worker_pin(
        "session-model", runtime, {"agent": {}}
    )
    assert (model, resolved, pin) == ("session-model", runtime, None)


# --- served-model attestation gate -------------------------------------------
#
# Four gaps had to close before "fail closed on mismatch" meant anything:
#   (a) the predicate is judged per call against the route AS OF that call,
#       because `agent.model` is legitimately reassigned on a provider switch;
#   (b) the duplicate short-circuit runs NO turn at all, so it must be exempt
#       or every duplicate fails closed;
#   (c) the success and error paths disagreed about which model to report, and
#       the error path (post-fallback) was the accurate one;
#   (d) a receipt is a SET, not a scalar - live rows show 16-20 calls per turn.


def test_attestation_gate_passes_a_fully_attested_turn():
    from gateway.run import _source_card_attestation_error

    assert (
        _source_card_attestation_error(
            [
                {"call": 1, "requested": "glm-5.3", "served": "glm-5.3"},
                {"call": 2, "requested": "glm-5.3", "served": "zai/glm-5.3"},
            ],
            ran_turn=True,
        )
        is None
    )


def test_attestation_gate_fails_closed_on_a_substitution():
    from gateway.run import _source_card_attestation_error

    error = _source_card_attestation_error(
        [{"call": 1, "requested": "glm-5.2", "served": "glm-5.3"}],
        ran_turn=True,
    )
    assert error is not None
    assert error.startswith("source_card_model_attestation_failed:")
    assert "glm-5.2" in error and "glm-5.3" in error


def test_attestation_gate_fails_closed_when_nothing_was_attested():
    """A turn that ran but reported no served model is unattested, not clean."""
    from gateway.run import _source_card_attestation_error

    assert _source_card_attestation_error([], ran_turn=True) is not None
    assert (
        _source_card_attestation_error(
            [{"call": 1, "requested": "glm-5.3", "served": None}], ran_turn=True
        )
        is not None
    )


def test_attestation_gate_exempts_the_duplicate_path():
    """The duplicate short-circuit runs no model turn, so there is nothing to attest."""
    from gateway.run import _source_card_attestation_error

    assert _source_card_attestation_error([], ran_turn=False) is None


def test_attestation_gate_reports_every_mismatched_call_not_just_the_last():
    from gateway.run import _source_card_attestation_error

    error = _source_card_attestation_error(
        [
            {"call": 1, "requested": "glm-5.3", "served": "glm-5.3"},
            {"call": 2, "requested": "glm-5.3", "served": "glm-5.4"},
            {"call": 3, "requested": "glm-5.3", "served": None},
        ],
        ran_turn=True,
    )
    assert "call 2" in error and "call 3" in error
    assert "call 1" not in error


def test_attestation_receipt_flows_from_the_real_conversation_loop():
    """The loop records what the provider said, with no lifecycle hook present.

    The only pre-existing read of `response.model` sat behind `has_hook`, so
    with no hook registered nothing carried the served model out of the loop.
    """
    from types import SimpleNamespace as NS

    from agent.served_model import (
        record_served_model,
        reset_served_models,
        served_model_receipt,
    )

    agent = NS(model="glm-5.3")
    reset_served_models(agent)
    for served in ("glm-5.3", "zai/glm-5.3", "glm-5.4"):
        record_served_model(agent, requested=agent.model, response=NS(model=served))

    receipt = served_model_receipt(agent)
    assert len(receipt) == 3
    from gateway.run import _source_card_attestation_error

    error = _source_card_attestation_error(receipt, ran_turn=True)
    assert error is not None
    assert "call 3" in error and "glm-5.4" in error
    assert "call 1" not in error and "call 2" not in error


# --- authenticated push rejection --------------------------------------------
#
# Landing does one bare `git push origin HEAD:refs/heads/main` and treats ANY
# nonzero as a landing failure. The starred-repo drain pushes card commits to
# the same branch every ~5 minutes (StartInterval 300, RunAtLoad true), so a
# push landing in the clone->validate->commit->push window is rejected.
#
# The distinction that matters is the one `source-card-commit-push:471-489`
# already encodes: an AUTHENTICATED rejection for this exact destination is
# safe to retry, while a generic nonzero may mean the server accepted the push
# before the client lost its acknowledgement, and retrying that could duplicate
# work. Only the parser is reused; the helper itself is not a drop-in (it takes
# a GLOBAL checkout mutex and presumes a staged card plus todo.md).


@pytest.mark.parametrize(
    "porcelain",
    [
        "!\trefs/heads/main:refs/heads/main\t[rejected] (fetch first)",
        "!\trefs/heads/main:refs/heads/main\t[rejected] (stale info)",
        "!\trefs/heads/main:refs/heads/main\t[rejected] (non-fast-forward)",
    ],
)
def test_authenticated_rejection_is_recognised(porcelain):
    from gateway.run import _source_card_push_rejected_authenticated

    assert _source_card_push_rejected_authenticated(porcelain, "refs/heads/main") is True


@pytest.mark.parametrize(
    "porcelain",
    [
        "",
        "fatal: could not readから remote",
        # a different destination must not authorize a retry
        "!\trefs/heads/other:refs/heads/other\t[rejected] (fetch first)",
        # a rejection for a different reason is not the concurrent-writer case
        "!\trefs/heads/main:refs/heads/main\t[rejected] (permission denied)",
        # more than one status line is ambiguous
        (
            "!\trefs/heads/main:refs/heads/main\t[rejected] (fetch first)\n"
            "!\trefs/heads/main:refs/heads/main\t[rejected] (stale info)"
        ),
        # a success line is not a rejection
        "\trefs/heads/main:refs/heads/main\t0000000..1111111",
    ],
)
def test_ambiguous_or_foreign_push_output_is_not_an_authenticated_rejection(porcelain):
    from gateway.run import _source_card_push_rejected_authenticated

    assert _source_card_push_rejected_authenticated(porcelain, "refs/heads/main") is False


def test_landing_survives_a_concurrent_writer_advancing_origin_main(tmp_path):
    """The drain pushes to this branch every ~5 minutes; landing must survive it.

    A second clone advances `origin/main` after this landing has cloned, so the
    push is rejected exactly the way a real concurrent writer rejects it. The
    card must still land, the concurrent writer's commit must survive, and the
    shared checkout must be untouched.
    """
    from gateway.run import _land_source_card

    fixture = _write_offline_route_fixture(tmp_path)
    repo = fixture["repo"]
    original_head = _git(repo, "rev-parse", "HEAD")

    card = fixture["cards_root"] / "igorwarzocha-howaboua-pi-stuff.md"
    card.write_text(_REPLAY_CARD, encoding="utf-8")
    unrelated = fixture["cards_root"] / "operator-owned-pending.md"
    unrelated.write_text("operator-owned pending card\n", encoding="utf-8")

    import gateway.run as gateway_run

    real_clone_cm = gateway_run._source_card_isolated_landing_repository
    advanced = {}

    from contextlib import contextmanager

    @contextmanager
    def _clone_then_advance_remote(repository):
        with real_clone_cm(repository) as checkout:
            # Advance origin/main only AFTER the isolated clone exists, so the
            # push this landing is about to make is genuinely stale.
            if not advanced:
                peer = tmp_path / "concurrent-peer"
                subprocess.run(
                    ["git", "clone", "--branch", "main", str(fixture["remote"]), str(peer)],
                    check=True,
                    capture_output=True,
                )
                _git(peer, "config", "user.name", "Concurrent Source Writer")
                _git(peer, "config", "user.email", "concurrent@example.invalid")
                (peer / "concurrent-writer.txt").write_text("drain\n", encoding="utf-8")
                _git(peer, "add", "concurrent-writer.txt")
                _git(peer, "commit", "-m", "drain: concurrent card commit")
                _git(peer, "push", "origin", "main")
                advanced["sha"] = _git(peer, "rev-parse", "HEAD")
            yield checkout

    gateway_run._source_card_isolated_landing_repository = _clone_then_advance_remote
    try:
        landed = _land_source_card(
            card_path=card,
            intake_text=f"https://x.com/i/status/{_REPLAY_X_STATUS_ID}",
            environment=_routing_environment(fixture),
            source_message_row_id=77,
        )
    finally:
        gateway_run._source_card_isolated_landing_repository = real_clone_cm

    _git(repo, "fetch", "origin", "main")
    remote_tip = _git(repo, "ls-remote", "origin", "refs/heads/main").split()[0]
    assert landed["commit"] == remote_tip
    # The concurrent writer's commit is still contained.
    assert (
        _git(repo, "merge-base", "--is-ancestor", advanced["sha"], "origin/main") == ""
    )
    assert _git(repo, "show", "origin/main:concurrent-writer.txt") == "drain"
    # The card landed.
    assert (
        _git(repo, "show", f"origin/main:{landed['path']}") + "\n" == _REPLAY_CARD
    )
    # The shared checkout is untouched.
    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert unrelated.read_text(encoding="utf-8") == "operator-owned pending card\n"
