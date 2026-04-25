import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Layers, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useViewedNodes, useViewedSubworkflowContexts } from '@/hooks/use-viewed-context';
import { NodeTooltip } from './NodeTooltip';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

/**
 * Graph node for workflow-type agents (subworkflows).
 * Shows a stacked-cards icon and a "Dive In" affordance when a
 * SubworkflowContext exists for this agent.
 */
export const WorkflowNode = memo(function WorkflowNode({ data, id, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const storeStatus = useWorkflowStore((s) => s.nodes[id]?.status);
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const elapsed = useWorkflowStore((s) => s.nodes[id]?.elapsed);
  const errorMessage = useWorkflowStore((s) => s.nodes[id]?.error_message);
  const navigateIntoSubworkflow = useWorkflowStore((s) => s.navigateIntoSubworkflow);
  const subworkflowContexts = useViewedSubworkflowContexts();

  const hasContext = subworkflowContexts.some((c) => c.parentAgent === id);
  const ctx = subworkflowContexts.find((c) => c.parentAgent === id);
  const childName = ctx?.workflowName;

  const statsText = (() => {
    if (status === 'failed' && errorMessage) {
      const msg = errorMessage.length > 35 ? errorMessage.slice(0, 32) + '...' : errorMessage;
      return { text: msg, className: 'text-red-400' };
    }
    if (status === 'running') {
      return { text: childName || 'Running subworkflow…', className: 'text-[var(--text-muted)]' };
    }
    if (status === 'completed') {
      const parts: string[] = [];
      if (childName) parts.push(childName);
      if (elapsed != null) parts.push(`${elapsed.toFixed(1)}s`);
      return { text: parts.join(' · ') || 'Done', className: 'text-[var(--text-muted)]' };
    }
    return { text: childName || null, className: 'text-[var(--text-muted)]' };
  })();

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <NodeTooltip
        data={{ status, elapsed, errorType: undefined, errorMessage, iteration: undefined }}
      >
        <div
          className={cn(
            'flex items-center gap-2 px-3 py-1.5 rounded-lg border-2 bg-[var(--node-bg)] min-w-[140px] max-w-[240px] transition-all duration-300 cursor-pointer',
            selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
            status === 'running' && 'shadow-[0_0_12px_var(--running-glow)]',
          )}
          style={{
            borderColor,
            borderStyle: 'dashed',
          }}
          onDoubleClick={(e) => {
            if (hasContext) {
              e.stopPropagation();
              navigateIntoSubworkflow(id);
            }
          }}
        >
          {/* Stacked layers icon */}
          <div
            className={cn(
              'flex items-center justify-center w-6 h-6 rounded-md flex-shrink-0',
              status === 'running' && 'animate-pulse',
            )}
            style={{ backgroundColor: `${borderColor}20` }}
          >
            <Layers className="w-3.5 h-3.5" style={{ color: borderColor }} />
          </div>

          <div className="flex flex-col min-w-0 flex-1">
            <div className="flex items-center gap-1">
              <span className="text-xs font-medium text-[var(--text)] truncate">{nodeData.label}</span>
            </div>
            {statsText.text && (
              <span className={cn('text-[10px] truncate leading-tight', statsText.className)}>
                {statsText.text}
              </span>
            )}
          </div>

          {/* "Dive in" indicator */}
          {hasContext && (
            <ChevronRight
              className="w-3.5 h-3.5 flex-shrink-0 text-[var(--text-muted)]"
            />
          )}
        </div>
      </NodeTooltip>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});
