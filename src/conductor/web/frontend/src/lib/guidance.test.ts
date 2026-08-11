import { describe, expect, it } from 'vitest';
import { mergeGuidance, type GuidanceEntry } from './guidance';

describe('mergeGuidance', () => {
  it('pushes an unapplied entry on guidance_received', () => {
    const result = mergeGuidance([], {
      type: 'guidance_received',
      data: { text: 'Be concise', pending: 1 },
    });
    expect(result).toEqual([{ text: 'Be concise', applied: false }]);
  });

  it('marks the oldest matching unapplied entry as applied on guidance_applied', () => {
    const entries: GuidanceEntry[] = [{ text: 'Be concise', applied: false }];
    const result = mergeGuidance(entries, {
      type: 'guidance_applied',
      data: { text: 'Be concise', source: 'dashboard', agent_name: 'planner' },
    });
    expect(result).toEqual([
      { text: 'Be concise', applied: true, source: 'dashboard', agentName: 'planner' },
    ]);
  });

  it('does not mutate the input array', () => {
    const entries: GuidanceEntry[] = [{ text: 'Be concise', applied: false }];
    const result = mergeGuidance(entries, {
      type: 'guidance_applied',
      data: { text: 'Be concise', source: 'dashboard' },
    });
    expect(entries[0]!.applied).toBe(false);
    expect(result).not.toBe(entries);
  });

  it('pushes an already-applied entry when there was no matching received (interrupt source)', () => {
    const result = mergeGuidance([], {
      type: 'guidance_applied',
      data: { text: 'Focus on Python 3', source: 'interrupt', agent_name: 'agent_b' },
    });
    expect(result).toEqual([
      { text: 'Focus on Python 3', applied: true, source: 'interrupt', agentName: 'agent_b' },
    ]);
  });

  it('pushes an already-applied entry for the cli (resume --guidance) source', () => {
    const result = mergeGuidance([], {
      type: 'guidance_applied',
      data: { text: 'Skip the benchmark step', source: 'cli' },
    });
    expect(result).toEqual([
      { text: 'Skip the benchmark step', applied: true, source: 'cli', agentName: undefined },
    ]);
  });

  it('matches FIFO order when multiple unapplied entries share no correlation id', () => {
    let entries: GuidanceEntry[] = [];
    entries = mergeGuidance(entries, {
      type: 'guidance_received',
      data: { text: 'first', pending: 1 },
    });
    entries = mergeGuidance(entries, {
      type: 'guidance_received',
      data: { text: 'first', pending: 2 },
    });
    // Two identical texts queued; the first guidance_applied should resolve
    // the earliest (index 0) unapplied entry.
    entries = mergeGuidance(entries, {
      type: 'guidance_applied',
      data: { text: 'first', source: 'dashboard' },
    });
    expect(entries[0]!.applied).toBe(true);
    expect(entries[1]!.applied).toBe(false);
  });

  it('leaves other unapplied entries untouched', () => {
    const entries: GuidanceEntry[] = [
      { text: 'one', applied: false },
      { text: 'two', applied: false },
    ];
    const result = mergeGuidance(entries, {
      type: 'guidance_applied',
      data: { text: 'two', source: 'dashboard' },
    });
    expect(result[0]).toEqual({ text: 'one', applied: false });
    expect(result[1]).toEqual({ text: 'two', applied: true, source: 'dashboard', agentName: undefined });
  });
});
