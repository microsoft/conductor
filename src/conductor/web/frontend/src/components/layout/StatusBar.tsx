import { Wifi, WifiOff, Loader2 } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useElapsedTimer } from '@/hooks/use-elapsed-timer';
import { cn } from '@/lib/utils';

export function StatusBar() {
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const agentsCompleted = useWorkflowStore((s) => s.agentsCompleted);
  const agentsTotal = useWorkflowStore((s) => s.agentsTotal);
  const wsStatus = useWorkflowStore((s) => s.wsStatus);
  const workflowFailure = useWorkflowStore((s) => s.workflowFailure);
  const elapsed = useElapsedTimer();

  const statusText = (() => {
    switch (workflowStatus) {
      case 'pending':
        return 'Waiting for workflow…';
      case 'running':
        return 'Running';
      case 'completed':
        return 'Completed';
      case 'failed': {
        if (!workflowFailure) return 'Failed';
        const et = workflowFailure.error_type || '';
        if (et === 'MaxIterationsError') return 'Failed: exceeded maximum iterations';
        if (et === 'TimeoutError') return 'Failed: workflow timed out';
        if (workflowFailure.message) return `Failed: ${workflowFailure.message}`;
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
            <span>Reconnecting…</span>
          </span>
        );
      case 'connecting':
        return (
          <span className="flex items-center gap-1 text-[var(--text-muted)]">
            <Loader2 className="w-3 h-3 animate-spin" />
            <span>Connecting…</span>
          </span>
        );
    }
  })();

  return (
    <footer className="flex items-center gap-4 px-4 py-1.5 bg-[var(--surface)] border-t border-[var(--border)] text-xs flex-shrink-0">
      <span className={cn('w-2 h-2 rounded-full flex-shrink-0', statusDotColor)} />
      <span className="text-[var(--text)]">{statusText}</span>
      {agentsTotal > 0 && (
        <span className="text-[var(--text-muted)]">
          {agentsCompleted}/{agentsTotal} agents
        </span>
      )}
      {workflowStatus !== 'pending' && (
        <span className="text-[var(--text-muted)] font-mono">{elapsed}</span>
      )}
      <span className="flex-1" />
      {wsIndicator}
    </footer>
  );
}
