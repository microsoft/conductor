import { memo } from 'react';
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

  // Subscribe to store status directly so we always reflect the latest state
  const storeStatus = useWorkflowStore((s) => s.nodes[id]?.status);
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const progressText = progress
    ? `${progress.completed + progress.failed}/${progress.total}${progress.failed > 0 ? ` (${progress.failed} failed)` : ''}`
    : null;

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <div
        className={cn(
          'flex flex-col gap-1 px-4 py-3 rounded-xl border-2 border-dashed bg-[var(--surface)]/80 min-w-[180px] transition-all duration-300',
          selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
          status === 'running' && 'shadow-[0_0_16px_var(--running-glow)]',
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
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});
