import { MetadataGrid } from './MetadataGrid';
import type { NodeData } from '@/stores/workflow-store';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { formatElapsed } from '@/lib/utils';
import type { NodeStatus } from '@/lib/constants';

interface WaitDetailProps {
  node: NodeData;
}

export function WaitDetail({ node }: WaitDetailProps) {
  const status = node.status as NodeStatus;
  const statusColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const items: Array<{ label: string; value: string | number | null | undefined }> = [];
  const requested = node.requested_seconds ?? node.duration_seconds;
  if (requested != null) items.push({ label: 'Requested', value: formatElapsed(requested) });
  if (node.waited_seconds != null) {
    items.push({ label: 'Waited', value: formatElapsed(node.waited_seconds) });
  } else if (node.elapsed != null) {
    items.push({ label: 'Elapsed', value: formatElapsed(node.elapsed) });
  }
  if (node.interrupted) items.push({ label: 'Interrupted', value: 'yes' });
  if (node.reason) items.push({ label: 'Reason', value: node.reason });
  if (node.error_type) items.push({ label: 'Error', value: node.error_type });
  if (node.error_message) items.push({ label: 'Message', value: node.error_message });

  return (
    <div className="space-y-4">
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
        <span className="text-xs text-[var(--text-muted)]">Wait</span>
      </div>

      <MetadataGrid items={items} />
    </div>
  );
}
