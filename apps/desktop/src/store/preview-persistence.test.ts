import { describe, expect, it } from 'vitest'

import { decodePreviewTabs } from './preview'

describe('persisted preview migration', () => {
  it('drops a persisted browser tab instead of reopening its website after restart', () => {
    const restored = decodePreviewTabs(
      JSON.stringify([
        {
          id: 'url:browser',
          target: {
            kind: 'url',
            label: 'Browser',
            source: 'https://example.com',
            url: 'https://example.com'
          }
        }
      ])
    )

    expect(restored).toEqual([])
  })

  it('drops legacy browser and file rows so no preview rail reopens', () => {
    const fileSource = '/work/notes.txt'

    const restored = decodePreviewTabs(
      JSON.stringify([
        {
          id: `file:file://${fileSource}`,
          target: {
            kind: 'file',
            label: 'notes.txt',
            path: fileSource,
            previewKind: 'text',
            source: fileSource,
            url: `file://${fileSource}`
          }
        },
        {
          id: 'url:browser',
          target: {
            kind: 'url',
            label: 'Browser',
            source: 'https://example.com',
            url: 'https://example.com'
          }
        }
      ])
    )

    expect(restored).toEqual([])
  })

})
