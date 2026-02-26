import { MetadataGrid } from './MetadataGrid';
import type { NodeData } from '@/stores/workflow-store';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { formatElapsed } from '@/lib/utils';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { NodeStatus } from '@/lib/constants';

interface GroupDetailProps {
  node: NodeData;
}

export function GroupDetail({ node }: GroupDetailProps) {
  const status = node.status as NodeStatus;
  const statusColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;
  const groupProgress = useWorkflowStore((s) => s.groupProgress);
  const progress = groupProgress[node.name];
  const isForEach = node.type === 'for_each_group';

  const items: Array<{ label: string; value: string | number | null | undefined }> = [];
  if (node.elapsed != null) items.push({ label: 'Elapsed', value: formatElapsed(node.elapsed) });
  if (progress) {
    items.push({ label: 'Total', value: progress.total });
    items.push({ label: 'Completed', value: progress.completed });
    if (progress.failed > 0) items.push({ label: 'Failed', value: progress.failed });
  }
  if (node.success_count != null) items.push({ label: 'Success', value: node.success_count });
  if (node.failure_count != null) items.push({ label: 'Failures', value: node.failure_count });

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
        <span className="text-xs text-[var(--text-muted)]">
          {isForEach ? 'For-Each Group' : 'Parallel Group'}
        </span>
      </div>

      {/* Progress bar */}
      {progress && progress.total > 0 && (
        <div className="space-y-1">
          <div className="flex justify-between text-[10px] text-[var(--text-muted)]">
            <span>Progress</span>
            <span>{progress.completed + progress.failed}/{progress.total}</span>
          </div>
          <div className="h-1.5 bg-[var(--bg)] rounded-full overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                width: `${((progress.completed + progress.failed) / progress.total) * 100}%`,
                background: progress.failed > 0
                  ? `linear-gradient(90deg, var(--completed) ${(progress.completed / (progress.completed + progress.failed)) * 100}%, var(--failed) 0%)`
                  : 'var(--completed)',
              }}
            />
          </div>
        </div>
      )}

      <MetadataGrid items={items} />
    </div>
  );
}
