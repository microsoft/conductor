import { useRef, useEffect, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { ActivityEntry } from '@/stores/workflow-store';
import { cn } from '@/lib/utils';

interface ActivityStreamProps {
  activity: ActivityEntry[];
}

export function ActivityStream({ activity }: ActivityStreamProps) {
  const [expanded, setExpanded] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll on new entries
  useEffect(() => {
    if (scrollRef.current && expanded) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [activity.length, expanded]);

  if (activity.length === 0) return null;

  return (
    <div className="space-y-1.5">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-[var(--text-muted)] hover:text-[var(--text)] transition-colors font-semibold"
      >
        {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        Activity ({activity.length})
      </button>
      {expanded && (
        <div
          ref={scrollRef}
          className="max-h-[400px] overflow-y-auto space-y-0.5"
        >
          {activity.map((entry, i) => (
            <ActivityEntryRow key={i} entry={entry} />
          ))}
        </div>
      )}
    </div>
  );
}

function ActivityEntryRow({ entry }: { entry: ActivityEntry }) {
  const typeColors: Record<string, string> = {
    reasoning: 'text-indigo-400/70',
    'tool-start': 'text-blue-400',
    'tool-complete': 'text-green-400',
    turn: 'text-amber-400',
    message: 'text-[var(--text)]',
  };

  return (
    <div className={cn(
      'py-1.5 px-2 rounded text-[11px] leading-relaxed border-b border-[var(--border-subtle)] last:border-b-0',
    )}>
      <div className="flex items-start gap-1.5">
        <span className="w-4 text-center flex-shrink-0">{entry.icon}</span>
        <span className="text-[var(--text-muted)] uppercase text-[9px] font-semibold tracking-wider w-12 flex-shrink-0 pt-px">
          {entry.label}
        </span>
        <span className={cn('break-words', typeColors[entry.type] || 'text-[var(--text)]')}>
          {entry.text}
        </span>
      </div>
      {entry.detail && (
        <div className="mt-1 ml-[4.25rem] px-2 py-1 bg-[var(--bg)] rounded text-[10px] font-mono text-[var(--text-muted)] whitespace-pre-wrap break-words max-h-24 overflow-y-auto">
          {entry.detail}
        </div>
      )}
    </div>
  );
}
