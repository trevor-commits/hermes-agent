import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $displayTimestamps } from '@/store/display-timestamps'

import { stubThreadEnvironment } from '../test-utils'

import { Thread } from '.'

// Timeline timestamps render only when `display.timestamps` is enabled.
$displayTimestamps.set(true)

const timestamp = new Date('2026-05-01T00:00:00.000Z')
stubThreadEnvironment()

function Harness({ custom, text, asyncResult }: { custom?: Record<string, unknown>; text: string; asyncResult?: string }) {
  const message = {
    id: 'system-1',
    role: 'system',
    content: [{ type: 'text', text }],
    createdAt: timestamp,
    metadata: { custom: { timelineTimestamp: timestamp.getTime() / 1000, ...custom, asyncResult } }
  } as unknown as ThreadMessage

  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [message],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

function expectTimestampSeparated(container: HTMLElement, precedingText: string) {
  const row = container.querySelector('[data-role="system"]')
  const stamp = row?.querySelector('[data-slot="timeline-timestamp"]')?.textContent

  expect(stamp).toBeTruthy()
  expect(row?.textContent).toContain(`${precedingText} ${stamp}`)
}

afterEach(cleanup)

describe('background report disclosure', () => {
  it('keeps result bodies out of the transcript until opened and removes them when collapsed', () => {
    const report = '{"blockers":[{"title":"Local-model readiness uses the wrong endpoint"}]}'
    const { container, getByRole } = render(<Harness asyncResult={report} text="2 background agents finished" />)

    expect(container.textContent).not.toContain('blockers')
    expectTimestampSeparated(container, '2 background agents finished')
    const toggle = getByRole('button', { name: '2 background agents finished' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(container.textContent).toContain(report)

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(container.textContent).not.toContain('blockers')
  })
})

describe('system message timestamp text separation', () => {
  it('collapses preserved compression tasks under a context handoff label', () => {
    const detail = [
      '[Your active task list was preserved across context compression]',
      '- [>] locate. Locate the interrupted chat (in_progress)',
      '- [ ] audit. Audit what it left incomplete (pending)',
      '',
      '[Skills pruned during compression — reload before acting on these tasks]',
      "Reload first: skill_view(name='ops-runbooks')."
    ].join('\n')

    const { container } = render(
      <Harness
        custom={{ contextHandoff: { detail, taskCount: 2 } }}
        text="Context handoff — 2 tasks preserved"
      />
    )

    const disclosure = container.querySelector('details[data-slot="context-handoff"]')

    expect(disclosure).not.toBeNull()
    expect((disclosure as HTMLDetailsElement).open).toBe(false)
    expect(disclosure?.querySelector('summary')?.textContent).toBe('Context handoff — 2 tasks preserved')
    expect(disclosure?.querySelector('pre')?.textContent).toContain("skill_view(name='ops-runbooks')")
    expect(container.querySelector('[data-role="user"]')).toBeNull()

    fireEvent.click(disclosure!.querySelector('summary')!)

    expect((disclosure as HTMLDetailsElement).open).toBe(true)
  })

  it('separates an ordinary system row timestamp in accessible and copied text', () => {
    const { container } = render(<Harness text="Review saved." />)

    expectTimestampSeparated(container, 'Review saved.')
  })

  it('separates a slash-status timestamp in accessible and copied text', () => {
    const { container } = render(<Harness text={'slash:/model\nmodel changed'} />)

    expectTimestampSeparated(container, 'model changed')
  })

  it('separates a steer timestamp in accessible and copied text', () => {
    const { container } = render(<Harness text="steer:rerun tests" />)

    expectTimestampSeparated(container, 'rerun tests')
  })
})
