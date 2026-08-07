import { useEffect, useRef } from 'react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { WorkflowEvent } from '@/types/events';

export function useReplay() {
  const setReplayMode = useWorkflowStore((s) => s.setReplayMode);
  const markReplayMode = useWorkflowStore((s) => s.markReplayMode);
  const setWsStatus = useWorkflowStore((s) => s.setWsStatus);
  const replayPlaying = useWorkflowStore((s) => s.replayPlaying);
  const replayPosition = useWorkflowStore((s) => s.replayPosition);
  const replayTotalEvents = useWorkflowStore((s) => s.replayTotalEvents);
  const replaySpeed = useWorkflowStore((s) => s.replaySpeed);
  const replayEvents = useWorkflowStore((s) => s.replayEvents);
  const setReplayPosition = useWorkflowStore((s) => s.setReplayPosition);

  // Load events on mount
  useEffect(() => {
    // This hook only mounts once App's /api/replay/info probe confirmed a
    // recorded-log server, so that — not the /api/state result — is the
    // authoritative signal. Latching here keeps Header's live controls hidden
    // even if the event fetch is slow or fails outright.
    markReplayMode();
    setWsStatus('connecting');
    fetch('/api/state')
      .then((r) => {
        // `fetch` resolves on 4xx/5xx; without this an HTML error page would
        // surface as an opaque JSON parse error instead of the real status.
        if (!r.ok) throw new Error(`HTTP ${r.status} from /api/state`);
        return r.json();
      })
      .then((events: WorkflowEvent[]) => {
        setReplayMode(events);
        setWsStatus('connected');
      })
      .catch((err) => {
        console.error('Failed to load replay events:', err);
        setWsStatus('disconnected');
      });
  }, [setReplayMode, markReplayMode, setWsStatus]);

  // Auto-play timer
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!replayPlaying || replayPosition >= replayTotalEvents) {
      if (timerRef.current) clearTimeout(timerRef.current);
      // Auto-pause at end
      if (replayPlaying && replayPosition >= replayTotalEvents) {
        useWorkflowStore.getState().setReplayPlaying(false);
      }
      return;
    }

    // Calculate delay between events based on timestamps and speed
    const currentEvent = replayEvents[replayPosition - 1];
    const nextEvent = replayEvents[replayPosition];
    let delay = 100;
    if (currentEvent && nextEvent) {
      const timeDiff = (nextEvent.timestamp - currentEvent.timestamp) * 1000;
      delay = Math.max(16, Math.min(timeDiff / replaySpeed, 2000));
    }

    timerRef.current = setTimeout(() => {
      setReplayPosition(replayPosition + 1);
    }, delay);

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [replayPlaying, replayPosition, replayTotalEvents, replaySpeed, replayEvents, setReplayPosition]);
}
