import { useState, useRef, useCallback, type ReactNode } from 'react';
import { cn, formatElapsed, formatCost, formatTokens } from '@/lib/utils';
import { NODE_STATUS_HEX, type NodeStatus } from '@/lib/constants';

interface TooltipData {
  status: NodeStatus;
  elapsed?: number | null;
  model?: string | null;
  tokens?: number | null;
  inputTokens?: number | null;
  outputTokens?: number | null;
  costUsd?: number | null;
  exitCode?: number | null;
  errorType?: string | null;
  errorMessage?: string | null;
  iteration?: number | null;
  selectedOption?: string | null;
}

interface NodeTooltipProps {
  data: TooltipData;
  children: ReactNode;
}

export function NodeTooltip({ data, children }: NodeTooltipProps) {
  const [visible, setVisible] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleEnter = useCallback(() => {
    timeoutRef.current = setTimeout(() => setVisible(true), 200);
  }, []);

  const handleLeave = useCallback(() => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    setVisible(false);
  }, []);

  const statusColor = NODE_STATUS_HEX[data.status] || NODE_STATUS_HEX.pending;

  return (
    <div className="relative" onMouseEnter={handleEnter} onMouseLeave={handleLeave}>
      {children}
      {visible && (
        <div
          className={cn(
            'absolute z-50 bottom-full left-1/2 -translate-x-1/2 mb-2',
            'bg-[var(--surface-raised)] border border-[var(--border)] shadow-lg',
            'rounded-lg px-3 py-2 max-w-[260px] pointer-events-none',
            'animate-[tooltip-in_150ms_ease-out]',
          )}
        >
          {/* Arrow */}
          <div className="absolute top-full left-1/2 -translate-x-1/2 w-0 h-0 border-x-[6px] border-x-transparent border-t-[6px] border-t-[var(--border)]" />

          <div className="flex flex-col gap-1.5 text-[11px]">
            {/* Status badge */}
            <div className="flex items-center gap-1.5">
              <span
                className="w-2 h-2 rounded-full flex-shrink-0"
                style={{ backgroundColor: statusColor }}
              />
              <span className="font-medium text-[var(--text)] capitalize">{data.status}</span>
              {data.iteration != null && data.iteration > 1 && (
                <span className="text-[var(--text-muted)] ml-auto">iter {data.iteration}</span>
              )}
            </div>

            {/* Divider */}
            <div className="h-px bg-[var(--border)]" />

            {/* Details grid */}
            <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
              {data.elapsed != null && (
                <>
                  <span className="text-[var(--text-muted)]">Elapsed</span>
                  <span className="text-[var(--text)] font-mono">{formatElapsed(data.elapsed)}</span>
                </>
              )}
              {data.model && (
                <>
                  <span className="text-[var(--text-muted)]">Model</span>
                  <span className="text-[var(--text)] truncate">{data.model}</span>
                </>
              )}
              {data.tokens != null && (
                <>
                  <span className="text-[var(--text-muted)]">Tokens</span>
                  <span className="text-[var(--text)] font-mono">
                    {formatTokens(data.tokens)}
                    {data.inputTokens != null && data.outputTokens != null && (
                      <span className="text-[var(--text-muted)]">
                        {' '}({formatTokens(data.inputTokens)}↑ {formatTokens(data.outputTokens)}↓)
                      </span>
                    )}
                  </span>
                </>
              )}
              {data.costUsd != null && (
                <>
                  <span className="text-[var(--text-muted)]">Cost</span>
                  <span className="text-[var(--text)] font-mono">{formatCost(data.costUsd)}</span>
                </>
              )}
              {data.exitCode != null && (
                <>
                  <span className="text-[var(--text-muted)]">Exit code</span>
                  <span className={cn('font-mono', data.exitCode === 0 ? 'text-[var(--completed)]' : 'text-[var(--failed)]')}>
                    {data.exitCode}
                  </span>
                </>
              )}
              {data.selectedOption && (
                <>
                  <span className="text-[var(--text-muted)]">Selected</span>
                  <span className="text-[var(--text)] truncate">{data.selectedOption}</span>
                </>
              )}
            </div>

            {/* Error message */}
            {data.errorMessage && (
              <>
                <div className="h-px bg-[var(--border)]" />
                <div className="text-red-400 leading-tight">
                  {data.errorType && <span className="font-medium">{data.errorType}: </span>}
                  <span className="break-words">{data.errorMessage.slice(0, 120)}{data.errorMessage.length > 120 ? '...' : ''}</span>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
