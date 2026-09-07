import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The store wires itself to gateway/profile atoms and the REST layer at import
// time paths; mock the RPC and notification seams, but use real atoms so the
// lifecycle tests exercise subscriptions, async sweeps, and the shared cache.
vi.mock('@/hermes', () => ({
  getHermesConfigRecord: vi.fn(),
  testMcpServer: vi.fn()
}))

vi.mock('@/i18n', () => ({
  translateNow: (key: string) => key
}))

vi.mock('@/store/notifications', () => ({
  dismissNotification: vi.fn(),
  notify: vi.fn()
}))

vi.mock('@/store/profile', async () => {
  const { atom } = await import('nanostores')

  return {
    $activeGatewayProfile: atom('default'),
    normalizeProfileKey: (name: string | null | undefined) => (name ?? '').trim() || 'default'
  }
})

vi.mock('@/store/session', async () => {
  const { atom } = await import('nanostores')

  return { $gatewayState: atom('closed') }
})

const { shouldNotifyOnTransition, startMcpHealthChecker, stopMcpHealthChecker } = await import('./mcp-health')
const { getHermesConfigRecord, testMcpServer } = await import('@/hermes')
const { probeCache, probeKey } = await import('@/lib/mcp-probe-cache')
const { dismissNotification, notify } = await import('@/store/notifications')
const { $activeGatewayProfile } = await import('@/store/profile')
const { $gatewayState } = await import('@/store/session')

type Status = 'error' | 'needs-auth' | 'ok'

describe('shouldNotifyOnTransition', () => {
  // The full previous × next decision table: notify only on a TRANSITION into
  // a bad state. Rechecks of an already-bad server stay quiet; ok never nudges.
  it.each<[previous: Status | null, next: Status, notify: boolean]>([
    // First observation of the session (previous unknown).
    [null, 'ok', false],
    [null, 'needs-auth', true],
    [null, 'error', true],
    // Healthy server stays healthy / breaks.
    ['ok', 'ok', false],
    ['ok', 'needs-auth', true],
    ['ok', 'error', true],
    // Already-broken server: rechecks must NOT re-notify…
    ['needs-auth', 'needs-auth', false],
    ['error', 'error', false],
    // …but flipping from one bad state to the other is a new transition.
    ['needs-auth', 'error', true],
    ['error', 'needs-auth', true],
    // Recovery is silent.
    ['needs-auth', 'ok', false],
    ['error', 'ok', false]
  ])('previous=%s next=%s → notify=%s', (previous, next, expected) => {
    expect(shouldNotifyOnTransition(previous, next)).toBe(expected)
  })
})

