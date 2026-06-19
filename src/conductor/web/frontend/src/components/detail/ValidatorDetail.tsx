import type { NodeData } from '@/stores/workflow-store';

interface ValidatorDetailProps {
  node: NodeData;
}

const STATE_STYLES: Record<string, { label: string; color: string }> = {
  running: { label: 'Validating…', color: '#3b82f6' },
  passed: { label: 'Passed', color: '#22c55e' },
  failed: { label: 'Failed', color: '#f59e0b' },
  error: { label: 'Validator error (treated as pass)', color: '#f59e0b' },
};

/**
 * Renders the semantic-validation status for an agent node (issue #220):
 * the pass/fail verdict from the validator's second LLM call, any reported
 * issues, and whether the primary agent was re-run with feedback.
 *
 * Returns null when the node has no validator activity, so it can be dropped
 * into the agent detail panel unconditionally.
 */
export function ValidatorDetail({ node }: ValidatorDetailProps) {
  const state = node.validator_state;
  if (!state) return null;

  const style = STATE_STYLES[state] ?? { label: 'Validating…', color: '#3b82f6' };
  const issues = node.validator_issues ?? [];
  const showIssues = (state === 'failed' || state === 'error') && issues.length > 0;

  return (
    <div className="border border-[var(--border)] rounded-lg overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 bg-[var(--bg)]">
        <span className="text-xs font-semibold text-[var(--text)]">Validation</span>
        <span
          className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ml-auto"
          style={{ backgroundColor: `${style.color}20`, color: style.color }}
        >
          {style.label}
        </span>
      </div>

      <div className="px-3 py-3 space-y-2 border-t border-[var(--border)]">
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-[var(--text-muted)]">
          {node.validator_model && <span>model: {node.validator_model}</span>}
          {node.validator_cost_usd != null && (
            <span>cost: ${node.validator_cost_usd.toFixed(4)}</span>
          )}
          {node.validator_attempts != null && node.validator_attempts > 1 && (
            <span>runs: {node.validator_attempts}</span>
          )}
        </div>

        {showIssues && (
          <div className="space-y-1">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--text-muted)]">
              Issues
            </div>
            <ul className="space-y-1">
              {issues.map((issue, i) => (
                <li key={i} className="text-xs text-[var(--text)] flex gap-1.5">
                  <span className="text-[var(--text-muted)] flex-shrink-0">•</span>
                  <span>{issue}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {node.validator_will_retry && (
          <div className="text-[10px] text-[var(--text-muted)] italic">
            Primary agent re-run once with this feedback appended.
          </div>
        )}
      </div>
    </div>
  );
}
