import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'

import selectorParser from 'postcss-selector-parser'
import { test } from 'vitest'

test('selector parsing preserves the CSS forms used by typography builds', () => {
  for (const selector of [
    '.prose :where(p):not(:where([class~="not-prose"] *))',
    'svg|a[href^="https://"]',
    ':is(.a, #b) > :not([hidden])',
    '.foo\\:bar::before',
    '&:hover, .a + .b',
  ]) {
    assert.equal(selectorParser().processSync(selector), selector)
  }
})

test('malformed selectors still reject', () => {
  assert.throws(() => selectorParser().processSync('.a['))
})

test('flat class selectors complete within a bounded child process', () => {
  // GHSA-rj75-hqrm-r3gf: this 400 KB input previously took about 34 seconds.
  // The child timeout stops a regressed synchronous parser and reaps its process.
  const result = execFileSync(process.execPath, ['-e', `
    const parser = require('postcss-selector-parser');
    const selector = '.a'.repeat(200000);
    const ast = parser().astSync(selector);
    if (ast.first.nodes.length !== 200000 || ast.toString() !== selector) {
      throw new Error('Flat selector lost nodes or changed bytes');
    }
    process.stdout.write('complete');
  `], { encoding: 'utf8', timeout: 5000 })
  assert.equal(result, 'complete')
})
