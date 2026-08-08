/**
 * backend-child.ts
 *
 * Windows-aware teardown for the desktop's managed backend child process.
 *
 * Node's `child.kill()` only signals the direct child. On Windows a backend
 * that spawned its own grandchildren (a `hermes` REPL, a pty terminal
 * session, the gateway) survives a plain SIGTERM and keeps files (e.g. the
 * venv shim) locked. So on Windows we tree-kill via `forceKillProcessTree`;
 * everywhere else a plain SIGTERM is the graceful first step (POSIX has no
 * mandatory locks, and the backend is not spawned detached so there's no
 * process-group to negative-pid-kill). Callers that are ending the owning
 * Electron process must wait for exit and escalate if the child survives.
 *
 * Extracted into its own dependency-free module (no electron import) so the
 * SIGTERM-vs-tree-kill branching can be asserted directly with a fake child
 * object and a spy `forceKillProcessTree`, instead of grepping main.ts source
 * text for the function body.
 */

export interface StopBackendChildDeps {
  /** Defaults to the real platform check; injectable for tests. */
  isWindows?: boolean
  /** Windows tree-kill implementation (real: taskkill /T /F via execFileSync). */
  forceKillProcessTree: (pid: number) => void
}

export interface StopBackendTreesForUpdateDeps {
  /** Synchronous Windows taskkill /T /F implementation. */
  forceKillProcessTree: (pid: number) => void
  /** Clears and stops the desktop's pooled backends. */
  stopAllPoolBackends: () => void
}

export interface BackendProcessRoot {
  pid?: number | null
}

export interface KillableChild extends BackendProcessRoot {
  killed?: boolean
  kill: (signal: string) => void
}

export interface AwaitableKillableChild extends KillableChild {
  exitCode: number | null
  signalCode: string | null
  once: (event: 'exit', listener: () => void) => unknown
}

/**
 * Stop a managed child process, choosing the right strategy for the platform.
 * No-ops silently if `child` is falsy, already killed, or the kill attempt
 * throws (the process may already be gone) -- mirrors the original inline
 * best-effort semantics in main.ts.
 */
export function stopBackendChild(child: KillableChild | null | undefined, deps: StopBackendChildDeps) {
  if (!child || child.killed) {
    return
  }

  const isWindows = deps.isWindows ?? process.platform === 'win32'

  try {
    if (isWindows && Number.isInteger(child.pid)) {
      deps.forceKillProcessTree(child.pid as number)
    } else {
      child.kill('SIGTERM')
    }
  } catch {
    // Already gone.
  }
}

/**
 * Stop every backend tree owned by a Windows Desktop update hand-off.
 *
 * Tree-kill the primary root while its PID is still live, then delegate pool
 * teardown to the existing routine that tree-kills each pooled root exactly
 * once before mutating its registry. In particular, do not signal the primary
 * first: if that root exits before taskkill /T runs, Windows can no longer
 * enumerate its MCP grandchildren and they survive with the venv locked.
 */
export function stopBackendTreesForUpdate(
  primary: BackendProcessRoot | null | undefined,
  deps: StopBackendTreesForUpdateDeps
): void {
  if (primary && Number.isInteger(primary.pid)) {
    deps.forceKillProcessTree(primary.pid as number)
  }

  deps.stopAllPoolBackends()
}

/**
 * Gracefully stop a managed backend and keep the owner alive until the child
 * exits. A child that survives the bounded grace period is force-stopped so it
 * cannot be orphaned when Electron exits.
 */
export async function stopBackendChildAndWait(
  child: AwaitableKillableChild | null | undefined,
  deps: StopBackendChildDeps,
  timeoutMs = 5000
): Promise<void> {
  if (!child || child.exitCode !== null || child.signalCode !== null) {
    return
  }

  await new Promise<void>(resolve => {
    let timer: ReturnType<typeof setTimeout> | null = null
    let settled = false

    const finish = () => {
      if (settled) {
        return
      }

      settled = true

      if (timer) {
        clearTimeout(timer)
      }

      resolve()
    }

    child.once('exit', finish)
    stopBackendChild(child, deps)

    if (child.exitCode !== null || child.signalCode !== null) {
      finish()

      return
    }

    timer = setTimeout(
      () => {
        const isWindows = deps.isWindows ?? process.platform === 'win32'

        try {
          if (isWindows && Number.isInteger(child.pid)) {
            deps.forceKillProcessTree(child.pid as number)
          } else {
            child.kill('SIGKILL')
          }
        } catch {
          // Already gone.
        }

        finish()
      },
      Math.max(0, timeoutMs)
    )
  })
}
