import { useRef, useEffect, useState, useCallback } from 'react';
import { TerminalSquare, FileOutput, Activity, ChevronDown, ChevronUp, Copy, Check } from 'lucide-react';
import { useWorkflowStore, type LogEntry, type ActivityLogEntry } from '@/stores/workflow-store';
import { formatOutput, cn } from '@/lib/utils';

type Tab = 'log' | 'activity' | 'output';

export function OutputPane() {
  const eventLog = useWorkflowStore((s) => s.eventLog);
  const activityLog = useWorkflowStore((s) => s.activityLog);
  const workflowOutput = useWorkflowStore((s) => s.workflowOutput);
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const [activeTab, setActiveTab] = useState<Tab>('log');
  const [isCollapsed, setIsCollapsed] = useState(false);

  // Auto-switch to output tab when workflow completes with output
  useEffect(() => {
    if (workflowStatus === 'completed' && workflowOutput != null) {
      setActiveTab('output');
    }
  }, [workflowStatus, workflowOutput]);

  const hasOutput = workflowOutput != null;

  if (isCollapsed) {
    return (
      <div className="flex items-center bg-[var(--surface)] border-t border-[var(--border)] px-3 py-1">
        <button
          onClick={() => setIsCollapsed(false)}
          className="flex items-center gap-1.5 text-xs text-[var(--text-muted)] hover:text-[var(--text)] transition-colors"
        >
          <ChevronUp className="w-3 h-3" />
          <TerminalSquare className="w-3 h-3" />
          <span>Output</span>
          {activityLog.length > 0 && (
            <span className="text-[10px] text-[var(--text-muted)]">({activityLog.length})</span>
          )}
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-[var(--surface)] border-t border-[var(--border)]">
      {/* Tab bar */}
      <div className="flex items-center justify-between px-2 flex-shrink-0 border-b border-[var(--border)]">
        <div className="flex items-center gap-0.5">
          <TabButton
            active={activeTab === 'log'}
            onClick={() => setActiveTab('log')}
            icon={<TerminalSquare className="w-3 h-3" />}
            label="Log"
            count={eventLog.length}
          />
          <TabButton
            active={activeTab === 'activity'}
            onClick={() => setActiveTab('activity')}
            icon={<Activity className="w-3 h-3" />}
            label="Activity"
            count={activityLog.length}
          />
          <TabButton
            active={activeTab === 'output'}
            onClick={() => setActiveTab('output')}
            icon={<FileOutput className="w-3 h-3" />}
            label="Output"
            badge={hasOutput ? (workflowStatus === 'failed' ? 'error' : 'success') : undefined}
          />
        </div>
        <button
          onClick={() => setIsCollapsed(true)}
          className="p-1 rounded text-[var(--text-muted)] hover:text-[var(--text)] hover:bg-[var(--surface-hover)] transition-colors"
          title="Collapse panel"
        >
          <ChevronDown className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        {activeTab === 'activity' ? (
          <ActivityView entries={activityLog} />
        ) : activeTab === 'log' ? (
          <LogView entries={eventLog} />
        ) : (
          <OutputView output={workflowOutput} status={workflowStatus} />
        )}
      </div>
    </div>
  );
}

// --- Tab Button ---

function TabButton({
  active,
  onClick,
  icon,
  label,
  count,
  badge,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  count?: number;
  badge?: 'success' | 'error';
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'flex items-center gap-1.5 px-3 py-1.5 text-xs transition-colors border-b-2 -mb-px',
        active
          ? 'text-[var(--text)] border-[var(--accent)]'
          : 'text-[var(--text-muted)] border-transparent hover:text-[var(--text-secondary)]',
      )}
    >
      {icon}
      <span>{label}</span>
      {count != null && count > 0 && (
        <span className="text-[10px] text-[var(--text-muted)] tabular-nums">{count}</span>
      )}
      {badge && (
        <span
          className={cn(
            'w-1.5 h-1.5 rounded-full',
            badge === 'success' ? 'bg-[var(--completed)]' : 'bg-[var(--failed)]',
          )}
        />
      )}
    </button>
  );
}

// --- Activity View (streaming firehose) ---

const ACTIVITY_TYPE_STYLES: Record<string, { color: string; label: string; labelColor: string }> = {
  reasoning:      { color: 'text-indigo-400/70', label: 'THINK',  labelColor: 'text-indigo-500' },
  'tool-start':   { color: 'text-blue-400',      label: 'TOOL →', labelColor: 'text-blue-500' },
  'tool-complete': { color: 'text-green-400',     label: 'TOOL ←', labelColor: 'text-green-600' },
  turn:           { color: 'text-amber-400',      label: 'STEP',   labelColor: 'text-amber-500' },
  message:        { color: 'text-[var(--text)]',  label: 'MSG',    labelColor: 'text-[var(--text-muted)]' },
  prompt:         { color: 'text-cyan-400/70',    label: 'PROMPT', labelColor: 'text-cyan-600' },
};

