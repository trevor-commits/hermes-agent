import type { Root } from 'hast'
import katex from 'katex'
import { unified } from 'unified'
import { VFile } from 'vfile'
import { describe, expect, it } from 'vitest'

import { createMemoizedMathPlugin } from './katex-memo'

describe('KaTeX consumer compatibility and trust', () => {
  it('ignores inherited trust while preserving explicitly trusted rendering', () => {
    const expression = String.raw`\href{javascript:alert(1)}{test}`
    const options = Object.assign(Object.create({ trust: true }), { throwOnError: false })

    expect(katex.renderToString(expression, options)).not.toContain('href="javascript:')
    expect(katex.renderToString(expression, { trust: true })).toContain('href="javascript:')
  })

  it.each([false, true])('renders and memoizes ordinary mathematics (display=%s)', display => {
    const tree: Root = {
      type: 'root',
      children: [{
        type: 'element', tagName: 'code',
        properties: { className: [display ? 'math-display' : 'math-inline'] },
        children: [{ type: 'text', value: String.raw`x^2 + \frac{1}{2}` }]
      }]
    }

    const second = structuredClone(tree)
    const processor = unified().use({ plugins: [createMemoizedMathPlugin().rehypePlugin] })
    const file = new VFile()

    processor.runSync(tree, file)
    processor.runSync(second, new VFile())

    expect(file.messages).toHaveLength(0)
    expect(JSON.stringify(tree)).toContain('katex')
    expect(JSON.stringify(tree)).toContain('mathml')
    expect(second).toEqual(tree)

    if (display) {
      expect(JSON.stringify(tree)).toContain('katex-display')
    }
  })
})
