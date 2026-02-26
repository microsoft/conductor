import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Play } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const StartNode = memo(function StartNode({ data, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const status = (nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;
  const isActive = status === 'running' || status === 'completed';

  return (
    <>
      <div
        className={cn(
          'flex items-center justify-center w-11 h-11 rounded-full border-2 transition-all duration-300',
          isActive ? 'bg-[var(--completed)]' : 'bg-[var(--node-bg)]',
          selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
          isActive && 'shadow-[0_0_12px_var(--completed-muted)]',
        )}
        style={{ borderColor }}
      >
        <Play className="w-4 h-4 ml-0.5" style={{ color: isActive ? 'white' : borderColor }} />
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});
