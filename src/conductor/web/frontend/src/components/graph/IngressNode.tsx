import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { ArrowDownToLine } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

const INGRESS_COLOR = '#a78bfa'; // violet-400

/**
 * Ingress node shown at the top of a sub-workflow graph.
 * Circular like start/end but with a violet tint and parent label below.
 * Double-click navigates back to the parent workflow.
 */
export const IngressNode = memo(function IngressNode({ data, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const status = (nodeData.status || 'pending') as NodeStatus;
  const isActive = status === 'running' || status === 'completed';
  const borderColor = isActive ? INGRESS_COLOR : NODE_STATUS_HEX[status] || INGRESS_COLOR;
  const parentLabel = nodeData.parentAgent;

  const navigateUp = useWorkflowStore((s) => s.navigateUp);

  return (
    <>
      <div className="flex flex-col items-center gap-1">
        <div
          className={cn(
            'flex items-center justify-center w-11 h-11 rounded-full border-2 border-dashed transition-all duration-300 cursor-pointer',
            isActive ? 'bg-[#a78bfa]' : 'bg-[var(--node-bg)]',
            selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
            isActive && 'shadow-[0_0_12px_rgba(167,139,250,0.4)]',
          )}
          style={{ borderColor }}
          onDoubleClick={(e) => {
            e.stopPropagation();
            navigateUp();
          }}
        >
          <ArrowDownToLine className="w-4 h-4" style={{ color: isActive ? 'white' : borderColor }} />
        </div>
        {parentLabel && (
          <span className="text-[10px] text-[var(--text-muted)] whitespace-nowrap">
            from <span className="font-medium text-[var(--text)]">{parentLabel}</span>
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});
