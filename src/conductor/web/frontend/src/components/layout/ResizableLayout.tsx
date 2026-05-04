import { PanelGroup, Panel, PanelResizeHandle } from 'react-resizable-panels';
import { WorkflowGraph } from '@/components/graph/WorkflowGraph';
import { DetailPanel } from '@/components/detail/DetailPanel';
import { OutputPane } from '@/components/layout/OutputPane';
import { useWorkflowStore } from '@/stores/workflow-store';
import { DialogOverlay } from '@/components/detail/DialogOverlay';

export function ResizableLayout() {
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const activeDialog = useWorkflowStore((s) => s.activeDialog);
  const dialogEngaged = useWorkflowStore((s) => s.dialogEngaged);

  return (
    <PanelGroup direction="vertical" className="flex-1 overflow-hidden">
      {/* Top: Graph + Detail */}
      <Panel defaultSize={70} minSize={30}>
        <PanelGroup direction="horizontal" className="h-full">
          <Panel defaultSize={selectedNode ? 65 : 100} minSize={40}>
            {activeDialog && dialogEngaged ? <DialogOverlay /> : <WorkflowGraph />}
          </Panel>
          {selectedNode && (
            <>
              <PanelResizeHandle className="w-[3px] bg-[var(--border)] hover:bg-[var(--text-muted)] transition-colors cursor-col-resize" />
              <Panel defaultSize={35} minSize={20} maxSize={60}>
                <DetailPanel />
              </Panel>
            </>
          )}
        </PanelGroup>
      </Panel>

      {/* Resize handle */}
      <PanelResizeHandle className="h-[3px] bg-[var(--border)] hover:bg-[var(--text-muted)] transition-colors cursor-row-resize" />

      {/* Bottom: Output pane */}
      <Panel defaultSize={30} minSize={5} maxSize={70} collapsible>
        <OutputPane />
      </Panel>
    </PanelGroup>
  );
}
