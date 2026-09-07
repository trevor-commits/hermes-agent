"""Detached cleanup must preserve work while it passes between turn stages."""

from __future__ import annotations

import io
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost

_actual_delegation_query = server._session_has_active_delegations


class _CapturedTimer:
    """Expose the real orphan callback without a wall-clock grace race."""

    def __init__(self, _delay, callback):
        self.callback = callback
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass


class _ObservedLock:
    """Signal a cleanup lock attempt so a safe blocking reader can proceed."""

    def __init__(self, cleanup_considered):
        self._lock = threading.Lock()
        self.cleanup_thread = None
        self.cleanup_considered = cleanup_considered

    def acquire(self, *args, **kwargs):
        if threading.current_thread() is self.cleanup_thread:
            self.cleanup_considered.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


class _PausedQueueClaim(dict):
    """Pause the drainer after it takes the last envelope, while locked."""

    def __init__(self, values, claimed, release_claim):
        super().__init__(values)
        self.claimed = claimed
        self.release_claim = release_claim

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "queued_prompt" and value is None and not self.claimed.is_set():
            self.claimed.set()
            assert self.release_claim.wait(5), "queue claim was not released"


@pytest.fixture
def cleanup_env(monkeypatch):
    closed = []
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_pending_ws_reaps", {})
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda *_a: False)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 1)
    monkeypatch.setattr(server.threading, "Timer", _CapturedTimer)
    monkeypatch.setattr(
        server, "_teardown_popped_session", lambda session, **_k: closed.append(session)
    )
    yield closed
    for sid in list(server._pending_ws_reaps):
        server._cancel_ws_orphan_reap(sid)


def _turn_session(agent, sid):
    return {
        "agent": agent,
        "session_key": sid,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": True,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "inflight_turn": None,
        "transport": server._detached_ws_transport,
    }


def _prepare_turn_env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *_a: None)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *_a: None)
    monkeypatch.setattr(server, "_sync_bot_capabilities", lambda *_a: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _s: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda _s: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "_voice_tts_enabled", lambda: False)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_get_usage", lambda _a: {})
    monkeypatch.setattr(server, "_is_successful_goal_turn", lambda *_a: False)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _s: False)


@pytest.mark.parametrize("hosted", [False, True])
def test_orphan_reap_preserves_last_queue_claim(monkeypatch, cleanup_env, hosted):
    sid = "queue-claim"
    claimed = threading.Event()
    release_claim = threading.Event()
    cleanup_considered = threading.Event()
    history_lock = _ObservedLock(cleanup_considered)
    session = _PausedQueueClaim(
        {
            "agent": None if hosted else SimpleNamespace(),
            "session_key": sid,
            "history_lock": history_lock,
            "running": False,
            "queued_prompt": {"text": "next step"},
            "transport": server._detached_ws_transport,
            "_compute_host_active": hosted,
        },
        claimed,
        release_claim,
    )
    server._sessions[sid] = session
    dispatched = []
    thread_errors = []

    def dispatch(_rid, _sid, current, text, **_kwargs):
        dispatched.append((current, text))
        return {"result": {}}

    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _s: hosted)
    monkeypatch.setattr(server, "_run_prompt_submit", dispatch)
    monkeypatch.setattr(server, "_submit_prompt_to_compute_host", dispatch)
    server._schedule_ws_orphan_reap(sid)
    reap = server._pending_ws_reaps[sid].callback

    def drain():
        try:
            server._drain_queued_prompt("queued", sid, session)
        except BaseException as exc:
            thread_errors.append(exc)

    def cleanup():
        try:
            reap()
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            cleanup_considered.set()

    drain_thread = threading.Thread(target=drain, daemon=True)
    cleanup_thread = threading.Thread(target=cleanup, daemon=True)
    history_lock.cleanup_thread = cleanup_thread
    if not hosted:
        # In-process draining happens before the prior turn worker exits.
        session["_run_thread"] = drain_thread
    try:
        drain_thread.start()
        assert claimed.wait(5), "real drainer did not claim the final envelope"
        assert session["queued_prompt"] is None
        assert session["running"] is False
        cleanup_thread.start()
        assert cleanup_considered.wait(5), "orphan cleanup did not inspect the claim"
    finally:
        release_claim.set()
        drain_thread.join(5)
        if cleanup_thread.ident is not None:
            cleanup_thread.join(5)

    assert not drain_thread.is_alive()
    assert not cleanup_thread.is_alive()
    assert not thread_errors
    assert server._sessions.get(sid) is session, "cleanup reclaimed a claimed queued turn"
    assert cleanup_env == []
    assert dispatched == [(session, "next step")]


