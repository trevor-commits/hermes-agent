"""SessionStore explicit suspension, crash-recovery markers, pruning and shared clock/id helpers."""

from __future__ import annotations

import logging
import json
import threading
from dataclasses import replace
import os
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from gateway.session import SessionEntry, SessionSource

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.session")


def _now() -> datetime:
    """Return the current local time."""
    return datetime.now()


def _new_session_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso(value) -> Optional[datetime]:
    """``datetime.fromisoformat`` that returns None for empty/malformed input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# Auto-continue freshness window (1 hour) after the ``resume_pending`` mark; ``gateway/run.py``
# bridges config.yaml ``agent.gateway_auto_continue_freshness`` into the env var at startup.
_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT = 60 * 60


def auto_continue_freshness_window() -> float:
    """Resume-scheduler freshness window; stale automation never discards the transcript."""
    raw = os.environ.get("HERMES_AUTO_CONTINUE_FRESHNESS")
    try:
        return float(raw) if raw else float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)
    except (TypeError, ValueError):
        return float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)


class SessionLifecycleMixin:
    """SessionStore explicit boundaries and crash-recovery markers."""

    def _is_session_ended_in_db(self, session_id: str) -> bool:
        """True iff state.db has this session with a non-null end_reason (same staleness test as
        ``_prune_stale_sessions_locked``; no DB/row or DB error -> False). Lets routing self-heal a
        session ended while the gateway stays alive. Store resolved from the owning profile.

        Used by ``get_or_create_session`` to self-heal at routing time: ``_prune_stale_sessions_locked``
        only runs at startup, so a session ended in the DB while the gateway stays alive (any path that
        finalizes the row without clearing sessions.json) would otherwise be reused as a live routing key
        and silently swallow every subsequent message until the next restart (#54878 — the live-gateway
        variant of #52804/FM9). DB errors are non-fatal — never block routing on a failed lookup.
        The store is resolved from the row's owning profile rather than the ambient scope: an unscoped
        background writer keeps its own copy of the same session, and comparing against that copy reports a
        live session as ended (#66887).
        """
        db = self._db_for_session_id(session_id)
        if not db or not session_id:
            return False
        try:
            row = db.get_session(session_id)
        except Exception:
            return False
        return bool(row is not None and row.get("end_reason") is not None)

    def _route_reset_reason(self, entry: SessionEntry) -> Optional[str]:
        """Only explicit suspension replaces a routed conversation; time never does."""
        return "suspended" if entry.suspended else None

    def _update_entry(self, session_key: str, mutate) -> bool:
        """Apply ``mutate(entry)`` under ``_lock`` and full-save; False when the entry is missing
        or *mutate* returned False (nothing to persist)."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or mutate(entry) is False:
                return False
            self._save()
            return True

    def _update_all_entries_locked(self, mutate) -> int:
        """Apply ``mutate(entry) -> bool`` to every entry under ``_lock``; save once if any
        returned True. Returns the count that did."""
        with self._lock:
            self._ensure_loaded_locked()
            changed = sum(1 for entry in self._entries.values() if mutate(entry))
            if changed:
                self._save()
        return changed

    def suspend_session(self, session_key: str) -> bool:
        """Mark a session suspended so it auto-resets on next access (/stop). True if it existed.

        Used by ``/stop`` to prevent stuck sessions from being resumed after a gateway restart (#7536).
        """
        return self._update_entry(session_key, lambda e: setattr(e, "suspended", True))

    def _set_turn_marker_locked(self, session_key: str, entry: SessionEntry, token, started_at) -> None:
        """Persist the active-turn pair BEFORE publishing it in memory, so a failed write can
        neither leak an unowned token nor drop a live one. Lock held."""
        candidate = entry.to_dict()
        candidate["active_turn_token"] = token
        candidate["active_turn_started_at"] = _iso(started_at)
        if started_at is not None:
            # Keeps the legacy 120s startup heuristic working for an older binary during a rolling
            # downgrade/upgrade window.
            candidate["updated_at"] = started_at.isoformat()
        self._save_entry(session_key, entry_data=candidate, lock_held=True)
        entry.active_turn_token = token
        entry.active_turn_started_at = started_at
        if started_at is not None:
            entry.updated_at = started_at

    def mark_turn_active(self, session_key: str) -> Optional[str]:
        """Persist exact ownership of the running agent turn; returns the opaque token for
        :meth:`clear_turn_active`. Re-marking replaces the previous token so a stale asynchronous
        unwind cannot clear a newer turn."""
        token = uuid.uuid4().hex
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None:
                return None
            self._set_turn_marker_locked(session_key, entry, token, _now())
        return token

    def clear_turn_active(self, session_key: str, token: str) -> bool:
        """Compare-and-swap clear an active-turn marker.

        Also finalizes any auto-skill claim owned by this exact turn across all
        aliases. Returns ``False`` when the entry disappeared or a newer turn
        owns it. A persistence failure restores every in-memory marker.
        """
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if entry is None or entry.active_turn_token != token:
                return False
            previous_claims = {
                key: candidate.auto_skill_claim_token
                for key, candidate in self._entries.items()
                if candidate.auto_skill_claim_token == token
            }
            if not previous_claims:
                candidate = entry.to_dict()
                candidate["active_turn_token"] = None
                candidate["active_turn_started_at"] = None
                self._save_entry(
                    session_key,
                    entry_data=candidate,
                    lock_held=True,
                )
                entry.active_turn_token = None
                entry.active_turn_started_at = None
                return True
            previous_started_at = entry.active_turn_started_at
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            for key in previous_claims:
                self._entries[key].auto_skill_claim_token = None
            data, generation = self._snapshot_routing_locked()

        try:
            self._persist_routing_data(data, generation)
        except Exception:
            with self._lock:
                current = self._entries.get(session_key)
                if current is not None and current.active_turn_token is None:
                    current.active_turn_token = token
                    current.active_turn_started_at = previous_started_at
                for key, claim_token in previous_claims.items():
                    candidate = self._entries.get(key)
                    if candidate is not None and candidate.auto_skill_claim_token is None:
                        candidate.auto_skill_claim_token = claim_token
            raise
        return True

    def recover_interrupted_turns(self, max_age_seconds: int = 60 * 60) -> int:
        """Promote crash-left turn markers into ``resume_pending`` (unclean startup only).
        Old/invalid markers are cleared without resuming; suspended sessions are never re-armed.
        Returns the number of newly promoted sessions."""
        now = _now()
        max_age = timedelta(seconds=max(0, max_age_seconds))
        promoted = 0

        def _promote(entry: SessionEntry) -> bool:
            nonlocal promoted
            claimed = bool(entry.auto_skill_claim_token)
            if claimed:
                entry.auto_skill_pending = True
                entry.auto_skill_claim_token = None
            if not entry.active_turn_token:
                return claimed
            started_at = entry.active_turn_started_at
            try:
                marker_is_stale = started_at is None or (
                    max_age_seconds > 0 and now - started_at > max_age
                )
            except TypeError:
                # Mixed aware/naive timestamps: clear rather than risk an unsafe old resume.
                marker_is_stale = True
            if not marker_is_stale and not entry.suspended:
                if entry.resume_pending:
                    # A drain-timeout marker is more specific; keep it.
                    if entry.last_resume_marked_at is None:
                        entry.last_resume_marked_at = now
                else:
                    entry.resume_pending = True
                    entry.resume_reason = "restart_interrupted"
                    entry.last_resume_marked_at = now  # freshness starts at discovery
                    promoted += 1
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            return True

        self._update_all_entries_locked(_promote)
        return promoted

    def discard_active_turn_markers(self) -> int:
        """Clear orphan turn markers after a verified clean shutdown."""
        def _discard(entry: SessionEntry) -> bool:
            if not entry.active_turn_token and entry.active_turn_started_at is None and entry.auto_skill_claim_token is None:
                return False
            entry.auto_skill_claim_token = None
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            return True
        return self._update_all_entries_locked(_discard)

    def mark_resume_pending(self, session_key: str, reason: str = "restart_timeout") -> bool:
        """Mark a session resumable after a restart interruption (keeps the session_id/transcript,
        unlike ``suspend_session``). True if marked."""
        def _apply(entry: SessionEntry):
            if entry.suspended:  # never override an explicit ``suspended`` (hard forced-wipe)
                return False
            entry.resume_pending = True
            entry.resume_reason = reason
            entry.last_resume_marked_at = _now()
        return self._update_entry(session_key, _apply)

    def clear_resume_pending(self, session_key: str) -> bool:
        """Clear the resume-pending flag after a successful resumed turn; True if cleared."""
        def _apply(entry: SessionEntry):
            if not entry.resume_pending:
                return False
            entry.resume_pending = False
            entry.resume_reason = None
            entry.last_resume_marked_at = None
        return self._update_entry(session_key, _apply)

    def prune_old_entries(self, max_age_days: int) -> int:
        """Drop routing entries idle (by ``updated_at``) for more than max_age_days; suspended
        entries and entries with active background processes are kept. Only the key -> session_id
        mapping is dropped (the transcript stays). ``max_age_days <= 0`` disables. Returns count."""
        if max_age_days is None or max_age_days <= 0:
            return 0
        cutoff = _now() - timedelta(days=max_age_days)
        with self._lock:
            self._ensure_loaded_locked()
            removed_keys = [
                key for key, entry in list(self._entries.items())
                if not entry.suspended
                # The callback is keyed by session_key, NOT session_id.
                and not self._has_active_processes_safe(entry.session_key, context="prune")
                and entry.updated_at < cutoff
            ]
            for key in removed_keys:
                self._entries.pop(key, None)
            if removed_keys:
                self._save()
        if removed_keys:
            logger.info("SessionStore pruned %d entries older than %d days",
                        len(removed_keys), max_age_days)
        return len(removed_keys)

    def suspend_recently_active(self, max_age_seconds: int = 120) -> int:
        """Mark sessions active within *max_age_seconds* as ``resume_pending`` after a crash/fast
        restart (already-pending and suspended entries are skipped). Returns the number marked.

        Called on gateway startup after a crash or fast restart to preserve in-flight sessions instead of
        destroying their conversation history (#7536). Only marks sessions updated within *max_age_seconds*
        to avoid touching long-idle sessions. Sets ``resume_pending=True`` so the next incoming message on
        the same session_key auto-resumes from the existing transcript.
        """
        cutoff = _now() - timedelta(seconds=max_age_seconds)

        def _mark(entry: SessionEntry) -> bool:
            if entry.resume_pending or entry.suspended or entry.updated_at < cutoff:
                return False
            entry.resume_pending = True
            entry.resume_reason = "restart_interrupted"
            entry.last_resume_marked_at = _now()
            return True
        return self._update_all_entries_locked(_mark)


    def claim_auto_skill_pending(
        self,
        session_key: str,
        expected_session_id: str,
        *,
        active_turn_token: Optional[str],
    ) -> bool:
        """Durably claim one session's channel/topic skill injection.

        The caller already owns the resolved session turn lease and has loaded
        the payload. Bind the claim to its durable active-turn token, then
        clear every routing alias in one routing snapshot. A crash before the
        turn clears re-arms the payload in :meth:`recover_interrupted_turns`.
        A route, token, or persistence mismatch fails closed.
        """
        if not session_key or not expected_session_id or not active_turn_token:
            return False

        previous: Dict[str, tuple[bool, Optional[str]]] = {}
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if (
                entry is None
                or entry.session_id != expected_session_id
                or not entry.auto_skill_pending
                or entry.active_turn_token != active_turn_token
            ):
                return False
            for key, candidate in self._entries.items():
                if candidate.session_id == expected_session_id:
                    previous[key] = (
                        candidate.auto_skill_pending,
                        candidate.auto_skill_claim_token,
                    )
                    candidate.auto_skill_pending = False
                    candidate.auto_skill_claim_token = active_turn_token
            data, generation = self._snapshot_routing_locked()

        try:
            self._persist_routing_data(data, generation)
        except Exception as exc:
            # No authoritative routing write landed. Restore retryability only
            # for aliases that still resolve to the same session; a concurrent
            # route transition must never be overwritten.
            with self._lock:
                for key, prior in previous.items():
                    candidate = self._entries.get(key)
                    if (
                        candidate is not None
                        and candidate.session_id == expected_session_id
                        and candidate.auto_skill_claim_token == active_turn_token
                    ):
                        (
                            candidate.auto_skill_pending,
                            candidate.auto_skill_claim_token,
                        ) = prior
            logger.warning(
                "gateway.session: auto-skill claim persistence failed for %s: %s",
                session_key,
                exc,
            )
            return False
        return True


    def resolve_session_after_turn_lease_wait(
        self,
        session_key: str,
        expected_session_id: str,
    ) -> Optional[SessionEntry]:
        """Re-resolve a contended turn before it can load or write history.

        A waiter may have resolved a proactive-rollover parent before the
        holder committed its child. Follow only the durable continuation tip,
        update the exact stale alias transactionally, and fail closed for an
        unrelated route transition or an ended parent without a continuation.
        """
        if not session_key or not expected_session_id:
            return None
        db = self._db_for_session_id(expected_session_id)
        if db is None:
            return None

        drain_lock = getattr(self, "_transcript_drain_lock", None)
        if drain_lock is None:
            drain_lock = threading.RLock()
            self._transcript_drain_lock = drain_lock

        with drain_lock:
            tip = self._compression_tip_for_session_id(expected_session_id)
            if not tip:
                return None
            try:
                expected_row = db.get_session(expected_session_id)
            except Exception:
                return None
            if (
                tip == expected_session_id
                and expected_row is not None
                and expected_row.get("end_reason") is not None
            ):
                return None

            with self._lock:
                self._ensure_loaded_locked()
                current = self._entries.get(session_key)
                if current is None:
                    return None
                if current.session_id == tip:
                    return current
                if current.session_id != expected_session_id:
                    return None
                if tip == expected_session_id:
                    return current

                template = next(
                    (
                        candidate
                        for candidate in self._entries.values()
                        if candidate.session_id == tip
                    ),
                    None,
                )
                if template is None:
                    refreshed = replace(current, session_id=tip)
                else:
                    refreshed = replace(
                        template,
                        session_key=current.session_key,
                        origin=current.origin,
                        display_name=current.display_name,
                        platform=current.platform,
                        chat_type=current.chat_type,
                    )
                previous = current
                self._entries[session_key] = refreshed
                reroutes = getattr(self, "_transcript_reroutes", None)
                if reroutes is None:
                    reroutes = {}
                    self._transcript_reroutes = reroutes
                previous_reroute = reroutes.get(expected_session_id)
                reroutes[expected_session_id] = tip
                try:
                    self._save()
                except Exception:
                    self._entries[session_key] = previous
                    if previous_reroute is None:
                        reroutes.pop(expected_session_id, None)
                    else:
                        reroutes[expected_session_id] = previous_reroute
                    raise
                return refreshed


    def rollover_session_with_carryover(
        self,
        session_key: str,
        expected_session_id: str,
        carryover_message: Dict[str, Any],
    ) -> Optional[SessionEntry]:
        """Atomically roll every alias after a successful high-context turn.

        The transcript-drain lock prevents an old-session append from crossing
        the database transition. Every routing key bound to the parent moves in
        the same database transaction, so a sibling alias cannot retain the
        ended parent or lose the fresh child's one-shot auto-skill state. The
        in-memory routes and legacy JSON mirror publish only after commit;
        state.db remains authoritative if the optional mirror write then fails.
        """
        from gateway.session import SessionEntry

        db = self._db_for_session_id(expected_session_id)
        if not db or not session_key or not expected_session_id:
            return None
        # ponytail: rollover is one SQLite transaction; separate multiplex routing stores
        # require a cross-store transaction before this optional rollover can run there.
        if db is not self._routing_db:
            return None

        drain_lock = getattr(self, "_transcript_drain_lock", None)
        if drain_lock is None:
            drain_lock = threading.RLock()
            self._transcript_drain_lock = drain_lock
        save_lock = getattr(self, "_save_lock", None)
        if save_lock is None:
            save_lock = threading.Lock()
            self._save_lock = save_lock

        with drain_lock:
            if self.has_dirty_transcript(expected_session_id):
                return None
            with self._lock:
                self._ensure_loaded_locked()
                old_entry = self._entries.get(session_key)
                if (
                    old_entry is None
                    or old_entry.session_id != expected_session_id
                ):
                    return None

                old_aliases = {
                    key: entry
                    for key, entry in self._entries.items()
                    if entry.session_id == expected_session_id
                }
                if session_key not in old_aliases:
                    return None

                now = _now()
                new_session_id = (
                    f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
                )
                new_entries: Dict[str, SessionEntry] = {}
                for alias_key, alias_entry in old_aliases.items():
                    new_entries[alias_key] = SessionEntry(
                        session_key=alias_key,
                        session_id=new_session_id,
                        created_at=now,
                        updated_at=now,
                        origin=alias_entry.origin,
                        display_name=alias_entry.display_name,
                        platform=alias_entry.platform,
                        chat_type=alias_entry.chat_type,
                        prev_session_id=expected_session_id,
                        is_fresh_reset=True,
                        auto_skill_pending=True,
                        # The parent turn still owns the exact route until its
                        # normal finally block. Carry that journal marker onto
                        # the child route so clean completion can clear it and
                        # an intervening crash remains recoverable. The fresh
                        # child skill itself is deliberately unclaimed.
                        active_turn_token=alias_entry.active_turn_token,
                        active_turn_started_at=(
                            alias_entry.active_turn_started_at
                        ),
                        model_override=(
                            dict(alias_entry.model_override)
                            if alias_entry.model_override
                            else None
                        ),
                    )
                new_entry = new_entries[session_key]
                new_entries_json = {
                    key: json.dumps(entry.to_dict())
                    for key, entry in new_entries.items()
                }
                new_entry_json = new_entries_json[session_key]
                origin_json = None
                if old_entry.origin is not None:
                    origin_json = json.dumps(old_entry.origin.to_dict())
                source_name = (
                    old_entry.platform.value if old_entry.platform else "unknown"
                )
                session_kwargs = {
                    "session_id": new_session_id,
                    "user_id": (
                        old_entry.origin.user_id if old_entry.origin else None
                    ),
                    "session_key": session_key,
                    "chat_id": (
                        old_entry.origin.chat_id if old_entry.origin else None
                    ),
                    "chat_type": (
                        old_entry.origin.chat_type if old_entry.origin else None
                    ),
                    "thread_id": (
                        old_entry.origin.thread_id if old_entry.origin else None
                    ),
                    "profile_name": (
                        old_entry.origin.profile if old_entry.origin else None
                    ),
                    "origin_json": origin_json,
                    "display_name": old_entry.display_name,
                    "parent_session_id": expected_session_id,
                    "model_config": {
                        "_reset_from": expected_session_id,
                        "_proactive_rollover": True,
                    },
                }

                with save_lock:
                    committed = db.atomic_gateway_rollover(
                        scope=self._routing_scope(),
                        session_key=session_key,
                        expected_session_id=expected_session_id,
                        new_entry_json=new_entry_json,
                        source=source_name,
                        session_kwargs=session_kwargs,
                        carryover_message=carryover_message,
                        new_entries_json=new_entries_json,
                    )
                    if not committed:
                        return None

                    # Publish process-local state only after the commit.
                    self._entries.update(new_entries)
                    reroutes = getattr(self, "_transcript_reroutes", None)
                    if reroutes is None:
                        reroutes = {}
                        self._transcript_reroutes = reroutes
                    reroutes[expected_session_id] = new_session_id

                    revision = self._next_routing_generation_locked()
                    self._persisted_routing_generation = max(
                        getattr(self, "_persisted_routing_generation", 0),
                        revision,
                    )
                    fast_entries = getattr(
                        self, "_fast_persisted_entries", None
                    )
                    if fast_entries is None:
                        fast_entries = {}
                        self._fast_persisted_entries = fast_entries
                    for alias_key, alias_entry_json in new_entries_json.items():
                        fast_entries[alias_key] = (revision, alias_entry_json)
                    mirror_data = {
                        key: entry.to_dict()
                        for key, entry in self._entries.items()
                    }

                    if getattr(self, "_write_sessions_json", True):
                        try:
                            self._save_sessions_json(mirror_data)
                        except Exception as exc:
                            logger.warning(
                                "gateway.session: sessions.json mirror failed "
                                "after proactive-rollover commit for %s: %s",
                                session_key,
                                exc,
                            )
                return new_entry
