'use strict'

/**
 * update-gate.ts
 *
 * Pure, dependency-injected gate that parks local backend spawns while an
 * in-app update is running (#73822, #50238).
 *
 * Two independent signals mean "an update owns the venv right now":
 *
 *  - the on-disk marker (`HERMES_HOME/.hermes-update-in-progress`), written
 *    by the updater — and by the desktop itself just before hand-off — and
 *  - the in-process `updateInFlight` flag, true for the whole
 *    `applyUpdates()` critical section.
 *
 * The marker alone is NOT enough (#73822): `applyUpdates` kills its own
 * backend early (`releaseBackendLock`) but only writes the marker AFTER the
 * Windows venv-blocker scan. Killing the backend drops the renderer's
 * WebSocket, the renderer reconnects within ~1s, and a marker-only gate
 * happily spawns a fresh backend inside the update's own critical section —
 * which `scanVenvBlockers` then reports as a blocker, aborting every update
 * attempt forever. Consulting the flag closes that window. On the success
 * path the marker is written BEFORE the flag clears in `applyUpdates`'
 * `finally`, so there is no instant where both signals are false and a
 * waiter could slip through mid-update.
 */

export type UpdateGateReason = 'marker' | 'update-in-flight' | null

export interface UpdateGateDeps {
  /** True when a live on-disk update marker exists (see update-marker.ts). */
  hasLiveMarker: () => boolean
  /** True while this process is inside applyUpdates()' critical section. */
  isUpdateInFlight: () => boolean
}

/** Why the gate is closed right now, or null when it is open. */
export function updateGateReason(deps: UpdateGateDeps): UpdateGateReason {
  if (deps.hasLiveMarker()) {
    return 'marker'
  }

  if (deps.isUpdateInFlight()) {
    return 'update-in-flight'
  }

  return null
}

export type UpdateClearanceOutcome = 'clear' | 'finished'

export interface WaitForUpdateClearanceOptions {
  pollMs: number
  /** Invoked once per poll while parked (boot progress / logging). */
  onWaitTick?: (reason: Exclude<UpdateGateReason, null>) => void | Promise<void>
  /** Existing app shutdown state; cancellation must never permit a spawn. */
  isCancelled?: () => boolean
  sleep?: (ms: number) => Promise<void>
}

/**
 * Park until no update signal remains. App shutdown cancels the pending start.
 *
 * Returns 'clear' when the gate was already open (no wait happened),
 * 'finished' when it opened during the wait. Elapsed time never authorizes
 * starting a backend against a checkout an updater may still be mutating.
 * onWaitTick keeps the existing boot-progress UI responsive during long runs.
 */
export async function waitForUpdateClearance(
  deps: UpdateGateDeps,
  options: WaitForUpdateClearanceOptions
): Promise<UpdateClearanceOutcome> {
  const sleep = options.sleep || (ms => new Promise<void>(r => setTimeout(r, ms)))
  const checkCancellation = () => {
    if (options.isCancelled?.()) {
      throw new Error('Backend startup cancelled because Hermes is shutting down.')
    }
  }

  checkCancellation()
  let reason = updateGateReason(deps)

  if (!reason) {
    return 'clear'
  }

  while (reason) {
    if (options.onWaitTick) {
      await options.onWaitTick(reason)
    }

    await sleep(options.pollMs)
    checkCancellation()
    reason = updateGateReason(deps)
  }

  return 'finished'
}
