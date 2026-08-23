/**
 * Authoritative activity guard for pooled profile-backend eviction.
 *
 * Renderer keepalive touches are only a recency hint: once the renderer socket
 * disconnects, a backend-owned turn can keep running without another touch.
 * Before either idle or LRU eviction stops a stale local backend, this module
 * asks the backend's existing JSON-RPC surface for its live sessions and
 * delegated children. Probe failures fail safe for a bounded grace period, then
 * become reapable so an unreachable backend cannot stay resident forever.
 */

const ACTIVE_SESSIONS_REQUEST_ID = 'pool-active'
const DELEGATIONS_REQUEST_ID = 'pool-delegations'
const DEFAULT_PROBE_TIMEOUT_MS = 5_000

export type PoolActivity = 'busy' | 'idle' | 'unknown'

export interface PoolActivityEntry {
  activityProbeUncertainSince?: null | number
  lastActiveAt?: null | number
}

export interface PoolBackendActivityEntry extends PoolActivityEntry {
  process?: unknown
}

interface ReapDecisionOptions {
  idleMs: number
  now: number
  probe: () => Promise<PoolActivity>
  uncertainGraceMs: number
}

interface GatewayActivityProbeOptions {
  timeoutMs?: number
  WebSocketImpl?: any
}

interface IdleReapOptions<K> {
  idleMs: number
  now: number
  probe: (key: K) => Promise<PoolActivity>
  uncertainGraceMs: number
}

interface LruEvictionOptions<K> {
  freshMs: number
  keep: number
  now: number
  probe: (key: K) => Promise<PoolActivity>
  uncertainGraceMs: number
}

/**
 * Decide whether one already-stale pool entry is safe to stop.
 *
 * A fresh renderer touch racing the probe wins. Confirmed backend work refreshes
 * the same recency clock the renderer uses, so a completed turn becomes eligible
 * after the normal idle window. An unavailable probe receives one bounded grace
 * window rather than becoming either an immediate destructive false-negative or
 * a permanent residency ticket.
 */
export async function shouldReapPoolEntry(
  entry: PoolActivityEntry,
  { idleMs, now, probe, uncertainGraceMs }: ReapDecisionOptions
): Promise<boolean> {
  const observedLastActiveAt = entry.lastActiveAt || 0

  if (now - observedLastActiveAt <= idleMs) {
    return false
  }

  let activity: PoolActivity = 'unknown'

  try {
    activity = await probe()
  } catch {
    activity = 'unknown'
  }

  // Recency changed while the async probe was in flight. Never let a stale
  // decision kill a backend a renderer just reattached to or used.
  if ((entry.lastActiveAt || 0) > observedLastActiveAt) {
    return false
  }

  if (activity === 'busy') {
    entry.lastActiveAt = now
    entry.activityProbeUncertainSince = null

    return false
  }

  if (activity === 'idle') {
    entry.activityProbeUncertainSince = null

    return true
  }

  const uncertainSince = entry.activityProbeUncertainSince

  if (uncertainSince == null) {
    entry.activityProbeUncertainSince = now

    return false
  }

  return now - uncertainSince > uncertainGraceMs
}

/** Select stale idle entries that remain safe to stop after backend validation. */
export async function selectSafeIdleReaps<K, E extends PoolBackendActivityEntry>(
  entries: Iterable<[K, E]>,
  { idleMs, now, probe, uncertainGraceMs }: IdleReapOptions<K>
): Promise<K[]> {
  const reaps: K[] = []

  for (const [key, entry] of [...entries]) {
    if (now - (entry.lastActiveAt || 0) <= idleMs) {
      continue
    }

    // Process-less remote descriptors hold no local work. Dropping the cached
    // route cannot interrupt the remote backend, so preserve the historical
    // cheap reap without opening a network probe.
    if (!entry.process) {
      reaps.push(key)

      continue
    }

    if (
      await shouldReapPoolEntry(entry, {
        idleMs,
        now,
        probe: () => probe(key),
        uncertainGraceMs
      })
    ) {
      reaps.push(key)
    }
  }

  return reaps
}

/**
 * Select enough stale local backends to reach the soft LRU cap, skipping any
 * candidate whose backend reports work or whose probe is still in its bounded
 * fail-safe window.
 */
