import { describe, expect, it } from 'vitest';
import {
  aggregateLabel,
  billingFromWire,
  billingState,
  billingStated,
  coerceBillingMode,
  countBilling,
  emptyBillingCounts,
  formatCostWithBilling,
  modeLabel,
  nodeBillingTitle,
  totalBillingLabel,
  withBilling,
  type BillingCounts,
  type BillingMode,
} from './billing';

function countsOf(modes: Array<BillingMode | null>): BillingCounts {
  const counts = emptyBillingCounts();
  for (const mode of modes) countBilling(counts, mode);
  return counts;
}

describe('coerceBillingMode', () => {
  it.each([
    [null, null],
    [undefined, null],
    ['metered_api', 'metered_api'],
    ['subscription', 'subscription'],
    ['unknown', 'unknown'],
    ['bedrock', 'unknown'], // a newer writer's value: unproven, never dropped
    ['', 'unknown'],
    ['mixed', 'unknown'], // aggregate-only state is not a per-execution mode
    [3, null],
    [true, null],
    [{ mode: 'subscription' }, null],
    [['subscription'], null],
  ])('%j -> %j', (value, expected) => {
    expect(coerceBillingMode(value)).toBe(expected);
  });
});

// [modes, breakdown, state, stated, detailed label, short label]
const ROWS: Array<
  [Array<BillingMode | null>, Partial<BillingCounts>, string, boolean, string | null, string | null]
> = [
  [[], {}, 'unknown', false, null, null],
  [['metered_api', 'metered_api', 'metered_api'], { metered_api: 3 }, 'metered_api', true, null, null],
  [
    ['subscription', 'subscription', 'subscription'],
    { subscription: 3 },
    'subscription',
    true,
    'API-equivalent estimate',
    'API-equivalent estimate',
  ],
  [
    ['subscription', 'subscription', 'metered_api'],
    { subscription: 2, metered_api: 1 },
    'mixed',
    true,
    'mixed billing: 2 subscription, 1 metered API',
    'mixed billing',
  ],
  [
    ['subscription', 'metered_api', 'unknown'],
    { subscription: 1, metered_api: 1, unknown: 1 },
    'mixed',
    true,
    'mixed billing: 1 subscription, 1 metered API, 1 unknown',
    'mixed billing',
  ],
  [
    ['subscription', 'subscription', 'unknown'],
    { subscription: 2, unknown: 1 },
    'mixed',
    true,
    'mixed billing: 2 subscription, 1 unknown',
    'mixed billing',
  ],
  [
    ['subscription', null],
    { subscription: 1, unstated: 1 },
    'mixed',
    true,
    'mixed billing: 1 subscription, 1 not reported',
    'mixed billing',
  ],
  [
    ['metered_api', null, null],
    { metered_api: 1, unstated: 2 },
    'mixed',
    true,
    'mixed billing: 1 metered API, 2 not reported',
    'mixed billing',
  ],
  [['unknown', 'unknown'], { unknown: 2 }, 'unknown', true, 'billing source unknown', 'billing source unknown'],
  [['unknown', null], { unknown: 1, unstated: 1 }, 'unknown', true, 'billing source unknown', 'billing source unknown'],
  [[null, null, null], { unstated: 3 }, 'unknown', false, null, null],
  [
    ['subscription', 'metered_api'],
    { subscription: 1, metered_api: 1 },
    'mixed',
    true,
    'mixed billing: 1 subscription, 1 metered API',
    'mixed billing',
  ],
];

