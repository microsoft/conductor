import { AlertTriangle } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { useReconnectWarning } from '@/hooks/use-reconnect-warning';
import { cn, formatElapsed } from '@/lib/utils';

/**
 * Warns the user when the dashboard has been unable to reconnect its
 * WebSocket for too long while the workflow still shows `'running'`
 * (issue #330). Without this, a silently crashed `--web-bg` process (or any
 * process that dies mid-run) leaves the dashboard indistinguishable from a
 * healthy long-running workflow — the only feedback otherwise is a small
 * spinner in the status bar.
 *
 * Points at the best available log location: the `--web-bg` child's
 * captured stderr/stdout logs when present, else the `--log-file` debug
 * log, else a generic hint to check the terminal that launched
 * `conductor run` (see the "Debugging --web-bg failures" section of
 * AGENTS.md).
 *
 * Only clears when the connection actually recovers (`wsDisconnectedSince`
 * resets in the store) — not on a timer — so a refresh isn't the only way
 * to dismiss the stale "running" impression.
 */
export function ReconnectWarningBanner() {
  const { stuck, elapsedMs } = useReconnectWarning();
  const bgStderrLog = useWorkflowStore((s) => s.bgStderrLog);
  const bgStdoutLog = useWorkflowStore((s) => s.bgStdoutLog);
  const systemLogFile = useWorkflowStore((s) => s.systemLogFile);

  if (!stuck) return null;

  const logHint = (() => {
    if (bgStderrLog) {
      return `Check the captured logs: ${bgStderrLog}${bgStdoutLog ? ` (and ${bgStdoutLog})` : ''}`;
    }
    if (systemLogFile) {
      return `Check the debug log: ${systemLogFile}`;
    }
    return 'Check the terminal where `conductor run` was launched, or re-run with --log-file to capture one.';
  })();

  return (
    <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 animate-[banner-in_200ms_ease-out]">
      <div
        className={cn(
          'flex items-center gap-2 px-4 py-2 rounded-lg',
          'bg-amber-950/90 border border-amber-500/40 shadow-lg shadow-amber-500/10',
          'backdrop-blur-sm max-w-[560px]',
        )}
      >
        <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0" />
        <div className="flex flex-col min-w-0">
          <span className="text-xs font-medium text-amber-300">
            Connection lost — workflow may have stopped responding
          </span>
          <span className="text-[11px] text-amber-400/80 truncate">
            Reconnecting for {formatElapsed(elapsedMs / 1000)} with no success. The Conductor
            process may have crashed.
          </span>
          <span className="text-[10px] text-amber-400/60 truncate" title={bgStderrLog ?? systemLogFile ?? undefined}>
            {logHint}
          </span>
        </div>
      </div>
    </div>
  );
}
