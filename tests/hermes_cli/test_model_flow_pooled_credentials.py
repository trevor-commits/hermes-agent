"""Pool-only credentials must be visible to interactive model setup flows."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hermes_cli.auth import PROVIDER_REGISTRY
from hermes_cli.model_setup_flows import _existing_api_key_for_model_flow


class _PoolEntry:
    access_token = "pool-secret"
    runtime_api_key = ""


class _AvailablePool:
    def has_credentials(self) -> bool:
        return True

    def peek(self):
        return _PoolEntry()


class _ExhaustedPool:
    def has_credentials(self) -> bool:
        return True

    def peek(self):
        return None






def test_generic_api_key_flow_passes_pool_key_to_existing_key_prompt(monkeypatch):
    from hermes_cli.model_setup_flows import _model_flow_api_key_provider

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    captured: dict[str, str] = {}

    def capture_prompt(_pconfig, existing_key, **_kwargs):
        captured["existing_key"] = existing_key
        return existing_key, True

    with (
        patch("hermes_cli.config.get_env_value", return_value=""),
        patch("agent.credential_pool.load_pool", return_value=_AvailablePool()),
        patch("hermes_cli.main._prompt_api_key", side_effect=capture_prompt),
    ):
        _model_flow_api_key_provider({}, "deepseek")

    assert captured["existing_key"] == "pool-secret"




def test_bedrock_flow_sees_pool_key_when_no_env(monkeypatch, capsys):
    """Bedrock API-key mode must also see pool-backed credentials."""
    from hermes_cli.model_setup_flows import _model_flow_bedrock_api_key

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    with (
        patch("hermes_cli.config.get_env_value", return_value=""),
        patch("agent.credential_pool.load_pool", return_value=_AvailablePool()),
        patch("builtins.input", return_value="k"),
    ):
        _model_flow_bedrock_api_key({}, "us-east-1")

    out = capsys.readouterr().out
    # The flow should show the pool-backed key, not prompt for a new one
    assert "pool-secret" in out[:200] or "pool-sec" in out[:200]


@pytest.mark.parametrize("provider", ["openrouter", "kimi-coding", "stepfun", "bedrock", "deepseek", "gemini"])
def test_model_flow_keeps_credential_endpoint_for_probes_and_save(monkeypatch, provider):
    """A borrowed key must not be sent to a default or unrelated local URL."""
    from hermes_cli import auth, config, main, models, model_setup_flows as flows

    paired_base = "https://credential-owner.example/v1"
    if provider == "bedrock":
        paired_base = "https://bedrock-mantle.us-east-1.api.aws/v1"
    key = "fixture-borrowed-key"
    cfg = {}
    probes = []
    monkeypatch.setattr(auth, "_resolve_api_key_provider_secret", lambda *_: (key, "credential_pool:" + provider, paired_base))
    monkeypatch.setattr(main, "_prompt_api_key", lambda _pc, existing, **_: (existing, False))
    monkeypatch.setattr(main, "_prompt_provider_choice", lambda *a, **kw: 0)
    monkeypatch.setattr(config, "get_env_value", lambda *_: "https://unrelated-local.example/v1")
    monkeypatch.setattr(config, "save_env_value", lambda *a, **kw: None)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(config, "save_config", lambda value: None)
    monkeypatch.setattr(flows, "line_input", lambda *_: "")
    monkeypatch.setattr(auth, "_save_model_choice", lambda *_: None)
    monkeypatch.setattr(auth, "deactivate_provider", lambda: None)
    monkeypatch.setattr(models, "model_ids", lambda **_: ["fixture-model"])
    monkeypatch.setattr(models, "get_pricing_for_provider", lambda *a, **kw: {})
    monkeypatch.setattr("agent.models_dev.list_agentic_models", lambda *_: ["fixture-model"])

    def catalog(api_key, base_url):
        probes.append((api_key, base_url))
        return ["fixture-model"]

    def select(*args, **kwargs):
        probes.append((kwargs["confirm_api_key"], kwargs["confirm_base_url"]))
        return "fixture-model"

    def tier(api_key, base_url):
        probes.append((api_key, base_url))
        return "paid"

    monkeypatch.setattr(models, "fetch_api_models", catalog)
    monkeypatch.setattr(auth, "_prompt_model_selection", select)
    monkeypatch.setattr("agent.gemini_native_adapter.probe_gemini_tier", tier)
    if provider == "bedrock":
        flows._model_flow_bedrock_api_key(cfg, "us-east-1")
        saved_base = cfg["providers"]["bedrock-mantle"]["base_url"]
    else:
        specialized = {
            "openrouter": flows._model_flow_openrouter,
            "kimi-coding": flows._model_flow_kimi,
            "stepfun": flows._model_flow_stepfun,
        }
        if provider in specialized:
            specialized[provider](cfg)
        else:
            flows._model_flow_api_key_provider(cfg, provider)
        saved_base = cfg["model"]["base_url"]
    assert probes and all(pair == (key, paired_base) for pair in probes)
    assert len(probes) == (2 if provider in {"stepfun", "gemini"} else 1)
    assert saved_base == paired_base


@pytest.mark.parametrize("provider", ["openrouter", "kimi-coding", "deepseek", "gemini"])
def test_replacing_key_does_not_reuse_the_old_credential_endpoint(monkeypatch, provider):
    from hermes_cli import auth, config, main, models, model_setup_flows as flows
    from hermes_constants import OPENROUTER_BASE_URL

    captured = []
    monkeypatch.setattr(auth, "_resolve_api_key_provider_secret", lambda *_: ("old-key", "credential_pool:" + provider, "https://old-owner.example/v1"))
    monkeypatch.setattr(main, "_prompt_api_key", lambda *a, **kw: ("new-key", False))
    monkeypatch.setattr(config, "get_env_value", lambda *_: "")
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(flows, "line_input", lambda *_: "")
    monkeypatch.setattr(models, "model_ids", lambda **_: ["fixture-model"])
    monkeypatch.setattr(models, "get_pricing_for_provider", lambda *a, **kw: {})
    monkeypatch.setattr("agent.models_dev.list_agentic_models", lambda *_: ["fixture-model"])
    monkeypatch.setattr("agent.gemini_native_adapter.probe_gemini_tier", lambda key, base: captured.append((key, base)) or "paid")
    monkeypatch.setattr(auth, "_prompt_model_selection", lambda *a, **kw: captured.append((kw["confirm_api_key"], kw["confirm_base_url"])) or None)
    pconfig = auth.PROVIDER_REGISTRY.get(provider)
    if pconfig and pconfig.base_url_env_var:
        monkeypatch.delenv(pconfig.base_url_env_var, raising=False)
    if provider == "openrouter":
        flows._model_flow_openrouter({})
    elif provider == "kimi-coding":
        flows._model_flow_kimi({})
    else:
        flows._model_flow_api_key_provider({}, provider)
    expected = OPENROUTER_BASE_URL if provider == "openrouter" else pconfig.inference_base_url
    assert captured and all(pair == ("new-key", expected) for pair in captured)


@pytest.mark.parametrize("provider", ["deepseek", "zai", "stepfun", "bedrock"])
def test_reused_credential_cannot_be_redirected_to_another_endpoint(monkeypatch, provider, capsys):
    from hermes_cli import auth, config, main, models, model_setup_flows as flows

    monkeypatch.setattr(auth, "_resolve_api_key_provider_secret", lambda *_: ("borrowed-key", "credential_pool:" + provider, "https://credential-owner.example/v1"))
    monkeypatch.setattr(main, "_prompt_api_key", lambda _pc, key, **_: (key, False))
    monkeypatch.setattr(config, "get_env_value", lambda *_: "")
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(flows, "line_input", lambda *_: "https://different-owner.example/v1")
    monkeypatch.setattr(flows, "_select_zai_endpoint", lambda *_: "https://different-owner.example/v1")
    # StepFun's first row preserves the custom endpoint; choose another row.
    monkeypatch.setattr(main, "_prompt_provider_choice", lambda *_: 1)

    def forbidden(*args, **kwargs):
        pytest.fail("Changing an endpoint must not forward the retained credential or save configuration")

    for name in ("save_config", "save_env_value"):
        monkeypatch.setattr(config, name, forbidden)
    monkeypatch.setattr(models, "fetch_api_models", forbidden)
    monkeypatch.setattr(auth, "_prompt_model_selection", forbidden)
    if provider == "bedrock":
        flows._model_flow_bedrock_api_key({}, "us-east-1")
    elif provider == "stepfun":
        flows._model_flow_stepfun({})
    else:
        flows._model_flow_api_key_provider({}, provider)
    assert "Replace the key before changing endpoints" in capsys.readouterr().out
