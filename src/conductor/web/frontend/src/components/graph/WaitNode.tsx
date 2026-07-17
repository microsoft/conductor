import { memo, useEffect, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Clock } from 'lucide-react';
import { cn, formatElapsed } from '@/lib/utils';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useNodeLiveData } from '@/hooks/use-viewed-context';
import { NodeTooltip } from './NodeTooltip';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const WaitNode = memo(function WaitNode({ data, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const nd = useNodeLiveData(nodeData);
  const storeStatus = nd?.status;
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const duration = nd?.duration_seconds ?? nd?.requested_seconds;
  const waited = nd?.waited_seconds;
  const elapsed = nd?.elapsed;
  const interrupted = nd?.interrupted;
  const errorType = nd?.error_type;
  const errorMessage = nd?.error_message;

  const liveElapsed = useLiveElapsed(nd?.startedAt, status);
  const transitionClass = useStatusTransition(status);

  const statsLine = (() => {
    if (status === 'failed' && errorMessage) {
      const msg = errorMessage.length > 40 ? errorMessage.slice(0, 37) + '...' : errorMessage;
      return { text: msg, className: 'text-red-400' };
    }
    if (status === 'running') {
      const target = typeof duration === 'number' ? ` / ${formatElapsed(duration)}` : '';
      return { text: `${liveElapsed}${target}`, className: 'text-[var(--text-muted)]' };
    }
    if (status === 'completed') {
      const parts: string[] = [];
      if (waited != null) parts.push(formatElapsed(waited));
      else if (elapsed != null) parts.push(formatElapsed(elapsed));
      if (interrupted) parts.push('interrupted');
      return { text: parts.join(' · ') || null, className: 'text-[var(--text-muted)]' };
    }
    if (status === 'pending' && typeof duration === 'number') {
      return { text: formatElapsed(duration), className: 'text-[var(--text-muted)]' };
    }
    return { text: null, className: '' };
  })();

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
      <NodeTooltip
        data={{
          status,
          elapsed: waited ?? elapsed,
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
            <Clock className="w-3.5 h-3.5" style={{ color: borderColor }} />
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

function useLiveElapsed(startedAt: number | undefined, status: NodeStatus): string {
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const lastEventTime = useWorkflowStore((s) => s.lastEventTime);
  const [display, setDisplay] = useState('0.0s');
  const rafRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (status === 'running') {
      if (replayMode) {
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
