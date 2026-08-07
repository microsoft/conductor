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
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const [stopping, setStopping] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [killing, setKilling] = useState(false);
  const [showYaml, setShowYaml] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);

  // `ReplayDashboard` serves no /api/stop|resume|kill, so these controls
  // would 404 against a recorded log.
  const isRunning = !replayMode && (workflowStatus === 'running' || workflowStatus === 'pending');
  const showPauseControls = !replayMode && isPaused;

  // Reset button states when transitioning out of paused
  useEffect(() => {
    if (!isPaused) {
      setStopping(false);
      setResuming(false);
      setKilling(false);
    }
  }, [isPaused]);

  // `fetch` resolves on 4xx/5xx, so an unchecked response leaves the button
  // latched in its disabled pending state with no console entry and no way
  // back except a page reload. Check the status explicitly.
  const postControl = async (
    path: string,
    action: string,
    setPending: (v: boolean) => void
  ) => {
    setPending(true);
    setControlError(null);
    try {
      const res = await fetch(path, { method: 'POST' });
      if (!res.ok) {
        console.error(`Failed to ${action}: HTTP ${res.status} from ${path}`);
        setControlError(`Could not ${action} — server returned HTTP ${res.status}.`);
        setPending(false);
      }
    } catch (err) {
      console.error(`Failed to ${action}:`, err);
      setControlError(`Could not ${action} — the dashboard is unreachable.`);
      setPending(false);
    }
  };

  const handleStop = () => postControl('/api/stop', 'stop the agent', setStopping);
  const handleResume = () => postControl('/api/resume', 'resume the agent', setResuming);
  const handleKill = () => postControl('/api/kill', 'kill the workflow', setKilling);

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
        {controlError && (
          <span className="text-xs text-red-400" role="alert">
            {controlError}
          </span>
        )}
        {showPauseControls ? (
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
              title="Stop the workflow and save a checkpoint for CLI resume"
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
            title="Pause the current agent, then choose Resume or Kill"
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
