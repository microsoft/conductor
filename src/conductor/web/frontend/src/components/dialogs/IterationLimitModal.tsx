import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Play, StopCircle } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { IterationLimitResponseTarget } from '@/types/events';

const DEFAULT_ADDITIONAL_ITERATIONS = 10;

/**
 * Modal shown when the engine emits ``iteration_limit_reached`` and the
 * workflow is awaiting a decision from the dashboard.
 *
 * Issue #198: in ``--web-bg`` (and ``--web``) the engine has no CLI prompt
 * to fall through to, so this modal is the only way the user can resolve
 * the gate without killing the process. The modal is intentionally
 * non-dismissable (no close button, no Escape, no click-outside-to-close)
 * because accidentally dismissing it would leave the workflow paused with
 * no way to resume — the user must explicitly continue or stop.
 *
 * Hidden automatically when ``iterationLimitGate`` clears (via the
 * ``iteration_limit_resolved`` event handler in the store) or when
 * ``skip_gates`` is set (engine will auto-stop without input).
 */
export function IterationLimitModal() {
  const gate = useWorkflowStore((s) => s.iterationLimitGate);
  const wsStatus = useWorkflowStore((s) => s.wsStatus);
  const sendIterationLimitResponse = useWorkflowStore(
    (s) => s.sendIterationLimitResponse,
  );

  const [additionalIterations, setAdditionalIterations] = useState<string>(
    String(DEFAULT_ADDITIONAL_ITERATIONS),
  );
  const [submitted, setSubmitted] = useState(false);

  // Reset local state whenever a new gate fires (e.g. a second limit later
  // in the same run). Keying off gate_id keeps stale input from leaking
  // between consecutive gates.
  useEffect(() => {
    if (gate?.gate_id) {
      setAdditionalIterations(String(DEFAULT_ADDITIONAL_ITERATIONS));
      setSubmitted(false);
    }
  }, [gate?.gate_id]);

  const parsedAdditional = useMemo(() => {
    const n = Number(additionalIterations);
    if (!Number.isFinite(n)) return null;
    if (n < 0) return null;
    return Math.floor(n);
  }, [additionalIterations]);

  // The modal is opt-in for visible gates. When skip_gates is true the
  // engine will auto-stop without input — showing a modal there would
  // give the user a button to click that doesn't actually do anything.
  if (!gate || gate.skip_gates) return null;

  const target = gate.agent_name ?? gate.group_name ?? 'workflow';
  const canInteract = wsStatus === 'connected' && !submitted;
  const continueDisabled =
    !canInteract || parsedAdditional == null || parsedAdditional <= 0;

  const buildTarget = (): IterationLimitResponseTarget =>
    gate.agent_name !== undefined
      ? { agent_name: gate.agent_name }
      : { group_name: gate.group_name };

  const handleContinue = () => {
    if (continueDisabled || parsedAdditional == null) return;
    setSubmitted(true);
    sendIterationLimitResponse(buildTarget(), gate.gate_id, parsedAdditional);
  };

  const handleStop = () => {
    if (!canInteract) return;
    setSubmitted(true);
    sendIterationLimitResponse(buildTarget(), gate.gate_id, 0);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleContinue();
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="iteration-limit-title"
      data-testid="iteration-limit-modal"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
    >
      <div className="relative flex flex-col w-[90vw] max-w-md rounded-xl border border-amber-500/40 bg-[var(--surface)] shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-2.5 px-4 py-3 border-b border-[var(--border)] bg-amber-500/10">
          <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0" />
          <h2
            id="iteration-limit-title"
            className="text-sm font-semibold text-[var(--text)]"
          >
            Max iterations reached
          </h2>
        </div>

        {/* Body */}
        <div className="px-4 py-4 space-y-3">
          <p className="text-xs text-[var(--text)]">
            <span className="font-semibold">{target}</span> reached{' '}
            <span className="tabular-nums">
              {gate.current_iteration}/{gate.max_iterations}
            </span>{' '}
            iterations.
          </p>

          {gate.possible_loop && (
            <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-amber-500/5 border border-amber-500/30">
              <AlertTriangle className="w-3.5 h-3.5 text-amber-400 flex-shrink-0" />
              <span className="text-[11px] text-amber-300">
                The same agent has run repeatedly — this may indicate a loop.
              </span>
            </div>
          )}

          {gate.agent_history.length > 0 && (
            <div className="space-y-1">
              <h3 className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold">
                Recent agents
              </h3>
              <ol className="text-[11px] text-[var(--text-muted)] list-decimal list-inside space-y-0.5">
                {gate.agent_history.map((name, i) => (
                  <li key={`${i}-${name}`}>{name}</li>
                ))}
              </ol>
            </div>
          )}

          <div className="space-y-1.5">
            <label
              htmlFor="iteration-limit-additional"
              className="block text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold"
            >
              Additional iterations
            </label>
            <input
              id="iteration-limit-additional"
              data-testid="iteration-limit-input"
              type="number"
              min={0}
              step={1}
              value={additionalIterations}
              onChange={(e) => setAdditionalIterations(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={!canInteract}
              autoFocus
              className="w-full text-xs px-3 py-2 rounded-lg border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] outline-none focus:border-amber-400 transition-colors disabled:opacity-50"
            />
            <p className="text-[10px] text-[var(--text-muted)]">
              Enter a positive number to continue, or press Stop to end the
              workflow.
            </p>
          </div>

          {wsStatus !== 'connected' && (
            <div className="text-[11px] text-red-300">
              Disconnected from server — reconnect to resolve this gate.
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-[var(--border)] bg-[var(--surface-raised)]">
          <button
            type="button"
            data-testid="iteration-limit-stop"
            onClick={handleStop}
            disabled={!canInteract}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border border-[var(--border)] text-[var(--text)] hover:bg-[var(--surface-hover)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <StopCircle className="w-3.5 h-3.5" />
            Stop
          </button>
          <button
            type="button"
            data-testid="iteration-limit-continue"
            onClick={handleContinue}
            disabled={continueDisabled}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-amber-500 text-white hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors font-medium"
          >
            <Play className="w-3.5 h-3.5" />
            Continue
          </button>
        </div>
      </div>
    </div>
  );
}
