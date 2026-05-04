import { memo, useEffect, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { ShieldCheck } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useViewedNodes } from '@/hooks/use-viewed-context';
import { NodeTooltip } from './NodeTooltip';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const GateNode = memo(function GateNode({ data, id, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const viewedNodes = useViewedNodes();
  const storeStatus = viewedNodes[id]?.status;
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const selectedOption = viewedNodes[id]?.selected_option;

  // Status transition animation
  const transitionClass = useStatusTransition(status);

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <NodeTooltip
        data={{
          status,
          selectedOption,
        }}
      >
        <div
          className={cn(
            'flex items-center gap-2 px-3 py-1.5 rounded-lg border-2 border-dashed bg-[var(--node-bg)] min-w-[140px] max-w-[220px] transition-all duration-300',
            selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
            status === 'waiting' && 'shadow-[0_0_12px_var(--waiting-muted)]',
            status === 'running' && 'shadow-[0_0_12px_var(--running-glow)]',
            transitionClass,
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
          <div className="flex flex-col min-w-0 flex-1">
            <span className="text-xs font-medium text-[var(--text)] truncate">{nodeData.label}</span>
            {status === 'waiting' && (
              <span className="text-[10px] text-[var(--waiting)] truncate leading-tight">
                Awaiting input...
              </span>
            )}
            {status === 'completed' && selectedOption && (
              <span className="text-[10px] text-[var(--text-muted)] truncate leading-tight">
                {selectedOption}
              </span>
            )}
          </div>
        </div>
      </NodeTooltip>
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

    if (status === 'running' || status === 'waiting') {
      setTransitionClass('node-activate');
    } else if ((prev === 'running' || prev === 'waiting') && status === 'completed') {
      setTransitionClass('node-complete');
    }

    const timer = setTimeout(() => setTransitionClass(''), 400);
    return () => clearTimeout(timer);
  }, [status]);

  return transitionClass;
}
