import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  type PoolActivityEntry,
  probeGatewayActivity,
  selectSafeIdleReaps,
  selectSafeLruEvictions,
  shouldReapPoolEntry
} from './pool-activity'

const NOW = 1_000_000
const IDLE_MS = 600_000
const UNCERTAIN_GRACE_MS = 120_000

function staleEntry(): PoolActivityEntry {
  return { lastActiveAt: NOW - IDLE_MS - 1 }
}

test('renderer disconnect does not reap a backend with an active turn', async () => {
  const entry = staleEntry()

  const reap = await shouldReapPoolEntry(entry, {
    idleMs: IDLE_MS,
    now: NOW,
    probe: async () => 'busy',
    uncertainGraceMs: UNCERTAIN_GRACE_MS
  })

  assert.equal(reap, false)
  assert.equal(entry.lastActiveAt, NOW, 'authoritative busy evidence refreshes the eviction clock')
})

test('a truly idle backend still reaps', async () => {
  const entry = staleEntry()

  const reap = await shouldReapPoolEntry(entry, {
    idleMs: IDLE_MS,
    now: NOW,
    probe: async () => 'idle',
    uncertainGraceMs: UNCERTAIN_GRACE_MS
  })

  assert.equal(reap, true)
})

test('stale busy evidence fails safe once, then an unreachable backend eventually reaps', async () => {
  const entry = staleEntry()

  assert.equal(
    await shouldReapPoolEntry(entry, {
      idleMs: IDLE_MS,
      now: NOW,
      probe: async () => 'busy',
      uncertainGraceMs: UNCERTAIN_GRACE_MS
    }),
    false
  )

  const firstUnknownAt = NOW + IDLE_MS + 1

  assert.equal(
    await shouldReapPoolEntry(entry, {
      idleMs: IDLE_MS,
      now: firstUnknownAt,
      probe: async () => 'unknown',
      uncertainGraceMs: UNCERTAIN_GRACE_MS
    }),
    false,
    'the first failed probe must not turn uncertainty into a destructive stop'
  )

  assert.equal(
    await shouldReapPoolEntry(entry, {
      idleMs: IDLE_MS,
      now: firstUnknownAt + UNCERTAIN_GRACE_MS + 1,
      probe: async () => 'unknown',
      uncertainGraceMs: UNCERTAIN_GRACE_MS
    }),
    true,
    'bounded uncertainty must not make a dead backend permanently resident'
  )
})

test('a renderer touch racing the activity probe cancels the reap', async () => {
  const entry = staleEntry()

  const reap = await shouldReapPoolEntry(entry, {
    idleMs: IDLE_MS,
    now: NOW,
    probe: async () => {
      entry.lastActiveAt = NOW

      return 'idle'
    },
    uncertainGraceMs: UNCERTAIN_GRACE_MS
  })

  assert.equal(reap, false)
})

test('idle reaping keeps active work but selects a truly idle sibling', async () => {
  const entries = new Map([
    ['active', { lastActiveAt: NOW - IDLE_MS - 1, process: { pid: 1 } }],
    ['idle', { lastActiveAt: NOW - IDLE_MS - 1, process: { pid: 2 } }]
  ])

  const reaps = await selectSafeIdleReaps(entries.entries(), {
    idleMs: IDLE_MS,
    now: NOW,
    probe: async key => (key === 'active' ? 'busy' : 'idle'),
    uncertainGraceMs: UNCERTAIN_GRACE_MS
  })

  assert.deepEqual(reaps, ['idle'])
})

test('LRU skips an active oldest backend and evicts the next idle candidate', async () => {
  const entries = new Map([
    ['active-oldest', { lastActiveAt: NOW - 500_000, process: { pid: 1 } }],
    ['idle-next', { lastActiveAt: NOW - 400_000, process: { pid: 2 } }],
    ['fresh', { lastActiveAt: NOW - 1_000, process: { pid: 3 } }]
  ])

  const evictions = await selectSafeLruEvictions(entries.entries(), {
    freshMs: 90_000,
    keep: 2,
    now: NOW,
    probe: async key => (key === 'active-oldest' ? 'busy' : 'idle'),
    uncertainGraceMs: UNCERTAIN_GRACE_MS
  })

  assert.deepEqual(evictions, ['idle-next'])
  assert.equal(entries.get('active-oldest')?.lastActiveAt, NOW)
})

