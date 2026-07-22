"""Regression coverage for CLI async-delegation completion ownership."""

import queue

from cli import HermesCLI


def test_cli_completion_drain_uses_visible_session_identity(monkeypatch):
    """A CLI window must not claim another window's restored completion."""
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()

    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_visible",
        "session_key": "visible-session",
    }
    calls = []

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            calls.append((session_key, owns_event(event)))
            return [(event, "completion payload")]

    claimed = []
    completed = []

    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: claimed.append((evt, consumer)) or "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: completed.append((evt, token)),
    )

    cli._drain_process_notifications("cli-idle")

    assert calls == [("visible-session", True)]
    queued = cli._pending_input.get_nowait()
    assert queued["_hermes_system_event"] is True
    assert queued["idempotency_key"] == "async-delegation:deleg_visible"
    assert queued["display_text"].startswith("✅ Background result ready")
    assert "internal system event" in queued["model_text"].lower()
    assert claimed == [(event, "cli-idle")]
    assert completed == []
    assert queued["_delivery_claim"]["claim_id"] == "claim-token"

    cli._finalize_process_notification_delivery(queued, succeeded=True)
    assert completed == [(event, "claim-token")]


def test_cli_failed_model_turn_releases_claim_and_wake(monkeypatch):
    """R1-F18: failed persistence stays retryable and clears local dedupe."""
    cli = HermesCLI.__new__(HermesCLI)
    cli._notification_model_wake_keys = {"async-delegation:deleg_retry": None}
    event = {"type": "async_delegation", "delegation_id": "deleg_retry"}
    turn = {
        "_hermes_system_event": True,
        "idempotency_key": "async-delegation:deleg_retry",
        "_delivery_claim": {"event": event, "claim_id": "claim-retry"},
    }
    released = []
    monkeypatch.setattr(
        "tools.async_delegation.release_event_delivery",
        lambda evt, claim: released.append((evt, claim)),
    )

    cli._finalize_process_notification_delivery(turn, succeeded=False)

    assert released == [(event, "claim-retry")]
    assert cli._notification_model_wake_keys == {}


def test_cli_normal_return_without_persisted_success_releases_claim(monkeypatch):
    """R1-F18: Python normal return is not durable model-turn proof."""
    cli = HermesCLI.__new__(HermesCLI)
    turn = {"_hermes_system_event": True}
    outcomes = []
    cli.chat = lambda *_args, **_kwargs: None
    cli._last_chat_turn_persisted = False
    cli._finalize_process_notification_delivery = (
        lambda observed, *, succeeded: outcomes.append((observed, succeeded))
    )

    cli._run_chat_for_process_loop(turn, "system event", images=None)

    assert outcomes == [(turn, False)]


def test_cli_completion_drain_allows_only_one_model_wake_per_delegation(monkeypatch):
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._pending_input = queue.Queue()

    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_once",
        "session_key": "visible-session",
        "status": "completed",
        "summary": "done",
    }

    class FakeRegistry:
        def drain_notifications(self, *, session_key="", owns_event=None):
            assert session_key == "visible-session"
            assert owns_event(event)
            return [(event, "first"), (dict(event), "duplicate")]

    claimed = []
    monkeypatch.setattr("tools.process_registry.process_registry", FakeRegistry())
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: claimed.append((evt, consumer)) or "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery", lambda *_args: None
    )

    cli._drain_process_notifications("cli-idle")

    assert cli._pending_input.qsize() == 1
    assert len(claimed) == 1


def test_cli_completion_ownership_rejects_foreign_session():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._session_db = None

    assert not cli._owns_process_notification(
        {"type": "async_delegation", "session_key": "foreign-session"}
    )


def test_cli_completion_ownership_accepts_compression_lineage():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"

    class FakeSessionDB:
        def resolve_resume_session_id(self, session_id):
            assert session_id == "pre-compression-session"
            return "visible-session"

    cli._session_db = FakeSessionDB()

    assert cli._owns_process_notification(
        {
            "type": "async_delegation",
            "session_key": "pre-compression-session",
        }
    )
