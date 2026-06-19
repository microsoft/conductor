import { useState } from 'react';
import { AlertTriangle, CheckCircle2, X, Eye, Square } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { formatCost, formatTokens, cn } from '@/lib/utils';
import { useElapsedTimer } from '@/hooks/use-elapsed-timer';

export function WorkflowErrorBanner() {
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const workflowFailure = useWorkflowStore((s) => s.workflowFailure);
  const workflowFailedAgent = useWorkflowStore((s) => s.workflowFailedAgent);
  const workflowTermination = useWorkflowStore((s) => s.workflowTermination);
  const selectNode = useWorkflowStore((s) => s.selectNode);

  if (workflowStatus !== 'failed' || !workflowFailure) return null;

  // Issue #245: a user-initiated Stop/Kill from the dashboard is an
  // intentional outcome, not a crash. Render a calm "Workflow Stopped" banner
  // that surfaces the checkpoint outcome (saved path, or why none was written)
  // instead of the alarming red "Workflow Failed" banner.
  if (workflowFailure.stopped_by_user) {
    const checkpointName = workflowFailure.checkpoint_path?.split('/').pop();
    return (
      <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 animate-[banner-in_200ms_ease-out]">
        <div
          className={cn(
            'flex items-center gap-2 px-4 py-2 rounded-lg',
            'bg-slate-900/90 border border-slate-500/40 shadow-lg shadow-slate-500/10',
            'backdrop-blur-sm max-w-[560px]',
          )}
        >
          <Square className="w-4 h-4 text-slate-300 flex-shrink-0" />
          <div className="flex flex-col min-w-0">
            <span className="text-xs font-medium text-slate-200">Workflow Stopped</span>
            {workflowFailure.checkpoint_path ? (
              <>
                <span
                  className="text-[11px] text-slate-300/80 truncate"
                  title={workflowFailure.checkpoint_path}
                >
                  Checkpoint saved: {checkpointName}
                </span>
                <span className="text-[10px] text-slate-400/70 truncate">
                  Resume from the CLI with: conductor resume
                </span>
              </>
            ) : workflowFailure.checkpoint_unavailable_reason ? (
              <span
                className="text-[11px] text-amber-300/80 truncate"
                title={workflowFailure.checkpoint_unavailable_reason}
              >
                No checkpoint could be saved — {workflowFailure.checkpoint_unavailable_reason}
              </span>
            ) : (
              // Checkpoint outcome not known yet (pause→Kill emits the path via a
              // later checkpoint_saved event). Show a neutral pending line rather
              // than flashing "No checkpoint could be saved" before it arrives.
              <span className="text-[11px] text-slate-400/70 truncate">Saving checkpoint…</span>
            )}
          </div>
          {workflowFailedAgent && (
            <button
              onClick={() => selectNode(workflowFailedAgent)}
              className="flex items-center gap-1 px-2 py-1 rounded text-[10px] font-medium text-slate-200 bg-slate-500/20 hover:bg-slate-500/30 transition-colors flex-shrink-0 ml-1"
            >
              <Eye className="w-3 h-3" />
              View
            </button>
          )}
        </div>
      </div>
    );
  }

  // Issue #219: explicit `type: terminate status: failed` deserves a distinct
  // banner — it is an intentional outcome, not a crash. Use the rendered
  // termination_reason rather than the generic message and mention the step
  // that fired so CI/dashboard consumers can tell them apart at a glance.
  const isExplicit = workflowTermination?.is_explicit && workflowTermination.status === 'failed';
  const errorText = isExplicit
    ? workflowTermination!.termination_reason || workflowFailure.message || 'Workflow terminated'
    : workflowFailure.message || workflowFailure.error_type || 'Unknown error';
  const titleText = isExplicit ? 'Workflow Terminated' : 'Workflow Failed';
  const isTimeout = workflowFailure.error_type === 'TimeoutError';

  return (
    <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 animate-[banner-in_200ms_ease-out]">
      <div
        className={cn(
          'flex items-center gap-2 px-4 py-2 rounded-lg',
          'bg-red-950/90 border border-red-500/40 shadow-lg shadow-red-500/10',
          'backdrop-blur-sm max-w-[560px]',
        )}
      >
        <AlertTriangle className="w-4 h-4 text-red-400 flex-shrink-0" />
        <div className="flex flex-col min-w-0">
          <span className="text-xs font-medium text-red-300">{titleText}</span>
          <span className="text-[11px] text-red-400/80 truncate">{errorText}</span>
          {isExplicit && workflowTermination?.terminated_by && (
            <span className="text-[10px] text-red-400/60 truncate">
              Terminated by: {workflowTermination.terminated_by}
            </span>
          )}
          {isTimeout && workflowFailure.current_agent && (
            <span className="text-[10px] text-red-400/60 truncate">
              Timed out on agent: {workflowFailure.current_agent}
            </span>
          )}
          {workflowFailure.checkpoint_path && (
            <span className="text-[10px] text-red-400/50 truncate" title={workflowFailure.checkpoint_path}>
              Checkpoint: {workflowFailure.checkpoint_path.split('/').pop()}
            </span>
          )}
        </div>
        {workflowFailedAgent && (
          <button
            onClick={() => selectNode(workflowFailedAgent)}
            className="flex items-center gap-1 px-2 py-1 rounded text-[10px] font-medium text-red-300 bg-red-500/20 hover:bg-red-500/30 transition-colors flex-shrink-0 ml-1"
          >
            <Eye className="w-3 h-3" />
            View
          </button>
        )}
      </div>
    </div>
  );
}

