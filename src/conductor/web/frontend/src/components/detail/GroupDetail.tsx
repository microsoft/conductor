import { useState } from 'react';
import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react';
import { MetadataGrid } from './MetadataGrid';
import { OutputViewer } from './OutputViewer';
import { ActivityStream } from './ActivityStream';
import type { NodeData, ForEachItemData } from '@/stores/workflow-store';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { formatElapsed, formatCost, formatTokens } from '@/lib/utils';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { NodeStatus } from '@/lib/constants';

interface GroupDetailProps {
  node: NodeData;
}

export function GroupDetail({ node }: GroupDetailProps) {
  const status = node.status as NodeStatus;
  const statusColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;
  const viewedCtx = useWorkflowStore((s) => s.getViewedContext());
  const progress = viewedCtx.groupProgress[node.name];
  const isForEach = node.type === 'for_each_group';

  const [showItems, setShowItems] = useState(true);

  const items: Array<{ label: string; value: string | number | null | undefined }> = [];
  if (node.elapsed != null) items.push({ label: 'Elapsed', value: formatElapsed(node.elapsed) });
  if (progress) {
    items.push({ label: 'Total', value: progress.total });
    items.push({ label: 'Completed', value: progress.completed });
    if (progress.failed > 0) items.push({ label: 'Failed', value: progress.failed });
  }
  if (node.success_count != null) items.push({ label: 'Success', value: node.success_count });
  if (node.failure_count != null) items.push({ label: 'Failures', value: node.failure_count });

  const forEachItems = node.for_each_items;

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

      {/* Per-item details for for-each groups */}
      {isForEach && forEachItems && forEachItems.length > 0 && (
        <div className="space-y-2">
          <button
            onClick={() => setShowItems(!showItems)}
            className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold hover:text-[var(--text)] transition-colors"
          >
            {showItems ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
            Items ({forEachItems.length})
          </button>

          {showItems && (
            <div className="space-y-1">
              {forEachItems.map((item) => (
                <ForEachItemRow key={`${item.key}-${item.index}`} item={item} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const ITEM_STATUS_COLORS: Record<ForEachItemData['status'], string> = {
  running: NODE_STATUS_HEX.running,
  completed: NODE_STATUS_HEX.completed,
  failed: NODE_STATUS_HEX.failed,
};

function ForEachItemRow({ item }: { item: ForEachItemData }) {
  const [expanded, setExpanded] = useState(item.status === 'running');
  const color = ITEM_STATUS_COLORS[item.status];

  const hasDetails = !!(
    item.prompt ||
    item.output != null ||
    (item.activity && item.activity.length > 0) ||
    item.error_type
  );

  const metadataItems: Array<{ label: string; value: string | number | null | undefined }> = [];
  if (item.elapsed != null) metadataItems.push({ label: 'Elapsed', value: formatElapsed(item.elapsed) });
  if (item.tokens != null) metadataItems.push({ label: 'Tokens', value: formatTokens(item.tokens) });
  if (item.cost_usd != null) metadataItems.push({ label: 'Cost', value: formatCost(item.cost_usd) });

  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] overflow-hidden">
      {/* Header row: clickable to expand/collapse */}
      <button
        onClick={() => hasDetails && setExpanded(!expanded)}
        className="flex items-center gap-2 w-full px-3 py-2 text-left hover:bg-[var(--node-bg)] transition-colors"
        disabled={!hasDetails}
      >
        {/* Expand/collapse chevron or status indicator */}
        {hasDetails ? (
          expanded ? (
            <ChevronDown className="w-3 h-3 text-[var(--text-muted)] flex-shrink-0" />
          ) : (
            <ChevronRight className="w-3 h-3 text-[var(--text-muted)] flex-shrink-0" />
          )
        ) : item.status === 'running' ? (
          <Loader2 className="w-3 h-3 animate-spin flex-shrink-0" style={{ color }} />
        ) : (
          <span
            className="w-2 h-2 rounded-full flex-shrink-0 ml-0.5 mr-0.5"
            style={{ backgroundColor: color }}
          />
        )}

        {/* Item key */}
        <span className="text-xs font-medium text-[var(--text)] truncate flex-1 min-w-0">
          {item.key}
        </span>

        {/* Compact metrics */}
        {!expanded && (item.elapsed != null || item.tokens != null || item.cost_usd != null) && (
          <span className="flex items-center gap-2 text-[10px] text-[var(--text-muted)] flex-shrink-0">
            {item.elapsed != null && <span>{formatElapsed(item.elapsed)}</span>}
            {item.tokens != null && <span>{formatTokens(item.tokens)}</span>}
            {item.cost_usd != null && <span>{formatCost(item.cost_usd)}</span>}
          </span>
        )}

        {/* Status badge */}
        <span
          className="text-[10px] font-bold uppercase tracking-wider flex-shrink-0 px-1.5 py-0.5 rounded"
          style={{
            backgroundColor: `${color}20`,
            color,
          }}
        >
          {item.status}
        </span>
      </button>

      {/* Expanded detail panel */}
      {expanded && hasDetails && (
        <div className="px-3 py-3 space-y-3 border-t border-[var(--border)]">
          {/* Metadata grid */}
          {metadataItems.length > 0 && (
            <MetadataGrid items={metadataItems} />
          )}

          {/* Prompt / Input */}
          {item.prompt && (
            <OutputViewer output={item.prompt} title="Input / Prompt" defaultExpanded={false} />
          )}

          {/* Activity stream */}
          {item.activity && item.activity.length > 0 && (
            <ActivityStream
              activity={item.activity}
              defaultExpanded={item.status !== 'completed'}
            />
          )}

          {/* Output */}
          {item.output != null && (
            <OutputViewer output={item.output} title="Output" defaultExpanded={true} />
          )}

          {/* Error info */}
          {item.status === 'failed' && (item.error_type || item.error_message) && (
            <div className="text-xs text-red-400">
              {item.error_type && (
                <span className="font-semibold">{item.error_type}</span>
              )}
              {item.error_message && (
                <span className="ml-1">— {item.error_message}</span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
