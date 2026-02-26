import { useState } from 'react';
import { formatOutput } from '@/lib/utils';
import { ChevronDown, ChevronRight, Copy, Check } from 'lucide-react';

interface OutputViewerProps {
  output: unknown;
  title?: string;
  defaultExpanded?: boolean;
  maxHeight?: string;
}

export function OutputViewer({ output, title = 'Output', defaultExpanded = true, maxHeight = '300px' }: OutputViewerProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [copied, setCopied] = useState(false);
  const text = formatOutput(output);

  if (!text) return null;

  const isJson = typeof output === 'object' && output !== null;

  const handleCopy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-[var(--text-muted)] hover:text-[var(--text)] transition-colors font-semibold"
        >
          {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
          {title}
        </button>
        {expanded && (
          <button
            onClick={handleCopy}
            className="flex items-center gap-1 text-[10px] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors"
            title="Copy to clipboard"
          >
            {copied ? <Check className="w-3 h-3 text-[var(--completed)]" /> : <Copy className="w-3 h-3" />}
          </button>
        )}
      </div>
      {expanded && (
        <pre
          className="bg-[var(--bg)] border border-[var(--border)] rounded-md p-3 font-mono text-[11px] leading-relaxed text-[var(--text)] overflow-auto whitespace-pre-wrap break-words"
          style={{ maxHeight }}
        >
          {isJson ? (
            <JsonHighlight text={text} />
          ) : (
            text
          )}
        </pre>
      )}
    </div>
  );
}

/** Simple JSON syntax highlighting */
function JsonHighlight({ text }: { text: string }) {
  // Simple regex-based highlighting for JSON
  const parts = text.split(/("(?:[^"\\]|\\.)*")/g);

  return (
    <>
      {parts.map((part, i) => {
        if (i % 2 === 1) {
          // It's a quoted string
          // Check if it's a key (followed by :) — rough heuristic
          const rest = parts.slice(i + 1).join('');
          const isKey = /^\s*:/.test(rest);
          return (
            <span key={i} className={isKey ? 'text-blue-400' : 'text-green-400'}>
              {part}
            </span>
          );
        }
        // Highlight numbers, booleans, null
        const highlighted = part.replace(
          /\b(true|false|null)\b|(-?\d+\.?\d*(?:e[+-]?\d+)?)/gi,
          (match, keyword, num) => {
            if (keyword) return `<span class="text-amber-400">${match}</span>`;
            if (num) return `<span class="text-purple-400">${match}</span>`;
            return match;
          },
        );
        return <span key={i} dangerouslySetInnerHTML={{ __html: highlighted }} />;
      })}
    </>
  );
}
