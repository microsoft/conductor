import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { ArrowUpFromLine } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

const EGRESS_COLOR = '#a78bfa'; // violet-400

/**
 * Egress node shown at the bottom of a sub-workflow graph.
 * Circular like start/end but with a violet tint and parent label below.
 * Double-click navigates back to the parent workflow.
 */
export const EgressNode = memo(function EgressNode({ data, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const status = (nodeData.status || 'pending') as NodeStatus;
  const isCompleted = status === 'completed';
  const isFailed = status === 'failed';
  const borderColor = isCompleted
    ? EGRESS_COLOR
    : isFailed
      ? NODE_STATUS_HEX.failed
      : EGRESS_COLOR;
  const parentLabel = nodeData.parentAgent;

  const navigateUp = useWorkflowStore((s) => s.navigateUp);

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <div className="flex flex-col items-center gap-1">
        <div
          className={cn(
            'flex items-center justify-center w-11 h-11 rounded-full border-2 border-dashed transition-all duration-300 cursor-pointer',
            isCompleted
              ? 'bg-[#a78bfa] shadow-[0_0_12px_rgba(167,139,250,0.4)]'
              : isFailed
                ? 'bg-[var(--failed)] shadow-[0_0_16px_var(--failed-muted)]'
                : 'bg-[var(--node-bg)]',
            selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
          )}
          style={{ borderColor }}
          onDoubleClick={(e) => {
            e.stopPropagation();
            navigateUp();
          }}
        >
          <ArrowUpFromLine className="w-4 h-4" style={{ color: (isCompleted || isFailed) ? 'white' : borderColor }} />
        </div>
        {parentLabel && (
          <span className="text-[10px] text-[var(--text-muted)] whitespace-nowrap">
            return to <span className="font-medium text-[var(--text)]">{parentLabel}</span>
          </span>
        )}
      </div>
    </>
  );
});
