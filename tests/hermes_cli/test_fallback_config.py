"""Tests for hermes_cli/fallback_config.py."""

from hermes_cli.fallback_config import get_fallback_chain, resolve_entry_api_key


class TestResolveEntryApiKey:
    def test_inline_api_key_wins(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"provider": "custom", "api_key": "inline-key", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "inline-key"

    def test_no_key_fields_returns_none(self):
        assert resolve_entry_api_key({"provider": "openrouter", "model": "glm"}) is None

    def test_whitespace_inline_key_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("FB_KEY", "env-key")
        entry = {"api_key": "   ", "key_env": "FB_KEY"}
        assert resolve_entry_api_key(entry) == "env-key"


def test_recovers_legacy_json_encoded_fallback_list():
    """A chain stringified by older ``config set`` versions still runs."""
    config = {
        "fallback_providers": (
            '[{"provider":"deepseek","model":"deepseek-chat"},'
            '{"provider":"openai-codex","model":"gpt-5.6-luna"},'
            '{"provider":"nous","model":"z-ai/glm-5.2"}]'
        )
    }

    chain = get_fallback_chain(config)

    assert [entry["provider"] for entry in chain] == [
        "deepseek",
        "openai-codex",
        "nous",
    ]
