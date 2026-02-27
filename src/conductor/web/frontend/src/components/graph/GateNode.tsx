import { memo, useMemo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { ShieldCheck } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const GateNode = memo(function GateNode({ data, id, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  // Read status directly from the store for immediate updates
  const storeStatus = useWorkflowStore((s) => s.nodes[id]?.status);
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const selectedOption = useWorkflowStore((s) => s.nodes[id]?.selected_option);
  const route = useWorkflowStore((s) => s.nodes[id]?.route);

  const tooltip = useMemo(() => {
    const parts: string[] = [`Status: ${status}`];
    if (selectedOption) parts.push(`Selected: ${selectedOption}`);
    if (route) parts.push(`Route: ${route}`);
    return parts.join('\n');
  }, [status, selectedOption, route]);

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <div
        title={tooltip}
        className={cn(
          'flex items-center gap-2 px-3 py-2 rounded-lg border-2 border-dashed bg-[var(--node-bg)] min-w-[140px] max-w-[200px] transition-all duration-300',
          selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
          status === 'waiting' && 'shadow-[0_0_12px_var(--waiting-muted)]',
          status === 'running' && 'shadow-[0_0_12px_var(--running-glow)]',
        )}
        style={{ borderColor }}
      >
        <div
          className={cn(
            'flex items-center justify-center w-6 h-6 rounded-md flex-shrink-0',
            status === 'waiting' && 'animate-pulse',
          )}
          style={{ backgroundColor: `${borderColor}20` }}
        >
          <ShieldCheck className="w-3.5 h-3.5" style={{ color: borderColor }} />
        </div>
        <span className="text-xs font-medium text-[var(--text)] truncate">{nodeData.label}</span>
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});
