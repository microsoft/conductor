import { Layers, ChevronRight, Coins, Hash } from 'lucide-react';
import { MetadataGrid } from './MetadataGrid';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useViewedSubworkflowContexts } from '@/hooks/use-viewed-context';
import type { NodeData, SubworkflowContext } from '@/stores/workflow-store';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { formatElapsed, formatCost, formatTokens } from '@/lib/utils';
import type { NodeStatus } from '@/lib/constants';

interface SubworkflowDetailProps {
  node: NodeData;
}

export function SubworkflowDetail({ node }: SubworkflowDetailProps) {
  const status = node.status as NodeStatus;
  const statusColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;
  const navigateIntoSubworkflow = useWorkflowStore((s) => s.navigateIntoSubworkflow);
  const allSubContexts = useViewedSubworkflowContexts();
  const subContexts = allSubContexts.filter((c) => c.parentAgent === node.name);

  const items: Array<{ label: string; value: string | number | null | undefined }> = [];
  if (node.elapsed != null) items.push({ label: 'Elapsed', value: formatElapsed(node.elapsed) });
  if (node.cost_usd != null) items.push({ label: 'Cost', value: formatCost(node.cost_usd) });
  if (node.tokens != null) items.push({ label: 'Tokens', value: formatTokens(node.tokens) });
  if (node.iteration != null && node.iteration > 1) items.push({ label: 'Iteration', value: node.iteration });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <span
          className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider"
          style={{ backgroundColor: `${statusColor}20`, color: statusColor }}
        >
          {status}
        </span>
        <span className="text-xs text-[var(--text-muted)]">Subworkflow Agent</span>
      </div>

      <MetadataGrid items={items} />

      {/* List subworkflow runs */}
      {subContexts.length > 0 && (
        <div className="space-y-2">
          <div className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold">
            Subworkflow Runs ({subContexts.length})
          </div>
          <div className="space-y-1">
            {subContexts.map((ctx, idx) => (
              <SubworkflowRunRow
                key={`${ctx.parentAgent}-${ctx.iteration}-${idx}`}
                ctx={ctx}
                onClick={() => navigateIntoSubworkflow(node.name, ctx.iteration)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Error info */}
      {status === 'failed' && (node.error_type || node.error_message) && (
        <div className="text-xs text-red-400">
          {node.error_type && <span className="font-semibold">{node.error_type}</span>}
          {node.error_message && <span className="ml-1">— {node.error_message}</span>}
        </div>
      )}

      {subContexts.length === 0 && status === 'pending' && (
        <div className="text-xs text-[var(--text-muted)] italic">
          Subworkflow has not started yet.
        </div>
      )}
    </div>
  );
}

function SubworkflowRunRow({ ctx, onClick }: { ctx: SubworkflowContext; onClick: () => void }) {
  const statusColor = NODE_STATUS_HEX[ctx.status] || NODE_STATUS_HEX.pending;

  return (
    <button
      onClick={onClick}
      className="flex items-center gap-2 w-full px-3 py-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] hover:bg-[var(--node-bg)] transition-colors text-left"
    >
      <Layers className="w-3.5 h-3.5 flex-shrink-0" style={{ color: statusColor }} />
      <div className="flex flex-col min-w-0 flex-1">
        <span className="text-xs font-medium text-[var(--text)] truncate">
          {ctx.workflowName || ctx.workflowFile || 'Subworkflow'}
        </span>
        <div className="flex items-center gap-2 text-[10px] text-[var(--text-muted)]">
          {ctx.agentsTotal > 0 && (
            <span className="flex items-center gap-0.5">
              <Hash className="w-2.5 h-2.5" />
              {ctx.agentsCompleted}/{ctx.agentsTotal} agents
            </span>
          )}
          {ctx.totalCost > 0 && (
            <span className="flex items-center gap-0.5">
              <Coins className="w-2.5 h-2.5" />
              {formatCost(ctx.totalCost)}
            </span>
          )}
        </div>
      </div>
      <span
        className="text-[10px] font-bold uppercase tracking-wider flex-shrink-0 px-1.5 py-0.5 rounded"
        style={{ backgroundColor: `${statusColor}20`, color: statusColor }}
      >
        {ctx.status}
      </span>
      <ChevronRight className="w-3.5 h-3.5 flex-shrink-0 text-[var(--text-muted)]" />
    </button>
  );
}
