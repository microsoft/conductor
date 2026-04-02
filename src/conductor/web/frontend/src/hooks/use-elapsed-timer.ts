import { useState, useEffect, useRef } from 'react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { formatElapsed } from '@/lib/utils';

export function useElapsedTimer(): string {
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const startTime = useWorkflowStore((s) => s.workflowStartTime);
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const lastEventTime = useWorkflowStore((s) => s.lastEventTime);
  const [display, setDisplay] = useState('—');
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (startTime == null) return;

    if (replayMode) {
      // In replay mode, compute elapsed from event timestamps, not wall clock
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      const now = lastEventTime ?? startTime;
      setDisplay(formatElapsed(now - startTime));
      return;
    }

    if (workflowStatus === 'running') {
      const tick = () => {
        const elapsed = Date.now() / 1000 - startTime;
        setDisplay(formatElapsed(elapsed));
      };
      tick();
      timerRef.current = setInterval(tick, 500);
      return () => {
        if (timerRef.current) clearInterval(timerRef.current);
      };
    } else if (workflowStatus === 'completed' || workflowStatus === 'failed') {
      // Freeze at final value
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }
  }, [workflowStatus, startTime, replayMode, lastEventTime]);

  return display;
}
