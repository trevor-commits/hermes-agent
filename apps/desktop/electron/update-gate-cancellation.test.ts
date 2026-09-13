import assert from 'node:assert/strict'

import { test } from 'vitest'

import { waitForUpdateClearance } from './update-gate'

test('quit interrupts an update-gated wait instead of waiting for the next poll', async () => {
  const controller = new AbortController()
  let slept = false

  const waiting = waitForUpdateClearance(
    { hasLiveMarker: () => true, isUpdateInFlight: () => false },
    {
      signal: controller.signal,
      pollMs: 1000,
      sleep: () => {
        slept = true

        return new Promise(() => {})
      }
    }
  )

  const rejected = assert.rejects(waiting, /Backend startup cancelled/)
  for (let i = 0; i < 10; i++) {
    await Promise.resolve()
  }
  assert.equal(slept, true)
  controller.abort()
  await rejected
})
