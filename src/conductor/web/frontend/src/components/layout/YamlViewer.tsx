import { useEffect, useMemo } from 'react';
import { X } from 'lucide-react';

interface YamlViewerProps {
  yaml: string;
  onClose: () => void;
}

function highlightYaml(yaml: string): (JSX.Element | string)[] {
  return yaml.split('\n').map((line, i) => {
    // Comment lines
    if (/^\s*#/.test(line)) {
      return <span key={i} className="text-emerald-500/70">{line}{'\n'}</span>;
    }

    // Lines with key: value
    const keyMatch = line.match(/^(\s*)(- )?([a-zA-Z_][\w.-]*)(:\s*)(.*)/);
    if (keyMatch) {
      const [, indent, dash, key, colon, value] = keyMatch;
      return (
        <span key={i}>
          {indent}{dash ?? ''}
          <span className="text-sky-400">{key}</span>
          <span className="text-[var(--text-muted)]">{colon}</span>
          {formatValue(value)}
          {'\n'}
        </span>
      );
    }

    // List items (- value)
    const listMatch = line.match(/^(\s*)(- )(.*)/);
    if (listMatch) {
      const [, indent, dash, value] = listMatch;
      return (
        <span key={i}>
          {indent}<span className="text-[var(--text-muted)]">{dash}</span>{formatValue(value)}{'\n'}
        </span>
      );
    }

    return <span key={i}>{line}{'\n'}</span>;
  });
}

function formatValue(value: string): JSX.Element | string {
  if (!value) return '';
  // Inline comment
  const commentIdx = value.indexOf(' #');
  const mainVal = commentIdx >= 0 ? value.slice(0, commentIdx) : value;
  const comment = commentIdx >= 0 ? value.slice(commentIdx) : '';

  let styledMain: JSX.Element | string = mainVal;
  if (/^(true|false|null|yes|no)$/i.test(mainVal.trim())) {
    styledMain = <span className="text-amber-400">{mainVal}</span>;
  } else if (/^\d+(\.\d+)?$/.test(mainVal.trim())) {
    styledMain = <span className="text-amber-400">{mainVal}</span>;
  } else if (/^["'].*["']$/.test(mainVal.trim())) {
    styledMain = <span className="text-green-400">{mainVal}</span>;
  } else if (mainVal.includes('|') || mainVal.includes('>')) {
    styledMain = <span className="text-[var(--text-secondary)]">{mainVal}</span>;
  }

  return (
    <>
      {styledMain}
      {comment && <span className="text-emerald-500/70">{comment}</span>}
    </>
  );
}

export function YamlViewer({ yaml, onClose }: YamlViewerProps) {
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  const highlighted = useMemo(() => highlightYaml(yaml), [yaml]);

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />

      {/* Panel — 80% width */}
      <div className="relative mx-auto w-[80%] h-[90%] my-auto flex flex-col bg-[var(--bg)] border border-[var(--border)] rounded-lg shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-2 border-b border-[var(--border)] rounded-t-lg bg-[var(--surface)]">
          <span className="text-sm font-semibold text-[var(--text)]">Workflow YAML</span>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-[var(--surface-hover)] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-auto p-6">
          <pre className="text-[13px] font-mono whitespace-pre leading-6">
            {highlighted}
          </pre>
        </div>
      </div>
    </div>
  );
}
