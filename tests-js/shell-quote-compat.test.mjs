import assert from 'node:assert/strict'

import shellQuote from 'shell-quote'
import { test } from 'vitest'

test('shell quoting preserves ordinary command arguments', () => {
  const args = ['node', 'a script.js', 'quote\'and"double', '$HOME', 'a;b', '']

  assert.deepEqual(shellQuote.parse(shellQuote.quote(args)), args)
})

test('a token after a shell comment cannot introduce another line', () => {
  for (const newline of ['\n', '\r', '\u2028', '\u2029']) {
    assert.throws(() => shellQuote.quote([{ comment: 'comment' }, `before${newline}after`]), TypeError)
  }
})
