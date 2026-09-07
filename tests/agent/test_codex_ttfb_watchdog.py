"""Regression tests for the Codex time-to-first-byte (TTFB) watchdog.

The chatgpt.com/backend-api/codex endpoint has an intermittent failure mode
where it accepts the connection but never emits a single stream event. The
watchdog in ``interruptible_api_call`` kills such a connection at a short TTFB
cutoff (instead of waiting out the much longer wall-clock stale timeout) so the
retry loop can reconnect promptly. Once any stream event arrives, the TTFB
watchdog is satisfied and a separate idle watchdog handles streams that stop
emitting SSE events.

The "bytes flowing" signal is ``agent._codex_stream_last_event_ts``, set on
*any* event by ``codex_runtime.run_codex_stream`` — so reasoning-only or
tool-call-only turns (which emit no output-text deltas) are not mistaken for a
stall.
"""

from __future__ import annotations

import sys
import time
import types
from types import SimpleNamespace

import pytest

# Stub optional heavy imports so run_agent imports cleanly in isolation.
sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())


def _make_codex_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    from run_agent import AIAgent

    agent = AIAgent(
        model="gpt-5.5",
        provider="openai-codex",
        api_key="sk-dummy",
        base_url="https://chatgpt.com/backend-api/codex",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    # The watchdog is gated on the codex_responses api_mode; assert/force it so
    # the test is robust to detection-logic changes elsewhere.
    agent.api_mode = "codex_responses"
    monkeypatch.setattr(agent, "_emit_status", lambda *a, **k: None)
    # Keep the wall-clock stale timeout high so any early kill is unambiguously
    # the TTFB path, not the stale-call path.
    monkeypatch.setattr(
        agent, "_compute_non_stream_stale_timeout", lambda *a, **k: 60.0
    )
    return agent






def test_ttfb_includes_silent_hang_hint_for_gpt_5_5(tmp_path, monkeypatch):
    """The no-first-byte watchdog should surface the same actionable hint as the
    stale-call timeout path when the model matches the silent-hang heuristic."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "0.4")

    closes: list = []
    statuses: list[str] = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(agent, "_buffer_status", lambda msg: statuses.append(msg))
    monkeypatch.setattr(agent, "_emit_status", lambda msg: statuses.append(msg))
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None):
        deadline = time.time() + 30
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
        message = str(excinfo.value)
        assert "gpt-5.4" in message
        assert "gpt-5.3-codex" in message
        assert "gpt-5.4-codex" in message
        assert "codex_ttfb_kill" in closes
        assert statuses, "expected a user-facing watchdog status"
        assert any("gpt-5.4" in s and "gpt-5.3-codex" in s for s in statuses)
    finally:
        stop["flag"] = True




def test_ttfb_does_not_kill_when_events_flow(tmp_path, monkeypatch):
    """Once a stream event has arrived, a generation that runs past the TTFB
    cutoff is NOT killed by the watchdog — it completes normally."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "0.4")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    sentinel = SimpleNamespace(ok=True)

    def fake_stream(api_kwargs, client=None, on_first_delta=None):
        # Bytes flowing: mark stream activity right away, then keep generating
        # past the 0.4s TTFB cutoff before returning a real response.
        agent._codex_stream_last_event_ts = time.time()
        if on_first_delta:
            on_first_delta()
        time.sleep(0.9)
        return sentinel

    monkeypatch.setattr(agent, "_run_codex_stream", fake_stream)

    resp = h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
    assert resp is sentinel
    assert "codex_ttfb_kill" not in closes


@pytest.mark.parametrize("input_size", [2, 220_000, 480_000])
@pytest.mark.parametrize("hard_timeout", [None, 90])
def test_active_codex_stream_survives_long_elapsed_time(monkeypatch, input_size, hard_timeout):
    """Forty minutes of valid stream activity is not forty minutes of silence."""
    from agent import chat_completion_helpers as h

    now = [1_000.0]
    response = SimpleNamespace(ok=True)
    agent = SimpleNamespace(
        platform="desktop",
        api_mode="codex_responses",
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        model="gpt-6-astra",
        _consecutive_stale_streams=0,
        _interrupt_requested=False,
        _compute_non_stream_stale_timeout=lambda _kwargs: 90.0,
        _touch_activity=lambda _message: None,
        _emit_wait_notice=lambda _message: None,
        _buffer_status=lambda _message: None,
    )

    class ActiveStreamThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.polls = 0

        def start(self):
            pass

        def join(self, timeout=None):
            now[0] += 30.0
            self.polls += 1
            agent._codex_stream_last_event_ts = now[0]

        def is_alive(self):
            if self.polls >= 80:
                self.target()
                return False
            return True

    monkeypatch.delenv("HERMES_CODEX_HARD_TIMEOUT_SECONDS", raising=False)
    if hard_timeout is not None:
        monkeypatch.setenv("HERMES_CODEX_HARD_TIMEOUT_SECONDS", str(hard_timeout))
    monkeypatch.setattr(h, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(h.threading, "Thread", ActiveStreamThread)
    monkeypatch.setattr(h, "_dispatch_nonstreaming_api_request", lambda *_a, **_k: response)

    payload = {"model": "gpt-6-astra", "input": "x" * input_size}
    if hard_timeout is None:
        assert h.interruptible_api_call(agent, payload) is response
        assert now[0] >= 3_400.0
        assert agent._consecutive_stale_streams == 0
    else:
        with pytest.raises(TimeoutError, match="threshold: 90s"):
            h.interruptible_api_call(agent, payload)
        assert now[0] < 1_200.0


@pytest.mark.parametrize("hard_timeout, expected", [(0, 480), (450, 450)])
def test_wait_notice_rearms_after_codex_activity(hard_timeout, expected):
    from agent.chat_completion_helpers import _codex_wait_notice_recovery

    assert _codex_wait_notice_recovery(
        stale_timeout=90,
        ttfb_enabled=True,
        ttfb_timeout=120,
        last_event_ts=490,
        call_start=100,
        idle_enabled=True,
        idle_timeout=120,
        elapsed=400,
        hard_timeout=hard_timeout,
    ) == f"; auto-reconnect at {expected}s"


def test_codex_sdk_stream_crosses_stale_window_without_reconnect(tmp_path, monkeypatch):
    """Real HTTP/SSE, SDK parsing, worker ownership and watchdog polling."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import openai
    from agent import chat_completion_helpers as h

    requests = []
    aborts = []
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [{"type": "response.created", "response": {"id": "resp-local"}}]
            events += [{"type": "response.reasoning_summary_text.delta", "delta": "thinking"}] * 20
            events += [
                {"type": "response.output_text.delta", "delta": "finished"},
                {"type": "response.completed", "response": {"id": "resp-local", "status": "completed", "output": []}},
            ]
            try:
                for event in events:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
                    if stop.wait(0.05):
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    agent = _make_codex_agent(tmp_path, monkeypatch)
    client = openai.OpenAI(
        api_key="local-test-only",
        base_url=f"http://127.0.0.1:{httpd.server_port}/v1",
        max_retries=0,
    )
    original_abort = agent._abort_request_openai_client

    def record_abort(*args, **kwargs):
        aborts.append(kwargs.get("reason"))
        return original_abort(*args, **kwargs)

    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **_k: client)
    monkeypatch.setattr(agent, "_abort_request_openai_client", record_abort)
    monkeypatch.setattr(agent, "_compute_non_stream_stale_timeout", lambda _k: 0.4)
    monkeypatch.delenv("HERMES_CODEX_HARD_TIMEOUT_SECONDS", raising=False)
    try:
        result = h.interruptible_api_call(agent, {"model": "gpt-6-astra", "input": "hello"})
        assert result.output_text == "finished"
        assert len(requests) == 1
        assert requests[0]["stream"] is True
        assert aborts == []
        assert client.is_closed()
    finally:
        stop.set()
        client.close()
        agent.close()
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()








@pytest.mark.parametrize(
    "stale_timeout",
    [float("inf"), float("-inf"), float("nan")],
)
def test_wait_notice_omits_reconnect_when_all_deadlines_are_non_finite(
    stale_timeout,
):
    """A disabled watchdog must not be advertised as a future reconnect."""
    from agent import chat_completion_helpers as h

    recovery = h._codex_wait_notice_recovery(
        stale_timeout=stale_timeout,
        ttfb_enabled=False,
        ttfb_timeout=float("nan"),
        last_event_ts=None,
        call_start=100.0,
        idle_enabled=False,
        idle_timeout=float("nan"),
        elapsed=30.0,
    )

    assert recovery == ""






def test_moa_heartbeat_survives_infinite_stale_timeout(monkeypatch):
    """The full 100-poll MoA heartbeat must leave a healthy call running."""
    from agent import chat_completion_helpers as h

    notices: list[str] = []
    response = SimpleNamespace(ok=True)
    agent = SimpleNamespace(
        platform="desktop",
        api_mode="chat_completions",
        provider="moa",
        _consecutive_stale_streams=0,
        _interrupt_requested=False,
        _compute_non_stream_stale_timeout=lambda _kwargs: float("inf"),
        _touch_activity=lambda _message: None,
        _emit_wait_notice=notices.append,
    )

    class HeartbeatThread:
        """Keep the synthetic worker alive through one heartbeat."""

        def __init__(self, *, target, daemon):
            self._polls = 0
            self._target = target

        def start(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            self._polls += 1
            if self._polls == 101:
                self._target()
                return False
            return True

    monkeypatch.setattr(h.threading, "Thread", HeartbeatThread)
    monkeypatch.setattr(
        h,
        "_dispatch_nonstreaming_api_request",
        lambda *_args, **_kwargs: response,
    )

    result = h.interruptible_api_call(agent, {"model": "openai-xai-wide"})

    assert result is response
    assert len(notices) == 1
    assert "waiting on openai-xai-wide" in notices[0]
    assert "auto-reconnect" not in notices[0]


def test_wait_notice_formatting_error_does_not_abort_request(monkeypatch):
    """Status construction is fail-open even if its formatter breaks."""
    from agent import chat_completion_helpers as h

    response = SimpleNamespace(ok=True)
    agent = SimpleNamespace(
        platform="desktop",
        api_mode="chat_completions",
        provider="moa",
        _consecutive_stale_streams=0,
        _interrupt_requested=False,
        _compute_non_stream_stale_timeout=lambda _kwargs: float("inf"),
        _touch_activity=lambda _message: None,
        _emit_wait_notice=lambda _message: None,
    )

    class HeartbeatThread:
        def __init__(self, *, target, daemon):
            self._polls = 0
            self._target = target

        def start(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            self._polls += 1
            if self._polls == 101:
                self._target()
                return False
            return True

    monkeypatch.setattr(h.threading, "Thread", HeartbeatThread)
    monkeypatch.setattr(
        h,
        "_dispatch_nonstreaming_api_request",
        lambda *_args, **_kwargs: response,
    )
    monkeypatch.setattr(
        h,
        "_codex_wait_notice_recovery",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad display state")),
    )

    result = h.interruptible_api_call(agent, {"model": "openai-xai-wide"})

    assert result is response










def test_large_codex_request_hard_ceiling_reclaims_silent_stall(tmp_path, monkeypatch):
    """#64507 regression: a large Codex request (TTFB watchdog disabled by the
    size gate, stale floor *raised*) that never emits a single byte must still
    be reclaimed at a finite hard ceiling — not hang for 13+ minutes while the
    worker stays idle and the session shows as active.

    Uses the real default TTFB threshold (120s) and asserts the request dies at
    the hard ceiling regardless of the size-based TTFB disable.
    """
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    # Real default TTFB threshold (no HERMES_CODEX_TTFB_* override) → for a
    # >10k-token request the no-byte TTFB watchdog is auto-disabled.
    monkeypatch.setenv("HERMES_CODEX_HARD_TIMEOUT_SECONDS", "3")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None):
        # No event marker AND no event ever: the exact issue-64507 stall.
        deadline = time.time() + 120
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    large_input = "x" * 44_000  # ~11k estimated tokens → TTFB disabled, stale raised
    t0 = time.time()
    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": large_input})
        elapsed = time.time() - t0
        # Must die at the hard ceiling (3s), nowhere near the raised stale floor.
        assert elapsed < 30, f"hard ceiling took {elapsed:.1f}s — stall not reclaimed"
        assert "stale_call_kill" in closes, f"stale kill expected, got {closes}"
        assert "timed out after" in str(excinfo.value)
        assert "with no response" in str(excinfo.value)
    finally:
        stop["flag"] = True


