import { memo, useMemo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Bot } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const AgentNode = memo(function AgentNode({ data, id, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const status = (nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const elapsed = useWorkflowStore((s) => s.nodes[id]?.elapsed);
  const model = useWorkflowStore((s) => s.nodes[id]?.model);
  const tokens = useWorkflowStore((s) => s.nodes[id]?.tokens);
  const costUsd = useWorkflowStore((s) => s.nodes[id]?.cost_usd);
  const iteration = useWorkflowStore((s) => s.nodes[id]?.iteration);

  const tooltip = useMemo(() => {
    const parts: string[] = [`Status: ${status}`];
    if (iteration != null && iteration > 1) parts.push(`Iteration: ${iteration}`);
    if (elapsed != null) parts.push(`Elapsed: ${formatSec(elapsed)}`);
    if (model) parts.push(`Model: ${model}`);
    if (tokens != null) parts.push(`Tokens: ${tokens.toLocaleString()}`);
    if (costUsd != null) parts.push(`Cost: $${costUsd.toFixed(4)}`);
    return parts.join('\n');
  }, [status, elapsed, model, tokens, costUsd, iteration]);

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <div
        title={tooltip}
        className={cn(
          'flex items-center gap-2 px-3 py-2 rounded-lg border-2 bg-[var(--node-bg)] min-w-[140px] max-w-[200px] transition-all duration-300',
          selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
          status === 'running' && 'shadow-[0_0_12px_var(--running-glow)]',
        )}
        style={{ borderColor }}
      >
        <div
          className={cn(
            'flex items-center justify-center w-6 h-6 rounded-md flex-shrink-0',
            status === 'running' && 'animate-pulse',
          )}
          style={{ backgroundColor: `${borderColor}20` }}
        >
          <Bot className="w-3.5 h-3.5" style={{ color: borderColor }} />
        </div>
        <span className="text-xs font-medium text-[var(--text)] truncate">{nodeData.label}</span>
        {iteration != null && iteration > 1 && (
          <span
            className="ml-auto flex-shrink-0 inline-flex items-center justify-center px-1.5 py-0.5 rounded-full text-[9px] font-bold leading-none"
            style={{
              backgroundColor: `${borderColor}25`,
              color: borderColor,
            }}
          >
            ×{iteration}
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});

function formatSec(s: number): string {
  if (s < 1) return `${(s * 1000).toFixed(0)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const sec = (s % 60).toFixed(0);
  return `${m}m ${sec}s`;
}