describe('aggregation truth table', () => {
  it.each(ROWS)('%j', (modes, breakdown, state, stated, detailed, short) => {
    const counts = countsOf(modes);
    expect(counts).toEqual({ ...emptyBillingCounts(), ...breakdown });
    expect(billingState(counts)).toBe(state);
    expect(billingStated(counts)).toBe(stated);
    expect(aggregateLabel(counts, true)).toBe(detailed);
    expect(aggregateLabel(counts)).toBe(short);
  });

  it('two subscription + one metered is mixed, not the majority mode', () => {
    expect(billingState(countsOf(['subscription', 'subscription', 'metered_api']))).toBe('mixed');
  });

  it('a known mode alongside unknown or unstated is mixed', () => {
    expect(billingState(countsOf(['subscription', 'unknown']))).toBe('mixed');
    expect(billingState(countsOf(['metered_api', null]))).toBe('mixed');
  });

  it('is permutation invariant', () => {
    const modes: Array<BillingMode | null> = ['subscription', 'metered_api', 'unknown', null];
    const first = countsOf(modes);
    expect(countsOf([...modes].reverse())).toEqual(first);
    expect(billingState(countsOf([...modes].reverse()))).toBe(billingState(first));
  });

  it('null counts as unstated', () => {
    const counts = emptyBillingCounts();
    countBilling(counts, null);
    expect(counts.unstated).toBe(1);
    expect(billingStated(counts)).toBe(false);
  });

  it('withBilling returns a new object and never modifies its input', () => {
    const before = countsOf(['subscription']);
    const after = withBilling(before, null);
    expect(after).not.toBe(before);
    expect(before).toEqual({ ...emptyBillingCounts(), subscription: 1 });
    expect(after).toEqual({ ...emptyBillingCounts(), subscription: 1, unstated: 1 });
  });

  it('emptyBillingCounts returns a fresh object each time', () => {
    const a = emptyBillingCounts();
    a.subscription = 5;
    expect(emptyBillingCounts().subscription).toBe(0);
  });
});

describe('billingFromWire', () => {
  it('round-trips the backend wire form and recomputes state', () => {
    const wire = { state: 'metered_api', breakdown: { subscription: 2, metered_api: 1 } };
    const counts = billingFromWire(wire);
    expect(counts).toEqual({ ...emptyBillingCounts(), subscription: 2, metered_api: 1 });
    expect(billingState(counts!)).toBe('mixed'); // the wire `state` is not trusted
  });

  it.each([null, undefined, 'subscription', 7, true, [], { breakdown: 'nope' }, { breakdown: [] }, {}])(
    'malformed %j is not stated',
    (value) => {
      expect(billingFromWire(value)).toBeNull();
    },
  );

  it('skips non-integer, negative and non-number counts', () => {
    const counts = billingFromWire({
      breakdown: { subscription: 1, metered_api: -1, unknown: 1.5, unstated: '2' },
    });
    expect(counts).toEqual({ ...emptyBillingCounts(), subscription: 1 });
  });

  it('counts unrecognized breakdown keys as unknown', () => {
    const counts = billingFromWire({ breakdown: { subscription: 1, bedrock: 2, vertex: 1 } });
    expect(counts).toEqual({ ...emptyBillingCounts(), subscription: 1, unknown: 3 });
    expect(billingState(counts!)).toBe('mixed');
  });

  it('an aggregate in which nothing stated provenance is not stated', () => {
    expect(billingFromWire({ breakdown: { unstated: 3 } })).toBeNull();
    expect(billingFromWire({ breakdown: { subscription: 0 } })).toBeNull();
  });
});

describe('labels', () => {
  it('modeLabel wording', () => {
    expect(modeLabel('subscription')).toBe('API-equivalent estimate');
    expect(modeLabel('unknown')).toBe('billing source unknown');
    expect(modeLabel('metered_api')).toBeNull();
    expect(modeLabel(null)).toBeNull();
    expect(modeLabel(undefined)).toBeNull();
  });

  it('aggregateLabel tolerates null/undefined and never echoes wire text', () => {
    expect(aggregateLabel(null)).toBeNull();
    expect(aggregateLabel(undefined, true)).toBeNull();
    const hostile = '<img src=x onerror=alert(1)>';
    const counts = billingFromWire({ breakdown: { [hostile]: 2, subscription: 1 } });
    expect(aggregateLabel(counts, true)).toBe('mixed billing: 1 subscription, 2 unknown');
    expect(coerceBillingMode(hostile)).toBe('unknown');
    expect(modeLabel(coerceBillingMode(hostile))).toBe('billing source unknown');
  });
});

