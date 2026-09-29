import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type { NodeData } from '@/stores/workflow-store';

// The vitest environment is `node` (no DOM), so collapsed iteration sections are not
// clickable. Start every boolean `useState` expanded so the historical iteration's
// metadata renders in the static markup too.
vi.mock('react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react')>();
  return {
    ...actual,
    useState: (initial: unknown) =>
      typeof initial === 'boolean' ? [true, () => undefined] : actual.useState(initial),
  };
});

const { AgentDetail } = await import('./AgentDetail');

function secondIterationNode(): NodeData {
  return {
    name: 'writer',
    type: 'agent',
    status: 'completed',
    iteration: 2,
    output: 'second',
    elapsed: 2,
    cost_usd: 0.02,
    billing_mode: 'subscription',
    activity: [],
    iterationHistory: [
      {
        iteration: 1,
        output: 'first',
        elapsed: 1,
        cost_usd: 0.01,
        billing_mode: 'unknown',
        activity: [],
      },
    ],
  } as unknown as NodeData;
}

describe('AgentDetail iteration cost labels', () => {
  it('labels both the current iteration and the historical iteration', () => {
    const html = renderToStaticMarkup(createElement(AgentDetail, { node: secondIterationNode() }));

    const [current, history] = html.split('Iteration 1');
    expect(current).toContain('Iteration 2 (current)');
    // Current snapshot carries billing_mode: subscription -> estimate label.
    expect(current).toContain('$0.0200 API-equivalent estimate');
    // Historical snapshot carries its own mode: unknown -> source-unknown label.
    expect(history).toContain('$0.0100 billing source unknown');
    // The two labels are not swapped or shared between iterations.
    expect(current).not.toContain('billing source unknown');
    expect(history).not.toContain('API-equivalent estimate');
  });
});
