import { MetadataGrid, buildAgentMetadata } from './MetadataGrid';
import { OutputViewer } from './OutputViewer';
import { ActivityStream } from './ActivityStream';
import type { NodeData } from '@/stores/workflow-store';

import { NODE_STATUS_HEX } from '@/lib/constants';
import type { NodeStatus } from '@/lib/constants';

interface AgentDetailProps {
  node: NodeData;
}

export function AgentDetail({ node }: AgentDetailProps) {
  const status = node.status as NodeStatus;
  const statusColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

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

      {/* Metadata */}
      <MetadataGrid items={buildAgentMetadata(node)} />

      {/* Prompt */}
      {node.prompt && (
        <OutputViewer output={node.prompt} title="Input / Prompt" defaultExpanded={true} />
      )}

      {/* Activity stream */}
      <ActivityStream activity={node.activity} />

      {/* Output */}
      {node.output != null && (
        <OutputViewer output={node.output} title="Output" />
      )}
    </div>
  );
}
