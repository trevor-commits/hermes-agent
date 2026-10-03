"""Completion booking preserves explicit run failures and incomplete tails (#93820).

The scheduler booked every finished run as ``cron_complete`` based on the run
lifecycle alone: a job whose agent turn died after a tool call, mid-API-wait,
or without any assistant text still surfaced as a healthy run (one audited
day held 10 such silently-failed sessions). The fix classifies the session's
LAST message row through the existing ``session_lifecycle_statuses`` helper
before ``end_session``: only a real assistant reply — a plain answer or the
``[SILENT]`` sentinel, both assistant-text rows — books as ``cron_complete``;
anything else books as ``cron_incomplete_no_output``. A known failed run books
``cron_failed`` even when its diagnostic response looks like a normal reply. Classification is
best-effort: a probe failure keeps the historical reason rather than
mislabeling a healthy run.
"""

import os
import sqlite3

import pytest

import cron.scheduler as cron_scheduler
from gateway.session_context import reset_session_vars


class _FakeCronAgent:
    def __init__(self, *args, **kwargs):
        pass

    def run_conversation(self, prompt, **kwargs):
        return {
            "completed": True,
            "failed": False,
            "final_response": "done",
            "turn_exit_reason": "",
        }

    def close(self):
        pass


class _RecordingSessionDB:
    """SessionDB double with a configurable lifecycle classification."""

    def __init__(self, *args, **kwargs):
        self.ended: list[tuple[str, str]] = []
        self.lifecycle = type(self).next_lifecycle

    next_lifecycle = "complete"

    def set_session_title(self, *args, **kwargs):
        return True

    def get_compression_tip(self, session_id):
        return None

    def session_lifecycle_statuses(self, session_ids):
        if isinstance(type(self).next_lifecycle, Exception):
            raise type(self).next_lifecycle
        return {sid: type(self).next_lifecycle for sid in session_ids}

    def end_session(self, session_id, reason):
        self.ended.append((session_id, reason))

    def close(self):
        pass


def _run_booked_job(
    monkeypatch, tmp_path, *, db_class=_RecordingSessionDB, agent_class=_FakeCronAgent,
):
    import hermes_state
    import run_agent

    instances = []

    def _capture_db(*args, **kwargs):
        db = db_class(*args, **kwargs)
        instances.append(db)
        return db

    monkeypatch.setattr(hermes_state, "SessionDB", _capture_db)
    monkeypatch.setattr(run_agent, "AIAgent", agent_class)
    monkeypatch.setattr(
        "hermes_constants.resolve_reasoning_config", lambda *_a, **_k: None
    )
    # The runtime key is read from the environment (never a literal here);
    # AIAgent and SessionDB are fakes above, so the value is never used.
    monkeypatch.setenv("HERMES_TEST_RUNTIME_KEY", "unused-placeholder")

    def _fake_runtime(**_kwargs):
        return {
            "api_key": os.environ.get("HERMES_TEST_RUNTIME_KEY", ""),
            "base_url": None,
            "provider": "test-provider",
            "api_mode": None,
            "command": None,
            "args": None,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _fake_runtime
    )
    monkeypatch.setattr("tools.mcp_tool_discovery.discover_mcp_tools", lambda: [])
    monkeypatch.setattr(cron_scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cron_scheduler, "get_fallback_chain", lambda _cfg: [])
    monkeypatch.setattr(
        cron_scheduler, "_guard_job_credential_exfil", lambda _job: None
    )
    result = cron_scheduler.run_job(
        {
            "id": "verify-complete",
            "name": "Verification",
            "prompt": "Do the thing",
            "schedule_display": "manual",
        }
    )
    return instances, result


@pytest.fixture(autouse=True)
def _clean_state():
    reset_session_vars()
    _RecordingSessionDB.next_lifecycle = "complete"
    yield
    reset_session_vars()


