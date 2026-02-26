import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { CircleStop } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const EndNode = memo(function EndNode({ data, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const status = (nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <div
        className={cn(
          'flex items-center justify-center w-11 h-11 rounded-full border-2 bg-[var(--node-bg)] transition-all duration-300',
          selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
          status === 'completed' && 'shadow-[0_0_12px_var(--completed-muted)]',
        )}
        style={{ borderColor }}
      >
        <CircleStop className="w-4 h-4" style={{ color: borderColor }} />
      </div>
    </>
  );
});
