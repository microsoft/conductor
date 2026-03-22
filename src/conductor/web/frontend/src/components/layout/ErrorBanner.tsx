import { useState } from 'react';
import { AlertTriangle, CheckCircle2, X, Eye } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { formatCost, formatTokens, cn } from '@/lib/utils';
import { useElapsedTimer } from '@/hooks/use-elapsed-timer';

export function WorkflowErrorBanner() {
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const workflowFailure = useWorkflowStore((s) => s.workflowFailure);
  const workflowFailedAgent = useWorkflowStore((s) => s.workflowFailedAgent);
  const selectNode = useWorkflowStore((s) => s.selectNode);

  if (workflowStatus !== 'failed' || !workflowFailure) return null;

  const errorText = workflowFailure.message || workflowFailure.error_type || 'Unknown error';
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
          <span className="text-xs font-medium text-red-300">Workflow Failed</span>
          <span className="text-[11px] text-red-400/80 truncate">{errorText}</span>
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
  const totalCost = useWorkflowStore((s) => s.totalCost);
  const totalTokens = useWorkflowStore((s) => s.totalTokens);
  const agentsCompleted = useWorkflowStore((s) => s.agentsCompleted);
  const agentsTotal = useWorkflowStore((s) => s.agentsTotal);
  const elapsed = useElapsedTimer();

  if (workflowStatus !== 'completed' || dismissed) return null;

  return (
    <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 animate-[banner-in_200ms_ease-out]">
      <div
        className={cn(
          'flex items-center gap-3 px-4 py-2 rounded-lg',
          'bg-green-950/90 border border-green-500/40 shadow-lg shadow-green-500/10',
          'backdrop-blur-sm',
        )}
      >
        <CheckCircle2 className="w-4 h-4 text-green-400 flex-shrink-0" />
        <span className="text-xs font-medium text-green-300">Completed</span>
        <div className="flex items-center gap-3 text-[11px] text-green-400/80 font-mono">
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
