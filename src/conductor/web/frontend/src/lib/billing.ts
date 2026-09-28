/**
 * Billing provenance helpers: how an execution's model usage is paid for, and how to say
 * so honestly. A pure mirror of `conductor.billing` (Python) for the dashboard tiers only.
 *
 * A cost figure is always an *estimate* computed from token counts and a price table. Every
 * label below is a constant defined here, never a value read from an event, so a tampered
 * or newer-writer event can neither inject text nor be displayed verbatim.
 */
import { formatCost } from './utils';

/** Provenance of ONE execution. Never `'mixed'`; that is an aggregate-only state. */
export type BillingMode = 'metered_api' | 'subscription' | 'unknown';

/** What one contributing execution counts as. `'unstated'` is a provider that reports nothing. */
export type BillingClass = BillingMode | 'unstated';

export type BillingState = BillingMode | 'mixed';

/** Execution counts per class. All four keys are always present. */
export type BillingCounts = Record<BillingClass, number>;

const BILLING_MODES: readonly BillingMode[] = ['metered_api', 'subscription', 'unknown'];
const DISPLAY_ORDER: readonly BillingClass[] = ['subscription', 'metered_api', 'unknown', 'unstated'];
const CLASS_NOUN: Record<BillingClass, string> = {
  subscription: 'subscription',
  metered_api: 'metered API',
  unknown: 'unknown',
  unstated: 'not reported',
};

export const SUBSCRIPTION_LABEL = 'API-equivalent estimate';
export const UNKNOWN_LABEL = 'billing source unknown';
export const MIXED_LABEL = 'mixed billing';

/** Short forms for node chips and collapsed group rows only; the long form goes on `title`. */
export const SUBSCRIPTION_SHORT = 'est.';
export const UNKNOWN_SHORT = 'source unknown';

/**
 * Normalize any value read from an event into a per-execution mode.
 *
 * `null`, `undefined` and non-strings give `null` (not stated); a recognized string is
 * returned as is; any other string gives `'unknown'` so a value this version does not
 * understand is surfaced as unproven rather than dropped or displayed.
 */
export function coerceBillingMode(value: unknown): BillingMode | null {
  if (typeof value !== 'string') return null;
  return (BILLING_MODES as readonly string[]).includes(value) ? (value as BillingMode) : 'unknown';
}

export function emptyBillingCounts(): BillingCounts {
  return { subscription: 0, metered_api: 0, unknown: 0, unstated: 0 };
}

/** Add one execution to a running breakdown (in place); `null` counts as `unstated`. */
export function countBilling(counts: BillingCounts, mode: BillingMode | null): void {
  counts[mode ?? 'unstated'] += 1;
}

/**
 * Return a new breakdown with one more execution counted, leaving `counts` untouched. The
 * store uses this rather than `countBilling`: `processEvent` shallow-copies state, so an
 * in-place update would keep the same reference (no re-render) and rewrite earlier snapshots.
 */
export function withBilling(counts: BillingCounts, mode: BillingMode | null): BillingCounts {
  const next = { ...counts };
  countBilling(next, mode);
  return next;
}

/** True when at least one contributing execution reported provenance. */
export function billingStated(counts: BillingCounts): boolean {
  return DISPLAY_ORDER.some((cls) => cls !== 'unstated' && counts[cls] > 0);
}

/** Derived from which classes are present; there is no dominant mode and no tie-break. */
export function billingState(counts: BillingCounts): BillingState {
  const present = DISPLAY_ORDER.filter((cls) => counts[cls] > 0);
  if (present.length === 0) return 'unknown'; // no relevant executions
  if (present.length === 1 && present[0] === 'metered_api') return 'metered_api';
  if (present.length === 1 && present[0] === 'subscription') return 'subscription';
  if (present.every((cls) => cls === 'unknown' || cls === 'unstated')) return 'unknown';
  return 'mixed'; // >1 class, or a known mode alongside unknown/unstated
}

