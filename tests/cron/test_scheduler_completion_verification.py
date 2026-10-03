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

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

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
    job_fields=None, execution_id=None, cancel_event=None, dispatch=False,
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
    job = {
        "id": "verify-complete",
        "name": "Verification",
        "prompt": "Do the thing",
        "schedule_display": "manual",
        **(job_fields or {}),
    }
    result = (
        cron_scheduler.run_one_job(job, cancel_event=cancel_event) if dispatch
        else cron_scheduler.run_job(job, execution_id=execution_id, cancel_event=cancel_event)
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


def test_explicit_failure_marker_is_applied_before_session_finalization(monkeypatch, tmp_path):
    response = "[CRON_FAILURE]\nThe required receipt is partial."

    class PartialAgent(_FakeCronAgent):
        def run_conversation(self, prompt, **kwargs):
            return {"completed": True, "failed": False, "final_response": response}

    instances, result = _run_booked_job(monkeypatch, tmp_path, agent_class=PartialAgent)

    assert result[0] is False
    assert result[2] == response
    assert result[3] == "The required receipt is partial."
    assert [reason for _sid, reason in instances[0].ended] == ["cron_failed"]


@pytest.mark.parametrize("receipt,expected_success", [
    ({"status": "complete", "execution_id": "current-fire"}, True),
    ({"status": "partial", "execution_id": "current-fire"}, False),
    ({"status": "complete", "execution_id": "older-fire"}, False),
    (None, False),
])
def test_completion_check_gates_audit_and_session_before_success(
    monkeypatch, tmp_path, receipt, expected_success,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "verify.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "p = Path(os.environ['HERMES_HOME']) / 'receipt.json'\n"
        "r = json.loads(p.read_text()) if p.exists() else {}\n"
        "ok = r.get('status') == 'complete' and r.get('execution_id') == os.environ['HERMES_CRON_EXECUTION_ID']\n"
        "print(json.dumps({'verified': ok}))\nraise SystemExit(0 if ok else 2)\n"
    )
    if receipt is not None:
        (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    audit = []
    monkeypatch.setattr(cron_scheduler._FireAudit, "write", lambda self, result, error: audit.append(error))
    instances, result = _run_booked_job(
        monkeypatch, tmp_path, job_fields={"completion_script": "verify.py"}, execution_id="current-fire",
    )
    assert result[0] is expected_success
    assert result[2] == "done"  # Keep the agent's diagnostic response even when its claim is rejected.
    assert "done" in result[1]
    assert len(audit) == 1
    assert (audit[0] is None) is expected_success
    if not expected_success:
        assert "Script exited with code 2" in result[3]
        assert audit == [result[3]]
    assert [reason for _sid, reason in instances[0].ended] == [
        "cron_complete" if expected_success else "cron_failed"
    ]


@pytest.mark.parametrize("response,script_exit,expected_success", [
    ("done", 0, True),
    ("done", 2, False),
    ("[CRON_FAILURE]\nThe required receipt is partial.", 0, False),
])
def test_completion_outcome_agrees_in_native_job_ledger_and_session(
    monkeypatch, tmp_path, response, script_exit, expected_success,
):
    from cron import executions, jobs
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"

    class PersistingAgent(_FakeCronAgent):
        def __init__(self, *, session_id, session_db, **kwargs):
            self.session_id, self.db = session_id, session_db

        def run_conversation(self, prompt, **kwargs):
            self.db.create_session(self.session_id, "cron")
            self.db.append_message(self.session_id, "user", prompt)
            self.db.append_message(self.session_id, "assistant", response)
            return {"completed": True, "failed": False, "final_response": response}

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "verify.py").write_text(
        "import os, sqlite3\nfrom pathlib import Path\n"
        "home = Path(os.environ['HERMES_HOME'])\n"
        "with sqlite3.connect(f'file:{home}/cron/executions.db?mode=ro', uri=True) as db:\n"
        "    row = db.execute('SELECT job_id, status FROM executions WHERE id=?', "
        "(os.environ['HERMES_CRON_EXECUTION_ID'],)).fetchone()\n"
        "assert row == (os.environ['HERMES_CRON_JOB_ID'], 'running')\n"
        "(home / 'check-ran').write_text('verified the exact running execution')\n"
        f"raise SystemExit({script_exit})\n"
    )
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr(cron_scheduler, "_launch_external_cron_worker", lambda _job: False)
    token = set_hermes_home_override(tmp_path)
    try:
        with jobs.use_cron_store(tmp_path):
            job = jobs.create_job(prompt="Do the thing", schedule="every 1h", deliver="local",
                                  completion_script="verify.py")
            _, processed = _run_booked_job(
                monkeypatch, tmp_path, db_class=lambda **kw: SessionDB(db_path=db_path),
                agent_class=PersistingAgent, job_fields=job, dispatch=True,
            )
            assert processed is True
            saved = jobs.get_job(job["id"])
            assert saved["last_status"] == ("ok" if expected_success else "error")
            assert saved["failure_streak"] == (0 if expected_success else 1)
            ledger = executions.list_executions(job_id=job["id"])
    finally:
        reset_hermes_home_override(token)

    assert len(ledger) == 1
    assert ledger[0]["status"] == ("completed" if expected_success else "failed")
    assert ledger[0]["error"] == saved["last_error"]
    assert (tmp_path / "check-ran").exists() is (not response.startswith("[CRON_FAILURE]"))
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        end_reason, = db.execute("SELECT end_reason FROM sessions").fetchone()
    assert end_reason == ("cron_complete" if expected_success else "cron_failed")


def test_completion_check_requires_host_identity_without_running_script(monkeypatch, tmp_path):
    from cron import scheduler_script

    def unexpected_script(*args, **kwargs):
        pytest.fail("script must not run without host identity")

    monkeypatch.setattr(scheduler_script, "_run_job_script", unexpected_script)
    instances, result = _run_booked_job(
        monkeypatch, tmp_path,
        job_fields={"completion_script": "verify.py", "execution_id": "untrusted-stored-fire"},
    )
    assert result[0] is False
    assert "host execution and job identity are required" in result[3]
    assert [reason for _sid, reason in instances[0].ended] == ["cron_failed"]


@pytest.mark.parametrize("completion_script", [None, "", "  "])
def test_unconfigured_check_preserves_recoverable_tool_failure(monkeypatch, tmp_path, completion_script):
    class RecoveredAgent(_FakeCronAgent):
        def run_conversation(self, prompt, **kwargs):
            return {"completed": True, "failed": False, "final_response": "Recovered successfully.",
                    "tool_results": [{"exit_code": 2}]}

    instances, result = _run_booked_job(
        monkeypatch, tmp_path, agent_class=RecoveredAgent,
        job_fields={"completion_script": completion_script},
    )
    assert result[0] is True
    assert result[3] is None
    assert [reason for _sid, reason in instances[0].ended] == ["cron_complete"]


def test_quoted_failure_marker_remains_ordinary_response(monkeypatch, tmp_path):
    class QuotingAgent(_FakeCronAgent):
        def run_conversation(self, prompt, **kwargs):
            return {"completed": True, "failed": False,
                    "final_response": "The old report quoted [CRON_FAILURE]; the current check passed."}

    instances, result = _run_booked_job(monkeypatch, tmp_path, agent_class=QuotingAgent)
    assert result[0] is True
    assert [reason for _sid, reason in instances[0].ended] == ["cron_complete"]


@pytest.mark.parametrize("script", ["missing.py", "../outside.py"])
def test_completion_check_keeps_script_path_guard(monkeypatch, tmp_path, script):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "outside.py").write_text("raise SystemExit(0)\n")
    instances, result = _run_booked_job(
        monkeypatch, tmp_path, job_fields={"completion_script": script}, execution_id="fire",
    )
    assert result[0] is False
    assert "Script not found" in result[3] or "outside the scripts directory" in result[3]
    assert [reason for _sid, reason in instances[0].ended] == ["cron_failed"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_completion_check_keeps_timeout_and_cancellation(monkeypatch, tmp_path, cancelled):
    from cron.scheduler_script import _completion_script_error

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "slow.py").write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(cron_scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cron_scheduler, "_SCRIPT_TIMEOUT", 2)
    cancel = threading.Event()
    if cancelled:
        cancel.set()
    error = _completion_script_error(
        {"id": "job", "completion_script": "slow.py"}, execution_id="fire", cancel_event=cancel,
    )
    assert ("cancelled" if cancelled else "timed out") in error


