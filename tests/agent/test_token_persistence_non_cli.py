from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import sys

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


def _mock_response(*, usage: dict, content: str = "done"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        model="test/model",
        usage=SimpleNamespace(**usage),
    )


def _make_agent(session_db, *, platform: str):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id=f"{platform}-session",
            platform=platform,
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
    )
    return agent


def test_run_conversation_persists_tokens_for_telegram_sessions():
    session_db = MagicMock()
    agent = _make_agent(session_db, platform="telegram")

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    # Per-call deltas are enqueued for the SessionDB background writer
    # (queue_token_counts) rather than written inline on the turn thread.
    session_db.queue_token_counts.assert_called_once()
    assert session_db.queue_token_counts.call_args.args[0] == "telegram-session"




def test_session_search_lazily_opens_db_when_entrypoint_did_not_pass_one(monkeypatch):
    sentinel_db = object()
    captured = {}

    class FakeSessionDB:
        def __new__(cls):
            return sentinel_db

    hermes_state = ModuleType("hermes_state")
    hermes_state.SessionDB = FakeSessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", hermes_state)
    hermes_state_registry = ModuleType("hermes_state_registry")
    hermes_state_registry.acquire = lambda db_path=None: sentinel_db
    monkeypatch.setitem(sys.modules, "hermes_state_registry", hermes_state_registry)

    session_search_mod = ModuleType("tools.session_search_tool")

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "results": []})

    session_search_mod.session_search = fake_session_search
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", session_search_mod)

    agent = _make_agent(None, platform="acp")
    result = json.loads(agent._invoke_tool(
        "session_search",
        {"query": "Hermes", "detail": "full"},
        "task-id",
    ))

    assert result["success"] is True
    assert captured["db"] is sentinel_db
    assert captured["query"] == "Hermes"
    assert captured["detail"] == "full"
    assert agent._session_db is sentinel_db


def test_sequential_session_search_forwards_detail(monkeypatch):
    session_db = MagicMock()
    captured = {}

    session_search_mod = ModuleType("tools.session_search_tool")

    def fake_session_search(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "results": []})

    session_search_mod.session_search = fake_session_search
    monkeypatch.setitem(sys.modules, "tools.session_search_tool", session_search_mod)

    agent = _make_agent(session_db, platform="acp")
    tool_call = SimpleNamespace(
        id="search-1",
        function=SimpleNamespace(
            name="session_search",
            arguments=json.dumps({"query": "Hermes", "detail": "full"}),
        ),
    )
    assistant_message = SimpleNamespace(tool_calls=[tool_call])
    messages = []

    agent._execute_tool_calls_sequential(
        assistant_message,
        messages,
        "task-id",
    )

    assert captured["db"] is session_db
    assert captured["query"] == "Hermes"
    assert captured["detail"] == "full"


@pytest.fixture
def isolated_session_search_profiles(tmp_path, monkeypatch):
    """Two disposable profile homes with distinct searchable sessions."""
    root = tmp_path / "hermes"
    other_home = root / "profiles" / "other"
    root.mkdir()
    other_home.mkdir(parents=True)

    default_db = SessionDB(root / "state.db")
    other_db = SessionDB(other_home / "state.db")
    try:
        default_db.create_session("default-only", source="cli")
        default_db.append_message(
            "default-only", role="user", content="sessionprofile routingprobe default"
        )
        other_db.create_session("other-only", source="cli")
        other_db.append_message(
            "other-only", role="user", content="sessionprofile routingprobe other"
        )
        assert default_db._conn is not None
        assert other_db._conn is not None
        default_db._conn.commit()
        other_db._conn.commit()
    finally:
        default_db.close()
        other_db.close()

    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    return {"root": root, "other_home": other_home}


@pytest.mark.parametrize(
    ("profile", "expected_session_id"),
    [("default", "default-only"), ("other", "other-only")],
)
def test_explicit_profile_search_uses_target_when_current_recall_is_unavailable(
    isolated_session_search_profiles, profile, expected_session_id
):
    """The real dispatcher must route explicit profiles without a current DB."""
    agent = _make_agent(None, platform="acp")
    current_recall = MagicMock(return_value=None)
    agent._get_session_db_for_recall = current_recall

    result = json.loads(
        agent._invoke_tool(
            "session_search",
            {"query": "sessionprofile routingprobe", "limit": 1, "profile": profile},
            "task-id",
        )
    )

    assert current_recall.call_count == 0
    assert result["success"] is True
    assert result["results"][0]["session_id"] == expected_session_id