def test_run_without_final_assistant_message_books_incomplete(monkeypatch, tmp_path):
    """Last row a tool result / pending call (lifecycle 'interrupted') must
    not surface as a healthy complete run."""
    _RecordingSessionDB.next_lifecycle = "interrupted"

    instances, result = _run_booked_job(monkeypatch, tmp_path)

    assert result[0] is True
    assert instances, "SessionDB was never constructed"
    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_incomplete_no_output"]


def test_run_with_final_assistant_reply_books_complete(monkeypatch, tmp_path):
    """A real assistant reply (plain answer or [SILENT] — both assistant
    text rows) keeps the healthy booking."""
    instances, result = _run_booked_job(monkeypatch, tmp_path)

    assert result[0] is True
    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_complete"]


def test_classification_probe_failure_keeps_historical_reason(monkeypatch, tmp_path):
    """Best-effort metadata: a failing classifier must not mislabel a run."""
    _RecordingSessionDB.next_lifecycle = RuntimeError("db busy")

    instances, result = _run_booked_job(monkeypatch, tmp_path)

    assert result[0] is True
    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_complete"]


@pytest.mark.parametrize(
    "outcome,tail_role,expected_success,expected_reason,expected_status",
    [
        ({"completed": False, "failed": True, "final_response": "Provider unavailable",
          "error": "HTTP 402: insufficient balance"}, "assistant", False, "cron_failed", "error"),
        ({"completed": True, "failed": False, "final_response": "done"},
         "assistant", True, "cron_complete", "complete"),
        ({"completed": True, "failed": False, "final_response": "[SILENT]"},
         "assistant", True, "cron_complete", "complete"),
        ({"completed": False, "interrupted": True, "final_response": "Stopped"},
         "assistant", False, "cron_failed", "error"),
        (RuntimeError("provider request failed"), "assistant", False, "cron_failed", "error"),
        ({"completed": True, "failed": False, "final_response": "done"},
         "tool", True, "cron_incomplete_no_output", "interrupted"),
        ({"completed": True, "failed": False, "final_response": "done"},
         "user", True, "cron_incomplete_no_output", "interrupted"),
    ],
)
def test_run_outcome_and_persisted_session_agree(
    monkeypatch, tmp_path, outcome, tail_role, expected_success, expected_reason, expected_status,
):
    """A saved diagnostic reply is not evidence that a failed agent run succeeded."""
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"

    class PersistingAgent(_FakeCronAgent):
        def __init__(self, *, session_id, session_db, **kwargs):
            self.session_id, self.db = session_id, session_db

        def run_conversation(self, prompt, **kwargs):
            self.db.create_session(self.session_id, "cron")
            self.db.append_message(self.session_id, "user", prompt)
            if tail_role != "user":
                reply = str(outcome) if isinstance(outcome, Exception) else outcome["final_response"]
                self.db.append_message(self.session_id, tail_role, reply)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    def open_db(**kwargs):
        return SessionDB(db_path=db_path)

    _, result = _run_booked_job(
        monkeypatch, tmp_path, db_class=open_db, agent_class=PersistingAgent,
    )

    assert result[0] is expected_success
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as saved:
        sid, end_reason, ended_at = saved.execute("SELECT id, end_reason, ended_at FROM sessions").fetchone()
        last_role, finish_reason = saved.execute(
            "SELECT role, finish_reason FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert last_role == tail_role
    assert finish_reason is None
    assert ended_at is not None
    assert end_reason == expected_reason
    reopened = SessionDB(db_path=db_path, read_only=True)
    try:
        assert reopened.session_lifecycle_statuses([sid]) == {sid: expected_status}
    finally:
        reopened.close()


def test_known_failure_cannot_fail_open_on_lifecycle_probe(monkeypatch, tmp_path):
    class FailedAgent(_FakeCronAgent):
        def run_conversation(self, prompt, **kwargs):
            return {"completed": False, "failed": True, "final_response": "Provider unavailable"}

    _RecordingSessionDB.next_lifecycle = RuntimeError("db busy")
    instances, result = _run_booked_job(monkeypatch, tmp_path, agent_class=FailedAgent)

    assert result[0] is False
    assert [reason for _sid, reason in instances[0].ended] == ["cron_failed"]
