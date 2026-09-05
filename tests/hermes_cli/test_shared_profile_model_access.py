"""Shared model access uses live references, never copied profile secrets."""

import json

import pytest

from agent import credential_pool as cp, secret_scope
from hermes_cli import auth


@pytest.fixture
def shared_profile(tmp_path, monkeypatch):
    root = tmp_path / "installation"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n")
    (root / ".env").write_text("DEEPSEEK_API_KEY=fixture-alpha\nTELEGRAM_BOT_TOKEN=fixture-bot\n")
    (root / "auth.json").write_text(json.dumps({
        "providers": {}, "credential_pool": {"deepseek": [{
            "id": "shared-env", "source": "env:DEEPSEEK_API_KEY",
            "auth_type": "api_key", "priority": 0,
            "base_url": "https://api.deepseek.com",
        }]},
    }))
    monkeypatch.setenv("HERMES_HOME", str(root))
    from hermes_cli.profiles import create_profile
    profile = create_profile("reader", no_alias=True, no_skills=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    token = secret_scope.set_secret_scope({})
    try:
        yield root, profile
    finally:
        secret_scope.reset_secret_scope(token)


def test_new_profile_keeps_shared_access_across_reads_and_key_rotation(shared_profile, monkeypatch):
    root, profile = shared_profile
    for value in ("fixture-alpha", "fixture-rotated", ""):
        (root / ".env").write_text(f"DEEPSEEK_API_KEY={value}\n")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-parent-value")
        for _ in range(2):
            pool = cp.load_pool("deepseek")
            selected = pool.peek()
            assert (selected.runtime_api_key if selected else "") == value
            assert auth.is_provider_explicitly_configured("deepseek") is bool(value)
        if (profile / "auth.json").exists():
            payload = (profile / "auth.json").read_text()
            assert "fixture-alpha" not in payload and "fixture-rotated" not in payload
    (root / ".env").unlink()
    assert not cp.load_pool("deepseek").has_available()


@pytest.mark.parametrize("local", ["fixture-local", ""])
def test_local_assignment_including_blank_wins(shared_profile, local):
    _, profile = shared_profile
    (profile / ".env").write_text(f"DEEPSEEK_API_KEY={local}\n")
    selected = cp.load_pool("deepseek").peek()
    assert (selected.runtime_api_key if selected else "") == local


@pytest.mark.parametrize("local_state", [
    {"credential_pool": {"deepseek": [{"id": "mine", "source": "manual", "auth_type": "api_key", "access_token": "fixture-local", "priority": 0}]}},
    {"providers": {"deepseek": {"api_key": "fixture-local"}}},
    {"suppressed_sources": {"deepseek": ["env:DEEPSEEK_API_KEY"]}},
])
def test_local_auth_or_suppression_does_not_borrow_root_env(shared_profile, local_state):
    _, profile = shared_profile
    (profile / "auth.json").write_text(json.dumps({"providers": {}, **local_state}))
    assert cp.get_env_prefer_dotenv("DEEPSEEK_API_KEY", provider="deepseek") == ""
    if "credential_pool" in local_state:
        assert cp.load_pool("deepseek").peek().runtime_api_key == "fixture-local"


@pytest.mark.parametrize("where", ["env", "model", "providers"])
def test_local_endpoint_without_key_cannot_receive_root_key(shared_profile, where):
    _, profile = shared_profile
    if where == "env":
        (profile / ".env").write_text("DEEPSEEK_BASE_URL=https://other.example/v1\n")
    else:
        config = ({"model": {"provider": "deepseek", "base_url": "https://other.example/v1"}}
                  if where == "model" else {"providers": {"deepseek": {"base_url": "https://other.example/v1"}}})
        (profile / "config.yaml").write_text(json.dumps(config))
    assert not cp.load_pool("deepseek").has_available()


def test_shared_pool_endpoint_stays_paired_with_key(shared_profile):
    root, _ = shared_profile
    store = json.loads((root / "auth.json").read_text())
    store["credential_pool"]["deepseek"][0]["base_url"] = "https://owned-proxy.example/v1"
    (root / "auth.json").write_text(json.dumps(store))
    selected = cp.load_pool("deepseek").peek()
    assert selected is not None
    assert selected.runtime_api_key == "fixture-alpha"
    assert selected.runtime_base_url == "https://owned-proxy.example/v1"
    resolved = auth.resolve_api_key_provider_credentials("deepseek")
    assert (resolved["api_key"], resolved["base_url"]) == ("fixture-alpha", "https://owned-proxy.example/v1")
    from hermes_cli.runtime_provider import resolve_runtime_provider
    resolved = resolve_runtime_provider(requested="deepseek")
    assert (resolved["api_key"], resolved["base_url"]) == ("fixture-alpha", "https://owned-proxy.example/v1")


def test_profile_scopes_and_non_model_secrets_remain_isolated(shared_profile):
    root, profile = shared_profile
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    second = root / "profiles" / "second"
    second.mkdir()
    (second / ".env").write_text("DEEPSEEK_API_KEY=fixture-second\n")
    token = set_hermes_home_override(str(second))
    try:
        assert cp.load_pool("deepseek").peek().runtime_api_key == "fixture-second"
    finally:
        reset_hermes_home_override(token)
    assert cp.load_pool("deepseek").peek().runtime_api_key == "fixture-alpha"
    assert cp.get_env_prefer_dotenv("TELEGRAM_BOT_TOKEN", provider="deepseek") == ""
    token = secret_scope.set_secret_scope(None)
    try:
        with pytest.raises(secret_scope.UnscopedSecretError):
            cp.get_env_prefer_dotenv("DEEPSEEK_API_KEY", provider="deepseek")
    finally:
        secret_scope.reset_secret_scope(token)


def test_profile_catalog_discovers_root_manual_provider(shared_profile, monkeypatch):
    root, _ = shared_profile
    store = json.loads((root / "auth.json").read_text())
    store["credential_pool"]["deepseek"] = [{
        "id": "root-manual", "source": "manual", "auth_type": "api_key",
        "access_token": "fixture-manual", "base_url": "https://api.deepseek.com", "priority": 0,
    }]
    (root / "auth.json").write_text(json.dumps(store))
    (root / ".env").write_text("")
    from hermes_cli import inventory, models, providers
    from agent import models_dev
    monkeypatch.setattr(models_dev, "PROVIDER_TO_MODELS_DEV", {"deepseek": "deepseek"})
    monkeypatch.setattr(models_dev, "fetch_models_dev", lambda: {"deepseek": {"env": ["DEEPSEEK_API_KEY"], "models": {}}})
    monkeypatch.setattr(providers, "HERMES_OVERLAYS", {})
    monkeypatch.setattr(models, "_PROVIDER_MODELS", {"deepseek": ["fixture-chat"], "ollama-cloud": []})
    monkeypatch.setattr(models, "get_curated_nous_model_ids", lambda: [])
    monkeypatch.setattr(models, "cached_provider_model_ids", lambda *a, **kw: ["fixture-chat"])
    for name in ("_apply_pricing", "_apply_capabilities", "_apply_featured"):
        monkeypatch.setattr(inventory, name, lambda *a, **kw: None)
    result = inventory.build_model_options_payload(inventory.ConfigContext("", "", "", {}, []), explicit_only=True)
    assert any(p["slug"] == "deepseek" and "fixture-chat" in p["models"] for p in result["providers"])


def test_runtime_respects_blank_with_stale_process_key(shared_profile, monkeypatch):
    _, profile = shared_profile
    (profile / ".env").write_text("DEEPSEEK_API_KEY=\n")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-parent-value")
    token = secret_scope.set_secret_scope(None)
    try:
        assert auth.resolve_api_key_provider_credentials("deepseek")["api_key"] == ""
    finally:
        secret_scope.reset_secret_scope(token)


def test_same_id_local_secret_is_an_override(shared_profile):
    root, profile = shared_profile
    store = json.loads((root / "auth.json").read_text())
    store["credential_pool"]["deepseek"][0]["access_token"] = "fixture-local"
    (profile / "auth.json").write_text(json.dumps(store))
    assert cp.get_env_prefer_dotenv("DEEPSEEK_API_KEY", provider="deepseek") == ""


def test_shared_reference_readd_and_suppression(shared_profile):
    root, _ = shared_profile
    assert cp.load_pool("deepseek").peek() is not None
    store = json.loads((root / "auth.json").read_text())
    (root / "auth.json").write_text(json.dumps({"credential_pool": {}}))
    assert not cp.load_pool("deepseek").has_available()
    store["credential_pool"]["deepseek"][0]["id"] = "readded"
    (root / "auth.json").write_text(json.dumps(store))
    assert cp.load_pool("deepseek").peek().runtime_api_key == "fixture-alpha"
    store["suppressed_sources"] = {"deepseek": ["env:DEEPSEEK_API_KEY"]}
    (root / "auth.json").write_text(json.dumps(store))
    assert not cp.load_pool("deepseek").has_available()


@pytest.mark.parametrize("status", ["exhausted", "dead"])
def test_hydration_keeps_same_key_quarantined(shared_profile, status):
    from agent.credential_persistence import fingerprint_secret_value
    from datetime import datetime, timezone, timedelta
    root, _ = shared_profile
    store = json.loads((root / "auth.json").read_text())
    store["credential_pool"]["deepseek"][0].update(
        last_status=status, secret_fingerprint=fingerprint_secret_value("fixture-alpha"),
        last_status_at=datetime.now(timezone.utc).timestamp(),
        last_error_reset_at=(datetime.now(timezone.utc) + timedelta(days=1)).timestamp(),
    )
    (root / "auth.json").write_text(json.dumps(store))
    for _ in range(2):
        pool = cp.load_pool("deepseek")
        assert pool.entries()[0].last_status == status
        assert not pool.has_available()


@pytest.mark.parametrize("section", ["credential_pool", "providers", "suppressed_sources"])
def test_malformed_optional_auth_container_does_not_break_discovery(shared_profile, section):
    _, profile = shared_profile
    (profile / "auth.json").write_text(json.dumps({"providers": {}, "credential_pool": {}, section: []}))
    assert cp.get_env_prefer_dotenv("DEEPSEEK_API_KEY", provider="deepseek") == "fixture-alpha"


def test_live_session_adopts_shared_key_rotation_and_keeps_endpoint(shared_profile):
    from run_agent import AIAgent
    from unittest.mock import MagicMock
    root, _ = shared_profile
    agent = object.__new__(AIAgent)
    agent.provider = agent.requested_provider = "deepseek"
    agent.api_mode = "chat_completions"
    agent.base_url = "https://api.deepseek.com"
    agent.api_key = "fixture-alpha"
    agent._client_kwargs = {"base_url": agent.base_url, "api_key": agent.api_key}
    agent._replace_primary_openai_client = MagicMock(return_value=True)
    agent._reapply_route_client_config = MagicMock()
    assert not agent._try_refresh_env_client_credentials()
    (root / ".env").write_text("DEEPSEEK_API_KEY=fixture-rotated\n")
    assert agent._try_refresh_env_client_credentials()
    assert (agent.api_key, agent.base_url) == ("fixture-rotated", "https://api.deepseek.com")


@pytest.mark.parametrize("owner", ["root", "local"])
def test_legacy_suppression_mapping_is_respected(shared_profile, owner):
    root, profile = shared_profile
    target = root if owner == "root" else profile
    store = json.loads((target / "auth.json").read_text()) if (target / "auth.json").exists() else {}
    store.setdefault("providers", {})
    store["suppressed_sources"] = {"deepseek": {"env:DEEPSEEK_API_KEY": True}}
    (target / "auth.json").write_text(json.dumps(store))
    if owner == "local":
        (profile / ".env").write_text("DEEPSEEK_API_KEY=fixture-local\n")
    assert cp.get_env_prefer_dotenv("DEEPSEEK_API_KEY", provider="deepseek") == ""
    assert auth.resolve_api_key_provider_credentials("deepseek")["api_key"] == ""


def test_explicit_runtime_endpoint_requires_its_own_key(shared_profile):
    from hermes_cli.runtime_provider import resolve_runtime_provider
    with pytest.raises(auth.AuthError, match="shared"):
        resolve_runtime_provider(requested="deepseek", explicit_base_url="https://other.example/v1")
    resolved = resolve_runtime_provider(
        requested="deepseek", explicit_base_url="https://other.example/v1",
        explicit_api_key="fixture-explicit",
    )
    assert (resolved["api_key"], resolved["base_url"]) == ("fixture-explicit", "https://other.example/v1")
