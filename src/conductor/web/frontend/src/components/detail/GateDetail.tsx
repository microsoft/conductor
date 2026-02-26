import { MetadataGrid } from './MetadataGrid';
import type { NodeData } from '@/stores/workflow-store';
import { NODE_STATUS_HEX } from '@/lib/constants';
import type { NodeStatus } from '@/lib/constants';

interface GateDetailProps {
  node: NodeData;
}

export function GateDetail({ node }: GateDetailProps) {
  const status = node.status as NodeStatus;
  const statusColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const items: Array<{ label: string; value: string | number | null | undefined }> = [];
  if (node.selected_option) items.push({ label: 'Selected', value: node.selected_option });
  if (node.route) items.push({ label: 'Route', value: node.route });
  if (node.additional_input) {
    const inputStr = typeof node.additional_input === 'object'
      ? JSON.stringify(node.additional_input)
      : node.additional_input;
    items.push({ label: 'Input', value: inputStr });
  }

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
        <span className="text-xs text-[var(--text-muted)]">Human Gate</span>
      </div>

      {node.prompt && (
        <div className="space-y-1.5">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold">Prompt</h4>
          <p className="text-xs text-[var(--text)] bg-[var(--bg)] border border-[var(--border)] rounded-md p-3">{node.prompt}</p>
        </div>
      )}

      {node.options && node.options.length > 0 && (
        <div className="space-y-1.5">
          <h4 className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold">Options</h4>
          <div className="flex flex-wrap gap-1.5">
            {node.options.map((opt) => (
              <span
                key={opt}
                className={`text-[11px] px-2 py-0.5 rounded border ${
                  opt === node.selected_option
                    ? 'border-[var(--completed)] text-[var(--completed)] bg-[var(--completed-muted)]'
                    : 'border-[var(--border)] text-[var(--text-muted)]'
                }`}
              >
                {opt}
              </span>
            ))}
          </div>
        </div>
      )}

      <MetadataGrid items={items} />
    </div>
  );
}
