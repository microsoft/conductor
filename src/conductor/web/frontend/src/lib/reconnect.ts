/**
 * Pure logic for detecting a dashboard WebSocket that's been failing to
 * reconnect for "too long" (issue #330).
 *
 * `wsStatus` oscillates between `'reconnecting'` (waiting out backoff) and
 * `'connecting'` (attempting) on every retry cycle — it never sits
 * continuously in `'reconnecting'`. So rather than timing the raw status,
 * the store tracks `wsDisconnectedSince`: a timestamp set on the *first*
 * drop from `'connected'` and preserved across the connecting/reconnecting
 * churn until the socket is `'connected'` again (see
 * `workflow-store.ts`'s `setWsStatus`). This module just compares that
 * timestamp against a threshold.
 */
import type { WorkflowStatus, WsStatus } from '@/stores/workflow-store';

/** How long the connection must have been down before we warn the user. */
export const RECONNECT_WARNING_THRESHOLD_MS = 60_000;

export interface ReconnectStuckInput {
  wsStatus: WsStatus;
  wsDisconnectedSince: number | null;
  workflowStatus: WorkflowStatus;
  replayMode: boolean;
  /** Current time in ms (injectable for testing). Defaults to `Date.now()`. */
  now?: number;
  /** Threshold in ms (injectable for testing). Defaults to {@link RECONNECT_WARNING_THRESHOLD_MS}. */
  thresholdMs?: number;
}

/**
 * Returns true when the dashboard has plausibly lost its connection to a
 * still-"running" workflow for longer than the threshold, and the user
 * should be warned that the workflow may have silently failed.
 *
 * Deliberately does *not* fire when:
 * - already connected, or never yet disconnected (`wsDisconnectedSince == null`)
 * - the workflow already reached a terminal status (`completed`/`failed`) —
 *   those cases have their own dedicated banners
 * - viewing a replay (there is no live process to have crashed)
 */
export function isReconnectStuck({
  wsStatus,
  wsDisconnectedSince,
  workflowStatus,
  replayMode,
  now = Date.now(),
  thresholdMs = RECONNECT_WARNING_THRESHOLD_MS,
}: ReconnectStuckInput): boolean {
  if (replayMode) return false;
  if (workflowStatus !== 'running') return false;
  if (wsStatus === 'connected') return false;
  if (wsDisconnectedSince == null) return false;
  return now - wsDisconnectedSince >= thresholdMs;
}
