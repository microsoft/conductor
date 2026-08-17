import { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { cn } from '@/lib/utils';

/** How long the "not sent" notice stays visible before auto-dismissing. */
const AUTO_DISMISS_MS = 5000;

/**
 * Surfaces a visible notice when a gate response, dialog reply, or
 * iteration-limit response couldn't be sent because the WebSocket wasn't
 * connected (issue #397). Previously these were silently dropped
 * (`if (send) { ... }` with no else branch) -- a user would click Approve
 * on a healthy-looking dashboard and nothing would ever happen, with no
 * console error and no visible feedback.
 *
 * Auto-dismisses after a few seconds rather than requiring interaction,
 * since the underlying condition (no send function) is already visible
 * via the reconnect banner / status bar if it persists.
 */
export function SendFailedBanner() {
  const wsSendFailed = useWorkflowStore((s) => s.wsSendFailed);
  const setWsSendFailed = useWorkflowStore((s) => s.setWsSendFailed);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!wsSendFailed) return;
    setVisible(true);
    const timer = setTimeout(() => {
      setVisible(false);
      setWsSendFailed(false);
    }, AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [wsSendFailed, setWsSendFailed]);

  if (!visible) return null;

  return (
    <div className="absolute top-14 left-1/2 -translate-x-1/2 z-20 animate-[banner-in_200ms_ease-out]">
      <div
        className={cn(
          'flex items-center gap-2 px-4 py-2 rounded-lg',
          'bg-red-950/90 border border-red-500/40 shadow-lg shadow-red-500/10',
          'backdrop-blur-sm max-w-[560px]',
        )}
      >
        <AlertTriangle className="w-4 h-4 text-red-400 flex-shrink-0" />
        <span className="text-xs font-medium text-red-300">
          Not connected — your response was not sent. Reconnecting…
        </span>
      </div>
    </div>
  );
}
