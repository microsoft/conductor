import { useState } from 'react';
import { Activity, Square, Download } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';

export function Header() {
  const workflowName = useWorkflowStore((s) => s.workflowName);
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const [stopping, setStopping] = useState(false);

  const isRunning = workflowStatus === 'running' || workflowStatus === 'pending';

  const handleStop = async () => {
    setStopping(true);
    try {
      await fetch('/api/stop', { method: 'POST' });
    } catch {
      // Server may already be shutting down
    }
  };

  return (
    <header className="flex items-center justify-between px-4 py-2 bg-[var(--surface)] border-b border-[var(--border)] flex-shrink-0">
      <div className="flex items-center gap-2">
        <Activity className="w-4 h-4 text-[var(--running)]" />
        <h1 className="text-sm font-semibold text-[var(--text)]">
          Conductor
        </h1>
        {workflowName && (
          <span className="text-sm text-[var(--text-muted)] font-normal">
            — {workflowName}
          </span>
        )}
      </div>
      <div className="flex items-center gap-3">
        {isRunning && (
          <button
            onClick={handleStop}
            disabled={stopping}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded
              bg-red-500/10 text-red-400 border border-red-500/20
              hover:bg-red-500/20 hover:border-red-500/30
              disabled:opacity-50 disabled:cursor-not-allowed
              transition-colors"
          >
            <Square className="w-3 h-3" />
            {stopping ? 'Stopping...' : 'Stop'}
          </button>
        )}
        {!isRunning && (
          <a
            href="/api/logs"
            download="conductor-logs.json"
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded
              bg-[var(--surface-hover)] text-[var(--text-secondary)] border border-[var(--border)]
              hover:text-[var(--text)] hover:bg-[var(--surface)]
              transition-colors"
            title="Download full event log as JSON"
          >
            <Download className="w-3 h-3" />
            Logs
          </a>
        )}
        <span className="text-xs text-[var(--text-muted)]">Dashboard v1.0</span>
      </div>
    </header>
  );
}
