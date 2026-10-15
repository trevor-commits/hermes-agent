import assert from 'node:assert/strict'

import { SourceMapConsumer } from 'source-map-js'
import { test } from 'vitest'

const flatMap = { version: 3, sources: ['source.js'], names: [], mappings: 'AAAA' }
const indexedMap = (line, map = flatMap) => ({ version: 3, sections: [{ offset: { line, column: 0 }, map }] })

test('flat source maps used by CSS builds preserve source positions', () => {
  const consumer = new SourceMapConsumer(flatMap)

  assert.deepEqual(consumer.originalPositionFor({ line: 1, column: 0 }), {
    source: 'source.js',
    line: 1,
    column: 0,
    name: null
  })
})

test('invalid and excessive indexed offsets reject before generating large maps', () => {
  for (const line of [-1, 0.5, NaN, Infinity, 1e9]) {
    assert.throws(() => new SourceMapConsumer(indexedMap(line)), /offset/)
  }
  assert.throws(() => new SourceMapConsumer(indexedMap(6e6, indexedMap(6e6))), /offset/)
})