export async function selectSafeLruEvictions<K, E extends PoolBackendActivityEntry>(
  entries: Iterable<[K, E]>,
  { freshMs, keep, now, probe, uncertainGraceMs }: LruEvictionOptions<K>
): Promise<K[]> {
  const spawned = [...entries].filter(([, entry]) => Boolean(entry.process))
  let removable = spawned.length - Math.max(0, keep)

  if (removable <= 0) {
    return []
  }

  const candidates = spawned
    .filter(([, entry]) => now - (entry.lastActiveAt || 0) > freshMs)
    .sort((a, b) => (a[1].lastActiveAt || 0) - (b[1].lastActiveAt || 0))

  const evictions: K[] = []

  for (const [key, entry] of candidates) {
    if (removable <= 0) {
      break
    }

    if (
      await shouldReapPoolEntry(entry, {
        idleMs: freshMs,
        now,
        probe: () => probe(key),
        uncertainGraceMs
      })
    ) {
      evictions.push(key)
      removable -= 1
    }
  }

  return evictions
}

/**
 * Ask a backend whether it owns live work without attaching to any session.
 *
 * `session.active_list` is the backend authority for running, waiting-input,
 * queued, and agent-build states. `delegation.status` covers children that can
 * outlive the foreground turn. The temporary socket owns no sessions, so closing
 * it cannot interrupt or reap the work it just observed.
 */
export function probeGatewayActivity(
  wsUrl: string,
  options: GatewayActivityProbeOptions = {}
): Promise<PoolActivity> {
  const WebSocketImpl = options.WebSocketImpl
  const timeoutMs = options.timeoutMs ?? DEFAULT_PROBE_TIMEOUT_MS

  if (typeof WebSocketImpl !== 'function') {
    return Promise.resolve('unknown')
  }

  return new Promise(resolve => {
    let activeSessionsIdle: boolean | null = null
    let delegationsIdle: boolean | null = null
    let settled = false
    let socket: any
    let timer: ReturnType<typeof setTimeout> | null = null

    const finish = (activity: PoolActivity) => {
      if (settled) {
        return
      }

      settled = true

      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }

      try {
        socket?.close?.()
      } catch {
        // Best-effort teardown; the result is already authoritative.
      }

      resolve(activity)
    }

    const maybeFinishIdle = () => {
      if (activeSessionsIdle === true && delegationsIdle === true) {
        finish('idle')
      }
    }

    const handleResponse = (event: any) => {
      let message: any

      try {
        message = JSON.parse(String(event?.data ?? ''))
      } catch {
        return
      }

      if (message?.id === ACTIVE_SESSIONS_REQUEST_ID) {
        if (message.error || !Array.isArray(message.result?.sessions)) {
          finish('unknown')

          return
        }

        const sessions = message.result.sessions

        if (
          sessions.some(
            session => !session || typeof session !== 'object' || typeof session.status !== 'string'
          )
        ) {
          finish('unknown')

          return
        }

        activeSessionsIdle = sessions.every(session => session.status === 'idle')

        if (!activeSessionsIdle) {
          finish('busy')

          return
        }

        maybeFinishIdle()

        return
      }

      if (message?.id === DELEGATIONS_REQUEST_ID) {
        if (message.error || !Array.isArray(message.result?.active)) {
          finish('unknown')

          return
        }

        delegationsIdle = message.result.active.length === 0

        if (!delegationsIdle) {
          finish('busy')

          return
        }

        maybeFinishIdle()
      }
    }

    try {
      socket = new WebSocketImpl(wsUrl)
    } catch {
      finish('unknown')

      return
    }

    socket.addEventListener('open', () => {
      try {
        socket.send(
          JSON.stringify({
            id: ACTIVE_SESSIONS_REQUEST_ID,
            jsonrpc: '2.0',
            method: 'session.active_list',
            params: {}
          })
        )
        socket.send(
          JSON.stringify({
            id: DELEGATIONS_REQUEST_ID,
            jsonrpc: '2.0',
            method: 'delegation.status',
            params: {}
          })
        )
      } catch {
        finish('unknown')
      }
    })
    socket.addEventListener('message', handleResponse)
    socket.addEventListener('error', () => finish('unknown'))
    socket.addEventListener('close', () => finish('unknown'))

    timer = setTimeout(() => finish('unknown'), Math.max(1, timeoutMs))
  })
}