interface FakeSocket {
  close(): void
  emit(type: string, event?: unknown): void
  sent: string[]
}

function fakeWebSocket(): { FakeWebSocket: new (url: string) => FakeSocket; instances: FakeSocket[] } {
  const instances: FakeSocket[] = []

  class FakeWebSocket implements FakeSocket {
    listeners: Record<string, ((event?: unknown) => void)[]> = {}
    sent: string[] = []

    constructor(_url: string) {
      instances.push(this)
    }

    addEventListener(type: string, handler: (event?: unknown) => void): void {
      ;(this.listeners[type] ||= []).push(handler)
    }

    close(): void {}

    emit(type: string, event?: unknown): void {
      for (const handler of this.listeners[type] || []) {
        handler(event)
      }
    }

    send(payload: string): void {
      this.sent.push(payload)
    }
  }

  return { FakeWebSocket, instances }
}

function rpcResult(id: string, result: unknown): { data: string } {
  return { data: JSON.stringify({ id, jsonrpc: '2.0', result }) }
}

test('gateway activity probe recognizes active, pending, queued, and delegated work', async () => {
  for (const status of ['working', 'waiting', 'starting', 'queued']) {
    const { FakeWebSocket, instances } = fakeWebSocket()

    const pending = probeGatewayActivity('ws://backend/api/ws?token=t', {
      timeoutMs: 1_000,
      WebSocketImpl: FakeWebSocket
    })

    instances[0].emit('open')
    instances[0].emit('message', rpcResult('pool-active', { sessions: [{ id: 's1', status }] }))

    assert.equal(await pending, 'busy', `status ${status} must protect the backend`)
  }

  const { FakeWebSocket, instances } = fakeWebSocket()

  const delegated = probeGatewayActivity('ws://backend/api/ws?token=t', {
    timeoutMs: 1_000,
    WebSocketImpl: FakeWebSocket
  })

  instances[0].emit('open')
  instances[0].emit('message', rpcResult('pool-active', { sessions: [{ id: 's1', status: 'idle' }] }))
  instances[0].emit('message', rpcResult('pool-delegations', { active: [{ subagent_id: 'child-1' }] }))

  assert.equal(await delegated, 'busy')
})

test('gateway activity probe reports idle only after both authoritative sources are idle', async () => {
  const { FakeWebSocket, instances } = fakeWebSocket()

  const pending = probeGatewayActivity('ws://backend/api/ws?token=t', {
    timeoutMs: 1_000,
    WebSocketImpl: FakeWebSocket
  })

  instances[0].emit('open')
  instances[0].emit('message', rpcResult('pool-active', { sessions: [{ id: 's1', status: 'idle' }] }))
  instances[0].emit('message', rpcResult('pool-delegations', { active: [] }))

  assert.equal(await pending, 'idle')
})

test('gateway activity probe treats malformed or dead evidence as unknown', async () => {
  const { FakeWebSocket, instances } = fakeWebSocket()

  const malformed = probeGatewayActivity('ws://backend/api/ws?token=t', {
    timeoutMs: 1_000,
    WebSocketImpl: FakeWebSocket
  })

  instances[0].emit('open')
  instances[0].emit('message', rpcResult('pool-active', { sessions: 'stale-cache' }))

  assert.equal(await malformed, 'unknown')

  const dead = fakeWebSocket()

  const unreachable = probeGatewayActivity('ws://backend/api/ws?token=t', {
    timeoutMs: 1_000,
    WebSocketImpl: dead.FakeWebSocket
  })

  dead.instances[0].emit('error', { message: 'ECONNREFUSED' })
  assert.equal(await unreachable, 'unknown')
})
