"""Cold-start regression tests for model context resolution."""

from unittest.mock import patch

from agent.model_metadata import DEFAULT_FALLBACK_CONTEXT, get_model_context_length


def test_context_resolution_uses_memory_only_models_dev_lookup():
    """A missing models.dev cache must not block the first chat turn."""
    with patch("agent.models_dev.fetch_models_dev", return_value={}) as fetch:
        context = get_model_context_length(
            "unlisted-zai-model",
            base_url="https://api.z.ai/api/coding/paas/v4",
            provider="zai",
        )

    assert context == DEFAULT_FALLBACK_CONTEXT
    fetch.assert_called_once_with(allow_network=False, allow_disk=False)
