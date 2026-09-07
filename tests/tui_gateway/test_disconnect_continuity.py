"""Exercise disconnect, delayed resume and explicit Stop over real WS frames."""

import threading
import time
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.ws import handle_ws


def test_websocket_reconnect_preserves_live_session_and_explicit_stop(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    stored = "20260906_000000_abcdef"
    sid = "reconnect-runtime"
    db.create_session(stored, source="desktop", model="gpt-6-astra")
    interrupted = []
    closed = []
    reap_checks = []
    real_schedule = server._schedule_ws_orphan_reap

    def schedule(*args, **kwargs):
        reap_checks.append(time.monotonic())
        return real_schedule(*args, **kwargs)

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "resolve_skin", lambda: {})
    monkeypatch.setattr(server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(server, "_start_backend_heartbeat_refresher", lambda: None)
    monkeypatch.setattr(server, "_schedule_startup_orphan_sweep", lambda: None)
    monkeypatch.setattr(server, "_release_wake_for_transport", lambda *_a: False)
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda *_a: False)
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", schedule)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0.05)
    monkeypatch.setattr(server, "_teardown_popped_session", lambda s, **_k: closed.append(s))

    session = {
        "agent": SimpleNamespace(interrupt=lambda: interrupted.append(True)),
        "session_key": stored,
        "source": "desktop",
        "running": True,
        "history": [{"role": "assistant", "content": "partial work"}],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "queued_prompt": {"text": "next step"},
    }
    server._sessions[sid] = session

    def wait_until(predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert predicate()

    def response(ws, request_id):
        for _ in range(30):
            frame = ws.receive_json()
            if frame.get("id") == request_id:
                return frame
        raise AssertionError(f"no RPC response for {request_id}")

    app = Starlette(routes=[WebSocketRoute("/ws", handle_ws)])
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws:
                assert ws.receive_json()["params"]["type"] == "gateway.ready"
                wait_until(lambda: bool(server._live_transports))
                session["transport"] = next(iter(server._live_transports))
                session["queued_prompt"]["transport"] = session["transport"]
                # TestClient cancels its ASGI task on context exit. Deliver
                # the close frame first and let the real handler detach it.
                ws.close()
                wait_until(lambda: session["transport"] is server._detached_ws_transport)

            wait_until(lambda: len(reap_checks) >= 3)
            assert server._sessions[sid] is session
            assert session["running"] is True
            assert session["queued_prompt"]["text"] == "next step"
            assert session["queued_prompt"]["transport"].closed is True
            assert interrupted == []
            assert closed == []

            with client.websocket_connect("/ws") as ws:
                assert ws.receive_json()["params"]["type"] == "gateway.ready"
                ws.send_json({"jsonrpc": "2.0", "id": "resume", "method": "session.resume", "params": {"session_id": stored}})
                resumed = response(ws, "resume")
                assert "error" not in resumed, resumed
                assert resumed["result"]["session_id"] == sid
                assert server._sessions[sid] is session
                assert session["history"] == [{"role": "assistant", "content": "partial work"}]
                assert sid not in server._pending_ws_reaps

                resumed_transport = session["transport"]
                dispatched = []
                monkeypatch.setattr(
                    server, "_run_prompt_submit",
                    lambda _rid, _sid, current, text, **_k: dispatched.append((text, current["transport"])),
                )
                session["running"] = False
                assert server._drain_queued_prompt("next", sid, session) is True
                assert dispatched == [("next step", resumed_transport)]
                session["queued_prompt"] = {"text": "cancel this next step"}

                ws.send_json({"jsonrpc": "2.0", "id": "stop", "method": "session.interrupt", "params": {"session_id": sid}})
                stopped = response(ws, "stop")
                assert "error" not in stopped, stopped
                assert interrupted == [True]
                assert session["_turn_cancel_requested"] is True
                assert session["queued_prompt"] is None
                session["running"] = False
                ws.close()
                wait_until(lambda: sid not in server._sessions)

            wait_until(lambda: sid not in server._sessions)
            assert closed == [session]
            assert db.get_session(stored)["id"] == stored
    finally:
        server._cancel_ws_orphan_reap(sid)
        server._sessions.pop(sid, None)
        db.close()
