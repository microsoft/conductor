import { describe, expect, it } from 'vitest';
import { isReconnectStuck, RECONNECT_WARNING_THRESHOLD_MS } from './reconnect';

describe('isReconnectStuck', () => {
  const base = {
    wsStatus: 'reconnecting' as const,
    wsDisconnectedSince: 1_000,
    workflowStatus: 'running' as const,
    replayMode: false,
  };

  it('is false when under the threshold', () => {
    expect(
      isReconnectStuck({ ...base, now: base.wsDisconnectedSince + RECONNECT_WARNING_THRESHOLD_MS - 1 }),
    ).toBe(false);
  });

  it('is true once at or over the threshold', () => {
    expect(
      isReconnectStuck({ ...base, now: base.wsDisconnectedSince + RECONNECT_WARNING_THRESHOLD_MS }),
    ).toBe(true);
    expect(
      isReconnectStuck({ ...base, now: base.wsDisconnectedSince + RECONNECT_WARNING_THRESHOLD_MS + 5_000 }),
    ).toBe(true);
  });

  it('is true regardless of which non-connected wsStatus is currently active (connecting vs reconnecting vs disconnected)', () => {
    const now = base.wsDisconnectedSince + RECONNECT_WARNING_THRESHOLD_MS + 1;
    for (const wsStatus of ['reconnecting', 'connecting', 'disconnected'] as const) {
      expect(isReconnectStuck({ ...base, wsStatus, now })).toBe(true);
    }
  });

  it('is false when connected, even past the threshold', () => {
    const now = base.wsDisconnectedSince + RECONNECT_WARNING_THRESHOLD_MS + 5_000;
    expect(isReconnectStuck({ ...base, wsStatus: 'connected', now })).toBe(false);
  });

  it('is false when wsDisconnectedSince is null (never disconnected, or already reconnected)', () => {
    const now = 10_000_000;
    expect(isReconnectStuck({ ...base, wsDisconnectedSince: null, now })).toBe(false);
  });

  it('is false when the workflow is not running (own banners cover completed/failed)', () => {
    const now = base.wsDisconnectedSince + RECONNECT_WARNING_THRESHOLD_MS + 1;
    for (const workflowStatus of ['pending', 'completed', 'failed'] as const) {
      expect(isReconnectStuck({ ...base, workflowStatus, now })).toBe(false);
    }
  });

  it('is false in replay mode (no live process to have crashed)', () => {
    const now = base.wsDisconnectedSince + RECONNECT_WARNING_THRESHOLD_MS + 1;
    expect(isReconnectStuck({ ...base, replayMode: true, now })).toBe(false);
  });

  it('respects a custom thresholdMs', () => {
    const now = base.wsDisconnectedSince + 5_000;
    expect(isReconnectStuck({ ...base, now, thresholdMs: 10_000 })).toBe(false);
    expect(isReconnectStuck({ ...base, now, thresholdMs: 5_000 })).toBe(true);
  });
});
