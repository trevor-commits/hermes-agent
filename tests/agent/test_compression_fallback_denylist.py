"""Compression aux->main fallback must skip denylisted main models.

2026-08-18 kimi burn: with the auxiliary summary model rate-limited, the
fallback in ``_fallback_to_main_for_compression`` re-billed an already-burning
sticky ``kimi-k3`` session on every compression attempt — 12.7M uncached
tokens in one morning. Denylisted mains (default: anything containing
``kimi``) now keep the auxiliary model and let compression fail closed via
the hard-context-ceiling machinery; every other main model keeps the
historical fallback behavior.
"""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor


def _make(model: str) -> ContextCompressor:
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        return ContextCompressor(
            model=model,
            threshold_percent=0.50,
            quiet_mode=True,
            summary_model_override="aux/summary-model",
        )


def test_denylisted_main_keeps_aux_model():
    compressor = _make("kimi-k3")
    compressor._fallback_to_main_for_compression(
        RuntimeError("429 rate limited"), "unavailable"
    )
    # Never cleared to "" (which would route compression onto the kimi main).
    assert compressor.summary_model == "aux/summary-model"
    # Still marked fallen-back so _generate_summary's retry branches stay
    # bounded (one more aux attempt, then cooldown) instead of recursing.
    assert compressor._summary_model_fallen_back is True
    assert compressor._last_aux_model_failure_model == "aux/summary-model"


def test_denylist_matches_model_substring_case_insensitively():
    compressor = _make("moonshot/Kimi-K3-Turbo")
    compressor._fallback_to_main_for_compression(
        RuntimeError("boom"), "failed"
    )
    assert compressor.summary_model == "aux/summary-model"


def test_normal_main_still_falls_back():
    compressor = _make("z-ai/glm-5.3")
    compressor._fallback_to_main_for_compression(
        RuntimeError("timeout"), "timed out"
    )
    # Historical behavior preserved: empty summary_model means "use main".
    assert compressor.summary_model == ""
    assert compressor._summary_model_fallen_back is True
