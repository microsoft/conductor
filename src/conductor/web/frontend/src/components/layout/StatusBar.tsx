import { useState, useEffect } from 'react';
import { Wifi, WifiOff, Loader2, Coins, Hash, Clock } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useElapsedTimer } from '@/hooks/use-elapsed-timer';
import { cn } from '@/lib/utils';

export function StatusBar() {
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const agentsCompleted = useWorkflowStore((s) => s.agentsCompleted);
  const agentsTotal = useWorkflowStore((s) => s.agentsTotal);
  const totalCost = useWorkflowStore((s) => s.totalCost);
  const totalTokens = useWorkflowStore((s) => s.totalTokens);
  const wsStatus = useWorkflowStore((s) => s.wsStatus);
  const workflowFailure = useWorkflowStore((s) => s.workflowFailure);
  const lastEventTime = useWorkflowStore((s) => s.lastEventTime);
  const elapsed = useElapsedTimer();

  // "Last activity X ago" — ticks every second while running
  const [idleSeconds, setIdleSeconds] = useState<number | null>(null);
  useEffect(() => {
    if (workflowStatus !== 'running' || lastEventTime == null) {
      setIdleSeconds(null);
      return;
    }
    const tick = () => {
      setIdleSeconds(Math.floor(Date.now() / 1000 - lastEventTime));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [workflowStatus, lastEventTime]);

  const isFailed = workflowStatus === 'failed';

  const statusText = (() => {
    switch (workflowStatus) {
      case 'pending':
        return 'Waiting for workflow\u2026';
      case 'running':
        return 'Running';
      case 'completed':
        return 'Completed';
      case 'failed': {
        if (!workflowFailure) return 'Failed';
        const et = workflowFailure.error_type || '';
        if (et === 'MaxIterationsError') return 'Failed: exceeded maximum iterations';
        if (et === 'TimeoutError') return 'Failed: workflow timed out';
        if (workflowFailure.message) {
          const msg = workflowFailure.message.length > 60
            ? workflowFailure.message.slice(0, 57) + '...'
            : workflowFailure.message;
          return `Failed: ${msg}`;
        }
        return `Failed: ${et}`;
      }
    }
  })();

  const statusDotColor = {
    pending: 'bg-[var(--pending)]',
    running: 'bg-[var(--running)] animate-pulse',
    completed: 'bg-[var(--completed)]',
    failed: 'bg-[var(--failed)]',
  }[workflowStatus];

  const wsIndicator = (() => {
    switch (wsStatus) {
      case 'connected':
        return (
          <span className="flex items-center gap-1 text-[var(--completed)]">
            <Wifi className="w-3 h-3" />
            <span>Connected</span>
          </span>
        );
      case 'disconnected':
        return (
          <span className="flex items-center gap-1 text-[var(--failed)]">
            <WifiOff className="w-3 h-3" />
            <span>Disconnected</span>
          </span>
        );
      case 'reconnecting':
        return (
          <span className="flex items-center gap-1 text-[var(--waiting)]">
            <Loader2 className="w-3 h-3 animate-spin" />
            <span>Reconnecting\u2026</span>
          </span>
        );
      case 'connecting':
        return (
          <span className="flex items-center gap-1 text-[var(--text-muted)]">
            <Loader2 className="w-3 h-3 animate-spin" />
            <span>Connecting\u2026</span>
          </span>
        );
    }
  })();

  return (
    <footer
      className={cn(
        'flex items-center gap-4 px-4 py-1.5 border-t text-xs flex-shrink-0 transition-colors duration-300',
        isFailed
          ? 'bg-red-950/50 border-red-500/30'
          : 'bg-[var(--surface)] border-[var(--border)]',
      )}
    >
      <span className={cn('w-2 h-2 rounded-full flex-shrink-0', statusDotColor)} />
      <span className={cn(isFailed ? 'text-red-300' : 'text-[var(--text)]')}>
        {statusText}
      </span>
      {agentsTotal > 0 && (
        <span className={cn(isFailed ? 'text-red-400/60' : 'text-[var(--text-muted)]')}>
          {agentsCompleted}/{agentsTotal} agents
        </span>
      )}
      {workflowStatus !== 'pending' && (
        <span className={cn('font-mono', isFailed ? 'text-red-400/60' : 'text-[var(--text-muted)]')}>
          {elapsed}
        </span>
      )}
      {totalTokens > 0 && (
        <span className={cn('flex items-center gap-1', isFailed ? 'text-red-400/60' : 'text-[var(--text-muted)]')} title="Total tokens used">
          <Hash className="w-3 h-3" />
          <span className="font-mono">{totalTokens.toLocaleString()}</span>
        </span>
      )}
      {totalCost > 0 && (
        <span className={cn('flex items-center gap-1', isFailed ? 'text-red-400/60' : 'text-[var(--text-muted)]')} title="Total cost">
          <Coins className="w-3 h-3" />
          <span className="font-mono">${totalCost.toFixed(4)}</span>
        </span>
      )}
      {idleSeconds != null && idleSeconds >= 5 && (
        <span
          className={cn(
            'flex items-center gap-1 font-mono',
            idleSeconds >= 60 ? 'text-amber-400' : 'text-[var(--text-muted)]',
          )}
          title="Time since last event from the provider"
        >
          <Clock className="w-3 h-3" />
          <span>
            {idleSeconds >= 60
              ? `${Math.floor(idleSeconds / 60)}m ${idleSeconds % 60}s idle`
              : `${idleSeconds}s idle`}
          </span>
        </span>
      )}
      <span className="flex-1" />
      {wsIndicator}
    </footer>
  );
}
