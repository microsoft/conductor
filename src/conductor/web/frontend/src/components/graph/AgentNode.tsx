import { memo, useEffect, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Bot } from 'lucide-react';
import { cn, formatElapsed, formatTokens, formatCost } from '@/lib/utils';
import { NODE_STATUS_HEX, CONTEXT_WARN_PCT, CONTEXT_DANGER_PCT } from '@/lib/constants';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useViewedNodes } from '@/hooks/use-viewed-context';
import { NodeTooltip } from './NodeTooltip';
import type { GraphNodeData } from './graph-layout';
import type { NodeStatus } from '@/lib/constants';

export const AgentNode = memo(function AgentNode({ data, id, selected }: NodeProps) {
  const nodeData = data as unknown as GraphNodeData;
  const viewedNodes = useViewedNodes();
  const storeStatus = viewedNodes[id]?.status;
  const status = (storeStatus || nodeData.status || 'pending') as NodeStatus;
  const borderColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const nd = viewedNodes[id];
  const elapsed = nd?.elapsed;
  const model = nd?.model;
  const tokens = nd?.tokens;
  const inputTokens = nd?.input_tokens;
  const outputTokens = nd?.output_tokens;
  const costUsd = nd?.cost_usd;
  const iteration = nd?.iteration;
  const errorType = nd?.error_type;
  const errorMessage = nd?.error_message;
  const contextPct = nd?.context_pct;

  // Live elapsed timer for running nodes
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
      if (tokens != null) parts.push(`${formatTokens(tokens)} tok`);
      if (costUsd != null) parts.push(formatCost(costUsd));
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
          model,
          tokens,
          inputTokens,
          outputTokens,
          costUsd,
          iteration,
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
            <Bot className="w-3.5 h-3.5" style={{ color: borderColor }} />
          </div>
          <div className="flex flex-col min-w-0 flex-1">
            <div className="flex items-center gap-1">
              <span className="text-xs font-medium text-[var(--text)] truncate">{nodeData.label}</span>
              {iteration != null && iteration > 1 && (
                <span
                  className="flex-shrink-0 inline-flex items-center justify-center px-1.5 py-0.5 rounded-full text-[9px] font-bold leading-none"
                  style={{
                    backgroundColor: `${borderColor}25`,
                    color: borderColor,
                  }}
                >
                  x{iteration}
                </span>
              )}
            </div>
            {statsLine.text && (
              <span className={cn('text-[10px] truncate leading-tight', statsLine.className)}>
                {statsLine.text}
              </span>
            )}
          </div>
          {/* Context window progress bar */}
          {contextPct != null && (
            <div className="absolute bottom-0 left-0 right-0 h-[2px] rounded-b-lg overflow-hidden"
              style={{ backgroundColor: 'rgba(255,255,255,0.06)' }}
            >
              <div
                className={cn(
                  'h-full transition-all duration-500',
                  contextPct >= CONTEXT_DANGER_PCT ? 'animate-[context-pulse_2s_ease-in-out_infinite]' : ''
                )}
                style={{
                  width: `${Math.min(contextPct, 100)}%`,
                  backgroundColor: contextPct >= CONTEXT_DANGER_PCT ? '#ef4444' : contextPct >= CONTEXT_WARN_PCT ? '#f59e0b' : '#22c55e',
                }}
              />
            </div>
          )}
        </div>
      </NodeTooltip>
      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)] !border-none !w-2 !h-2" />
    </>
  );
});

/** Hook that returns a live-ticking elapsed string while status is 'running'. */
function useLiveElapsed(id: string, status: NodeStatus): string {
  const startedAt = useViewedNodes()[id]?.startedAt;
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

/** Hook that returns a transient CSS class on status transitions. */
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
