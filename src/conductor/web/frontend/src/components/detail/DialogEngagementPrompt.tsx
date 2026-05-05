import { MessageCircle, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { NodeData } from '@/stores/workflow-store';

interface DialogEngagementPromptProps {
  node: NodeData;
}

export function DialogEngagementPrompt({ node }: DialogEngagementPromptProps) {
  const engageDialog = useWorkflowStore((s) => s.engageDialog);
  const sendDialogDecline = useWorkflowStore((s) => s.sendDialogDecline);
  const wsStatus = useWorkflowStore((s) => s.wsStatus);

  const dialogId = node.dialog_id || '';
  const messages = node.dialog_messages || [];
  const canAct = wsStatus === 'connected';

  // Show the first agent message (opening message)
  const openingMessage = messages.find((m) => m.role === 'agent');

  const handleDecline = () => {
    if (!canAct) return;
    sendDialogDecline(node.name, dialogId);
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Active dialog banner */}
      <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-fuchsia-500/10 border border-fuchsia-500/30">
        <span className="relative flex h-2.5 w-2.5 flex-shrink-0">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-fuchsia-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-fuchsia-500" />
        </span>
        <span className="text-xs font-semibold text-fuchsia-400 tracking-wide">
          Dialog Requested
        </span>
      </div>

      {/* Opening agent message */}
      {openingMessage && (
        <div className="rounded-lg px-3 py-2 bg-amber-500/10 border border-amber-500/30">
          <div className="text-[10px] font-semibold mb-1 text-[var(--text-muted)]">
            {node.name}
          </div>
          <div className="dialog-markdown text-xs leading-relaxed text-[var(--text)]">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {openingMessage.content}
            </ReactMarkdown>
          </div>
        </div>
      )}

      {/* Engagement choice */}
      <div className="space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold">
          How would you like to proceed?
        </div>
        <div className="flex gap-2">
          <button
            onClick={engageDialog}
            disabled={!canAct}
            className="flex-1 flex items-center justify-center gap-1.5 text-xs px-3 py-2 rounded-lg border border-fuchsia-500/40 bg-fuchsia-500/10 text-fuchsia-300 hover:bg-fuchsia-500/20 transition-colors font-medium disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <MessageCircle className="w-3 h-3" />
            💬 Discuss
          </button>
          <button
            onClick={handleDecline}
            disabled={!canAct}
            className="flex-1 flex items-center justify-center gap-1.5 text-xs px-3 py-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] text-[var(--text-muted)] hover:bg-[var(--surface-hover)] transition-colors font-medium disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <X className="w-3 h-3" />
            ✕ Skip & continue
          </button>
        </div>
      </div>
    </div>
  );
}
