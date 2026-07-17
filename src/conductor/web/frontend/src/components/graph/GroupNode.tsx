import { memo, useEffect, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { GitBranch, Repeat, ChevronRight, ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useNodeLiveData } from '@/hooks/use-viewed-context';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const GroupNode = memo(function GroupNode({ data, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const isForEach = nodeData.type === 'for_each_group';
  const Icon = isForEach ? Repeat : GitBranch;
  const progress = nodeData.progress;

  const nd = useNodeLiveData(nodeData);
  const storeStatus = nd?.status;
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  // Status transition animation
  const transitionClass = useStatusTransition(status);

  const toggleContextExpanded = useWorkflowStore((s) => s.toggleContextExpanded);
  const expanded = nodeData.expanded === true;
  const canExpand = nodeData.canExpand === true;
  const groupExpansionKey = nodeData.groupExpansionKey as string | undefined;

  const onToggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (groupExpansionKey != null) toggleContextExpanded(groupExpansionKey);
  };

  const progressText = progress
    ? `${progress.completed + progress.failed}/${progress.total}${progress.failed > 0 ? ` (${progress.failed} failed)` : ''}`
    : null;

  const progressPct =
    progress && progress.total > 0
      ? ((progress.completed + progress.failed) / progress.total) * 100
      : 0;

  const hasFailures = progress != null && progress.failed > 0;

  // Expanded (for_each-of-workflow): a titled container. Iteration members are
  // drawn as nested React Flow nodes positioned inside this node's bounds.
  if (expanded) {
    return (
      <>
        <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
        <div
          className={cn(
            'w-full h-full rounded-xl border-2 border-dashed bg-[var(--surface)]/40 transition-all duration-300 animate-[subflow-expand-in_200ms_ease-out]',
            selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
            status === 'running' && 'shadow-[0_0_16px_var(--running-glow)]',
            transitionClass,
          )}
          style={{ borderColor, minHeight: '100%' }}
        >
          <div className="flex items-center gap-1.5 px-3 py-2">
            <button
              onClick={onToggle}
              className="nodrag flex items-center justify-center w-5 h-5 rounded hover:bg-[var(--surface-hover)] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors flex-shrink-0"
              title="Collapse for-each iterations"
            >
              <ChevronDown className="w-3.5 h-3.5" />
            </button>
            <Icon className="w-3.5 h-3.5 flex-shrink-0" style={{ color: borderColor }} />
            <span className="text-xs font-semibold text-[var(--text)] truncate">{nodeData.label}</span>
            {progressText && (
              <span className="text-[10px] text-[var(--text-muted)] font-mono flex-shrink-0">{progressText}</span>
            )}
          </div>
        </div>
        <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      </>
    );
  }

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <div
        className={cn(
          'flex flex-col gap-1 px-4 py-3 rounded-xl border-2 border-dashed bg-[var(--surface)]/80 min-w-[180px] transition-all duration-300',
          selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
          status === 'running' && 'shadow-[0_0_16px_var(--running-glow)]',
          transitionClass,
        )}
        style={{ borderColor, minHeight: '100%' }}
      >
        <div className="flex items-center gap-2">
          {/* Expand chevron for a for_each-of-workflow group (only once its
              iterations exist). Keeps the same left position as the collapse
              chevron in the expanded header so it doesn't jump on toggle. */}
          {canExpand && (
            <button
              onClick={onToggle}
              className="nodrag flex items-center justify-center w-5 h-5 rounded hover:bg-[var(--surface-hover)] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors flex-shrink-0 -ml-1"
              title="Expand for-each iterations inline"
            >
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          )}
          <Icon className="w-3.5 h-3.5" style={{ color: borderColor }} />
          <span className="text-xs font-medium text-[var(--text-secondary)]">{nodeData.label}</span>
        </div>
        {progressText && (
          <span className="text-[10px] text-[var(--text-muted)] font-mono">{progressText}</span>
        )}
        {/* Inline progress bar */}
        {progress && progress.total > 0 && status === 'running' && (
          <div className="w-full h-1 rounded-full bg-[var(--border)] overflow-hidden mt-0.5">
            <div
              className="h-full rounded-full transition-all duration-500 ease-out"
              style={{
                width: `${progressPct}%`,
                backgroundColor: hasFailures ? 'var(--failed)' : 'var(--completed)',
              }}
            />
          </div>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});

function useStatusTransition(status: NodeStatus): string {
  const prevStatusRef = useRef<NodeStatus>(status);
  const [transitionClass, setTransitionClass] = useState('');

  useEffect(() => {
    const prev = prevStatusRef.current;
    prevStatusRef.current = status;
    if (prev === status) return;

    if (status === 'running') {
      setTransitionClass('node-activate');
    } else if (prev === 'running' && (status === 'completed' || status === 'failed')) {
      setTransitionClass(status === 'completed' ? 'node-complete' : 'node-fail');
    }

    const timer = setTimeout(() => setTransitionClass(''), 400);
    return () => clearTimeout(timer);
  }, [status]);

  return transitionClass;
}