describe('background MCP health sweeps', () => {
  beforeEach(() => {
    stopMcpHealthChecker()
    $gatewayState.set('closed')
    $activeGatewayProfile.set('default')
    probeCache.clear()
    vi.clearAllMocks()
    vi.useFakeTimers()
  })

  afterEach(async () => {
    stopMcpHealthChecker()
    await vi.advanceTimersByTimeAsync(0)
    vi.useRealTimers()
  })

  it('discards a failed probe from before a disconnect, even after reconnect', async () => {
    const server = { url: 'http://localhost:8765/mcp', enabled: true }
    const name = 'late-disconnect'

    let rejectOldProbe!: (reason: Error) => void

    const oldProbe = new Promise<never>((_resolve, reject) => {
      rejectOldProbe = reject
    })

    const healthy = { ok: true, tools: [] }

    vi.mocked(getHermesConfigRecord).mockResolvedValue({ mcp_servers: { [name]: server } })
    vi.mocked(testMcpServer).mockReturnValueOnce(oldProbe).mockResolvedValue(healthy)
    $gatewayState.set('open')
    startMcpHealthChecker()
    await vi.advanceTimersByTimeAsync(0)
    expect(testMcpServer).toHaveBeenCalledTimes(1)

    $gatewayState.set('closed')
    $gatewayState.set('open')
    rejectOldProbe(new Error('gateway disconnected'))
    await vi.advanceTimersByTimeAsync(0)

    expect(notify).not.toHaveBeenCalled()
    expect(testMcpServer).toHaveBeenCalledTimes(2)
    expect(probeCache.get(probeKey(name, server, 'default'))?.result).toEqual(healthy)
  })

  it('clears only the recovered server warning after a verified healthy sweep', async () => {
    const server = { url: 'http://localhost:8765/mcp', enabled: true }
    const name = 'verified-recovery'

    vi.mocked(getHermesConfigRecord).mockResolvedValue({ mcp_servers: { [name]: server } })
    vi.mocked(testMcpServer)
      .mockResolvedValueOnce({ ok: false, error: 'connection refused', tools: [] })
      .mockResolvedValue({ ok: true, tools: [] })
    $gatewayState.set('open')
    startMcpHealthChecker()
    await vi.advanceTimersByTimeAsync(0)
    expect(notify).toHaveBeenCalledTimes(1)
    expect(dismissNotification).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(30 * 60_000)

    expect(testMcpServer).toHaveBeenCalledTimes(2)
    expect(dismissNotification).toHaveBeenCalledExactlyOnceWith(`mcp-health-default::${name}`)
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('still reports a current authentication failure after reconnect', async () => {
    const server = { url: 'http://localhost:8765/mcp', enabled: true }
    const name = 'current-auth-error'

    let rejectOldProbe!: (reason: Error) => void

    const oldProbe = new Promise<never>((_resolve, reject) => {
      rejectOldProbe = reject
    })

    vi.mocked(getHermesConfigRecord).mockResolvedValue({ mcp_servers: { [name]: server } })
    vi.mocked(testMcpServer)
      .mockReturnValueOnce(oldProbe)
      .mockResolvedValue({ ok: false, error: '401 unauthorized', tools: [] })
    $gatewayState.set('open')
    startMcpHealthChecker()
    await vi.advanceTimersByTimeAsync(0)
    $gatewayState.set('closed')
    $gatewayState.set('open')
    rejectOldProbe(new Error('gateway disconnected'))
    await vi.advanceTimersByTimeAsync(0)

    expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      id: `mcp-health-default::${name}`,
      title: 'notifications.mcp.needsAuthTitle'
    }))
    expect(dismissNotification).not.toHaveBeenCalled()
  })

  it('reports a new failure after a verified recovery cleared the warning', async () => {
    const name = 'failure-after-recovery'
    const server = { url: 'http://localhost:8765/mcp', enabled: true }

    vi.mocked(getHermesConfigRecord).mockResolvedValue({ mcp_servers: { [name]: server } })
    vi.mocked(testMcpServer)
      .mockResolvedValueOnce({ ok: false, error: 'connection refused', tools: [] })
      .mockResolvedValueOnce({ ok: true, tools: [] })
      .mockResolvedValue({ ok: false, error: '401 unauthorized', tools: [] })
    $gatewayState.set('open')
    startMcpHealthChecker()
    await vi.advanceTimersByTimeAsync(0)
    expect(notify).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(30 * 60_000)
    expect(dismissNotification).toHaveBeenCalledExactlyOnceWith(`mcp-health-default::${name}`)
    expect(notify).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(30 * 60_000)
    expect(testMcpServer).toHaveBeenCalledTimes(3)
    expect(notify).toHaveBeenCalledTimes(2)
    expect(notify).toHaveBeenLastCalledWith(expect.objectContaining({
      id: `mcp-health-default::${name}`,
      title: 'notifications.mcp.needsAuthTitle'
    }))
  })

  it('never launches configured stdio servers from a health sweep', async () => {
    vi.mocked(getHermesConfigRecord).mockResolvedValue({
      mcp_servers: {
        stdio: { command: 'must-not-run', enabled: true },
        disabled: { url: 'http://localhost:8765/mcp', enabled: false }
      }
    })
    $gatewayState.set('open')
    startMcpHealthChecker()
    await vi.advanceTimersByTimeAsync(0)

    expect(testMcpServer).not.toHaveBeenCalled()
    expect(notify).not.toHaveBeenCalled()
    expect(probeCache.size).toBe(0)
  })
})