export function WorkflowSuccessBanner() {
  const [dismissed, setDismissed] = useState(false);
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const workflowTermination = useWorkflowStore((s) => s.workflowTermination);
  const totalCost = useWorkflowStore((s) => s.totalCost);
  const totalTokens = useWorkflowStore((s) => s.totalTokens);
  const agentsCompleted = useWorkflowStore((s) => s.agentsCompleted);
  const agentsTotal = useWorkflowStore((s) => s.agentsTotal);
  const elapsed = useElapsedTimer();

  if (workflowStatus !== 'completed' || dismissed) return null;

  // Issue #219: when the workflow ended via `type: terminate status: success`,
  // surface the structured reason and terminate step name so the success
  // banner clearly distinguishes "early exit by author intent" from "the last
  // agent finished and routed to $end".
  const isExplicit =
    workflowTermination?.is_explicit && workflowTermination.status === 'success';

  return (
    <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 animate-[banner-in_200ms_ease-out]">
      <div
        className={cn(
          'flex items-center gap-3 px-4 py-2 rounded-lg',
          'bg-green-950/90 border border-green-500/40 shadow-lg shadow-green-500/10',
          'backdrop-blur-sm max-w-[560px]',
        )}
      >
        <CheckCircle2 className="w-4 h-4 text-green-400 flex-shrink-0" />
        <div className="flex flex-col min-w-0">
          <span className="text-xs font-medium text-green-300">
            {isExplicit ? 'Workflow Terminated' : 'Completed'}
          </span>
          {isExplicit && workflowTermination?.termination_reason && (
            <span className="text-[11px] text-green-400/80 truncate">
              {workflowTermination.termination_reason}
            </span>
          )}
          {isExplicit && workflowTermination?.terminated_by && (
            <span className="text-[10px] text-green-400/60 truncate">
              Terminated by: {workflowTermination.terminated_by}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-[11px] text-green-400/80 font-mono flex-shrink-0 ml-auto">
          <span>{elapsed}</span>
          {agentsTotal > 0 && (
            <span>{agentsCompleted}/{agentsTotal} agents</span>
          )}
          {totalTokens > 0 && (
            <span>{formatTokens(totalTokens)} tok</span>
          )}
          {totalCost > 0 && (
            <span>{formatCost(totalCost)}</span>
          )}
        </div>
        <button
          onClick={() => setDismissed(true)}
          className="p-0.5 rounded text-green-500/60 hover:text-green-300 transition-colors flex-shrink-0 ml-1"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  );
}
