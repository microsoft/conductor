import { Activity } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';

export function Header() {
  const workflowName = useWorkflowStore((s) => s.workflowName);

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
      <span className="text-xs text-[var(--text-muted)]">Dashboard v1.0</span>
    </header>
  );
}
