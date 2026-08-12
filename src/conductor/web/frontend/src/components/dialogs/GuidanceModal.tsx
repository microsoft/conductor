import { useState, useEffect } from 'react';
import { MessageSquarePlus, Send, X } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';

interface GuidanceModalProps {
  onClose: () => void;
}

/**
 * Modal for submitting mid-run guidance to a ``--web``/``--web-bg`` workflow
 * (issue #400).
 *
 * Unlike ``IterationLimitModal`` nothing blocks on this — it is dismissable
 * (close button, Escape, click-outside) because a queued or applied
 * submission needs no further action from the user. When the workflow is
 * currently paused (``isPaused``), the primary button reads "Send & resume"
 * to make clear that submitting also unblocks the paused agent.
 */
export function GuidanceModal({ onClose }: GuidanceModalProps) {
  const isPaused = useWorkflowStore((s) => s.isPaused);
  const wsStatus = useWorkflowStore((s) => s.wsStatus);
  const userGuidance = useWorkflowStore((s) => s.userGuidance);
  const sendGuidance = useWorkflowStore((s) => s.sendGuidance);

  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canInteract = wsStatus === 'connected' && !sending;
  const trimmed = text.trim();
  const sendDisabled = !canInteract || trimmed.length === 0;

  const handleSend = async () => {
    if (sendDisabled) return;
    setSending(true);
    setError(null);
    const result = await sendGuidance(trimmed);
    setSending(false);
    if (result.ok) {
      setText('');
    } else {
      setError(result.error);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void handleSend();
    }
  };

  // Close on Escape regardless of which element has focus — mirrors
  // FileViewer.tsx's window-level listener. Wiring Escape only to the
  // textarea's onKeyDown (the prior approach) stops working the moment
  // focus leaves it (e.g. after tabbing to Close/Send), silently
  // contradicting this component's own "dismissable via Escape" claim.
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="guidance-title"
      data-testid="guidance-modal"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="relative flex flex-col w-[90vw] max-w-lg rounded-xl border border-[var(--border)] bg-[var(--surface)] shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-2.5 px-4 py-3 border-b border-[var(--border)] bg-[var(--surface-raised)]">
          <div className="flex items-center gap-2.5">
            <MessageSquarePlus className="w-4 h-4 text-sky-400 flex-shrink-0" />
            <h2 id="guidance-title" className="text-sm font-semibold text-[var(--text)]">
              Guide this run
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded hover:bg-[var(--surface-hover)] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="px-4 py-4 space-y-3">
          <p className="text-xs text-[var(--text-muted)]">
            Applied at the next step boundary, or immediately if an agent is
            currently paused.
          </p>

          <textarea
            data-testid="guidance-textarea"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={!canInteract}
            autoFocus
            rows={3}
            placeholder="e.g. Prefer Python 3.12 examples"
            className="w-full text-xs px-3 py-2 rounded-lg border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] outline-none focus:border-sky-400 transition-colors disabled:opacity-50 resize-none"
          />

          {error && (
            <div className="text-[11px] text-red-300" role="alert">
              {error}
            </div>
          )}

          {wsStatus !== 'connected' && (
            <div className="text-[11px] text-red-300">
              Disconnected from server — reconnect to send guidance.
            </div>
          )}

          {userGuidance.length > 0 && (
            <div className="space-y-1 max-h-40 overflow-y-auto">
              <h3 className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold">
                Guidance this run
              </h3>
              <ul className="space-y-1">
                {userGuidance.map((entry, i) => (
                  <li
                    key={`${i}-${entry.text}`}
                    className="flex items-start gap-1.5 text-[11px] text-[var(--text-secondary)]"
                  >
                    <span
                      data-testid="guidance-entry-marker"
                      className={entry.applied ? 'text-emerald-400' : 'text-amber-400'}
                    >
                      {entry.applied ? '✓' : '…'}
                    </span>
                    <span>{entry.text}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-[var(--border)] bg-[var(--surface-raised)]">
          <button
            type="button"
            onClick={onClose}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border border-[var(--border)] text-[var(--text)] hover:bg-[var(--surface-hover)] transition-colors"
          >
            Close
          </button>
          <button
            type="button"
            data-testid="guidance-send"
            onClick={() => void handleSend()}
            disabled={sendDisabled}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-sky-500 text-white hover:bg-sky-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors font-medium"
          >
            <Send className="w-3.5 h-3.5" />
            {sending ? 'Sending…' : isPaused ? 'Send & resume' : 'Send'}
          </button>
        </div>
      </div>
    </div>
  );
}
