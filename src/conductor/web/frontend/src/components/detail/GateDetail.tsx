import { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { Check, Loader2, Send, FileText } from 'lucide-react';
import { MetadataGrid } from './MetadataGrid';
import type { NodeData } from '@/stores/workflow-store';
import { useWorkflowStore } from '@/stores/workflow-store';

interface GateDetailProps {
  node: NodeData;
}

export function GateDetail({ node }: GateDetailProps) {
  const sendGateResponse = useWorkflowStore((s) => s.sendGateResponse);
  const wsStatus = useWorkflowStore((s) => s.wsStatus);

  const [selectedValue, setSelectedValue] = useState<string | null>(null);
  const [promptForValue, setPromptForValue] = useState('');
  const [pendingPromptFor, setPendingPromptFor] = useState<string | null>(null);
  const [isSending, setIsSending] = useState(false);

  const isWaiting = node.status === 'waiting';
  const isCompleted = node.status === 'completed';

  // Reset local state when the gate transitions back to 'waiting' (re-entry in a loop)
  useEffect(() => {
    if (isWaiting) {
      setSelectedValue(null);
      setPromptForValue('');
      setPendingPromptFor(null);
      setIsSending(false);
    }
  }, [isWaiting]);

  const canInteract = isWaiting && wsStatus === 'connected' && selectedValue === null;

  const handleOptionClick = (value: string, promptFor?: string | null) => {
    if (!canInteract) return;

    if (promptFor) {
      // Show text input before sending
      setSelectedValue(value);
      setPendingPromptFor(promptFor);
      return;
    }

    // Send immediately
    setSelectedValue(value);
    setIsSending(true);
    sendGateResponse(node.name, value);
  };

  const handlePromptForSubmit = () => {
    if (selectedValue === null || pendingPromptFor === null) return;
    const additionalInput: Record<string, string> = { [pendingPromptFor]: promptForValue };
    setIsSending(true);
    sendGateResponse(node.name, selectedValue, additionalInput);
    setPendingPromptFor(null);
  };

  // Use option_details for interactive buttons if available
  const optionDetails = node.option_details;

  // Find the selected option's label for completed state
  const selectedOptDetail = optionDetails?.find((o) => o.value === node.selected_option);
  const selectedLabel = selectedOptDetail?.label || node.selected_option;

  return (
    <div className="space-y-3">
      {/* --- WAITING STATE --- */}
      {isWaiting && (
        <>
          {/* Amber "Decision Required" banner */}
          <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/30">
            <span className="relative flex h-2.5 w-2.5 flex-shrink-0">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-500" />
            </span>
            <span className="text-xs font-semibold text-amber-400 tracking-wide">
              Decision Required
            </span>
          </div>

          {/* Prompt callout */}
          {node.prompt && (
            <div className="border-l-2 border-amber-500/50 pl-3 py-0.5">
              <PromptMarkdown text={node.prompt} muted={false} onFileClick={setViewingFile} />
            </div>
          )}

          {/* Interactive option buttons */}
          {optionDetails && optionDetails.length > 0 && (
            <div className="space-y-2">
              <div className="flex flex-col gap-1.5">
                {optionDetails.map((opt) => {
                  const isSelected = selectedValue === opt.value;
                  const isDimmed = selectedValue !== null && !isSelected;

                  return (
                    <button
                      key={opt.value}
                      disabled={!canInteract && !isSelected}
                      onClick={() => handleOptionClick(opt.value, opt.prompt_for)}
                      className={`w-full text-left px-3 py-2.5 rounded-lg border transition-all duration-150 ${
                        isSelected
                          ? 'border-green-500/60 bg-green-500/10'
                          : isDimmed
                            ? 'border-[var(--border)] opacity-40 cursor-default'
                            : 'border-[var(--border)] bg-[var(--surface)] hover:border-amber-400/60 hover:bg-amber-500/5 cursor-pointer group'
                      }`}
                    >
                      <div className="flex items-center gap-2.5">
                        {/* Radio indicator / check */}
                        <div className="flex-shrink-0">
                          {isSelected ? (
                            <div className="w-4 h-4 rounded-full bg-green-500 flex items-center justify-center">
                              <Check className="w-2.5 h-2.5 text-white" strokeWidth={3} />
                            </div>
                          ) : (
                            <div
                              className={`w-4 h-4 rounded-full border-2 transition-colors ${
                                isDimmed
                                  ? 'border-[var(--border)]'
                                  : 'border-[var(--border)] group-hover:border-amber-400'
                              }`}
                            />
                          )}
                        </div>
                        {/* Label */}
                        <div className="flex-1 min-w-0">
                          <span
                            className={`text-xs font-medium ${
                              isSelected ? 'text-green-400' : 'text-[var(--text)]'
                            }`}
                          >
                            {opt.label}
                          </span>
                        </div>
                        {/* Route hint */}
                        {opt.route && (
                          <span className="text-[10px] text-[var(--text-muted)] flex-shrink-0">
                            → {opt.route}
                          </span>
                        )}
                      </div>
                    </button>
                  );
                })}
              </div>

              {/* Sending indicator */}
              {isSending && !pendingPromptFor && (
                <div className="flex items-center gap-2 px-1">
                  <Loader2 className="w-3 h-3 text-green-400 animate-spin" />
                  <span className="text-[10px] text-green-400">Sending...</span>
                </div>
              )}

              {/* Helper text when no selection yet */}
              {canInteract && (
                <p className="text-[10px] text-[var(--text-muted)] px-1">
                  Select an option to continue the workflow
                </p>
              )}
            </div>
          )}

          {/* Fallback: display-only options when no option_details */}
          {!optionDetails && node.options && node.options.length > 0 && (
            <div className="space-y-1.5">
              <h4 className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold">
                Options
              </h4>
              <div className="flex flex-wrap gap-1.5">
                {node.options.map((opt) => (
                  <span
                    key={opt}
                    className="text-[11px] px-2 py-0.5 rounded border border-[var(--border)] text-[var(--text-muted)]"
                  >
                    {opt}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* prompt_for text input card */}
          {pendingPromptFor && (
            <div className="rounded-lg border border-[var(--border)] bg-[var(--bg)] overflow-hidden">
              <div className="px-3 py-2 border-b border-[var(--border)] bg-[var(--surface)]">
                <h4 className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold">
                  {pendingPromptFor}
                </h4>
              </div>
              <div className="p-3 space-y-2">
                <input
                  type="text"
                  value={promptForValue}
                  onChange={(e) => setPromptForValue(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handlePromptForSubmit()}
                  placeholder={`Enter ${pendingPromptFor}...`}
                  className="w-full text-xs px-3 py-2 rounded-lg border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] outline-none focus:border-amber-400 transition-colors"
                  autoFocus
                />
                <div className="flex items-center justify-between">
                  <span className="text-[10px] text-[var(--text-muted)]">
                    Press Enter or click Submit
                  </span>
                  <button
                    onClick={handlePromptForSubmit}
                    className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-amber-500 text-white hover:bg-amber-600 transition-colors font-medium"
                  >
                    <Send className="w-3 h-3" />
                    Submit
                  </button>
                </div>
              </div>
            </div>
          )}
        </>
      )}

      {/* --- COMPLETED STATE --- */}
      {isCompleted && (
        <>
          {/* Green "Completed" banner */}
          <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-green-500/10 border border-green-500/30">
            <Check className="w-3.5 h-3.5 text-green-400 flex-shrink-0" />
            <span className="text-xs font-semibold text-green-400 tracking-wide">
              Decision Completed
            </span>
          </div>

          {/* Prompt (dimmed, for context) */}
          {node.prompt && (
            <div className="border-l-2 border-[var(--border)] pl-3 py-0.5">
              <PromptMarkdown text={node.prompt} muted={true} onFileClick={setViewingFile} />
            </div>
          )}

          {/* Selected option card */}
          {selectedLabel && (
            <div className="flex items-center gap-2.5 px-3 py-2.5 rounded-lg border border-green-500/30 bg-green-500/5">
              <div className="w-4 h-4 rounded-full bg-green-500 flex items-center justify-center flex-shrink-0">
                <Check className="w-2.5 h-2.5 text-white" strokeWidth={3} />
              </div>
              <span className="text-xs font-medium text-[var(--text)]">{selectedLabel}</span>
              {node.route && (
                <span className="ml-auto text-[10px] text-[var(--text-muted)]">
                  → {node.route}
                </span>
              )}
            </div>
          )}

          {/* Unselected options (dimmed, for context) */}
          {optionDetails && optionDetails.length > 1 && (
            <div className="space-y-1">
              {optionDetails
                .filter((o) => o.value !== node.selected_option)
                .map((opt) => (
                  <div
                    key={opt.value}
                    className="flex items-center gap-2.5 px-3 py-2 rounded-lg opacity-35"
                  >
                    <div className="w-4 h-4 rounded-full border-2 border-[var(--border)] flex-shrink-0" />
                    <span className="text-xs text-[var(--text-muted)]">{opt.label}</span>
                    {opt.route && (
                      <span className="ml-auto text-[10px] text-[var(--text-muted)]">
                        → {opt.route}
                      </span>
                    )}
                  </div>
                ))}
            </div>
          )}

          {/* Display-only options fallback (no option_details) */}
          {!optionDetails && node.options && node.options.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {node.options.map((opt) => (
                <span
                  key={opt}
                  className={`text-[11px] px-2.5 py-1 rounded-lg border ${
                    opt === node.selected_option
                      ? 'border-green-500/30 text-green-400 bg-green-500/5'
                      : 'border-[var(--border)] text-[var(--text-muted)] opacity-40'
                  }`}
                >
                  {opt === node.selected_option && '✓ '}
                  {opt}
                </span>
              ))}
            </div>
          )}

          {/* Metadata (route, additional input) */}
          <CompletedMetadata node={node} />
        </>
      )}

      {/* --- OTHER STATES (pending, failed, etc.) --- */}
      {!isWaiting && !isCompleted && (
        <>
          <div className="flex items-center gap-2">
            <span className="text-xs text-[var(--text-muted)]">Human Gate</span>
            <span className="text-[10px] text-[var(--text-muted)] capitalize">({node.status})</span>
          </div>

          {node.prompt && (
            <div className="border-l-2 border-[var(--border)] pl-3 py-0.5">
              <PromptMarkdown text={node.prompt} muted={true} />
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** Returns true if the href looks like a relative file path (not a URL, anchor, or scheme). */
function isRelativeFileLink(href: string | undefined): href is string {
  if (!href) return false;
  // Reject URLs with schemes, protocol-relative, anchor-only, absolute paths
  if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return false;  // http:, mailto:, javascript:, file:, etc.
  if (href.startsWith('//')) return false;                 // protocol-relative
  if (href.startsWith('#')) return false;                  // anchor-only
  if (href.startsWith('/') || href.startsWith('\\')) return false; // absolute
  return true;
}

/** Renders prompt text as markdown with dashboard-consistent styling. */
function PromptMarkdown({
  text,
  muted,
}: {
  text: string;
  muted: boolean;
}) {
  const textColor = muted ? 'text-[var(--text-muted)]' : 'text-[var(--text)]';

  return (
    <div className={`gate-markdown text-xs leading-relaxed ${textColor}`}>
      <ReactMarkdown
        components={{
          // Headings
          h1: ({ children }) => (
            <h1 className="text-sm font-bold mb-2 mt-1">{children}</h1>
          ),
          h2: ({ children }) => (
            <h2 className="text-xs font-bold mb-1.5 mt-1">{children}</h2>
          ),
          h3: ({ children }) => (
            <h3 className="text-xs font-semibold mb-1 mt-1">{children}</h3>
          ),
          // Paragraphs
          p: ({ children }) => <p className="mb-1.5 last:mb-0">{children}</p>,
          // Lists
          ul: ({ children }) => (
            <ul className="list-disc list-inside mb-1.5 space-y-0.5">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="list-decimal list-inside mb-1.5 space-y-0.5">{children}</ol>
          ),
          li: ({ children }) => <li>{children}</li>,
          // Inline code
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
          // Code blocks
          pre: ({ children }) => (
            <pre className="bg-[var(--bg)] border border-[var(--border)] rounded-md px-2.5 py-2 font-mono text-[11px] my-1.5 overflow-x-auto">
              {children}
            </pre>
          ),
          // Bold / italic
          strong: ({ children }) => (
            <strong className="font-semibold">{children}</strong>
          ),
          em: ({ children }) => <em className="italic">{children}</em>,
          // Links — open relative file links in VSCode
          a: ({ href, children }) => {
            if (isRelativeFileLink(href)) {
              const vscodeUrl = `vscode://file/${href}`;
              return (
                <a
                  href={vscodeUrl}
                  className="inline-flex items-center gap-0.5 text-blue-400 hover:text-blue-300 underline underline-offset-2"
                  title={`Open ${href} in VSCode`}
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
          // Blockquote
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-[var(--border)] pl-2.5 my-1.5 opacity-80">
              {children}
            </blockquote>
          ),
          // Horizontal rule
          hr: () => <hr className="border-[var(--border)] my-2" />,
          // Tables
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

function CompletedMetadata({ node }: { node: NodeData }) {
  const items: Array<{ label: string; value: string | number | null | undefined }> = [];

  if (node.route) items.push({ label: 'Route', value: `→ ${node.route}` });
  if (node.additional_input) {
    const inputStr =
      typeof node.additional_input === 'object'
        ? JSON.stringify(node.additional_input)
        : node.additional_input;
    items.push({ label: 'Additional Input', value: inputStr });
  }

  if (items.length === 0) return null;

  return <MetadataGrid items={items} />;
}
