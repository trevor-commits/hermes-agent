"""Cron guards against the 30K hard context ceiling.

Three independent defences, one per failure mode observed on the live fleet:

* **B1** — a job whose STATIC first-turn prompt already exceeds the ceiling is
  refused at definition time instead of burning a scheduled run every 4 hours.
* **B3** — a run-scoped cumulative tool-output budget bounds the SUM of tool
  results, which the per-result ``tool_result_max_chars`` cap cannot.
* **B4** — three consecutive identical hard-ceiling failures auto-pause the job
  and alert the operator once.
"""

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cron.jobs import (
    _jobs_lock,
    DEFAULT_HARD_CONTEXT_CEILING_TOKENS,
    HARD_CONTEXT_CEILING_PAUSE_AFTER,
    create_job,
    estimate_job_first_turn_tokens,
    get_job,
    load_jobs,
    mark_job_run,
    pause_job,
    resolve_hard_context_ceiling_tokens,
    resume_job,
    save_jobs,
    trigger_job,
    update_job,
)


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory (mirrors tests/cron/test_jobs.py)."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


# estimate_tokens_rough is ~chars/4 for ASCII, so 121K chars ~ 30.2K tokens —
# just past the 30K ceiling, in the same shape as the live 48K-token offender.
# Kept only just over the line: create_job's pre-existing gateway-lifecycle
# scan tokenizes the whole prompt, and its cost grows with prompt length.
OVERSIZED_PROMPT = "summarize this dossier: " + ("x" * 121_000)
CEILING_ERROR = "hard context ceiling"


# =========================================================================
# B1 — definition-time first-turn size gate
# =========================================================================

