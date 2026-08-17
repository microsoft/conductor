/**
 * Pure logic for tracking accumulated mid-run guidance (issue #400).
 *
 * A `guidance_received` event is the opening half of a pair whose closer is
 * `guidance_applied` — pushed as soon as `POST /api/guidance` accepts a
 * submission, before the engine has actually applied it at the next step
 * boundary or pause. `guidance_applied` marks the oldest still-unapplied
 * entry as applied, matching them up in submission order (FIFO) since the
 * channel has no other correlation id.
 *
 * Not every `guidance_applied` has a preceding `guidance_received` in this
 * run's history: the TTY Esc/Ctrl+G interrupt path, `resume --guidance`, and
 * a Copilot follow-up all call `add_user_guidance` directly without going
 * through the HTTP endpoint. For those, `mergeGuidance` pushes a new,
 * already-applied entry rather than dropping the event.
 */
import type { GuidanceAppliedData, GuidanceReceivedData } from '@/types/events';

export interface GuidanceEntry {
  text: string;
  applied: boolean;
  /** Present only once applied. */
  source?: 'interrupt' | 'dashboard' | 'cli';
  agentName?: string | null;
}

/**
 * Fold a `guidance_received` or `guidance_applied` event into the
 * accumulated guidance list, returning a new array (the store's immutable
 * update convention).
 */
export function mergeGuidance(
  entries: GuidanceEntry[],
  event:
    | { type: 'guidance_received'; data: GuidanceReceivedData }
    | { type: 'guidance_applied'; data: GuidanceAppliedData },
): GuidanceEntry[] {
  if (event.type === 'guidance_received') {
    return [...entries, { text: event.data.text, applied: false }];
  }

  // guidance_applied: mark the oldest matching unapplied entry as applied,
  // or push an already-applied entry when there was no `received` (interrupt
  // / resume --guidance / follow-up sources).
  const idx = entries.findIndex((e) => !e.applied && e.text === event.data.text);
  if (idx === -1) {
    return [
      ...entries,
      {
        text: event.data.text,
        applied: true,
        source: event.data.source,
        agentName: event.data.agent_name,
      },
    ];
  }

  const next = [...entries];
  next[idx] = {
    ...next[idx]!,
    applied: true,
    source: event.data.source,
    agentName: event.data.agent_name,
  };
  return next;
}