def test_completion_check_scopes_concurrent_profiles_and_restores_parent_env(monkeypatch, tmp_path):
    from cron.scheduler_script import _completion_script_error
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override

    monkeypatch.setattr(cron_scheduler, "_get_hermes_home", get_hermes_home)
    monkeypatch.setenv("HERMES_CRON_EXECUTION_ID", "parent-fire")
    monkeypatch.setenv("HERMES_CRON_JOB_ID", "parent-job")
    parent_env = dict(os.environ)
    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    for home in homes:
        (home / "scripts").mkdir(parents=True)
        (home / "scripts" / "verify.py").write_text(
            "import json, os\nfrom pathlib import Path\n"
            "p = Path(__file__).resolve().parents[1]\n"
            "got = {k: os.environ[k] for k in ('HERMES_HOME', 'HERMES_CRON_JOB_ID', 'HERMES_CRON_EXECUTION_ID')}\n"
            "(p / 'seen.json').write_text(json.dumps(got))\n"
            "assert Path(got['HERMES_HOME']).resolve() == p\n"
            "assert got['HERMES_CRON_JOB_ID'] == 'same-job'\n"
            "assert got['HERMES_CRON_EXECUTION_ID'] == p.name\n"
        )

    def verify(home):
        token = set_hermes_home_override(home)
        try:
            return _completion_script_error(
                {"id": "same-job", "completion_script": "verify.py"}, execution_id=home.name,
            )
        finally:
            reset_hermes_home_override(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(verify, homes)) == [None, None]
    assert verify(homes[0]) is None  # A -> B -> A does not retain the sibling's identity.
    for home in homes:
        seen = json.loads((home / "seen.json").read_text())
        assert seen["HERMES_CRON_EXECUTION_ID"] == home.name
    assert dict(os.environ) == parent_env