/**
 * Parse the wire form of an aggregate (`{state, breakdown}`): `state` is ignored and
 * recomputed, unrecognized breakdown keys count as `unknown`, malformed input (or an
 * aggregate in which nothing stated provenance) is "not stated" and gives `null`.
 */
export function billingFromWire(value: unknown): BillingCounts | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const breakdown = (value as Record<string, unknown>).breakdown;
  if (typeof breakdown !== 'object' || breakdown === null || Array.isArray(breakdown)) return null;
  const counts = emptyBillingCounts();
  for (const [key, count] of Object.entries(breakdown as Record<string, unknown>)) {
    if (typeof count !== 'number' || !Number.isInteger(count) || count < 0) continue;
    const cls: BillingClass = (DISPLAY_ORDER as readonly string[]).includes(key)
      ? (key as BillingClass)
      : 'unknown';
    counts[cls] += count;
  }
  return billingStated(counts) ? counts : null;
}

/** Label for one execution's figure, or `null` when it needs none. */
export function modeLabel(mode: BillingMode | null | undefined): string | null {
  if (mode === 'subscription') return SUBSCRIPTION_LABEL;
  if (mode === 'unknown') return UNKNOWN_LABEL;
  return null; // metered_api keeps today's presentation; null/undefined is not stated
}

/**
 * Label for a displayed total, or `null` when the figure needs none. `detailed` adds the
 * breakdown to the mixed label (activity lines and tooltips); the status bar uses the short form.
 */
export function aggregateLabel(counts: BillingCounts | null | undefined, detailed = false): string | null {
  if (!counts || !billingStated(counts)) return null;
  const state = billingState(counts);
  if (state === 'subscription') return SUBSCRIPTION_LABEL;
  if (state === 'unknown') return UNKNOWN_LABEL;
  if (state === 'mixed') {
    if (!detailed) return MIXED_LABEL;
    const parts = DISPLAY_ORDER.filter((cls) => counts[cls] > 0)
      .map((cls) => `${counts[cls]} ${CLASS_NOUN[cls]}`)
      .join(', ');
    return `${MIXED_LABEL}: ${parts}`;
  }
  return null; // metered_api
}

/**
 * A cost with its billing label. `metered_api`, `null` and `undefined` give `formatCost(cost)`
 * unchanged, and so does a missing figure: a label with no dollar amount qualifies nothing.
 * Otherwise the long form is `$0.0123 API-equivalent estimate` and the compact form (node
 * chips) is `$0.0123 (est.)` / `$0.0123 (source unknown)`.
 */
export function formatCostWithBilling(
  cost: number | undefined,
  mode: BillingMode | null | undefined,
  options: { compact?: boolean } = {},
): string {
  const base = formatCost(cost);
  if (cost == null) return base;
  if (options.compact) {
    if (mode === 'subscription') return `${base} (${SUBSCRIPTION_SHORT})`;
    if (mode === 'unknown') return `${base} (${UNKNOWN_SHORT})`;
    return base;
  }
  const label = modeLabel(mode);
  return label ? `${base} ${label}` : base;
}

/**
 * Long label for a node's `title`, or `null`. It describes the dollar figure shown on the
 * node chip, so it needs both a completed node and a displayed cost: a stale mode left over
 * from an earlier loop iteration must not label a node that is running again.
 */
export function nodeBillingTitle(
  status: string | null | undefined,
  cost: number | null | undefined,
  mode: BillingMode | null | undefined,
): string | null {
  if (status !== 'completed' || cost == null) return null;
  return modeLabel(mode);
}

/**
 * Label for the status bar's total, or `null`. It qualifies a displayed dollar figure, so it
 * is withheld unless that figure is positive; a bar that only says "N unpriced" shows none.
 * `detailed` adds the breakdown (tooltip); the visible chip uses the short form.
 */
export function totalBillingLabel(
  totalCost: number,
  counts: BillingCounts | null | undefined,
  detailed = false,
): string | null {
  if (!(totalCost > 0)) return null;
  return aggregateLabel(counts, detailed);
}
