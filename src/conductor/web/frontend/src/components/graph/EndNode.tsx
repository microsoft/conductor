import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Check, Square } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const EndNode = memo(function EndNode({ data, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const status = (nodeData.status || 'pending') as NodeStatus;
  const isCompleted = status === 'completed';
  const isFailed = status === 'failed';
  const isPending = !isCompleted && !isFailed;
  const borderColor = isCompleted
    ? NODE_STATUS_HEX.completed
    : isFailed
      ? NODE_STATUS_HEX.failed
      : NODE_STATUS_HEX.pending;

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <div
        className={cn(
          'flex items-center justify-center w-11 h-11 rounded-full border-2 transition-all duration-300',
          isCompleted
            ? 'bg-[var(--completed)] shadow-[0_0_16px_var(--completed-muted)]'
            : isFailed
              ? 'bg-[var(--failed)] shadow-[0_0_16px_var(--failed-muted)]'
              : 'bg-[var(--node-bg)]',
          selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
        )}
        style={{ borderColor }}
      >
        {isCompleted ? (
          <Check className="w-5 h-5 text-white" strokeWidth={3} />
        ) : isFailed ? (
          <Square className="w-3.5 h-3.5 text-white" fill="white" />
        ) : (
          <Check className="w-5 h-5" strokeWidth={2.5} style={{ color: isPending ? NODE_STATUS_HEX.pending : borderColor }} />
        )}
      </div>
    </>
  );
});