class TestFirstTurnSizeGate:
    def test_usable_context_is_eighty_percent_of_hard_ceiling(self):
        from cron.context_budget import evaluate_context_parts

        evaluation = evaluate_context_parts(
            ["static", "skill"],
            hard_ceiling_tokens=30_000,
            token_estimator=lambda text: {"static": 10_000, "skill": 14_000}[text],
        )

        assert evaluation.usable_tokens == 24_000
        assert evaluation.estimated_tokens == 24_000
        assert evaluation.exceeded is True

    def test_ceiling_defaults_to_30k_when_config_leaves_it_unset(self):
        """Stock config ships ``threshold_tokens: None`` — that must not read
        as "no ceiling", or the gate is a silent no-op everywhere."""
        assert DEFAULT_HARD_CONTEXT_CEILING_TOKENS == 30_000
        with patch("hermes_cli.config.read_user_config_raw",
                   return_value={"compression": {"threshold_tokens": None}}):
            assert resolve_hard_context_ceiling_tokens() == 30_000

    def test_ceiling_honours_an_explicitly_configured_threshold(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("compression:\n  threshold_tokens: 12000\n")
        with patch("cron.jobs.get_hermes_home", return_value=tmp_path):
            assert resolve_hard_context_ceiling_tokens() == 12_000

    def test_ceiling_fails_open_when_config_is_unreadable(self):
        with patch("hermes_cli.config.read_user_config_raw",
                   side_effect=OSError("boom")):
            assert resolve_hard_context_ceiling_tokens() == 30_000

    def test_create_rejects_a_prompt_that_can_never_fit(self, tmp_cron_dir):
        with pytest.raises(ValueError, match=CEILING_ERROR) as exc:
            create_job(prompt=OVERSIZED_PROMPT, schedule="every 4h")

        message = str(exc.value)
        # The operator needs both numbers to know how far over they are.
        assert "30,000" in message
        assert f"{estimate_job_first_turn_tokens({'prompt': OVERSIZED_PROMPT}):,}" in message

    def test_create_accepts_an_ordinary_prompt(self, tmp_cron_dir):
        job = create_job(prompt="check the build queue", schedule="every 4h")
        assert job["prompt"] == "check the build queue"
        assert get_job(job["id"]) is not None

    def test_create_rejects_oversized_attached_skill(self, tmp_cron_dir):
        payload = json.dumps(
            {"success": True, "content": "s" * 96_000}
        )
        with patch("tools.skills_tool.skill_view", return_value=payload):
            with pytest.raises(ValueError, match="usable context budget"):
                create_job(
                    prompt="run the attached workflow",
                    skills=["oversized-static-skill"],
                    schedule="every 4h",
                )

    def test_create_rejects_oversized_attached_bundle(self, tmp_cron_dir):
        segment = ("bundle-member", "b" * 96_000)
        payload = ("bundle invocation", ["bundle-member"], [], [segment])
        with patch(
            "agent.skill_bundles.resolve_bundle_command_key",
            return_value="oversized-bundle",
        ), patch(
            "agent.skill_bundles.build_bundle_invocation_message",
            return_value=payload,
        ):
            with pytest.raises(ValueError, match="usable context budget"):
                create_job(
                    prompt="run the bundle",
                    skills=["oversized-bundle"],
                    schedule="every 4h",
                )

    def test_update_rejects_new_oversized_skill(self, tmp_cron_dir):
        job = create_job(prompt="small", schedule="every 4h")
        payload = json.dumps(
            {"success": True, "content": "u" * 96_000}
        )
        with patch("tools.skills_tool.skill_view", return_value=payload):
            with pytest.raises(ValueError, match="usable context budget"):
                update_job(job["id"], {"skills": ["oversized-update-skill"]})

    def test_explicit_ceiling_parameter_overrides_the_configured_one(self, tmp_cron_dir):
        with pytest.raises(ValueError, match=CEILING_ERROR):
            create_job(
                prompt="x" * 4_000,          # ~1,000 tokens: fine at 30K
                schedule="every 4h",
                first_turn_ceiling_tokens=100,
            )

    def test_no_agent_jobs_are_exempt(self, tmp_cron_dir):
        """A no_agent job never builds a model turn, so the ceiling can't bite."""
        job = create_job(
            prompt="x" * 4_000,          # ~1,000 tokens, well over the ceiling below
            schedule="every 4h",
            script="watchdog.sh",
            no_agent=True,
            first_turn_ceiling_tokens=100,
        )
        assert job["no_agent"] is True

    def _seed_legacy_oversized_job(self, prompt=OVERSIZED_PROMPT):
        """Write an oversized record straight to the store.

        A legacy job predates the gate, so it must be seeded the way it was
        actually written — not through create_job/update_job, which now
        (correctly) refuse it.
        """
        job = create_job(prompt="placeholder", schedule="every 4h")
        with _jobs_lock():
            jobs = load_jobs()
            for record in jobs:
                if record["id"] == job["id"]:
                    record["prompt"] = prompt
            save_jobs(jobs)
        return job["id"]

    def test_edit_that_shrinks_an_oversized_legacy_job_is_allowed(self, tmp_cron_dir):
        """The gate must never trap an existing job in its broken state."""
        job_id = self._seed_legacy_oversized_job(OVERSIZED_PROMPT * 2)

        # Still far over the ceiling, but strictly smaller — accepted.
        shrunk = update_job(job_id, {"prompt": OVERSIZED_PROMPT})
        assert shrunk["prompt"] == OVERSIZED_PROMPT

        # All the way down under the ceiling — obviously accepted.
        assert update_job(job_id, {"prompt": "short"})["prompt"] == "short"

    def test_edit_that_grows_an_oversized_job_is_rejected(self, tmp_cron_dir):
        job_id = self._seed_legacy_oversized_job()

        with pytest.raises(ValueError, match=CEILING_ERROR):
            update_job(job_id, {"prompt": OVERSIZED_PROMPT + " and one more thing"})

    def test_edit_of_an_unrelated_field_is_never_gated(self, tmp_cron_dir):
        """Only prompt edits change the estimate, so only they are checked."""
        job_id = self._seed_legacy_oversized_job()
        assert update_job(job_id, {"name": "renamed"})["name"] == "renamed"

    def test_gate_does_not_block_pausing_an_oversized_legacy_job(self, tmp_cron_dir):
        """pause/resume/trigger all route through update_job — an unscoped
        gate would leave a broken job unpausable."""
        job_id = self._seed_legacy_oversized_job()

        assert pause_job(job_id, reason="operator")["state"] == "paused"
        assert resume_job(job_id)["state"] == "scheduled"


# =========================================================================
# B3 — cumulative per-run tool-output budget
# =========================================================================

class TestCumulativeToolOutputBudget:
    def test_validation_range_on_create(self, tmp_cron_dir):
        job = create_job(
            prompt="collect",
            schedule="every 1h",
            tool_result_total_max_chars=40_000,
        )
        assert job["tool_result_total_max_chars"] == 40_000

        for invalid in (True, 0, -1, 3_999, 400_001, "40000"):
            with pytest.raises(ValueError, match="tool_result_total_max_chars"):
                create_job(
                    prompt="collect",
                    schedule="every 1h",
                    tool_result_total_max_chars=invalid,
                )

    def test_omitted_budget_leaves_the_field_absent(self, tmp_cron_dir):
        job = create_job(prompt="collect", schedule="every 1h")
        assert "tool_result_total_max_chars" not in job

    def test_update_validates_the_budget_too(self, tmp_cron_dir):
        job = create_job(prompt="collect", schedule="every 1h")
        assert update_job(
            job["id"], {"tool_result_total_max_chars": 4_000}
        )["tool_result_total_max_chars"] == 4_000

        with pytest.raises(ValueError, match="tool_result_total_max_chars"):
            update_job(job["id"], {"tool_result_total_max_chars": 999})

    def test_scheduler_resolver_matches_the_store_range(self):
        from cron.scheduler import _resolve_cron_tool_result_total_max_chars

        assert _resolve_cron_tool_result_total_max_chars({}) is None
        assert _resolve_cron_tool_result_total_max_chars(
            {"tool_result_total_max_chars": 4_000}
        ) == 4_000
        for invalid in (True, 0, 3_999, 400_001, "4000"):
            with pytest.raises(ValueError, match="tool_result_total_max_chars"):
                _resolve_cron_tool_result_total_max_chars(
                    {"tool_result_total_max_chars": invalid}
                )

    def _budgeted_agent(self, budget):
        import threading

        return SimpleNamespace(
            tool_result_total_max_chars=budget,
            _tool_result_total_chars_used=0,
            _tool_result_budget_lock=threading.Lock(),
            tool_result_budget_withheld_count=0,
        )

    def test_results_pass_through_until_the_budget_is_reached(self):
        from agent.tool_executor import _apply_run_tool_output_budget

        agent = self._budgeted_agent(10_000)
        first = "a" * 6_000
        assert _apply_run_tool_output_budget(agent, first) == first
        assert agent.tool_result_budget_withheld_count == 0

    def test_results_past_the_budget_are_withheld_not_failed(self):
        from agent.tool_executor import _apply_run_tool_output_budget

        agent = self._budgeted_agent(10_000)
        _apply_run_tool_output_budget(agent, "a" * 6_000)
        # The crossing result consumes exactly the remaining 4,000 chars.
        crossing = _apply_run_tool_output_budget(agent, "b" * 6_000)
        assert len(crossing) == 4_000
        assert crossing.endswith(
            "[tool result truncated: run tool-output budget exhausted]"
        )
        assert agent._tool_result_total_chars_used == 10_000
        assert agent.tool_result_budget_withheld_count == 1

        withheld = _apply_run_tool_output_budget(agent, "c" * 6_000)
        assert withheld == ""
        assert _apply_run_tool_output_budget(agent, "d" * 50) == ""
        assert agent._tool_result_total_chars_used == 10_000
        assert agent.tool_result_budget_withheld_count == 3

    def test_crossing_and_later_results_persist_complete_output_first(self):
        from agent.tool_executor import _apply_run_tool_output_budget

        class RecordingEnv:
            def __init__(self):
                self.writes = []

            def get_temp_dir(self):
                return "/tmp/test-cron-budget"

            def execute(self, _cmd, timeout, stdin_data):
                self.writes.append((timeout, stdin_data))
                return {"returncode": 0}

        agent = self._budgeted_agent(100)
        agent._tool_result_total_chars_used = 80
        env = RecordingEnv()

        crossing_full = "crossing-full-output" * 5
        emitted = _apply_run_tool_output_budget(
            agent,
            "x" * 50,
            full_content=crossing_full,
            tool_name="terminal",
            tool_use_id="crossing",
            env=env,
        )
        later_full = "later-full-output" * 5
        later = _apply_run_tool_output_budget(
            agent,
            "y" * 50,
            full_content=later_full,
            tool_name="terminal",
            tool_use_id="later",
            env=env,
        )

        assert len(emitted) == 20
        assert later == ""
        assert [write[1] for write in env.writes] == [crossing_full, later_full]

    def test_subdirectory_hint_is_applied_before_emitted_cap(self):
        from agent.tool_executor import _prepare_text_tool_result_for_context
        from tools.budget_config import DEFAULT_BUDGET

        class RecordingEnv:
            def get_temp_dir(self):
                return "/tmp/test-cron-hints"

            def execute(self, _cmd, timeout, stdin_data):
                return {"returncode": 0}

        agent = self._budgeted_agent(12)
        emitted = _prepare_text_tool_result_for_context(
            agent,
            "abcdefghij",
            tool_name="terminal",
            tool_use_id="hint-order",
            env=RecordingEnv(),
            persistence_config=DEFAULT_BUDGET,
            subdir_hints="HINT",
        )

        assert len(emitted) == 12
        assert emitted.endswith("…")
        assert agent._tool_result_total_chars_used == 12

    def test_no_budget_is_a_no_op(self):
        from agent.tool_executor import _apply_run_tool_output_budget

        agent = self._budgeted_agent(None)
        payload = "z" * 500_000
        assert _apply_run_tool_output_budget(agent, payload) == payload
        assert agent.tool_result_budget_withheld_count == 0

    def test_agents_without_the_attributes_are_unaffected(self):
        """Legacy/test doubles must not start raising AttributeError."""
        from agent.tool_executor import _apply_run_tool_output_budget

        assert _apply_run_tool_output_budget(SimpleNamespace(), "hello") == "hello"

    def test_run_job_wires_the_budget_and_surfaces_truncation(self, tmp_path):
        """The withheld-notice must reach the run's stored output, not just
        the model — otherwise an operator reads a thin report as a quiet run."""
        from cron.scheduler import run_job

        fake_db = MagicMock()
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "partial report"}
        # Stand in for what _apply_run_tool_output_budget records during the run.
        mock_agent.tool_result_budget_withheld_count = 3

        job = {
            "id": "budgeted-job",
            "name": "budgeted",
            "prompt": "collect everything",
            "tool_result_total_max_chars": 40_000,
        }
        with contextlib.ExitStack() as stack:
            for cm in (
                patch("cron.scheduler._hermes_home", tmp_path),
                patch("cron.scheduler._resolve_origin", return_value=None),
                patch("hermes_cli.env_loader.load_hermes_dotenv"),
                patch("hermes_cli.env_loader.reset_secret_source_cache"),
                patch("hermes_state.SessionDB", return_value=fake_db),
                patch(
                    "hermes_cli.runtime_provider.resolve_runtime_provider",
                    return_value={
                        "api_key": "test-key",
                        "base_url": "https://example.invalid/v1",
                        "provider": "openrouter",
                        "api_mode": "chat_completions",
                    },
                ),
                patch("run_agent.AIAgent", return_value=mock_agent),
            ):
                stack.enter_context(cm)
            success, output, final_response, error = run_job(job)

        assert success is True
        assert error is None
        assert final_response == "partial report"
        assert mock_agent.tool_result_total_max_chars == 40_000
        assert "3 tool result(s) withheld" in output
        assert "40,000" in output

    def test_failed_run_carries_the_truncation_notice_into_last_error(self, tmp_path):
        """A failure that followed a truncated tool loop must say so in
        last_error, not just in the success-path output doc."""
        from cron.scheduler import run_job

        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {
            "failed": True,
            "completed": False,
            "error": "RuntimeError: something broke",
        }
        mock_agent.tool_result_budget_withheld_count = 2

        job = {
            "id": "budgeted-failure-job",
            "name": "budgeted failure",
            "prompt": "collect everything",
            "tool_result_total_max_chars": 40_000,
        }
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state.SessionDB", return_value=MagicMock()), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "test-key",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent", return_value=mock_agent):
            success, output, _final, error = run_job(job)

        assert success is False
        assert "something broke" in error
        assert "2 tool result(s) withheld" in error
        assert "2 tool result(s) withheld" in output

    def test_run_job_rejects_a_malformed_stored_budget(self, tmp_path):
        from cron.scheduler import run_job

        job = {
            "id": "bad-budget-job",
            "name": "bad budget",
            "prompt": "collect",
            "tool_result_total_max_chars": 10,
        }
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state.SessionDB", return_value=MagicMock()), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "test-key",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent", return_value=MagicMock()):
            success, _output, _final, error = run_job(job)

        assert success is False
        assert "tool_result_total_max_chars" in error