def test_orphan_reap_preserves_worker_tail_and_leftover_steer(
    monkeypatch, tmp_path, cleanup_env
):
    sid = "worker-tail"
    entered_tail = threading.Event()
    release_tail = threading.Event()
    prompts = []
    emitted = []

    def run_conversation(text, **_kwargs):
        prompts.append(text)
        return {
            "messages": [],
            "final_response": "done",
            "pending_steer": "follow-up" if len(prompts) == 1 else None,
        }

    agent = SimpleNamespace(
        session_id=sid,
        model="test/model",
        provider="test",
        clear_interrupt=lambda: None,
        run_conversation=run_conversation,
    )
    session = _turn_session(agent, sid)
    server._sessions[sid] = session

    def settled(_sid, _session, _agent):
        if len(prompts) == 1:
            entered_tail.set()
            assert release_tail.wait(5), "worker tail was not released"

    _prepare_turn_env(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_emit_settled_session_info", settled)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    server._schedule_ws_orphan_reap(sid)
    reap = server._pending_ws_reaps[sid].callback

    assert server._run_prompt_submit("first", sid, session, "first prompt")
    first_thread = session["_run_thread"]
    try:
        assert entered_tail.wait(5), emitted
        assert prompts == ["first prompt"]
        assert session["running"] is False
        assert first_thread.is_alive()
        reap()
    finally:
        release_tail.set()
        first_thread.join(5)
        last_thread = session.get("_run_thread")
        if last_thread is not None and last_thread is not first_thread:
            last_thread.join(5)

    assert not first_thread.is_alive()
    assert last_thread is None or not last_thread.is_alive()
    assert server._sessions.get(sid) is session, "cleanup reclaimed the live worker tail"
    assert cleanup_env == []
    assert prompts == ["first prompt", "follow-up"]


def test_hosted_continuation_keeps_parent_session_until_child_work_finishes(
    monkeypatch, tmp_path, cleanup_env
):
    sid = "hosted-continuation"
    continuation_started = threading.Event()
    release_continuation = threading.Event()
    completion_boundary = threading.Event()
    prompts = []
    frames = []
    continuation_thread = None
    real_thread = threading.Thread

    class ObservedTurnThread(real_thread):
        def __init__(self, *args, **kwargs):
            nonlocal continuation_thread
            super().__init__(*args, **kwargs)
            if len(prompts) == 1 and getattr(kwargs.get("target"), "__name__", "") == "run":
                # Publish identity before start(): the host may join this
                # replacement before its provider seam gets scheduled.
                continuation_thread = self

        def join(self, *args, **kwargs):
            if self is continuation_thread:
                # A host that waits for the replacement worker is also safe.
                completion_boundary.set()
            return super().join(*args, **kwargs)

    def run_conversation(text, **_kwargs):
        nonlocal continuation_thread
        prompts.append(text)
        if len(prompts) == 2:
            continuation_thread = threading.current_thread()
            continuation_started.set()
            assert release_continuation.wait(5), "hosted continuation was not released"
        return {
            "messages": [],
            "final_response": "done",
            "pending_steer": "follow-up" if len(prompts) == 1 else None,
        }

    def emit(frame):
        frames.append(frame)
        if frame.get("type") in {"turn.end", "turn.error"}:
            completion_boundary.set()

    agent = SimpleNamespace(
        session_id=sid,
        model="test/model",
        provider="test",
        clear_interrupt=lambda: None,
        run_conversation=run_conversation,
    )
    child = _turn_session(agent, sid)
    child["running"] = False
    parent = _turn_session(None, sid)
    parent["_compute_host_active"] = True
    child_registry = {sid: child}
    parent_registry = {sid: parent}
    server._sessions = child_registry
    _prepare_turn_env(monkeypatch, tmp_path)
    monkeypatch.setattr(server.threading, "Thread", ObservedTurnThread)
    monkeypatch.setattr(server, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _s: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _s: None)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    monkeypatch.setattr(host, "_ensure_server_session", lambda _server, _frame: child)
    monkeypatch.setattr(host, "emit", emit)
    host_thread = real_thread(
        target=host._run_real_turn,
        args=({"sid": sid, "request_id": "first", "text": "first prompt"},),
        daemon=True,
    )
    try:
        host_thread.start()
        assert continuation_started.wait(5), frames
        assert completion_boundary.wait(5), "host did not settle or wait for its continuation"
        assert continuation_thread.is_alive()
        assert child["running"] is True
        assert not any(frame.get("type") == "turn.error" for frame in frames), frames
        assert not any(frame.get("type") == "turn.end" for frame in frames), (
            "host ended the turn while its continuation was still running",
            frames,
        )

        # Exercise the serving-process half against its separate registry.
        # The live child is blocked at the provider seam throughout this step.
        server._sessions = parent_registry
        for frame in list(frames):
            if frame.get("type") == "turn.end":
                server._on_compute_host_turn_done("first", sid, parent, frame)
        server._schedule_ws_orphan_reap(sid)
        server._pending_ws_reaps[sid].callback()
        preserved = parent_registry.get(sid) is parent
    finally:
        server._sessions = child_registry
        release_continuation.set()
        host_thread.join(5)
        if continuation_thread is not None:
            continuation_thread.join(5)
        host.close()

    assert not host_thread.is_alive()
    assert continuation_thread is not None and not continuation_thread.is_alive()
    assert prompts == ["first prompt", "follow-up"]
    assert preserved, "cleanup reclaimed the return route of a live hosted continuation"
    assert cleanup_env == []
    terminal_frames = [frame for frame in frames if frame.get("type") == "turn.end"]
    assert len(terminal_frames) == 1, frames
    assert terminal_frames[0]["session_info"]["running"] is False

    # A completed hosted request is not proof that its host-owned session can
    # retire: background work may outlive this request in the other process.
    server._sessions = parent_registry
    server._on_compute_host_turn_done("first", sid, parent, terminal_frames[0])
    server._schedule_ws_orphan_reap(sid)
    server._pending_ws_reaps[sid].callback()
    assert parent_registry.get(sid) is parent
    assert cleanup_env == []
    server._close_session_by_id(sid, end_reason="tui_close")
    assert sid not in parent_registry
    assert cleanup_env == [parent]


def test_hosted_async_delegation_keeps_its_parent_return_route(monkeypatch, cleanup_env):
    from tools import async_delegation

    sid = "hosted-delegation"
    child = _turn_session(SimpleNamespace(session_id=sid), sid)
    child["running"] = False
    parent = _turn_session(None, sid)
    parent["_compute_host_active"] = True
    child_records = {
        "delegation-in-child": {
            "status": "running",
            "session_key": sid,
            "origin_ui_session_id": sid,
        },
    }
    monkeypatch.setattr(server, "_session_has_active_delegations", _actual_delegation_query)
    monkeypatch.setattr(async_delegation, "_records", child_records)
    server._sessions = {sid: child}
    assert server._session_has_active_delegations(sid, child) is True

    # The serving process has its own empty registry. No mutation or RPC copies
    # a live child record into this registry when the parent request finishes.
    monkeypatch.setattr(async_delegation, "_records", {})
    server._sessions = {sid: parent}
    assert server._session_has_active_delegations(sid, parent) is False
    monkeypatch.setattr(server, "_emit", lambda *_a, **_k: None)
    server._on_compute_host_turn_done(
        "completed-request", sid, parent,
        {"type": "turn.end", "session_info": {"running": False}},
    )
    assert parent["running"] is False
    assert child_records["delegation-in-child"]["status"] == "running"
    server._schedule_ws_orphan_reap(sid)
    server._pending_ws_reaps[sid].callback()

    assert server._sessions.get(sid) is parent, (
        "cleanup removed the return route of delegation still registered in the host"
    )
    assert cleanup_env == []
    server._close_session_by_id(sid, end_reason="tui_close")
    assert sid not in server._sessions
    assert cleanup_env == [parent]
