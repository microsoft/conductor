import { useEffect, useMemo, useState, useCallback } from 'react';
import { X, ChevronRight, ChevronDown } from 'lucide-react';

interface YamlViewerProps {
  yaml: string;
  onClose: () => void;
}

/** Compute indent level (number of leading spaces) for each line. */
function getIndent(line: string): number {
  const m = line.match(/^(\s*)/);
  return m ? m[1]!.length : 0;
}

/** For each line, find the range of child lines (more-indented block below it). */
function computeFoldRanges(lines: string[]): Map<number, number> {
  const ranges = new Map<number, number>();
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    if (!line.trim()) continue;
    const indent = getIndent(line);
    // Find the last contiguous line that's more indented (or blank)
    let end = i;
    for (let j = i + 1; j < lines.length; j++) {
      const jLine = lines[j]!;
      if (!jLine.trim()) { end = j; continue; }
      if (getIndent(jLine) > indent) { end = j; } else break;
    }
    if (end > i) ranges.set(i, end);
  }
  return ranges;
}

function highlightLine(line: string): JSX.Element | string {
  // Comment lines
  if (/^\s*#/.test(line)) {
    return <span className="text-emerald-500/70">{line}</span>;
  }

  // Lines with key: value
  const keyMatch = line.match(/^(\s*)(- )?([a-zA-Z_][\w.-]*)(:\s*)(.*)/);
  if (keyMatch) {
    const [, indent, dash, key, colon, value] = keyMatch;
    return (
      <span>
        {indent}{dash ?? ''}
        <span className="text-sky-400">{key}</span>
        <span className="text-[var(--text-muted)]">{colon}</span>
        {formatValue(value)}
      </span>
    );
  }

  // List items (- value)
  const listMatch = line.match(/^(\s*)(- )(.*)/);
  if (listMatch) {
    const [, indent, dash, value] = listMatch;
    return (
      <span>
        {indent}<span className="text-[var(--text-muted)]">{dash}</span>{formatValue(value)}
      </span>
    );
  }

  return <span>{line}</span>;
}

function formatValue(value: string): JSX.Element | string {
  if (!value) return '';
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
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  const lines = useMemo(() => yaml.split('\n'), [yaml]);
  const foldRanges = useMemo(() => computeFoldRanges(lines), [lines]);

  const toggleFold = useCallback((lineIdx: number) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(lineIdx)) next.delete(lineIdx);
      else next.add(lineIdx);
      return next;
    });
  }, []);

  // Build visible lines, skipping collapsed ranges
  const visibleLines = useMemo(() => {
    const result: { idx: number; line: string; foldable: boolean; isCollapsed: boolean }[] = [];
    let skip = -1;
    for (let i = 0; i < lines.length; i++) {
      if (i <= skip) continue;
      const foldEnd = foldRanges.get(i);
      const foldable = foldEnd != null;
      const isCollapsed = collapsed.has(i);
      result.push({ idx: i, line: lines[i]!, foldable, isCollapsed });
      if (isCollapsed && foldEnd != null) {
        skip = foldEnd;
      }
    }
    return result;
  }, [lines, foldRanges, collapsed]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />

      {/* Panel */}
      <div className="relative w-[80%] h-[90%] flex flex-col bg-[var(--bg)] border border-[var(--border)] rounded-lg shadow-2xl">
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
        <div className="flex-1 overflow-auto">
          <pre
            className="text-[13px] font-mono whitespace-pre leading-6"
            style={{ padding: '1rem' }}
          >
            {visibleLines.map(({ idx, line, foldable, isCollapsed }) => (
              <div key={idx} className="flex">
                {/* Fold toggle gutter */}
                <span
                  className="inline-flex items-center justify-center flex-shrink-0"
                  style={{ width: '1.25rem' }}
                >
                  {foldable ? (
                    <button
                      onClick={() => toggleFold(idx)}
                      className="text-[var(--text-muted)] hover:text-[var(--text)] p-0 leading-none"
                      style={{ background: 'none', border: 'none', cursor: 'pointer' }}
                    >
                      {isCollapsed
                        ? <ChevronRight className="w-3 h-3" />
                        : <ChevronDown className="w-3 h-3" />}
                    </button>
                  ) : null}
                </span>
                <span className="flex-1">
                  {highlightLine(line)}
                  {isCollapsed && (
                    <span
                      className="text-[var(--text-muted)] text-[11px] ml-2 px-1.5 py-0.5 rounded bg-[var(--surface-hover)] cursor-pointer"
                      onClick={() => toggleFold(idx)}
                    >
                      ···
                    </span>
                  )}
                </span>
              </div>
            ))}
          </pre>
        </div>
      </div>
    </div>
  );
}
