import { formatElapsed, formatCost, formatTokens, formatContextFull } from '@/lib/utils';

interface MetadataGridProps {
  items: Array<{ label: string; value: string | number | null | undefined }>;
}

export function MetadataGrid({ items }: MetadataGridProps) {
  const filtered = items.filter((item) => item.value != null && item.value !== '');

  if (filtered.length === 0) return null;

  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
      {filtered.map(({ label, value }) => (
        <div key={label} className="contents">
          <dt className="text-[var(--text-muted)] whitespace-nowrap">{label}</dt>
          <dd className="text-[var(--text)] break-words">
            {typeof value === 'object' ? JSON.stringify(value) : String(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

// Helper to build metadata items from node data
export function buildAgentMetadata(nd: {
  elapsed?: number;
  model?: string;
  tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  context_window_used?: number;
  context_window_max?: number;
  iteration?: number;
  error_type?: string;
  error_message?: string;
}) {
  const items: Array<{ label: string; value: string | number | null | undefined }> = [];

  if (nd.elapsed != null) items.push({ label: 'Elapsed', value: formatElapsed(nd.elapsed) });
  if (nd.model) items.push({ label: 'Model', value: nd.model });
  if (nd.tokens != null) items.push({ label: 'Tokens', value: formatTokens(nd.tokens) });
  if (nd.input_tokens != null && nd.output_tokens != null) {
    items.push({ label: 'In / Out', value: `${formatTokens(nd.input_tokens)} / ${formatTokens(nd.output_tokens)}` });
  }
  if (nd.cost_usd != null) items.push({ label: 'Cost', value: formatCost(nd.cost_usd) });
  if (nd.context_window_used != null && nd.context_window_max != null) {
    items.push({ label: 'Context', value: formatContextFull(nd.context_window_used, nd.context_window_max) });
  }
  if (nd.iteration != null) items.push({ label: 'Iteration', value: nd.iteration });
  if (nd.error_type) items.push({ label: 'Error', value: nd.error_type });
  if (nd.error_message) items.push({ label: 'Message', value: nd.error_message });

  return items;
}
