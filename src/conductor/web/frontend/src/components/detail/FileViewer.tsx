import { useState, useEffect, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import { X, FileText, Loader2, AlertTriangle } from 'lucide-react';

interface FileViewerProps {
  /** Relative file path to fetch from the workflow root */
  filePath: string;
  /** Called when the viewer should be closed */
  onClose: () => void;
}

interface FileData {
  path: string;
  content: string;
  size: number;
  extension: string;
}

const MARKDOWN_EXTENSIONS = new Set(['.md', '.markdown', '.mdx']);

export function FileViewer({ filePath, onClose }: FileViewerProps) {
  const [data, setData] = useState<FileData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchFile = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const encoded = filePath
        .split('/')
        .map((seg) => encodeURIComponent(seg))
        .join('/');
      const res = await fetch(`/api/files/${encoded}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.error || `HTTP ${res.status}`);
        return;
      }
      const json: FileData = await res.json();
      setData(json);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load file');
    } finally {
      setLoading(false);
    }
  }, [filePath]);

  useEffect(() => {
    fetchFile();
  }, [fetchFile]);

  // Close on Escape
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  const isMarkdown = data ? MARKDOWN_EXTENSIONS.has(data.extension) : false;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="relative flex flex-col w-[90vw] max-w-3xl max-h-[80vh] rounded-xl border border-[var(--border)] bg-[var(--surface)] shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-2 px-4 py-2.5 border-b border-[var(--border)] bg-[var(--surface-raised)] flex-shrink-0">
          <FileText className="w-4 h-4 text-[var(--text-muted)] flex-shrink-0" />
          <span className="text-xs font-medium text-[var(--text)] truncate flex-1" title={filePath}>
            {filePath}
          </span>
          {data && (
            <span className="text-[10px] text-[var(--text-muted)] flex-shrink-0 tabular-nums">
              {formatSize(data.size)}
            </span>
          )}
          <button
            onClick={onClose}
            className="p-1 rounded-md text-[var(--text-muted)] hover:text-[var(--text)] hover:bg-[var(--surface-hover)] transition-colors flex-shrink-0"
            title="Close (Esc)"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto px-5 py-4 min-h-0">
          {loading && (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-5 h-5 text-[var(--text-muted)] animate-spin" />
            </div>
          )}

          {error && (
            <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/30">
              <AlertTriangle className="w-4 h-4 text-red-400 flex-shrink-0" />
              <span className="text-xs text-red-300">{error}</span>
            </div>
          )}

          {data && !error && (
            isMarkdown ? (
              <div className="file-viewer-markdown text-xs leading-relaxed text-[var(--text)]">
                <MarkdownContent content={data.content} />
              </div>
            ) : (
              <pre className="font-mono text-[11px] leading-[1.6] text-[var(--text)] whitespace-pre-wrap break-words">
                {data.content}
              </pre>
            )
          )}
        </div>
      </div>
    </div>
  );
}

function MarkdownContent({ content }: { content: string }) {
  return (
    <ReactMarkdown
      components={{
        h1: ({ children }) => <h1 className="text-base font-bold mb-3 mt-2 text-[var(--text)]">{children}</h1>,
        h2: ({ children }) => <h2 className="text-sm font-bold mb-2 mt-3 text-[var(--text)]">{children}</h2>,
        h3: ({ children }) => <h3 className="text-xs font-bold mb-1.5 mt-2 text-[var(--text)]">{children}</h3>,
        p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
        ul: ({ children }) => <ul className="list-disc list-inside mb-2 space-y-1 ml-2">{children}</ul>,
        ol: ({ children }) => <ol className="list-decimal list-inside mb-2 space-y-1 ml-2">{children}</ol>,
        li: ({ children }) => <li>{children}</li>,
        code: ({ children, className }) => {
          const isBlock = className?.includes('language-');
          if (isBlock) {
            return (
              <code className="block bg-[var(--bg)] border border-[var(--border)] rounded px-3 py-2 font-mono text-[11px] my-2 overflow-x-auto whitespace-pre">
                {children}
              </code>
            );
          }
          return (
            <code className="bg-[var(--bg)] border border-[var(--border)] rounded px-1 py-0.5 font-mono text-[11px]">
              {children}
            </code>
          );
        },
        pre: ({ children }) => (
          <pre className="bg-[var(--bg)] border border-[var(--border)] rounded-md px-3 py-2.5 font-mono text-[11px] my-2 overflow-x-auto">
            {children}
          </pre>
        ),
        strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
        em: ({ children }) => <em className="italic">{children}</em>,
        a: ({ href, children }) => (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-400 hover:text-blue-300 underline underline-offset-2"
          >
            {children}
          </a>
        ),
        blockquote: ({ children }) => (
          <blockquote className="border-l-2 border-[var(--border)] pl-3 my-2 opacity-80">{children}</blockquote>
        ),
        hr: () => <hr className="border-[var(--border)] my-3" />,
        table: ({ children }) => (
          <div className="overflow-x-auto my-2">
            <table className="text-[11px] border-collapse w-full">{children}</table>
          </div>
        ),
        th: ({ children }) => (
          <th className="border border-[var(--border)] px-2 py-1 text-left bg-[var(--bg)] font-semibold">{children}</th>
        ),
        td: ({ children }) => (
          <td className="border border-[var(--border)] px-2 py-1">{children}</td>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
