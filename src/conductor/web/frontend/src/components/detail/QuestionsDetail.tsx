import { useState } from 'react';
import { Check, ListChecks, SkipForward } from 'lucide-react';
import { GateDetail, PromptMarkdown } from './GateDetail';
import { FileViewer } from './FileViewer';
import { MetadataGrid } from './MetadataGrid';
import type { NodeData } from '@/stores/workflow-store';

interface QuestionsDetailProps {
  node: NodeData;
}

/**
 * Detail view for a `type: questions` node.
 *
 * While waiting, each question is presented through the same gate channel the
 * engine already uses, so `GateDetail` renders it unchanged — the progress
 * header travels inside the prompt markdown and Back/Skip arrive as ordinary
 * choices. Only the completed state differs: a gate resolves to one choice,
 * whereas this node resolves to a set of answers.
 */
export function QuestionsDetail({ node }: QuestionsDetailProps) {
  const [viewingFile, setViewingFile] = useState<string | null>(null);

  const isWaiting = node.status === 'waiting';
  const isCompleted = node.status === 'completed';

  if (isWaiting) {
    return (
      <div className="space-y-3">
        <QuestionsProgress node={node} />
        {node.questions_reject_reason && (
          <div className="px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/30 text-[11px] text-red-400">
            {node.questions_reject_reason}
          </div>
        )}
        <GateDetail node={node} />
      </div>
    );
  }

  if (!isCompleted) {
    return (
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <span className="text-xs text-[var(--text-muted)]">Questions</span>
          <span className="text-[10px] text-[var(--text-muted)] capitalize">({node.status})</span>
        </div>
        {node.prompt && (
          <div className="border-l-2 border-[var(--border)] pl-3 py-0.5">
            <PromptMarkdown text={node.prompt} muted={true} onFileClick={setViewingFile} />
          </div>
        )}
        {viewingFile && <FileViewer filePath={viewingFile} onClose={() => setViewingFile(null)} />}
      </div>
    );
  }

  const answered = node.questions_answered_count ?? 0;
  const skipped = node.questions_skipped_count ?? 0;
  const outcome = node.questions_outcome ?? 'completed';

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg bg-green-500/10 border border-green-500/30">
        <Check className="w-3.5 h-3.5 text-green-400 flex-shrink-0" />
        <span className="text-xs font-semibold text-green-400 tracking-wide">
          {outcome === 'aborted'
            ? 'Questions Aborted'
            : outcome === 'skipped_remaining'
              ? 'Remaining Questions Skipped'
              : 'Questions Completed'}
        </span>
      </div>

      <MetadataGrid
        items={[
          { label: 'Answered', value: answered },
          { label: 'Skipped', value: skipped },
          { label: 'Outcome', value: outcome },
        ]}
      />

      {viewingFile && <FileViewer filePath={viewingFile} onClose={() => setViewingFile(null)} />}
    </div>
  );
}

/** Answered/skipped counters shown above the active question. */
function QuestionsProgress({ node }: { node: NodeData }) {
  const total = node.questions_total ?? 0;
  const answered = node.questions_answered_count ?? 0;
  const skipped = node.questions_skipped_count ?? 0;
  const done = answered + skipped;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2 text-[10px] text-[var(--text-muted)]">
        <ListChecks className="w-3 h-3 flex-shrink-0" />
        <span>
          {done} of {total} answered
        </span>
        {skipped > 0 && (
          <span className="flex items-center gap-1">
            <SkipForward className="w-3 h-3" />
            {skipped} skipped
          </span>
        )}
      </div>
      <div className="h-1 rounded-full bg-[var(--border)] overflow-hidden">
        <div
          className="h-full bg-amber-500 transition-all duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
