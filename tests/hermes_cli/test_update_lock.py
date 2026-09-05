"""Cross-process update mutual exclusion (``hermes_cli.update_lock``).

Three surfaces can start an update of one install tree: a terminal ``hermes
update``, the dashboard's Update button (which spawns that same command
detached), and the desktop's Update button (Tauri updater → install-mode
bootstrap on its failure screen). Before the shared lock, two of them could run
concurrently and rewrite source under a live interpreter — observed in the wild
as an installer ``git checkout`` rewinding the checkout ~9k commits while a
dashboard-spawned ``hermes update`` was mid-``npm install``, which then failed
against the rewound tree's manifests.

These exercise the real marker file against a temp home — no mocks — because
the contract that matters is what the Rust updater and the Electron gate see on
disk.
"""

from __future__ import annotations

import multiprocessing
import os
import time

import pytest

from hermes_cli.update_lock import (
    HANDOFF_PID_ENV,
    UpdateLock,
    describe_holder,
    read_live_update,
    update_marker_path,
)

# A pid no live process owns. os.kill(pid, 0) must report it dead so a crashed
# updater can never wedge every future update. Deliberately larger than any
# platform's pid_t so it also covers the corrupt-marker path (OverflowError).
DEAD_PID = 4294967294


@pytest.fixture
def marker(tmp_path):
    return tmp_path / ".hermes-update-in-progress"


