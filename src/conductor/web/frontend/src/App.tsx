import { useEffect } from 'react';
import { Header } from '@/components/layout/Header';
import { StatusBar } from '@/components/layout/StatusBar';
import { ResizableLayout } from '@/components/layout/ResizableLayout';
import { useWebSocket } from '@/hooks/use-websocket';
import { useWorkflowStore } from '@/stores/workflow-store';

export default function App() {
  useWebSocket();

  const selectNode = useWorkflowStore((s) => s.selectNode);
  const workflowName = useWorkflowStore((s) => s.workflowName);

  // Update document title
  useEffect(() => {
    document.title = workflowName ? `Conductor — ${workflowName}` : 'Conductor Dashboard';
  }, [workflowName]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        selectNode(null);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectNode]);

  return (
    <div className="h-full flex flex-col bg-[var(--bg)]">
      <Header />
      <ResizableLayout />
      <StatusBar />
    </div>
  );
}