# =========================================================================
# B4 — auto-pause after repeated identical hard-ceiling failures
# =========================================================================

CEILING_ERROR_TEXT = (
    "RuntimeError: hard_context_ceiling_blocked:compression_attempts_exhausted"
)


class TestHardCeilingAutoPause:
    def _job(self):
        return create_job(prompt="daily digest", schedule="every 4h")

    def test_pauses_on_the_third_consecutive_matching_failure(self, tmp_cron_dir):
        job = self._job()

        assert mark_job_run(job["id"], False, CEILING_ERROR_TEXT) is None
        assert get_job(job["id"])["state"] == "scheduled"
        assert mark_job_run(job["id"], False, CEILING_ERROR_TEXT) is None
        assert get_job(job["id"])["state"] == "scheduled"

        reason = mark_job_run(job["id"], False, CEILING_ERROR_TEXT)
        assert reason is not None
        assert f"{HARD_CONTEXT_CEILING_PAUSE_AFTER} consecutive" in reason
        assert CEILING_ERROR_TEXT in reason  # the exact last_error, verbatim

        paused = get_job(job["id"])
        assert paused["state"] == "paused"
        assert paused["enabled"] is False
        assert paused["paused_at"]
        assert paused["paused_reason"] == reason

    def test_alerts_only_once_while_it_stays_paused(self, tmp_cron_dir):
        job = self._job()
        for _ in range(HARD_CONTEXT_CEILING_PAUSE_AFTER):
            reason = mark_job_run(job["id"], False, CEILING_ERROR_TEXT)
        assert reason

        assert mark_job_run(job["id"], False, CEILING_ERROR_TEXT) is None
        assert get_job(job["id"])["state"] == "paused"

    def test_an_ok_run_breaks_the_streak(self, tmp_cron_dir):
        job = self._job()
        mark_job_run(job["id"], False, CEILING_ERROR_TEXT)
        mark_job_run(job["id"], False, CEILING_ERROR_TEXT)
        mark_job_run(job["id"], True)
        cleared = get_job(job["id"])
        assert cleared.get("hard_context_ceiling_streak") is None
        assert cleared.get("hard_context_ceiling_fingerprint") is None

        for _ in range(2):
            assert mark_job_run(job["id"], False, CEILING_ERROR_TEXT) is None
        assert get_job(job["id"])["state"] == "scheduled"

    def test_a_different_failure_breaks_the_streak(self, tmp_cron_dir):
        job = self._job()
        mark_job_run(job["id"], False, CEILING_ERROR_TEXT)
        mark_job_run(job["id"], False, CEILING_ERROR_TEXT)
        mark_job_run(job["id"], False, "TimeoutError: provider did not respond")

        for _ in range(2):
            assert mark_job_run(job["id"], False, CEILING_ERROR_TEXT) is None
        assert get_job(job["id"])["state"] == "scheduled"

    def test_changed_hard_ceiling_fingerprint_restarts_the_streak(self, tmp_cron_dir):
        job = self._job()
        other = "hard_context_ceiling_blocked:input_too_large"
        mark_job_run(job["id"], False, CEILING_ERROR_TEXT)
        mark_job_run(job["id"], False, CEILING_ERROR_TEXT)

        assert mark_job_run(job["id"], False, other) is None
        changed = get_job(job["id"])
        assert changed["hard_context_ceiling_streak"] == 1
        assert changed["hard_context_ceiling_fingerprint"] == other

        assert mark_job_run(job["id"], False, other) is None
        assert mark_job_run(job["id"], False, other) is not None

    def test_terminal_completion_wins_over_the_auto_pause_alert(self, tmp_cron_dir):
        """A job that also exhausts its repeat limit is finished, not paused —
        alerting "paused" there would misdescribe a completed record."""
        job = create_job(prompt="thrice", schedule="every 4h", repeat=3)
        for _ in range(HARD_CONTEXT_CEILING_PAUSE_AFTER):
            reason = mark_job_run(job["id"], False, CEILING_ERROR_TEXT)

        assert reason is None
        finished = get_job(job["id"])
        assert finished["state"] == "completed"
        # The diagnosis still rides along on the record.
        assert "consecutive hard-context-ceiling failures" in finished["paused_reason"]

    def test_resume_clears_the_streak(self, tmp_cron_dir):
        job = self._job()
        for _ in range(HARD_CONTEXT_CEILING_PAUSE_AFTER):
            mark_job_run(job["id"], False, CEILING_ERROR_TEXT)
        assert get_job(job["id"])["state"] == "paused"

        resumed = resume_job(job["id"])
        assert resumed["state"] == "scheduled"
        assert resumed.get("hard_context_ceiling_streak") is None
        assert resumed.get("hard_context_ceiling_fingerprint") is None

        # A cleared streak needs three FRESH failures, not one.
        assert mark_job_run(job["id"], False, CEILING_ERROR_TEXT) is None
        assert mark_job_run(job["id"], False, CEILING_ERROR_TEXT) is None
        assert get_job(job["id"])["state"] == "scheduled"
        assert mark_job_run(job["id"], False, CEILING_ERROR_TEXT) is not None
        assert get_job(job["id"])["state"] == "paused"

    def test_manual_trigger_clears_count_and_fingerprint(self, tmp_cron_dir):
        job = self._job()
        mark_job_run(job["id"], False, CEILING_ERROR_TEXT)
        triggered = trigger_job(job["id"])

        assert triggered.get("hard_context_ceiling_streak") is None
        assert triggered.get("hard_context_ceiling_fingerprint") is None

    def test_run_one_job_alerts_the_operator_through_the_normal_delivery_path(
        self, tmp_cron_dir
    ):
        from cron.scheduler import run_one_job

        job = self._job()
        job_record = {
            **get_job(job["id"]),
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "123"},
        }

        with patch("cron.scheduler.run_job",
                   return_value=(False, "# failed", "", CEILING_ERROR_TEXT)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._deliver_result", return_value=None) as deliver_mock:
            for _ in range(HARD_CONTEXT_CEILING_PAUSE_AFTER):
                assert run_one_job(job_record) is True

        pause_alerts = [
            call for call in deliver_mock.call_args_list
            if "auto-paused" in str(call.args[1])
        ]
        assert len(pause_alerts) == 1
        assert "consecutive hard-context-ceiling failures" in str(pause_alerts[0].args[1])
        assert get_job(job["id"])["state"] == "paused"


def test_oversized_runtime_context_blocks_before_agent_or_provider(
    tmp_cron_dir, tmp_path
):
    from cron.scheduler import run_job

    job = create_job(prompt="small stored prompt", schedule="every 4h")
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("hermes_state.SessionDB", return_value=MagicMock()), \
         patch("cron.scheduler._build_job_prompt", return_value="r" * 96_000), \
         patch("run_agent.AIAgent") as agent_cls, \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider") as provider:
        success, output, final_response, error = run_job(job)

    assert success is False
    assert final_response == ""
    assert "[blocked_config]" in error
    assert "assembled runtime context" in error
    assert "24,000-token usable context budget" in error
    assert "BLOCKED (configuration)" in output
    agent_cls.assert_not_called()
    provider.assert_not_called()