function ActivityView({ entries }: { entries: ActivityLogEntry[] }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);
  const selectNode = useWorkflowStore((s) => s.selectNode);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    autoScrollRef.current = atBottom;
  }, []);

  useEffect(() => {
    if (scrollRef.current && autoScrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [entries.length]);

  if (entries.length === 0) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-xs text-[var(--text-muted)]">Waiting for agent activity…</p>
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      className="h-full overflow-y-auto font-mono text-[11px] leading-[1.6] px-3 py-2"
    >
      {entries.map((entry, i) => {
        const style = ACTIVITY_TYPE_STYLES[entry.type] || ACTIVITY_TYPE_STYLES.message;
        const time = formatTimestamp(entry.timestamp);

        return (
          <div key={i} className="group">
            <div className="flex gap-1.5 hover:bg-[var(--surface-hover)] rounded px-1 -mx-1">
              <span className="text-[var(--text-muted)] flex-shrink-0 select-none tabular-nums">{time}</span>
              <span className={cn('flex-shrink-0 w-[5ch] text-[10px] font-semibold tabular-nums select-none', style!.labelColor)}>{style!.label}</span>
              <button
                onClick={() => selectNode(entry.source)}
                className="text-[var(--text-secondary)] flex-shrink-0 min-w-[8ch] max-w-[16ch] truncate hover:text-[var(--accent)] hover:underline transition-colors text-left"
                title={`Select ${entry.source}`}
              >
                {entry.source}
              </button>
              <span className={cn('break-words min-w-0', style!.color,
                entry.type === 'reasoning' && 'italic',
              )}>
                {entry.message}
              </span>
            </div>
            {entry.detail && (
              <div className="ml-[calc(7ch+5ch+8ch+1rem)] px-2 py-1 my-0.5 bg-[var(--bg)] rounded text-[10px] text-[var(--text-muted)] whitespace-pre-wrap break-words max-h-24 overflow-y-auto border-l-2 border-[var(--border)]">
                {entry.detail}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// --- Log View (high-level events) ---

const LEVEL_STYLES: Record<string, { color: string; icon: string }> = {
  info: { color: 'text-blue-400', icon: '›' },
  success: { color: 'text-green-400', icon: '✓' },
  error: { color: 'text-red-400', icon: '✗' },
  warning: { color: 'text-amber-400', icon: '⚠' },
  debug: { color: 'text-[var(--text-muted)]', icon: '·' },
};

function LogView({ entries }: { entries: LogEntry[] }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    autoScrollRef.current = atBottom;
  }, []);

  useEffect(() => {
    if (scrollRef.current && autoScrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [entries.length]);

  if (entries.length === 0) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-xs text-[var(--text-muted)]">Waiting for events…</p>
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      className="h-full overflow-y-auto font-mono text-[11px] leading-[1.6] px-3 py-2"
    >
      {entries.map((entry, i) => {
        const style = LEVEL_STYLES[entry.level] || LEVEL_STYLES.info;
        const time = formatTimestamp(entry.timestamp);

        return (
          <div key={i} className="flex gap-2 hover:bg-[var(--surface-hover)] rounded px-1 -mx-1">
            <span className="text-[var(--text-muted)] flex-shrink-0 select-none tabular-nums">{time}</span>
            <span className={cn('flex-shrink-0 w-3 text-center select-none', style!.color)}>{style!.icon}</span>
            <span className="text-[var(--text-secondary)] flex-shrink-0 min-w-[8ch] max-w-[16ch] truncate">{entry.source}</span>
            <span className={cn('break-words', entry.level === 'error' ? 'text-red-400' : entry.level === 'success' ? 'text-green-400' : 'text-[var(--text)]')}>
              {entry.message}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function formatTimestamp(ts: number): string {
  const d = new Date(ts * 1000);
  const h = d.getHours().toString().padStart(2, '0');
  const m = d.getMinutes().toString().padStart(2, '0');
  const s = d.getSeconds().toString().padStart(2, '0');
  return `${h}:${m}:${s}`;
}

// --- Output View ---

function OutputView({ output, status }: { output: unknown; status: string }) {
  const [copied, setCopied] = useState(false);

  const text = formatOutput(output);

  const handleCopy = async () => {
    if (!text) return;
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (output == null) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-xs text-[var(--text-muted)]">
          {status === 'running' ? 'Workflow running — output will appear when complete…' :
           status === 'failed' ? 'Workflow failed — no output produced' :
           'No output yet'}
        </p>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-1 border-b border-[var(--border-subtle)] flex-shrink-0">
        <span className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider font-semibold">
          Workflow Result
        </span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 text-[10px] text-[var(--text-muted)] hover:text-[var(--text)] transition-colors px-1.5 py-0.5 rounded hover:bg-[var(--surface-hover)]"
          title="Copy to clipboard"
        >
          {copied ? (
            <>
              <Check className="w-3 h-3 text-[var(--completed)]" />
              <span className="text-[var(--completed)]">Copied</span>
            </>
          ) : (
            <>
              <Copy className="w-3 h-3" />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>

      {/* JSON output */}
      <div className="flex-1 overflow-auto px-3 py-2">
        <pre className="font-mono text-[11px] leading-relaxed text-[var(--text)] whitespace-pre-wrap break-words">
          {typeof output === 'object' ? (
            <JsonHighlight text={text} />
          ) : (
            text
          )}
        </pre>
      </div>
    </div>
  );
}

/** Simple JSON syntax highlighting */
function JsonHighlight({ text }: { text: string }) {
  const parts = text.split(/("(?:[^"\\]|\\.)*")/g);

  return (
    <>
      {parts.map((part, i) => {
        if (i % 2 === 1) {
          const rest = parts.slice(i + 1).join('');
          const isKey = /^\s*:/.test(rest);
          return (
            <span key={i} className={isKey ? 'text-blue-400' : 'text-green-400'}>
              {part}
            </span>
          );
        }
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
