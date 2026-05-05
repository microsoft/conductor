import { useWorkflowStore } from '@/stores/workflow-store';
import { DialogDetail } from './DialogDetail';
import { MessageCircle } from 'lucide-react';

export function DialogOverlay() {
  const activeDialog = useWorkflowStore((s) => s.activeDialog);
  const nodes = useWorkflowStore((s) => s.nodes);

  if (!activeDialog) return null;

  const node = nodes[activeDialog.agentName];
  if (!node) return null;

  return (
    <div className="h-full flex flex-col bg-[var(--bg)] overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2.5 px-5 py-3 border-b border-[var(--border)] bg-[var(--surface)] flex-shrink-0">
        <MessageCircle className="w-4 h-4 text-fuchsia-400" />
        <h2 className="text-sm font-semibold text-[var(--text)]">
          Dialog with {activeDialog.agentName}
        </h2>
      </div>

      {/* Dialog content */}
      <div className="flex-1 overflow-hidden px-5 py-4">
        <DialogDetail node={node} />
      </div>
    </div>
  );
}
