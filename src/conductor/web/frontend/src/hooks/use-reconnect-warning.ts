import { useEffect, useState } from 'react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { isReconnectStuck, RECONNECT_WARNING_THRESHOLD_MS } from '@/lib/reconnect';

export interface ReconnectWarning {
  /** True once the dashboard has been unable to reconnect for too long (#330). */
  stuck: boolean;
  /** How long the connection has been down, in ms (0 when not disconnected). */
  elapsedMs: number;
}

/**
 * Ticks once a second (mirroring `StatusBar`'s `idleSeconds` pattern) to
 * detect when the dashboard has been failing to reconnect for longer than
 * {@link RECONNECT_WARNING_THRESHOLD_MS}, so a warning banner can tell the
 * user the workflow may have silently died — see issue #330 and
 * `lib/reconnect.ts` for why this is driven by `wsDisconnectedSince` rather
 * than the raw (oscillating) `wsStatus`.
 */
export function useReconnectWarning(): ReconnectWarning {
  const wsStatus = useWorkflowStore((s) => s.wsStatus);
  const wsDisconnectedSince = useWorkflowStore((s) => s.wsDisconnectedSince);
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const replayMode = useWorkflowStore((s) => s.replayMode);

  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (wsDisconnectedSince == null || wsStatus === 'connected') return;
    const tick = () => setNow(Date.now());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [wsDisconnectedSince, wsStatus]);

  const stuck = isReconnectStuck({ wsStatus, wsDisconnectedSince, workflowStatus, replayMode, now });
  const elapsedMs = wsDisconnectedSince == null ? 0 : Math.max(0, now - wsDisconnectedSince);

  return { stuck, elapsedMs };
}
