import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Octagon } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useViewedNodes } from '@/hooks/use-viewed-context';
import { NodeTooltip } from './NodeTooltip';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

/**
 * Renders a `type: terminate` step distinctly from regular agent / script /
 * gate / workflow nodes (issue #219). Terminate steps are intentional
 * end-of-workflow signals, not model invocations or shell commands, so the
 * visual deliberately avoids the agent/script affordances:
 *
 * - Octagon icon (universal "stop" semantic) instead of bot/terminal/etc.
 * - Body shows the rendered termination `reason` once the step fires (it is
 *   captured on the node from the `agent_completed` / `agent_failed` event
 *   payload's `termination_reason` field).
 * - Pending → grey, success → green, failed → red. Status comes from the
 *   shared `NODE_STATUS_HEX` map so the visual is consistent with every
 *   other node type's status semantics.
 *
 * The error/success banners at the workflow level (see ErrorBanner.tsx) are
 * responsible for the workflow-wide red/green message — this node is just
 * about identifying the terminate step in the DAG graph.
 */
export const TerminateNode = memo(function TerminateNode({ data, id, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const viewedNodes = useViewedNodes();
  const storeStatus = viewedNodes[id]?.status;
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const nd = viewedNodes[id];
  const reason = nd?.termination_reason;
  const terminationStatus = nd?.termination_status;
  const errorMessage = nd?.error_message;
  const errorType = nd?.error_type;

  // Body shows the rendered termination reason. Until the step fires we only
  // have the static type to display ("terminate"). On failure paths, prefer
  // `termination_reason` (set explicitly by the engine) and fall back to
  // `error_message` (set by the generic `agent_failed` handler) so we never
  // show an empty banner.
  const bodyText = reason || errorMessage;
  const bodyClassName =
    status === 'failed'
      ? 'text-red-400'
      : status === 'completed'
        ? 'text-green-400'
        : 'text-[var(--text-muted)]';

  return (
    <>
      <Handle
        type="target"
        position={Position.Top}
        className="!bg-[var(--border)] !border-none !w-2 !h-2"
      />
      <NodeTooltip
        data={{
          status,
          reason,
          terminationStatus,
          errorType,
          errorMessage,
        }}
      >
        <div
          className={cn(
            'flex items-center gap-2 px-3 py-1.5 rounded-lg border-2 bg-[var(--node-bg)] min-w-[140px] max-w-[260px] transition-all duration-300',
            selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
            status === 'completed' && 'shadow-[0_0_12px_var(--completed-muted)]',
            status === 'failed' && 'shadow-[0_0_12px_var(--failed-muted)]',
          )}
          style={{ borderColor }}
        >
          <div
            className="flex items-center justify-center w-6 h-6 rounded-md flex-shrink-0"
            style={{ backgroundColor: `${borderColor}20` }}
          >
            <Octagon
              className="w-3.5 h-3.5"
              style={{ color: borderColor }}
              fill={status === 'completed' || status === 'failed' ? borderColor : 'transparent'}
              fillOpacity={0.2}
            />
          </div>
          <div className="flex flex-col min-w-0 flex-1">
            <span className="text-xs font-medium text-[var(--text)] truncate">
              {nodeData.label}
            </span>
            <span className="text-[10px] uppercase tracking-wide text-[var(--text-muted)] truncate leading-tight">
              terminate{terminationStatus ? ` · ${terminationStatus}` : ''}
            </span>
            {bodyText && (
              <span
                className={cn('text-[10px] truncate leading-tight mt-0.5', bodyClassName)}
                title={bodyText}
              >
                {bodyText.length > 50 ? bodyText.slice(0, 47) + '...' : bodyText}
              </span>
            )}
          </div>
        </div>
      </NodeTooltip>
      {/* Terminate is a sink — no outbound handle. Keeping a hidden source
          handle would let stray edges attach in the layout; omit it entirely
          so dagre treats this node as terminal. */}
    </>
  );
});
