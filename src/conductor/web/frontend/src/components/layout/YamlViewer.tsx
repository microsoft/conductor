import { useEffect } from 'react';
import { X } from 'lucide-react';

interface YamlViewerProps {
  yaml: string;
  onClose: () => void;
}

export function YamlViewer({ yaml, onClose }: YamlViewerProps) {
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />

      {/* Panel */}
      <div className="relative ml-auto w-full max-w-2xl h-full flex flex-col bg-[var(--surface)] border-l border-[var(--border)] shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-2 border-b border-[var(--border)]">
          <span className="text-sm font-semibold text-[var(--text)]">Workflow YAML</span>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-[var(--surface-hover)] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-auto p-4">
          <pre className="text-xs font-mono text-[var(--text-secondary)] whitespace-pre leading-relaxed">
            {yaml}
          </pre>
        </div>
      </div>
    </div>
  );
}
