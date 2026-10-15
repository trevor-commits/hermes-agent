import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchHistoryWindow } from '@/app/chat/history-window'
import { fetchTimelineIndex } from '@/components/assistant-ui/thread/timeline-index'

import {
  setApiRequestConnection,
  setApiRequestLocalMode,
  setApiRequestProfile,
  setSessionOwnerResolver
} from './client'
import { getLatestSessionMessages, getSession, getSessionMessages } from './sessions'

describe('session reads retain the owning profile', () => {
  const api = vi.fn()

  beforeEach(() => {
    api.mockReset().mockResolvedValue({
      session_id: 'stored',
      messages: [],
      entries: [],
      pagination: { limit: 120, offset: 0, order: 'latest', returned: 0, has_more: false }
    })
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
    setApiRequestProfile('default')
    setApiRequestLocalMode(true)
  })

  afterEach(() => {
    setSessionOwnerResolver(null)
    setApiRequestConnection(null)
    setApiRequestProfile(null)
    setApiRequestLocalMode(false)
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it.each([
    ['local', null, true],
    ['local', null, false],
    ['remote', 'remote', false],
    ['remote', 'another-connection', false]
  ] as const)(
    'reads detail, tail, timeline and history from owner %s while ambient is %s (local mode: %s)',
    async (owner, ambient, localMode) => {
      setApiRequestConnection(ambient)
      setApiRequestLocalMode(localMode)
      setSessionOwnerResolver(() => ({ connectionId: owner, profile: 'display-name', targetProfile: 'research' }))
      const id = `stored-${owner}-${ambient}-${localMode}`

      await getSession(id)
      await getSessionMessages(id)
      await getLatestSessionMessages(id)
      await fetchTimelineIndex(id, undefined)
      await fetchHistoryWindow(id, 40, undefined, new AbortController().signal)

      expect(api).toHaveBeenCalledTimes(5)

      for (const [request] of api.mock.calls) {
        expect(new URL(request.path, 'http://test').searchParams.get('profile')).toBe('research')
        expect(request.profile).toBe('research')
        expect(request.connectionId ?? 'local').toBe(owner)

        if (owner === 'local' && !localMode) {
          expect(request.connectionId).toBe('local')
        }
      }
    }
  )

  it('keeps explicit profile probes and exact caller routes authoritative, and leaves unknown owners unpinned', async () => {
    setApiRequestConnection(null)
    setSessionOwnerResolver(() => ({ connectionId: 'local', profile: 'research' }))
    await getSessionMessages('probe', 'other-profile')
    expect(api.mock.lastCall?.[0].path).toBe('/api/sessions/probe/messages?profile=other-profile')

    await getSessionMessages('explicit', { connectionId: 'other-host', profile: 'other-profile' })
    expect(api.mock.lastCall?.[0]).toMatchObject({
      connectionId: 'other-host',
      path: '/api/sessions/explicit/messages?profile=other-profile'
    })

    setSessionOwnerResolver(() => undefined)
    await getSessionMessages('unknown')
    expect(api.mock.lastCall?.[0]).toEqual({ path: '/api/sessions/unknown/messages' })
  })

  it('retains a legacy owner profile on the ambient remote without inventing a local route', async () => {
    setApiRequestConnection(null)
    setApiRequestLocalMode(false)
    setSessionOwnerResolver(() => 'research')

    await getSessionMessages('legacy-profile-owner')
    expect(api.mock.lastCall?.[0]).toEqual({
      profile: 'research',
      path: '/api/sessions/legacy-profile-owner/messages?profile=research'
    })

    await getSessionMessages('legacy-profile-probe', 'other-profile')
    expect(api.mock.lastCall?.[0].path).toBe('/api/sessions/legacy-profile-probe/messages?profile=other-profile')
  })
})
