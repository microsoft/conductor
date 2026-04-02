import { memo, useEffect, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Terminal } from 'lucide-react';
import { cn, formatElapsed } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import { NodeTooltip } from './NodeTooltip';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const ScriptNode = memo(function ScriptNode({ data, id, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const storeStatus = useWorkflowStore((s) => s.nodes[id]?.status);
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const elapsed = useWorkflowStore((s) => s.nodes[id]?.elapsed);
  const exitCode = useWorkflowStore((s) => s.nodes[id]?.exit_code);
  const errorType = useWorkflowStore((s) => s.nodes[id]?.error_type);
  const errorMessage = useWorkflowStore((s) => s.nodes[id]?.error_message);

  // Live elapsed timer
  const liveElapsed = useLiveElapsed(id, status);

  // Status transition animation
  const transitionClass = useStatusTransition(status);

  // Build stats line
  const statsLine = (() => {
    if (status === 'failed' && errorMessage) {
      const msg = errorMessage.length > 40 ? errorMessage.slice(0, 37) + '...' : errorMessage;
      return { text: msg, className: 'text-red-400' };
    }
    if (status === 'running') {
      return { text: liveElapsed, className: 'text-[var(--text-muted)]' };
    }
    if (status === 'completed') {
      const parts: string[] = [];
      if (elapsed != null) parts.push(formatElapsed(elapsed));
      if (exitCode != null) parts.push(`exit ${exitCode}`);
      return { text: parts.join(' · ') || null, className: 'text-[var(--text-muted)]' };
    }
    return { text: null, className: '' };
  })();

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <NodeTooltip
        data={{
          status,
          elapsed,
          exitCode,
          errorType,
          errorMessage,
        }}
      >
        <div
          className={cn(
            'flex items-center gap-2 px-3 py-1.5 rounded-lg border-2 bg-[var(--node-bg)] min-w-[140px] max-w-[220px] transition-all duration-300',
            selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
            status === 'running' && 'shadow-[0_0_12px_var(--running-glow)]',
            transitionClass,
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
          <div className="flex flex-col min-w-0 flex-1">
            <span className="text-xs font-medium text-[var(--text)] truncate">{nodeData.label}</span>
            {statsLine.text && (
              <span className={cn('text-[10px] truncate leading-tight', statsLine.className)}>
                {statsLine.text}
              </span>
            )}
          </div>
        </div>
      </NodeTooltip>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});

function useLiveElapsed(id: string, status: NodeStatus): string {
  const startedAt = useWorkflowStore((s) => s.nodes[id]?.startedAt);
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const lastEventTime = useWorkflowStore((s) => s.lastEventTime);
  const [display, setDisplay] = useState('0.0s');
  const rafRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (status === 'running') {
      if (replayMode) {
        // In replay mode, use event timestamps instead of wall clock
        if (rafRef.current) clearInterval(rafRef.current);
        const origin = startedAt ?? (lastEventTime ?? 0);
        const now = lastEventTime ?? origin;
        setDisplay(formatElapsed(now - origin));
        return;
      }
      const origin = startedAt != null ? startedAt * 1000 : Date.now();
      const tick = () => {
        const sec = (Date.now() - origin) / 1000;
        setDisplay(formatElapsed(sec));
      };
      tick();
      rafRef.current = setInterval(tick, 1000);
      return () => {
        if (rafRef.current) clearInterval(rafRef.current);
      };
    } else {
      if (rafRef.current) clearInterval(rafRef.current);
    }
  }, [status, startedAt, replayMode, lastEventTime]);

  return display;
}

function useStatusTransition(status: NodeStatus): string {
  const prevStatusRef = useRef<NodeStatus>(status);
  const [transitionClass, setTransitionClass] = useState('');

  useEffect(() => {
    const prev = prevStatusRef.current;
    prevStatusRef.current = status;
    if (prev === status) return;

    if (status === 'running') {
      setTransitionClass('node-activate');
    } else if (prev === 'running' && (status === 'completed' || status === 'failed')) {
      setTransitionClass(status === 'completed' ? 'node-complete' : 'node-fail');
    }

    const timer = setTimeout(() => setTransitionClass(''), 400);
    return () => clearTimeout(timer);
  }, [status]);

  return transitionClass;
}
