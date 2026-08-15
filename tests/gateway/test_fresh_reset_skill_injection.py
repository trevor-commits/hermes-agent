"""Regression tests for topic/channel skill auto-injection after /new or /reset.

Covers the fix for issue #6508.

Before the fix:
    1. User sends ``/new`` — ``reset_session`` creates a fresh SessionEntry
       with ``created_at == updated_at``.
    2. User sends the next message.
    3. ``get_or_create_session`` finds the entry and bumps
       ``entry.updated_at = now`` (microseconds after ``created_at``).
    4. ``_handle_message_with_agent`` checks
       ``_is_new_session = (created_at == updated_at) or was_auto_reset``.
       Both are False → ``_is_new_session = False`` → topic/channel skills
       are silently skipped for the first message of a manually reset session.

After the fix:
    ``reset_session`` stamps the new entry with ``is_fresh_reset=True``.
    ``_handle_message_with_agent`` ORs this into ``_is_new_session`` and
    consumes the flag immediately after the check, so subsequent messages
    are treated as continuing the session and the flag does not leak.

We use ``was_auto_reset`` for surprise resets (idle/daily/suspended) and
``is_fresh_reset`` for user-initiated resets because the former also drives
a "Session automatically reset due to inactivity" user-facing notice and
a context-note prepend into the agent's prompt — both wrong for an explicit
/new or /reset.
"""

import json
from types import SimpleNamespace

from gateway import run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore


def _make_store(tmp_path):
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


def _make_source(chat_id="123", user_id="u1"):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id=user_id,
    )


def _is_new_session(entry) -> bool:
    """Mirror of the predicate in ``_handle_message_with_agent``.

    Kept in-sync with the production check so this test fails loudly if the
    upstream logic regresses.
    """
    return (
        entry.created_at == entry.updated_at
        or getattr(entry, "was_auto_reset", False)
        or getattr(entry, "is_fresh_reset", False)
    )


# ---------------------------------------------------------------------------
# reset_session stamps is_fresh_reset=True
# ---------------------------------------------------------------------------

class TestResetSessionStampsFreshReset:
    def test_reset_session_sets_is_fresh_reset_true(self, tmp_path):
        store = _make_store(tmp_path)
        source = _make_source()
        store.get_or_create_session(source)
        session_key = store._generate_session_key(source)

        new_entry = store.reset_session(session_key)

        assert new_entry is not None
        assert new_entry.is_fresh_reset is True
        assert new_entry.auto_skill_pending is True


# ---------------------------------------------------------------------------
# Core regression: _is_new_session stays True after updated_at bump
# ---------------------------------------------------------------------------

class TestIsNewSessionSurvivesUpdatedAtBump:
    def test_is_new_session_true_after_reset_then_next_message(self, tmp_path):
        """The actual bug: _is_new_session was False on message after /reset."""
        store = _make_store(tmp_path)
        source = _make_source()
        store.get_or_create_session(source)
        session_key = store._generate_session_key(source)

        # User sends /reset
        store.reset_session(session_key)

        # Next inbound message — get_or_create_session bumps updated_at
        entry = store.get_or_create_session(source)

        # Before the fix: created_at != updated_at, was_auto_reset=False → False
        # After the fix: is_fresh_reset=True carries the signal through the bump
        assert _is_new_session(entry) is True


# ---------------------------------------------------------------------------
# Vanilla-session behavior is unchanged
# ---------------------------------------------------------------------------

class TestVanillaBehaviorUnaffected:
    def test_ongoing_session_not_flagged_as_new(self, tmp_path):
        store = _make_store(tmp_path)
        source = _make_source()
        store.get_or_create_session(source)

        # Second message on the same session — updated_at bumps,
        # is_fresh_reset was never set
        entry = store.get_or_create_session(source)
        assert entry.is_fresh_reset is False
        assert _is_new_session(entry) is False


