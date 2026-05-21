import { MetadataGrid } from './MetadataGrid';
import { OutputViewer } from './OutputViewer';
import type { NodeData } from '@/stores/workflow-store';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { formatElapsed } from '@/lib/utils';
import type { NodeStatus } from '@/lib/constants';

interface SetDetailProps {
  node: NodeData;
}

/**
 * Detail panel for `type: set` workflow steps.
 *
 * Shows set-specific fields (`set_output_type`, `set_output_keys`,
 * `set_value_repr`) instead of the LLM-agent fields (model, tokens,
 * cost, prompt/output) which don't apply to a pure-context step.
 *
 * The value preview is rendered through `OutputViewer` so users get the
 * same expand/copy controls as other detail panels. `set_value_repr` is
 * already JSON-truncated server-side at ~512 chars; we pass it as the
 * raw string so OutputViewer treats it as plain text.
 */
export function SetDetail({ node }: SetDetailProps) {
  const status = node.status as NodeStatus;
  const statusColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const outputType = node.set_output_type;
  const outputKeys = node.set_output_keys;
  const valueRepr = node.set_value_repr;
  const keyCount = outputKeys?.length ?? 0;

  const items: Array<{ label: string; value: string | number | null | undefined }> = [];
  if (node.elapsed != null) items.push({ label: 'Elapsed', value: formatElapsed(node.elapsed) });
  if (outputType) items.push({ label: 'Output Type', value: outputType });
  if (keyCount > 0) {
    items.push({ label: 'Bindings', value: outputKeys!.join(', ') });
  } else if (status === 'completed') {
    items.push({ label: 'Bindings', value: 'scalar' });
  }
  if (node.error_type) items.push({ label: 'Error', value: node.error_type });
  if (node.error_message) items.push({ label: 'Message', value: node.error_message });

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
        <span className="text-xs text-[var(--text-muted)]">Set</span>
      </div>

      <MetadataGrid items={items} />

      {valueRepr && (
        <OutputViewer output={valueRepr} title="Value preview" />
      )}
    </div>
  );
}
