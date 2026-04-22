import { useState, useEffect } from 'react';
import { Header } from '@/components/layout/Header';
import { BreadcrumbBar } from '@/components/layout/BreadcrumbBar';
import { StatusBar } from '@/components/layout/StatusBar';
import { ReplayBar } from '@/components/layout/ReplayBar';
import { ResizableLayout } from '@/components/layout/ResizableLayout';
import { useWebSocket } from '@/hooks/use-websocket';
import { useReplay } from '@/hooks/use-replay';
import { useWorkflowStore } from '@/stores/workflow-store';

function LiveMode() {
  useWebSocket();
  return null;
}

function ReplayMode() {
  useReplay();
  return null;
}

export default function App() {
  const [isReplayMode, setIsReplayMode] = useState<boolean | null>(null);
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const selectNode = useWorkflowStore((s) => s.selectNode);
  const workflowName = useWorkflowStore((s) => s.workflowName);

  // Detect replay mode on mount
  useEffect(() => {
    fetch('/api/replay/info')
      .then((r) => {
        if (r.ok) setIsReplayMode(true);
        else setIsReplayMode(false);
      })
      .catch(() => setIsReplayMode(false));
  }, []);

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

  if (isReplayMode === null) return null;

  return (
    <div className="h-full flex flex-col bg-[var(--bg)]">
      {isReplayMode ? <ReplayMode /> : <LiveMode />}
      <Header />
      <BreadcrumbBar />
      <ResizableLayout />
      {replayMode ? <ReplayBar /> : <StatusBar />}
    </div>
  );
}