describe('formatCostWithBilling', () => {
  it.each<[BillingMode | null | undefined]>([['metered_api'], [null], [undefined]])(
    '%s leaves the cost unchanged',
    (mode) => {
      expect(formatCostWithBilling(0.0123, mode)).toBe('$0.0123');
      expect(formatCostWithBilling(0.0123, mode, { compact: true })).toBe('$0.0123');
    },
  );

  it('long form appends the label', () => {
    expect(formatCostWithBilling(0.0123, 'subscription')).toBe('$0.0123 API-equivalent estimate');
    expect(formatCostWithBilling(0.0123, 'unknown')).toBe('$0.0123 billing source unknown');
  });

  it('compact form uses the short chip', () => {
    expect(formatCostWithBilling(0.0123, 'subscription', { compact: true })).toBe('$0.0123 (est.)');
    expect(formatCostWithBilling(0.0123, 'unknown', { compact: true })).toBe('$0.0123 (source unknown)');
  });

  it('never labels a missing figure', () => {
    expect(formatCostWithBilling(undefined, 'subscription')).toBe('');
    expect(formatCostWithBilling(undefined, 'unknown', { compact: true })).toBe('');
  });
});

describe('nodeBillingTitle', () => {
  it('completed node with a cost and subscription provenance is labelled', () => {
    expect(nodeBillingTitle('completed', 0.0123, 'subscription')).toBe('API-equivalent estimate');
    expect(nodeBillingTitle('completed', 0.0123, 'unknown')).toBe('billing source unknown');
  });

  it('completed but unpriced node has no label', () => {
    expect(nodeBillingTitle('completed', undefined, 'subscription')).toBeNull();
    expect(nodeBillingTitle('completed', null, 'unknown')).toBeNull();
  });

  it.each(['running', 'pending', 'failed', 'waiting', undefined, null])(
    'a %s node with stale cost and provenance has no label',
    (status) => {
      expect(nodeBillingTitle(status, 0.0123, 'subscription')).toBeNull();
    },
  );

  it('legacy or metered provenance is unchanged: no label', () => {
    expect(nodeBillingTitle('completed', 0.0123, null)).toBeNull();
    expect(nodeBillingTitle('completed', 0.0123, undefined)).toBeNull();
    expect(nodeBillingTitle('completed', 0.0123, 'metered_api')).toBeNull();
  });
});

describe('totalBillingLabel', () => {
  const subscription = countsOf(['subscription', 'subscription']);
  const mixed = countsOf(['subscription', 'metered_api']);

  it('a displayed positive total keeps its label and detail', () => {
    expect(totalBillingLabel(0.5, subscription)).toBe('API-equivalent estimate');
    expect(totalBillingLabel(0.5, subscription, true)).toBe('API-equivalent estimate');
    expect(totalBillingLabel(0.5, mixed)).toBe('mixed billing');
    expect(totalBillingLabel(0.5, mixed, true)).toBe('mixed billing: 1 subscription, 1 metered API');
  });

  it.each([0, -1, Number.NaN])('a %s total shows no billing detail, only unpriced executions', (total) => {
    expect(totalBillingLabel(total, countsOf(['unknown']))).toBeNull();
    expect(totalBillingLabel(total, countsOf(['unknown']), true)).toBeNull();
    expect(totalBillingLabel(total, mixed, true)).toBeNull();
  });

  it('legacy or unstated provenance is unchanged: no label', () => {
    expect(totalBillingLabel(0.5, countsOf([null, null]))).toBeNull();
    expect(totalBillingLabel(0.5, countsOf([null]), true)).toBeNull();
    expect(totalBillingLabel(0.5, countsOf(['metered_api']), true)).toBeNull();
    expect(totalBillingLabel(0.5, null)).toBeNull();
    expect(totalBillingLabel(0.5, undefined, true)).toBeNull();
  });
});
