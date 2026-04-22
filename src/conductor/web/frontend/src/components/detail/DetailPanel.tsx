import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import { AgentDetail } from './AgentDetail';
import { ScriptDetail } from './ScriptDetail';
import { GateDetail } from './GateDetail';
import { GroupDetail } from './GroupDetail';
import { SubworkflowDetail } from './SubworkflowDetail';
import { cn } from '@/lib/utils';

export function DetailPanel() {
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const viewedCtx = useWorkflowStore((s) => s.getViewedContext());
  const selectNode = useWorkflowStore((s) => s.selectNode);

  // Slide-in animation state
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    // Trigger animation on next frame after mount
    requestAnimationFrame(() => setMounted(true));
    return () => setMounted(false);
  }, [selectedNode]);

  const node = selectedNode ? viewedCtx.nodes[selectedNode] : null;

  if (!selectedNode || !node) {
    return (
      <div className="h-full flex flex-col bg-[var(--surface)]">
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)]">
          <h2 className="text-sm font-semibold text-[var(--text)]">Detail</h2>
        </div>
        <div className="flex-1 flex items-center justify-center">
          <p className="text-xs text-[var(--text-muted)]">Click a node to view details</p>
        </div>
      </div>
    );
  }

  const DetailComponent = (() => {
    switch (node.type) {
      case 'script':
        return ScriptDetail;
      case 'human_gate':
        return GateDetail;
      case 'parallel_group':
      case 'for_each_group':
        return GroupDetail;
      case 'workflow':
        return SubworkflowDetail;
      default:
        return AgentDetail;
    }
  })();

  return (
    <div
      className={cn(
        'h-full flex flex-col bg-[var(--surface)] transition-all duration-150 ease-out',
        mounted ? 'translate-x-0 opacity-100' : 'translate-x-4 opacity-0',
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border)] flex-shrink-0">
        <h2 className="text-sm font-semibold text-[var(--text)] truncate">{selectedNode}</h2>
        <button
          onClick={() => selectNode(null)}
          className="p-1 rounded hover:bg-[var(--surface-hover)] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors"
          title="Close panel"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-4 py-3">
        <DetailComponent node={node} />
      </div>
    </div>
  );
}
