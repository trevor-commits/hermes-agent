"""Cross-process mutual exclusion for in-flight Hermes updates.

Three different surfaces can start an update of the same install tree:

* ``hermes update`` from a terminal,
* the dashboard's Update button (``POST /api/hermes/update`` →
  ``_spawn_hermes_action(["update"])``, detached),
* the desktop's Update button, which hands off to the Tauri
  ``hermes-setup --update`` and, on its failure screen, to install-mode
  bootstrap (``install.ps1`` / ``install.sh``).

Until now only the Tauri updater published an "update in progress" marker
(``UpdateMarkerGuard`` in ``apps/bootstrap-installer/src-tauri/src/update.rs``),
and only the Electron desktop consumed it (``electron/update-marker.ts``, to
gate local backend startup). Nothing stopped two *updaters* from running at
once — so a dashboard-spawned ``hermes update`` and an installer-driven
``git checkout`` could mutate the same checkout concurrently, rewriting source
under a live interpreter and leaving the tree half-updated.

Native marker reads, claims, and releases use the existing ``.mutex`` sidecar
to serialize their transactions. Other writers must honor that same mutex to
participate; shared marker bytes alone do not establish cross-entrypoint
atomicity. Format and location remain compatible with Rust and Electron:

    <HERMES_HOME>/.hermes-update-in-progress   body: "<pid>\\n<started_at_unix>"

A marker remains protected while its pid is alive or cannot be inspected.
Elapsed time is diagnostic: a slow build can still mutate the checkout after
twenty minutes. A dead owner's marker is removed on read only while holding
the sidecar mutex.

One layering wrinkle: the Tauri updater holds this marker for its WHOLE run and
then spawns ``hermes update`` as a child stage. Without a handoff the child
sees its own parent's live marker and refuses — the GUI update deadlocks
against itself on every attempt ("Hermes is still running", retry forever).
Two mechanisms recognize the orchestrating parent, and either suffices:

* The updater exports :data:`HANDOFF_PID_ENV` naming its own pid, and
  ``acquire`` treats a live holder matching that pid as the lock we are
  already running under. The env var alone grants nothing: the pid must also
  be the live marker owner, so a stale or forged value cannot bypass the lock.
* A live holder that is a *process ancestor* of ours is likewise our own
  orchestrator. This is the load-bearing path for the fleet: the staged
  ``hermes-setup`` binary under ``~/.hermes`` is only refreshed by a full
  installer run (``copy_self_to_hermes_home`` deliberately no-ops during
  ``--update``), so every desktop whose staged updater predates the
  HANDOFF_PID_ENV export runs an old parent against a new child. Without the
  ancestry check those users get exit 2 ("Hermes is still running") on every
  GUI update forever, with no Hermes process actually running.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MARKER_NAME = ".hermes-update-in-progress"

# Set by an orchestrating updater (the Tauri `hermes-setup --update` flow) to
# its own pid before spawning `hermes update` as a child stage. The parent
# holds the marker for its whole run, so without this the child refuses its
# own parent's lock and the GUI update can never complete. See update_child_env
# in apps/bootstrap-installer/src-tauri/src/update.rs — keep the name in sync.
HANDOFF_PID_ENV = "HERMES_UPDATE_HANDOFF_PID"

# Exit code meaning "another updater/instance owns this install right now".
# Already the de-facto contract: the Windows shim + venv-holder guards in
# _cmd_update_impl exit 2, and the Tauri updater matches on it
# (UPDATE_EXIT_CONCURRENT in apps/bootstrap-installer/src-tauri/src/update.rs)
# to show "Hermes is still running" instead of a generic failure. Naming it
# here keeps the concurrent-update refusal on that same understood contract.
UPDATE_EXIT_CONCURRENT = 2


def update_marker_path() -> Path:
    """Path of the shared update marker.

    Uses the *process* Hermes home (never the context-local profile override):
    the Rust updater resolves ``$HERMES_HOME`` or the platform default, and the
    desktop pins that same value into the updater's env. A profile-scoped path
    here would put the lock somewhere the other two owners never look.
    """
    from hermes_constants import get_process_hermes_home

    return get_process_hermes_home() / MARKER_NAME


def _pid_alive(pid: int) -> bool:
    """Protect a positive pid unless the process is confirmed dead.

    Uses the existing psutil dependency for a cross-platform no-kill probe.
    Do NOT hand-roll this with ``os.kill(pid, 0)``: on Windows
    that is not a no-op — CPython routes ``sig=0`` to
    ``GenerateConsoleCtrlEvent``, which Ctrl+C's the target's whole console
    process group (bpo-14484). A liveness check that killed the updater it was
    asking about would be a spectacular way to fix a concurrency bug.

    Missing probe support or denied access cannot authorize another writer.
    Unlike gateway discovery, update ownership must fail closed on uncertainty.
    """
    if pid <= 0:
        return False
    try:
        import psutil
    except Exception as exc:
        logger.debug("Could not load pid probe: %s", exc)
        return True
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, OverflowError):
        return False
    except Exception as exc:
        logger.debug("Could not probe pid %s: %s", pid, exc)
        return True


def _handoff_pid() -> int | None:
    """Pid of the orchestrating updater that spawned us, if any.

    Read from :data:`HANDOFF_PID_ENV`. Malformed values count as absent —
    a broken handoff must fall back to the normal refusal, never crash.
    """
    raw = os.environ.get(HANDOFF_PID_ENV, "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _is_ancestor_pid(pid: int) -> bool:
    """True when ``pid`` is a live ancestor (parent chain) of this process.

    The orchestrating updater spawns ``hermes update`` as a (grand)child, so a
    live marker owned by one of our ancestors can only be the claim we are
    already running under — an unrelated concurrent updater is never in our
    parent chain. This heals the fleet of staged ``hermes-setup`` binaries
    that predate the HANDOFF_PID_ENV export and can never send it.

    Never includes our own pid, and any failure counts as "not an ancestor":
    an unprovable ancestry must fall back to the normal refusal.
    """
    if pid <= 0:
        return False
    try:
        import psutil

        return any(parent.pid == pid for parent in psutil.Process().parents())
    except Exception as exc:
        logger.debug("Could not walk process ancestry for pid %s: %s", pid, exc)
        return False


@dataclass(frozen=True)
class UpdateHolder:
    """An update owner that has not been confirmed dead."""

    pid: int
    age_seconds: float | None


class _MarkerMutex:
    """Serialize marker transactions on the existing, persistent sidecar.

    Never unlink this file: replacing its inode would allow two processes to
    hold different locks for the same marker. Failure to lock is a refusal,
    including unsupported storage or an unavailable locking implementation.
    """

    def __init__(self, marker: Path) -> None:
        self.path = marker.with_name(marker.name + ".mutex")
        self.acquired = False
        self.handle = None
        self._release = None

    def __enter__(self) -> "_MarkerMutex":
        try:
            from gateway.status import _release_file_lock, _try_acquire_file_lock

            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("a+", encoding="utf-8")
            self._release = _release_file_lock
            self.acquired = _try_acquire_file_lock(self.handle)
        except Exception as exc:
            logger.debug("Could not lock update marker %s: %s", self.path, exc)
        return self

    def __exit__(self, *_exc) -> None:
        if self.handle is not None:
            try:
                if self.acquired:
                    self._release(self.handle)
            finally:
                self.handle.close()


def _read_live_update(marker: Path, *, cleanup_stale: bool) -> UpdateHolder | None:
    """Read the marker; deletion requires the caller to hold its mutex."""
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    lines = raw.splitlines()
    try:
        pid = int(lines[0].strip())
    except (IndexError, ValueError):
        pid = -1
    try:
        started_at = float(lines[1].strip())
    except (IndexError, ValueError):
        started_at = float("nan")

    age = max(0, time.time() - started_at) if math.isfinite(started_at) else None
    if not _pid_alive(pid):
        if cleanup_stale:
            marker.unlink(missing_ok=True)
        return None

    return UpdateHolder(pid=pid, age_seconds=age)


def read_live_update(*, path: Path | None = None) -> UpdateHolder | None:
    """Return the live update holding the lock, or ``None``.

    A live or unprobeable positive pid remains protected regardless of age.
    Missing timestamps only affect the diagnostic age. Stale cleanup is
    performed only while holding the sidecar mutex,
    so this read cannot remove a claim being published or released. A busy
    mutex permits observation only. Never raises.
    """
    marker = path or update_marker_path()
    try:
        with _MarkerMutex(marker) as mutex:
            return _read_live_update(marker, cleanup_stale=mutex.acquired)
    except (OSError, UnicodeError):
        return None  # absent or unreadable => no live update


def describe_holder(holder: UpdateHolder | None) -> str:
    """One-line, user-facing explanation of who holds the update lock."""
    if holder is None:
        return (
            "✗ Could not safely acquire the Hermes update lock.\n\n"
            "  Another process may be changing the update marker, or its\n"
            "  storage is unavailable. No checkout changes were started.\n"
            "  Wait for the other update or restore access, then retry."
        )
    if holder.age_seconds is None:
        started = "start time unavailable"
    else:
        minutes, seconds = divmod(int(max(holder.age_seconds, 0)), 60)
        elapsed = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        started = f"started {elapsed} ago"
    return (
        f"✗ Another Hermes update is already running (PID {holder.pid}, "
        f"{started}).\n"
        "\n"
        "  Two updates mutating the same checkout corrupt it: one rewrites\n"
        "  source while the other is mid-install. Wait for it to finish, or\n"
        "  close the window/dashboard tab that started it, then retry."
    )


class UpdateLock:
    """Context manager owning the shared update marker for this process.

    ``acquired`` is False when another live update already holds it — callers
    decide whether that's a hard refusal (CLI/dashboard) or a wait. Releasing
    only removes the marker when *we* still own it, so a marker rewritten by a
    handoff partner (the Tauri updater overwrites it with its own pid) is never
    deleted out from under its new owner.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self.path = path or update_marker_path()
        self.acquired = False
        self.holder: UpdateHolder | None = None

    def acquire(self) -> bool:
        """Claim the lock. Returns False (and sets ``holder``) if it's taken.

        A live holder whose pid matches :data:`HANDOFF_PID_ENV` — or is a
        process ancestor of ours — is our own orchestrating parent (the Tauri
        updater spawning `hermes update` as a stage): we run under ITS claim
        rather than refusing or re-writing the marker, and ``release`` leaves
        the parent's marker untouched. The ancestry path exists because staged
        updaters older than the HANDOFF_PID_ENV export never send the env var.
        """
        try:
            with _MarkerMutex(self.path) as mutex:
                if not mutex.acquired:
                    self.holder = _read_live_update(self.path, cleanup_stale=False)
                    return False
                existing = _read_live_update(self.path, cleanup_stale=True)
                if existing is not None:
                    if existing.pid == _handoff_pid() or _is_ancestor_pid(existing.pid):
                        return True
                    self.holder = existing
                    return False
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=self.path.parent,
                        prefix=self.path.name + ".", delete=False,
                    ) as handle:
                        temporary = Path(handle.name)
                        handle.write(f"{os.getpid()}\n{int(time.time())}\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self.path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                self.acquired = True
                return True
        except (OSError, UnicodeError) as exc:
            logger.debug("Could not write update marker %s: %s", self.path, exc)
            return False

    def release(self) -> None:
        """Drop the marker if this process still owns it. Never raises."""
        if not self.acquired:
            return
        try:
            with _MarkerMutex(self.path) as mutex:
                if not mutex.acquired:
                    return
                try:
                    raw = self.path.read_text(encoding="utf-8")
                    owner = int(raw.splitlines()[0].strip())
                except (FileNotFoundError, IndexError, ValueError):
                    self.acquired = False
                    return
                if owner == os.getpid():
                    self.path.unlink()
                # A handoff partner's marker is not ours to remove.
                self.acquired = False
        except (OSError, UnicodeError):
            return

    def __enter__(self) -> "UpdateLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
