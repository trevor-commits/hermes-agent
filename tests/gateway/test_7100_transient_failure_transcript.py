"""Tests for #7100 — transient failures (429/timeout) must not drop the
user message from the transcript.

The #1630 fix introduced a blanket skip of transcript writes on any
``failed`` agent result.  That was correct for context-overflow failures
(which would otherwise cause a session-growth loop), but it also caused
transient provider failures (rate limits, read timeouts, connection
resets) to silently drop the user's message — so the agent had no memory
of the last turn on the next attempt.

The gateway classifier must distinguish:

* verified hard-ceiling blocks (``hard_context_ceiling_blocked=True`` AND
  ``agent_persisted=True``) OR ``compression_exhausted=True`` OR
  context-keyword errors OR a generic ``400`` on a long history
  → context-overflow → skip transcript
* everything else that fails — including an UNVERIFIED hard-ceiling block,
  whose user message may genuinely be unsaved → transient → persist the
  user message (the duplicate-flush guard handles the already-saved case)
"""


def _classify(agent_result: dict, history_len: int) -> tuple[bool, bool]:
    """Replicate the gateway classifier from GatewayRunner._run_agent.

    Returns ``(agent_failed_early, is_context_overflow_failure)``.
    """
    agent_failed_early = bool(agent_result.get("failed"))
    err = str(agent_result.get("error", "")).lower()
    is_context_overflow_failure = agent_failed_early and (
        (
            bool(agent_result.get("hard_context_ceiling_blocked"))
            and bool(agent_result.get("agent_persisted"))
        )
        or bool(agent_result.get("compression_exhausted"))
        or any(p in err for p in (
            "context length", "context size", "context window",
            "maximum context", "token limit", "too many tokens",
            "reduce the length", "exceeds the limit",
            "request entity too large", "prompt is too long",
            "payload too large", "input is too long",
        ))
        or ("400" in err and history_len > 50)
    )
    return agent_failed_early, is_context_overflow_failure


class TestContextOverflowStillSkipsTranscript:
    """#1630 behavior must be preserved for real context-overflow cases."""

    def test_compression_exhausted_is_context_overflow(self):
        agent_result = {
            "failed": True,
            "compression_exhausted": True,
            "error": "Request payload too large: max compression attempts reached.",
        }
        failed, ctx_overflow = _classify(agent_result, history_len=100)
        assert failed
        assert ctx_overflow

    def test_verified_hard_ceiling_block_is_context_overflow(self):
        """input_too_large keeps compression_exhausted False even when the
        user turn is verified durable — the flag pair must classify."""
        agent_result = {
            "failed": True,
            "hard_context_ceiling_blocked": True,
            "agent_persisted": True,
            "compression_exhausted": False,
            "error": "hard_context_ceiling_blocked:input_too_large",
        }
        failed, ctx_overflow = _classify(agent_result, history_len=2)
        assert failed
        assert ctx_overflow

    def test_unverified_hard_ceiling_block_stays_transient(self):
        """An unverified block may sit on a genuinely unsaved user message —
        it must reach the transient branch's fallback persist, where the
        duplicate-flush guard decides."""
        agent_result = {
            "failed": True,
            "hard_context_ceiling_blocked": True,
            "agent_persisted": False,
            "compression_exhausted": False,
            "error": "hard_context_ceiling_blocked:compression_stalled",
        }
        failed, ctx_overflow = _classify(agent_result, history_len=30)
        assert failed
        assert not ctx_overflow


class TestTransientFailureKeepsUserMessage:
    """Transient provider failures must NOT skip the transcript — doing so
    drops the user message and the agent forgets the turn. (#7100)"""

    def test_rate_limit_429_is_not_context_overflow(self):
        agent_result = {
            "failed": True,
            "error": (
                "API call failed after 3 retries: 429 Too Many Requests "
                "— rate limit exceeded"
            ),
        }
        failed, ctx_overflow = _classify(agent_result, history_len=10)
        assert failed
        assert not ctx_overflow

    def test_read_timeout_is_not_context_overflow(self):
        agent_result = {
            "failed": True,
            "error": "ReadTimeout: HTTPSConnectionPool(host='api.z.ai'): Read timed out.",
        }
        failed, ctx_overflow = _classify(agent_result, history_len=10)
        assert failed
        assert not ctx_overflow


class TestSuccessfulResultUnaffected:
    def test_successful_result_neither_failed_nor_overflow(self):
        agent_result = {
            "final_response": "Hello!",
            "messages": [{"role": "assistant", "content": "Hello!"}],
        }
        failed, ctx_overflow = _classify(agent_result, history_len=10)
        assert not failed
        assert not ctx_overflow
