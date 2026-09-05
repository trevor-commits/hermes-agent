/**
 * In-app update mutual-exclusion marker (#50238).
 *
 * The Tauri updater writes HERMES_HOME/.hermes-update-in-progress for the whole
 * duration of an `--update` run (see apps/bootstrap-installer/src-tauri/src/
 * update.rs `UpdateMarkerGuard`). The marker body is two lines: the updater's
 * pid and the unix-seconds it started.
 *
 * Why: if the user relaunches the desktop mid-update — the window vanished with
 * no progress and looks crashed — a fresh instance must NOT spawn its own local
 * backend. That backend re-locks the venv shim, the updater's straggler cleanup
 * (`force_kill_other_hermes`, taskkill /IM hermes.exe) kills it, the launch
 * fails with the 45s "backend didn't come up" timeout, and the user relaunches
 * into the same trap — an infinite respawn/kill loop. The desktop gates local
 * backend startup on this marker and parks until the update finishes.
 *
 * This module holds the PURE, side-effect-light logic (path, pid liveness,
 * parse + staleness) so it is unit-testable without booting Electron. The
 * polling/boot-progress wrapper lives in main.ts where the boot-progress and
 * log sinks are.
 */

import fs from 'fs'
import path from 'path'

export function markerPath(hermesHome) {
  return path.join(hermesHome, '.hermes-update-in-progress')
}

// Protect a positive pid unless it is confirmed dead. Signal 0 does
// not deliver a signal — it just probes existence/permission. ESRCH => dead;
// EPERM or an unknown probe failure cannot authorize a second writer.
// Injectable `kill` keeps it unit-testable.
export function isPidAlive(pid, kill: typeof process.kill = process.kill.bind(process)) {
  if (!Number.isInteger(pid) || pid <= 0) {
    return false
  }

  try {
    kill(pid, 0)

    return true
  } catch (err) {
    return err?.code !== 'ESRCH'
  }
}

/**
 * Read + interpret the marker.
 *
 * Returns `{ pid, ageMs }` for a positive pid that is alive or cannot be
 * inspected. Age is diagnostic only: a slow build can keep mutating after
 * twenty minutes. A missing timestamp produces a null age, not permission
 * to start another updater. Dead-pid markers are pruned for recovery.
 *
 * Pure-ish: file I/O against the given path, plus an injectable pid probe and
 * clock for tests.
 */
export function readLiveUpdateMarker(
  hermesHome,
  {
    kill,
    now = Date.now
  }: {
    now?: () => number
    kill?: typeof process.kill
  } = {}
) {
  const file = markerPath(hermesHome)
  let raw

  try {
    raw = fs.readFileSync(file, 'utf8')
  } catch (err) {
    if (err?.code === 'ENOENT') {
      return null
    }

    // Unreadable storage cannot prove the updater released its claim.
    throw err
  }

  const [pidLine, startedLine] = String(raw).split('\n')
  const pid = Number.parseInt((pidLine || '').trim(), 10)
  const startedAt = Number.parseInt((startedLine || '').trim(), 10)
  const elapsedMs = now() - startedAt * 1000
  const ageMs = Number.isFinite(elapsedMs) ? Math.max(0, elapsedMs) : null
  const alive = Number.isInteger(pid) && isPidAlive(pid, kill)

  if (!alive) {
    try {
      fs.unlinkSync(file)
    } catch {
      void 0
    }

    return null
  }

  return { pid, ageMs }
}

/**
 * Write the update-in-progress marker *from the desktop* before handing off
 * to the detached updater.
 *
 * The Tauri-based hermes-setup.exe takes several seconds to initialise its
 * window and reach the Rust `run_update` entry point where it writes the
 * marker itself. During that gap the desktop's `app.quit()` teardown kills
 * the backend child, the renderer's WebSocket drops, and the renderer
 * immediately calls `ensureBackend()` → `waitForUpdateToFinish()`. Because
 * the updater hasn't written the marker yet, the gate sees no live update
 * and spawns a *new* backend — which re-locks `.pyd` files in the venv.
 * When the updater finally reaches the venv-rebuild stage it finds those
 * files locked and the update bricks.
 *
 * Fix: the desktop writes the marker itself, using the spawned updater's
 * PID, immediately after `spawn()`. The updater's `UpdateMarkerGuard` will
 * later adopt it or another hand-off stage may replace the PID. A live
 * holder's original timestamp is preserved across those transfers for elapsed
 * time reporting. When the updater finishes
 * it deletes the marker as before.
 * If the updater never starts (spawn failure) the marker still contains a
 * real PID, so `readLiveUpdateMarker` will self-heal once that PID exits.
 */
export function writeUpdateMarker(
  hermesHome,
  pid,
  {
    kill,
    now = Date.now,
    startedAt
  }: {
    now?: () => number
    kill?: typeof process.kill
    startedAt?: number
  } = {}
) {
  const file = markerPath(hermesHome)
  const nowMs = now()
  const owner = readLiveUpdateMarker(hermesHome, { kill, now: () => nowMs })

  const acquiredAt =
    typeof startedAt === 'number' && Number.isInteger(startedAt)
      ? startedAt
      : owner && owner.ageMs !== null
        ? Math.floor((nowMs - owner.ageMs) / 1000)
        : Math.floor(nowMs / 1000)

  try {
    fs.writeFileSync(file, `${pid}\n${acquiredAt}\n`, 'utf8')
  } catch {
    // Best-effort: if we can't write the marker, proceed anyway. The
    // updater will write its own when it reaches run_update.
  }
}

/**
 * Whether a NEW updater hand-off must be refused because a different,
 * already-alive updater currently owns the marker (#75778).
 *
 * `writeUpdateMarker` unconditionally overwrites the marker file. Called
 * before every hand-off with no conflict check, a user who clicks "Update"
 * again while a prior updater is still parked mid-run (e.g. "waiting for
 * Hermes to exit…") clobbers that still-running updater's claim: the
 * retry's pre-write now names the NEW child, so the OLD process — alive
 * and mutating the checkout — is no longer recorded as the owner. A second
 * live updater can then run over the same tree unrecorded, the exact
 * two-updaters-at-once hazard `UpdateMarkerGuard` in the Rust updater
 * exists to prevent (apps/bootstrap-installer/src-tauri/src/update.rs).
 *
 * Returns the live foreign owner (with a ready-to-show message) when the
 * hand-off must be refused, or `null` when it's safe to spawn — no marker,
 * or the existing one is stale/dead and self-heals via
 * `readLiveUpdateMarker`.
 */
export function updateHandoffConflict(
  hermesHome,
  opts: {
    now?: () => number
    kill?: typeof process.kill
  } = {}
) {
  const owner = readLiveUpdateMarker(hermesHome, opts)

  if (!owner) {
    return null
  }

  const mins = Math.floor((owner.ageMs ?? 0) / 60_000)
  const secs = Math.floor(((owner.ageMs ?? 0) % 60_000) / 1000)
  const elapsed = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`
  const started = owner.ageMs === null ? 'start time unavailable' : `started ${elapsed} ago`

  return {
    pid: owner.pid,
    ageMs: owner.ageMs,
    message: `An update is already running (PID ${owner.pid}, ${started}). Wait for it to finish, then try again.`
  }
}
