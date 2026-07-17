import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Layers, ChevronRight, ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useNodeLiveData } from '@/hooks/use-viewed-context';
import { NodeTooltip } from './NodeTooltip';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

/**
 * Graph node for workflow-type agents (subworkflows).
 *
 * Collapsed (default) it is a compact pill with a chevron that expands the
 * child DAG inline as a container; the child nodes render inside via React
 * Flow parent/child nesting (see `buildGraphElements`). Double-clicking still
 * drills into the subworkflow as a focused view (handled by `WorkflowGraph`).
 */
export const WorkflowNode = memo(function WorkflowNode({ data, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const nd = useNodeLiveData(nodeData);
  const status = (nd?.status || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;
  const elapsed = nd?.elapsed;
  const errorMessage = nd?.error_message;

  const toggleContextExpanded = useWorkflowStore((s) => s.toggleContextExpanded);

  const expanded = nodeData.expanded === true;
  const canExpand = nodeData.canExpand === true;
  const childContextKey = nodeData.childContextKey as string | undefined;
  const childName = nodeData.childName as string | undefined;

  const onToggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (childContextKey != null) toggleContextExpanded(childContextKey);
  };

  // Expanded: render a titled container. The child DAG is drawn as nested
  // React Flow nodes positioned inside this node's bounds.
  if (expanded) {
    return (
      <>
        <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
        <div
          className={cn(
            'w-full h-full rounded-xl border-2 border-dashed bg-[var(--surface)]/40 transition-all duration-300',
            selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
            status === 'running' && 'shadow-[0_0_16px_var(--running-glow)]',
          )}
          style={{ borderColor, minHeight: '100%' }}
        >
          <div className="flex items-center gap-1.5 px-3 py-2">
            <button
              onClick={onToggle}
              className="nodrag flex items-center justify-center w-5 h-5 rounded hover:bg-[var(--surface-hover)] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors flex-shrink-0"
              title="Collapse subworkflow"
            >
              <ChevronDown className="w-3.5 h-3.5" />
            </button>
            <Layers className="w-3.5 h-3.5 flex-shrink-0" style={{ color: borderColor }} />
            <span className="text-xs font-semibold text-[var(--text)] truncate">{nodeData.label}</span>
            {childName && (
              <span className="text-[10px] text-[var(--text-muted)] truncate">· {childName}</span>
            )}
          </div>
        </div>
        <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      </>
    );
  }

  // Collapsed pill (default).
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
            'flex items-center gap-2 px-3 py-1.5 rounded-lg border-2 bg-[var(--node-bg)] min-w-[140px] max-w-[240px] transition-all duration-300',
            selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
            status === 'running' && 'shadow-[0_0_12px_var(--running-glow)]',
          )}
          style={{
            borderColor,
            borderStyle: 'dashed',
          }}
        >
          {/* Expand-inline chevron on the LEFT, matching the collapse chevron's
              position in the expanded header so it doesn't jump sides on toggle.
              Only shown once the child DAG is available. */}
          {canExpand && (
            <button
              onClick={onToggle}
              className="nodrag flex items-center justify-center w-5 h-5 rounded hover:bg-[var(--surface-hover)] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors flex-shrink-0"
              title="Expand subworkflow inline (double-click to focus)"
            >
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          )}

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
        </div>
      </NodeTooltip>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});
