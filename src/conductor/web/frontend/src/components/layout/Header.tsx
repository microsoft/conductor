import { useState, useEffect } from 'react';
import { Activity, Square, Play, X, Download, FileCode } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { YamlViewer } from '@/components/layout/YamlViewer';

export function Header() {
  const workflowName = useWorkflowStore((s) => s.workflowName);
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const isPaused = useWorkflowStore((s) => s.isPaused);
  const workflowYaml = useWorkflowStore((s) => s.workflowYaml);
  const conductorVersion = useWorkflowStore((s) => s.conductorVersion);
  const [stopping, setStopping] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [killing, setKilling] = useState(false);
  const [showYaml, setShowYaml] = useState(false);

  const isRunning = workflowStatus === 'running' || workflowStatus === 'pending';

  // Reset button states when transitioning out of paused
  useEffect(() => {
    if (!isPaused) {
      setStopping(false);
      setResuming(false);
      setKilling(false);
    }
  }, [isPaused]);

  const handleStop = async () => {
    setStopping(true);
    try {
      await fetch('/api/stop', { method: 'POST' });
    } catch (err) {
      console.error('Failed to stop agent:', err);
      setStopping(false);
    }
  };

  const handleResume = async () => {
    setResuming(true);
    try {
      await fetch('/api/resume', { method: 'POST' });
    } catch (err) {
      console.error('Failed to resume agent:', err);
      setResuming(false);
    }
  };

  const handleKill = async () => {
    setKilling(true);
    try {
      await fetch('/api/kill', { method: 'POST' });
    } catch (err) {
      console.error('Failed to kill workflow:', err);
      setKilling(false);
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
        {isPaused ? (
          <>
            <button
              onClick={handleResume}
              disabled={resuming}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded
                bg-emerald-500/10 text-emerald-400 border border-emerald-500/20
                hover:bg-emerald-500/20 hover:border-emerald-500/30
                disabled:opacity-50 disabled:cursor-not-allowed
                transition-colors"
              title="Re-execute the paused agent"
            >
              <Play className="w-3 h-3" />
              {resuming ? 'Resuming...' : 'Resume'}
            </button>
            <button
              onClick={handleKill}
              disabled={killing}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded
                bg-red-500/10 text-red-400 border border-red-500/20
                hover:bg-red-500/20 hover:border-red-500/30
                disabled:opacity-50 disabled:cursor-not-allowed
                transition-colors"
              title="Stop workflow entirely (checkpoint saved for CLI resume)"
            >
              <X className="w-3 h-3" />
              {killing ? 'Killing...' : 'Kill'}
            </button>
          </>
        ) : isRunning ? (
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
        ) : null}
        {workflowYaml && (
          <button
            onClick={() => setShowYaml(true)}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded
              bg-[var(--surface-hover)] text-[var(--text-secondary)] border border-[var(--border)]
              hover:text-[var(--text)] hover:bg-[var(--surface)]
              transition-colors"
            title="View workflow YAML configuration"
          >
            <FileCode className="w-3 h-3" />
            YAML
          </button>
        )}
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
        <span className="text-xs text-[var(--text-muted)]">v{conductorVersion ?? '—'}</span>
      </div>
      {showYaml && workflowYaml && (
        <YamlViewer yaml={workflowYaml} onClose={() => setShowYaml(false)} />
      )}
    </header>
  );
}