@pytest.mark.parametrize("profile", ["missing", "invalid/profile"])
def test_explicit_unknown_profile_fails_closed_without_current_recall_db(
    isolated_session_search_profiles, profile
):
    """A missing or malformed profile must not fall back to the current DB."""
    agent = _make_agent(None, platform="acp")
    current_recall = MagicMock(return_value=None)
    agent._get_session_db_for_recall = current_recall

    result = json.loads(
        agent._invoke_tool(
            "session_search",
            {"query": "sessionprofile routingprobe", "limit": 1, "profile": profile},
            "task-id",
        )
    )

    assert current_recall.call_count == 0
    assert result["success"] is False
    assert f"profile '{profile}'" in result["error"]


def test_hook_modified_profile_routes_through_real_dispatcher(
    isolated_session_search_profiles, monkeypatch
):
    """The dispatcher must use hook-modified arguments when routing profiles."""
    monkeypatch.setattr(
        "hermes_cli.plugins._dispatch_pre_tool_call_hooks",
        lambda _name, args, **_kwargs: (None, {**args, "profile": "other"}),
    )
    agent = _make_agent(None, platform="acp")
    current_recall = MagicMock(return_value=None)
    agent._get_session_db_for_recall = current_recall

    result = json.loads(
        agent._invoke_tool(
            "session_search",
            {"query": "sessionprofile routingprobe", "limit": 1, "profile": "default"},
            "task-id",
        )
    )

    assert current_recall.call_count == 0
    assert result["success"] is True
    assert result["results"][0]["session_id"] == "other-only"


@pytest.mark.parametrize(
    ("initial_profile", "hook_mutates", "hook_profile"),
    [
        pytest.param(None, False, None, id="null"),
        pytest.param("", False, None, id="empty"),
        pytest.param(" \t\n", False, None, id="whitespace"),
        pytest.param(False, False, None, id="false"),
        pytest.param(0, False, None, id="zero"),
        pytest.param([], False, None, id="list"),
        pytest.param({}, False, None, id="object"),
        pytest.param("other", True, "", id="hook-empty"),
        pytest.param("other", True, [], id="hook-list"),
        pytest.param("other\n", False, None, id="target-newline"),
        pytest.param("default\n", False, None, id="default-newline"),
        pytest.param("other", True, "other\n", id="hook-target-newline"),
    ],
)
def test_explicit_invalid_profile_fails_closed_before_current_recall(
    isolated_session_search_profiles,
    monkeypatch,
    initial_profile,
    hook_mutates,
    hook_profile,
):
    """Invalid supplied or hook-mutated profiles cannot use current recall."""
    if hook_mutates:
        monkeypatch.setattr(
            "hermes_cli.plugins._dispatch_pre_tool_call_hooks",
            lambda _name, args, **_kwargs: (None, {**args, "profile": hook_profile}),
        )

    current_db = SessionDB(
        isolated_session_search_profiles["other_home"] / "state.db", read_only=True
    )
    try:
        agent = _make_agent(None, platform="acp")
        current_recall = MagicMock(return_value=current_db)
        agent._get_session_db_for_recall = current_recall
        result = json.loads(
            agent._invoke_tool(
                "session_search",
                {
                    "query": "sessionprofile routingprobe",
                    "limit": 1,
                    "profile": initial_profile,
                },
                "task-id",
            )
        )
    finally:
        current_db.close()

    assert current_recall.call_count == 0
    assert result["success"] is False
    assert "valid profile identifier" in result["error"]


def test_session_search_without_profile_uses_current_recall_db(
    isolated_session_search_profiles,
):
    """An omitted profile retains the ordinary current-profile search path."""
    current_db = SessionDB(
        isolated_session_search_profiles["other_home"] / "state.db", read_only=True
    )
    try:
        agent = _make_agent(current_db, platform="acp")
        result = json.loads(
            agent._invoke_tool(
                "session_search",
                {"query": "sessionprofile routingprobe", "limit": 1},
                "task-id",
            )
        )
    finally:
        current_db.close()

    assert result["success"] is True
    assert result["results"][0]["session_id"] == "other-only"
