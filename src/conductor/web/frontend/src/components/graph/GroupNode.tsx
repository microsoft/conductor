import { memo, useEffect, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { GitBranch, Repeat } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const GroupNode = memo(function GroupNode({ data, id, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const isForEach = nodeData.type === 'for_each_group';
  const Icon = isForEach ? Repeat : GitBranch;
  const progress = nodeData.progress;

  const storeStatus = useWorkflowStore((s) => s.getViewedContext().nodes[id]?.status);
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  // Status transition animation
  const transitionClass = useStatusTransition(status);

  const progressText = progress
    ? `${progress.completed + progress.failed}/${progress.total}${progress.failed > 0 ? ` (${progress.failed} failed)` : ''}`
    : null;

  const progressPct =
    progress && progress.total > 0
      ? ((progress.completed + progress.failed) / progress.total) * 100
      : 0;

  const hasFailures = progress != null && progress.failed > 0;

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
