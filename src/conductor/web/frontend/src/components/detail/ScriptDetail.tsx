import { MetadataGrid } from './MetadataGrid';
import { OutputViewer } from './OutputViewer';
import type { NodeData } from '@/stores/workflow-store';
import { NODE_STATUS_HEX } from '@/lib/constants';
import { formatElapsed } from '@/lib/utils';
import type { NodeStatus } from '@/lib/constants';

interface ScriptDetailProps {
  node: NodeData;
}

export function ScriptDetail({ node }: ScriptDetailProps) {
  const status = node.status as NodeStatus;
  const statusColor = NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;

  const items: Array<{ label: string; value: string | number | null | undefined }> = [];
  if (node.elapsed != null) items.push({ label: 'Elapsed', value: formatElapsed(node.elapsed) });
  if (node.exit_code != null) items.push({ label: 'Exit Code', value: node.exit_code });
  if (node.error_type) items.push({ label: 'Error', value: node.error_type });
  if (node.error_message) items.push({ label: 'Message', value: node.error_message });

  // Build combined output
  let outputText = '';
  if (node.stdout) outputText += node.stdout;
  if (node.stderr) {
    outputText += (outputText ? '\n\n--- stderr ---\n' : '') + node.stderr;
  }

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
        <span className="text-xs text-[var(--text-muted)]">Script</span>
      </div>

      <MetadataGrid items={items} />

      {outputText && (
        <OutputViewer output={outputText} title="Output" />
      )}
    </div>
  );
}