class TestAutoSkillPendingClaim:
    def test_canonical_db_failure_keeps_claim_retryable_across_restart(
        self,
        tmp_path,
        monkeypatch,
    ):
        """A mirror-only claim can never masquerade as durable consumption."""
        store = _make_store(tmp_path)
        entry = store.get_or_create_session(_make_source())
        token = store.mark_turn_active(entry.session_key)
        persisted_generation = store._persisted_routing_generation

        def fail_canonical_write(*_args, **_kwargs):
            raise OSError("canonical routing unavailable")

        monkeypatch.setattr(
            store._db,
            "replace_gateway_routing_entries",
            fail_canonical_write,
        )

        assert store.claim_auto_skill_pending(
            entry.session_key,
            entry.session_id,
            active_turn_token=token,
        ) is False
        assert store._persisted_routing_generation == persisted_generation
        assert entry.auto_skill_pending is True
        assert entry.auto_skill_claim_token is None

        mirror = json.loads(
            (tmp_path / "sessions.json").read_text(encoding="utf-8")
        )
        assert mirror[entry.session_key]["auto_skill_pending"] is True

        restarted = _make_store(tmp_path)
        restored = restarted.lookup_by_session_key(entry.session_key)
        assert restored is not None
        assert restored.auto_skill_pending is True
        assert restored.auto_skill_claim_token is None

        retry_token = restarted.mark_turn_active(restored.session_key)
        assert restarted.claim_auto_skill_pending(
            restored.session_key,
            restored.session_id,
            active_turn_token=retry_token,
        ) is True
        assert restarted.clear_turn_active(
            restored.session_key,
            retry_token,
        ) is True

        clean_restart = _make_store(tmp_path)
        consumed = clean_restart.lookup_by_session_key(entry.session_key)
        assert consumed is not None
        assert consumed.auto_skill_pending is False
        assert consumed.auto_skill_claim_token is None

    def test_fresh_create_claims_once_and_persists(self, tmp_path):
        store = _make_store(tmp_path)
        source = _make_source()
        entry = store.get_or_create_session(source)
        token = store.mark_turn_active(entry.session_key)

        assert entry.auto_skill_pending is True
        assert store.claim_auto_skill_pending(
            entry.session_key,
            entry.session_id,
            active_turn_token=token,
        ) is True
        assert entry.auto_skill_pending is False
        assert store.claim_auto_skill_pending(
            entry.session_key,
            entry.session_id,
            active_turn_token=token,
        ) is False
        assert store.clear_turn_active(entry.session_key, token) is True

        restored = SessionEntry.from_dict(entry.to_dict())
        assert restored.auto_skill_pending is False

    def test_claim_clears_aliases_for_same_resolved_session(self, tmp_path):
        store = _make_store(tmp_path)
        source = _make_source()
        entry = store.get_or_create_session(source)
        alias = SessionEntry(
            session_key=f"{entry.session_key}:alias",
            session_id=entry.session_id,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            origin=entry.origin,
            platform=entry.platform,
            chat_type=entry.chat_type,
            auto_skill_pending=True,
        )
        with store._lock:
            store._entries[alias.session_key] = alias
            store._save()
        token = store.mark_turn_active(entry.session_key)

        assert store.claim_auto_skill_pending(
            entry.session_key,
            entry.session_id,
            active_turn_token=token,
        ) is True
        assert entry.auto_skill_pending is False
        assert alias.auto_skill_pending is False
        assert store.claim_auto_skill_pending(
            alias.session_key,
            alias.session_id,
            active_turn_token=token,
        ) is False
        assert store.clear_turn_active(entry.session_key, token) is True

    def test_wrong_resolved_session_cannot_claim(self, tmp_path):
        store = _make_store(tmp_path)
        entry = store.get_or_create_session(_make_source())
        token = store.mark_turn_active(entry.session_key)

        assert store.claim_auto_skill_pending(
            entry.session_key,
            "different-session",
            active_turn_token=token,
        ) is False
        assert entry.auto_skill_pending is True

    def test_claim_requires_exact_durable_active_turn(self, tmp_path):
        store = _make_store(tmp_path)
        entry = store.get_or_create_session(_make_source())
        token = store.mark_turn_active(entry.session_key)

        assert store.claim_auto_skill_pending(
            entry.session_key,
            entry.session_id,
            active_turn_token="not-the-owner",
        ) is False
        assert entry.auto_skill_pending is True
        assert store.claim_auto_skill_pending(
            entry.session_key,
            entry.session_id,
            active_turn_token=token,
        ) is True

    def test_interrupted_claim_rearms_payload_across_aliases_on_restart(
        self, tmp_path
    ):
        store = _make_store(tmp_path)
        entry = store.get_or_create_session(_make_source())
        alias = SessionEntry.from_dict(entry.to_dict())
        alias.session_key = f"{entry.session_key}:alias"
        with store._lock:
            store._entries[alias.session_key] = alias
            store._save()

        token = store.mark_turn_active(entry.session_key)
        assert store.claim_auto_skill_pending(
            entry.session_key,
            entry.session_id,
            active_turn_token=token,
        ) is True

        restarted = _make_store(tmp_path)
        assert restarted.recover_interrupted_turns() == 1
        restored = restarted.lookup_by_session_key(entry.session_key)
        restored_alias = restarted.lookup_by_session_key(alias.session_key)
        assert restored is not None and restored.auto_skill_pending is True
        assert restored_alias is not None and restored_alias.auto_skill_pending is True
        assert restored.auto_skill_claim_token is None
        assert restored_alias.auto_skill_claim_token is None

    def test_successful_claim_stays_consumed_across_aliases_and_restart(
        self, tmp_path
    ):
        store = _make_store(tmp_path)
        entry = store.get_or_create_session(_make_source())
        alias = SessionEntry.from_dict(entry.to_dict())
        alias.session_key = f"{entry.session_key}:alias"
        with store._lock:
            store._entries[alias.session_key] = alias
            store._save()

        token = store.mark_turn_active(entry.session_key)
        assert store.claim_auto_skill_pending(
            entry.session_key,
            entry.session_id,
            active_turn_token=token,
        ) is True
        assert store.clear_turn_active(entry.session_key, token) is True

        restarted = _make_store(tmp_path)
        restored = restarted.lookup_by_session_key(entry.session_key)
        restored_alias = restarted.lookup_by_session_key(alias.session_key)
        assert restored is not None and restored.auto_skill_pending is False
        assert restored_alias is not None and restored_alias.auto_skill_pending is False
        assert restored.auto_skill_claim_token is None
        assert restored_alias.auto_skill_claim_token is None

    def test_orphan_claim_token_rearms_instead_of_losing_injection(self, tmp_path):
        store = _make_store(tmp_path)
        entry = store.get_or_create_session(_make_source())
        with store._lock:
            entry.auto_skill_pending = False
            entry.auto_skill_claim_token = "orphaned-claim"
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            store._save()

        restarted = _make_store(tmp_path)
        assert restarted.recover_interrupted_turns() == 0
        restored = restarted.lookup_by_session_key(entry.session_key)
        assert restored is not None and restored.auto_skill_pending is True
        assert restored.auto_skill_claim_token is None


def test_auto_skill_loader_failure_does_not_mutate_event(monkeypatch):
    event = SimpleNamespace(auto_skill="missing-skill", text="original request")

    def fail_load(*_args, **_kwargs):
        raise OSError("skill tree unavailable")

    monkeypatch.setattr("agent.skill_commands._load_skill_payload", fail_load)

    assert gateway_run.GatewayRunner._apply_claimed_auto_skill(
        event,
        "task-id",
        "session-key",
    ) is False
    assert event.text == "original request"


# ---------------------------------------------------------------------------
# Persistence through sessions.json round-trip
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_is_fresh_reset_survives_to_dict_from_dict(self, tmp_path):
        """Protect against the gateway restarting between /reset and the
        next message — the flag must be persisted in sessions.json.
        """
        store = _make_store(tmp_path)
        source = _make_source()
        store.get_or_create_session(source)
        session_key = store._generate_session_key(source)
        new_entry = store.reset_session(session_key)

        assert new_entry.is_fresh_reset is True
        restored = SessionEntry.from_dict(new_entry.to_dict())
        assert restored.is_fresh_reset is True
        assert restored.auto_skill_pending is True