def test_marker_path_follows_process_hermes_home(tmp_path, monkeypatch):
    """The lock must land where the Rust updater and Electron gate look.

    All three resolve the *process* HERMES_HOME; a profile-scoped path would
    put the lock somewhere the other two owners never read.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert update_marker_path() == tmp_path / ".hermes-update-in-progress"


def test_acquire_writes_pid_and_start_time(marker):
    lock = UpdateLock(path=marker)

    assert lock.acquire() is True
    assert lock.acquired is True

    lines = marker.read_text(encoding="utf-8").splitlines()
    assert int(lines[0]) == os.getpid(), "the Electron gate probes this pid for liveness"
    assert int(lines[1]) == pytest.approx(time.time(), abs=5)
    assert len(lines) == 2, "wire format is exactly pid + started_at"


def test_second_acquire_is_refused_while_the_first_is_live(marker):
    """The bug: two updaters mutating one checkout at the same time."""
    first = UpdateLock(path=marker)
    assert first.acquire() is True

    second = UpdateLock(path=marker)
    assert second.acquire() is False
    assert second.holder is not None
    assert second.holder.pid == os.getpid()
    assert second.acquired is False


def test_refused_lock_does_not_delete_the_live_owners_marker(marker):
    first = UpdateLock(path=marker)
    first.acquire()

    second = UpdateLock(path=marker)
    second.acquire()
    second.release()

    assert marker.exists(), "a refused claimant must never clear the live owner's lock"

    first.release()
    assert not marker.exists()


def test_release_leaves_a_marker_a_handoff_partner_now_owns(marker):
    """The desktop writes the marker, then the Tauri updater takes ownership.

    Releasing must not delete a marker whose pid is no longer ours — that would
    reopen the gate while the partner is still mid-update.
    """
    lock = UpdateLock(path=marker)
    lock.acquire()

    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")
    lock.release()

    assert marker.exists(), "the partner's marker is not ours to remove"


def test_dead_owner_is_reclaimed_not_honored(marker):
    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

    lock = UpdateLock(path=marker)
    assert lock.acquire() is True
    assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()


def test_long_running_owner_is_not_reclaimed(marker):
    """A slow build can legitimately keep mutating after twenty minutes."""
    long_ago = int(time.time()) - 21 * 60
    body = f"{os.getpid()}\n{long_ago}\n"
    marker.write_text(body, encoding="utf-8")

    lock = UpdateLock(path=marker)
    assert lock.acquire() is False
    assert read_live_update(path=marker).pid == os.getpid()
    assert marker.read_text(encoding="utf-8") == body


@pytest.mark.parametrize("error", [PermissionError("denied"), RuntimeError("probe unavailable")])
def test_uncertain_positive_pid_keeps_ownership(marker, monkeypatch, error):
    def unavailable(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("psutil.Process", unavailable)
    body = f"424242\n{int(time.time()) - 21 * 60}\n"
    marker.write_text(body, encoding="utf-8")
    lock = UpdateLock(path=marker)
    assert lock.acquire() is False
    assert read_live_update(path=marker).pid == 424242
    assert marker.read_text(encoding="utf-8") == body


@pytest.mark.parametrize("timestamp", ["", "nan", "inf", "garbage"])
def test_live_owner_with_unknown_start_time_still_blocks(marker, timestamp):
    body = f"{os.getpid()}\n{timestamp}\n"
    marker.write_text(body, encoding="utf-8")
    lock = UpdateLock(path=marker)
    assert lock.acquire() is False
    assert "start time unavailable" in describe_holder(lock.holder)
    assert marker.read_text(encoding="utf-8") == body


@pytest.mark.parametrize(
    "body",
    ["", "not-a-pid\n123\n", "\n\n", str(DEAD_PID)],
    ids=["empty", "garbage-pid", "blank-lines", "no-start-time"],
)
def test_malformed_markers_never_block_an_update(marker, body):
    marker.write_text(body, encoding="utf-8")

    assert read_live_update(path=marker) is None
    assert UpdateLock(path=marker).acquire() is True


def test_stale_marker_is_removed_on_read(marker):
    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

    assert read_live_update(path=marker) is None
    assert not marker.exists(), "whoever notices a stale marker clears it"


def test_absent_marker_reports_no_live_update(marker):
    assert read_live_update(path=marker) is None


def test_context_manager_releases_even_on_exception(marker):
    with pytest.raises(RuntimeError):
        with UpdateLock(path=marker) as lock:
            assert lock.acquired is True
            raise RuntimeError("update blew up mid-flight")

    assert not marker.exists(), "a crashed update must not strand the lock"


def test_describe_holder_names_the_pid_and_elapsed_time(marker):
    lock = UpdateLock(path=marker)
    lock.acquire()

    holder = read_live_update(path=marker)
    assert holder is not None
    message = describe_holder(holder)

    assert str(os.getpid()) in message, "the user needs the pid to find the other update"
    assert "already running" in message


def test_unwritable_marker_location_refuses_the_update(tmp_path):
    """Storage failure must never permit unguarded checkout mutation."""
    lock = UpdateLock(path=tmp_path / "nonexistent-file" / "marker")
    (tmp_path / "nonexistent-file").write_text("i am a file, not a dir", encoding="utf-8")

    assert lock.acquire() is False
    assert lock.acquired is False, "nothing was written, so there is nothing to release"
    assert "Could not safely acquire" in describe_holder(lock.holder)


def test_native_claim_honors_existing_spawn_sidecar_mutex(marker):
    from gateway.status import _release_file_lock, _try_acquire_file_lock

    sidecar = marker.with_name(marker.name + ".mutex")
    with sidecar.open("a+", encoding="utf-8") as handle:
        assert _try_acquire_file_lock(handle)
        try:
            lock = UpdateLock(path=marker)
            assert lock.acquire() is False
            assert lock.acquired is False
            assert not marker.exists()
        finally:
            _release_file_lock(handle)
    assert lock.acquire() is True
    lock.release()
    assert sidecar.exists(), "the shared mutex inode must never be unlinked"


def test_failed_marker_publication_refuses_the_update(marker, monkeypatch):
    def unavailable(*_args):
        raise OSError("storage unavailable")

    monkeypatch.setattr("hermes_cli.update_lock.os.replace", unavailable)
    lock = UpdateLock(path=marker)
    assert lock.acquire() is False
    assert lock.acquired is False
    assert not marker.exists()
    assert "Could not safely acquire" in describe_holder(lock.holder)


def test_stale_read_does_not_delete_marker_during_mutex_transaction(marker):
    from gateway.status import _release_file_lock, _try_acquire_file_lock

    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")
    with marker.with_name(marker.name + ".mutex").open("a+", encoding="utf-8") as handle:
        assert _try_acquire_file_lock(handle)
        try:
            assert read_live_update(path=marker) is None
            assert marker.exists(), "an observation outside the mutex must not delete"
        finally:
            _release_file_lock(handle)
    assert read_live_update(path=marker) is None
    assert not marker.exists()


def test_release_waits_for_mutex_before_checking_and_removing_owner(marker):
    from gateway.status import _release_file_lock, _try_acquire_file_lock

    lock = UpdateLock(path=marker)
    assert lock.acquire()
    with marker.with_name(marker.name + ".mutex").open("a+", encoding="utf-8") as handle:
        assert _try_acquire_file_lock(handle)
        try:
            lock.release()
            assert marker.exists()
            assert lock.acquired, "a blocked release can be retried"
            marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")
        finally:
            _release_file_lock(handle)
    lock.release()
    assert marker.exists(), "retry must recheck the handoff partner's ownership"


def _concurrent_update_claim(marker, ready, results, finish):
    lock = UpdateLock(path=marker)
    ready.wait(timeout=15)
    acquired = lock.acquire()
    results.put((os.getpid(), acquired))
    try:
        finish.wait(timeout=15)
    finally:
        lock.release()


def test_concurrent_native_claims_have_one_live_owner(marker):
    """Real sibling processes must not both enter checkout mutation."""
    context = multiprocessing.get_context("spawn")
    ready = context.Barrier(3)
    results = context.Queue()
    finish = context.Event()
    processes = [
        context.Process(target=_concurrent_update_claim, args=(marker, ready, results, finish))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        ready.wait(timeout=15)
        claims = [results.get(timeout=15) for _ in processes]
        winners = [pid for pid, acquired in claims if acquired]
        assert len(winners) == 1, claims
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == winners[0]
    finally:
        finish.set()
        for process in processes:
            process.join(timeout=20)
        results.close()
        results.join_thread()
    assert all(process.exitcode == 0 for process in processes)
    assert not marker.exists()


class TestHandoffFromOrchestratingUpdater:
    """The Tauri updater holds the marker, then spawns ``hermes update``.

    The regression: the child saw its own parent's live marker and exited 2,
    so every GUI update failed with "Hermes is still running" and retrying
    just re-ran the same self-deadlock. The parent names its pid in
    HANDOFF_PID_ENV; a live holder matching it is our own orchestrator.
    """

    @pytest.mark.parametrize("age_seconds", [0, 21 * 60])
    def test_child_runs_under_the_parents_live_claim(self, marker, monkeypatch, age_seconds):
        # Stand in for the parent updater with our own (live) pid.
        body = f"{os.getpid()}\n{int(time.time()) - age_seconds}\n"
        marker.write_text(body, encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, str(os.getpid()))

        lock = UpdateLock(path=marker)
        assert lock.acquire() is True
        assert lock.acquired is False, "the parent's claim is not ours to own"

        lock.release()
        assert marker.exists(), "the parent still needs its marker after our stage ends"
        assert marker.read_text(encoding="utf-8") == body

    def test_handoff_pid_that_is_not_the_live_holder_grants_nothing(self, marker, monkeypatch):
        """The env var alone must not bypass the lock."""
        marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, str(os.getpid() + 1))

        lock = UpdateLock(path=marker)
        assert lock.acquire() is False
        assert lock.holder is not None

    @pytest.mark.parametrize("value", ["", "not-a-pid", "-1", "0"], ids=["empty", "garbage", "negative", "zero"])
    def test_malformed_handoff_values_fall_back_to_refusal(self, marker, monkeypatch, value):
        marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, value)

        assert UpdateLock(path=marker).acquire() is False

    def test_handoff_env_with_no_marker_claims_normally(self, marker, monkeypatch):
        """A handoff pid must not stop us writing our own claim when unlocked."""
        monkeypatch.setenv(HANDOFF_PID_ENV, str(os.getpid()))

        lock = UpdateLock(path=marker)
        assert lock.acquire() is True
        assert lock.acquired is True
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()


class TestAncestryHandoff:
    """Staged updaters older than the HANDOFF_PID_ENV export never send it.

    ``hermes-setup`` under ``~/.hermes`` is only refreshed by a full installer
    run, so an updated checkout (new lock) driven by a pre-handoff staged
    updater (old parent) deadlocks on exit 2 forever unless the child also
    recognizes a live holder that is its own process ancestor.

    ``_pid_alive`` is pinned True here because the hermetic conftest guards
    ``os.kill`` probes of pids outside the test subtree (our ppid included);
    liveness has its own coverage above — ancestry is what's under test.
    """

    @pytest.fixture(autouse=True)
    def _liveness_pinned_true(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: True)

    def test_marker_owned_by_our_parent_process_is_our_orchestrator(self, marker):
        marker.write_text(f"{os.getppid()}\n{int(time.time())}\n", encoding="utf-8")

        lock = UpdateLock(path=marker)
        assert lock.acquire() is True, "a live ancestor's claim is the one we run under"
        assert lock.acquired is False, "the parent's claim is not ours to own"

        lock.release()
        assert marker.exists(), "the parent still needs its marker after our stage ends"
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getppid()

    def test_live_non_ancestor_holder_is_still_refused(self, marker):
        """Ancestry must not open the lock to unrelated concurrent updaters."""
        marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

        lock = UpdateLock(path=marker)
        assert lock.acquire() is False
        assert lock.holder is not None
        assert lock.holder.pid == DEAD_PID
