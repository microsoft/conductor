import { useState, useRef, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Send, MessageCircle, FileText } from 'lucide-react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { NodeData } from '@/stores/workflow-store';

interface DialogDetailProps {
  node: NodeData;
}

/** Returns true if the href looks like a relative file path (not a URL, anchor, or scheme). */
function isRelativeFileLink(href: string | undefined): href is string {
  if (!href) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return false;
  if (href.startsWith('//')) return false;
  if (href.startsWith('#')) return false;
  if (href.startsWith('/') || href.startsWith('\\')) return false;
  return true;
}

function DialogMarkdown({ text }: { text: string }) {
  return (
    <div className="dialog-markdown text-xs leading-relaxed text-[var(--text)]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => <h1 className="text-sm font-bold mb-2 mt-1">{children}</h1>,
          h2: ({ children }) => <h2 className="text-xs font-bold mb-1.5 mt-1">{children}</h2>,
          h3: ({ children }) => <h3 className="text-xs font-semibold mb-1 mt-1">{children}</h3>,
          p: ({ children }) => <p className="mb-1.5 last:mb-0">{children}</p>,
          ul: ({ children }) => <ul className="list-disc list-inside mb-1.5 space-y-0.5">{children}</ul>,
          ol: ({ children }) => <ol className="list-decimal list-inside mb-1.5 space-y-0.5">{children}</ol>,
          li: ({ children }) => <li>{children}</li>,
          code: ({ children, className }) => {
            const isBlock = className?.includes('language-');
            if (isBlock) {
              return (
                <code className="block bg-[var(--bg)] border border-[var(--border)] rounded px-2 py-1.5 font-mono text-[11px] my-1 overflow-x-auto whitespace-pre">
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
            <pre className="bg-[var(--bg)] border border-[var(--border)] rounded-md px-2.5 py-2 font-mono text-[11px] my-1.5 overflow-x-auto">
              {children}
            </pre>
          ),
          strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
          em: ({ children }) => <em className="italic">{children}</em>,
          a: ({ href, children }) => {
            if (isRelativeFileLink(href)) {
              const handleOpenInEditor = (e: React.MouseEvent) => {
                e.preventDefault();
                fetch('/api/open-file', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ path: href }),
                });
              };
              return (
                <a
                  href="#"
                  onClick={handleOpenInEditor}
                  className="inline-flex items-center gap-0.5 text-blue-400 hover:text-blue-300 underline underline-offset-2"
                  title={`Open ${href} in default editor`}
                >
                  <FileText className="w-3 h-3 inline flex-shrink-0" />
                  {children}
                </a>
              );
            }
            return (
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-400 hover:text-blue-300 underline underline-offset-2"
              >
                {children}
              </a>
            );
          },
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-[var(--border)] pl-2.5 my-1.5 opacity-80">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="border-[var(--border)] my-2" />,
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
        {text}
      </ReactMarkdown>
    </div>
  );
}

export function DialogDetail({ node }: DialogDetailProps) {
  const sendDialogMessage = useWorkflowStore((s) => s.sendDialogMessage);
  const wsStatus = useWorkflowStore((s) => s.wsStatus);

  const [inputValue, setInputValue] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const isActive = node.dialog_active === true;
  const dialogId = node.dialog_id || '';
  const messages = node.dialog_messages || [];
  const canSend = isActive && wsStatus === 'connected';

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length, node.dialog_awaiting_response]);

  const handleSend = () => {
    if (!inputValue.trim() || !canSend) return;
    sendDialogMessage(node.name, dialogId, inputValue.trim());
    setInputValue('');
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header banner */}
      {isActive ? (
        <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-fuchsia-500/10 border border-fuchsia-500/30 mb-3 flex-shrink-0">
          <span className="relative flex h-2.5 w-2.5 flex-shrink-0">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-fuchsia-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-fuchsia-500" />
          </span>
          <span className="text-xs font-semibold text-fuchsia-400 tracking-wide">
            Dialog Mode
          </span>
          <span className="ml-auto text-[10px] text-[var(--text-muted)]">
            {messages.length} message{messages.length !== 1 ? 's' : ''}
          </span>
        </div>
      ) : (
        <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-[var(--surface)] border border-[var(--border)] mb-3 flex-shrink-0">
          <MessageCircle className="w-3.5 h-3.5 text-[var(--text-muted)]" />
          <span className="text-xs font-semibold text-[var(--text-muted)] tracking-wide">
            Dialog Completed
          </span>
          <span className="ml-auto text-[10px] text-[var(--text-muted)]">
            {messages.length} message{messages.length !== 1 ? 's' : ''}
          </span>
        </div>
      )}

      {/* Messages area */}
      <div className="flex-1 overflow-y-auto space-y-3 min-h-0 mb-3">
        {messages.map((msg, idx) => (
          <div
            key={idx}
            className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className={`max-w-[85%] rounded-lg px-3 py-2 ${
                msg.role === 'agent'
                  ? 'bg-amber-500/10 border border-amber-500/30'
                  : 'bg-blue-500/10 border border-blue-500/30'
              }`}
            >
              <div className="text-[10px] font-semibold mb-1 text-[var(--text-muted)]">
                {msg.role === 'agent' ? node.name : 'You'}
              </div>
              <DialogMarkdown text={msg.content} />
            </div>
          </div>
        ))}
        {node.dialog_awaiting_response && (
          <div className="flex justify-start">
            <div className="max-w-[85%] rounded-lg px-3 py-2 bg-amber-500/10 border border-amber-500/30">
              <div className="text-[10px] font-semibold mb-1 text-[var(--text-muted)]">{node.name}</div>
              <div className="flex gap-1 items-center h-4">
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400/60 animate-bounce [animation-delay:0ms]" />
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400/60 animate-bounce [animation-delay:150ms]" />
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400/60 animate-bounce [animation-delay:300ms]" />
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input area (shown when dialog is active) */}
      {isActive && (
        <div className="flex-shrink-0 border-t border-[var(--border)] pt-3">
          <div className="flex gap-2">
            <input
              type="text"
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type your message..."
              className="flex-1 text-xs px-3 py-2 rounded-lg border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] outline-none focus:border-fuchsia-400 transition-colors"
              disabled={!canSend}
              autoFocus
            />
            <button
              onClick={handleSend}
              disabled={!canSend || !inputValue.trim()}
              className="flex items-center justify-center gap-1.5 text-xs px-8 py-2 rounded-lg bg-fuchsia-500 text-white hover:bg-fuchsia-600 transition-colors font-medium disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Send className="w-3 h-3" />
              Send
            </button>
          </div>
          <p className="text-[10px] text-[var(--text-muted)] mt-1.5 px-1">
            Press Enter to send · Type &quot;done&quot; to end dialog
          </p>
        </div>
      )}
    </div>
  );
}
