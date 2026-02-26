import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { MetadataGrid, buildAgentMetadata } from './MetadataGrid';
import { OutputViewer } from './OutputViewer';
import { ActivityStream } from './ActivityStream';
import type { NodeData, IterationSnapshot } from '@/stores/workflow-store';

import { NODE_STATUS_HEX } from '@/lib/constants';
import type { NodeStatus } from '@/lib/constants';

interface AgentDetailProps {
  node: NodeData;
}

export function AgentDetail({ node }: AgentDetailProps) {
  const status = node.status as NodeStatus;
  const statusColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;
  const hasHistory = node.iterationHistory && node.iterationHistory.length > 0;

  return (
    <div className="space-y-4">
      {/* Status badge */}
      <div className="flex items-center gap-2">
        <span
          className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider"
          style={{
            backgroundColor: `${statusColor}20`,
            color: statusColor,
          }}
        >
          {status}
        </span>
        <span className="text-xs text-[var(--text-muted)]">Agent</span>
      </div>

      {/* Current iteration */}
      {hasHistory ? (
        <IterationSection
          label={`Iteration ${node.iteration ?? '?'} (current)`}
          defaultExpanded={true}
          status={status}
          snapshot={{
            iteration: node.iteration ?? 0,
            prompt: node.prompt,
            output: node.output,
            elapsed: node.elapsed,
            model: node.model,
            tokens: node.tokens,
            input_tokens: node.input_tokens,
            output_tokens: node.output_tokens,
            cost_usd: node.cost_usd,
            activity: node.activity,
            error_type: node.error_type,
            error_message: node.error_message,
          }}
        />
      ) : (
        <>
          {/* Metadata */}
          <MetadataGrid items={buildAgentMetadata(node)} />

          {/* Prompt */}
          {node.prompt && (
            <OutputViewer output={node.prompt} title="Input / Prompt" defaultExpanded={true} />
          )}

          {/* Activity stream */}
          <ActivityStream activity={node.activity} defaultExpanded={status !== 'completed'} />

          {/* Output */}
          {node.output != null && (
            <OutputViewer output={node.output} title="Output" />
          )}
        </>
      )}

      {/* Previous iterations (most recent first) */}
      {hasHistory &&
        [...node.iterationHistory!].reverse().map((snap) => (
          <IterationSection
            key={snap.iteration}
            label={`Iteration ${snap.iteration}`}
            defaultExpanded={false}
            status={status}
            snapshot={snap}
          />
        ))}
    </div>
  );
}

interface IterationSectionProps {
  label: string;
  defaultExpanded: boolean;
  snapshot: IterationSnapshot;
  status: NodeStatus;
}

function IterationSection({ label, defaultExpanded, snapshot, status }: IterationSectionProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  return (
    <div className="border border-[var(--border)] rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 w-full px-3 py-2 bg-[var(--bg)] hover:bg-[var(--node-bg)] transition-colors text-left"
      >
        {expanded ? (
          <ChevronDown className="w-3.5 h-3.5 text-[var(--text-muted)] flex-shrink-0" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 text-[var(--text-muted)] flex-shrink-0" />
        )}
        <span className="text-xs font-semibold text-[var(--text)]">{label}</span>
        {snapshot.elapsed != null && (
          <span className="text-[10px] text-[var(--text-muted)] ml-auto">
            {formatSecCompact(snapshot.elapsed)}
          </span>
        )}
      </button>
      {expanded && (
        <div className="px-3 py-3 space-y-3 border-t border-[var(--border)]">
          {/* Metadata */}
          <MetadataGrid items={buildAgentMetadata(snapshot)} />

          {/* Prompt */}
          {snapshot.prompt && (
            <OutputViewer output={snapshot.prompt} title="Input / Prompt" defaultExpanded={false} />
          )}

          {/* Activity stream */}
          <ActivityStream activity={snapshot.activity} defaultExpanded={defaultExpanded && status !== 'completed'} />

          {/* Output */}
          {snapshot.output != null && (
            <OutputViewer output={snapshot.output} title="Output" defaultExpanded={true} />
          )}

          {/* Error info */}
          {snapshot.error_type && (
            <div className="text-xs text-red-400">
              <span className="font-semibold">{snapshot.error_type}</span>
              {snapshot.error_message && (
                <span className="ml-1">— {snapshot.error_message}</span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function formatSecCompact(s: number): string {
  if (s < 1) return `${(s * 1000).toFixed(0)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const sec = (s % 60).toFixed(0);
  return `${m}m ${sec}s`;
}
