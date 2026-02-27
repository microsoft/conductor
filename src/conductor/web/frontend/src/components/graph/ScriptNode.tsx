import { memo, useMemo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Terminal } from 'lucide-react';
import { cn } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const ScriptNode = memo(function ScriptNode({ data, id, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  // Read status directly from the store for immediate updates
  const storeStatus = useWorkflowStore((s) => s.nodes[id]?.status);
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const elapsed = useWorkflowStore((s) => s.nodes[id]?.elapsed);
  const exitCode = useWorkflowStore((s) => s.nodes[id]?.exit_code);

  const tooltip = useMemo(() => {
    const parts: string[] = [`Status: ${status}`];
    if (elapsed != null) parts.push(`Elapsed: ${formatSec(elapsed)}`);
    if (exitCode != null) parts.push(`Exit code: ${exitCode}`);
    return parts.join('\n');
  }, [status, elapsed, exitCode]);

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
          <Terminal className="w-3.5 h-3.5" style={{ color: borderColor }} />
        </div>
        <span className="text-xs font-medium text-[var(--text)] truncate">{nodeData.label}</span>
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
