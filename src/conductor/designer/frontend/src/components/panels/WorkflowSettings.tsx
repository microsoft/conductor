/**
 * Workflow-level settings editor — top-level config like name, entry_point, runtime, limits.
 */

import { useDesignerStore } from '@/stores/designer-store';
import type { WorkflowDef } from '@/types/designer';

export function WorkflowSettings() {
  const workflow = useDesignerStore((s) => s.config.workflow);
  const agents = useDesignerStore((s) => s.config.agents);
  const updateWorkflow = useDesignerStore((s) => s.updateWorkflow);

  const update = (patch: Partial<WorkflowDef>) => {
    updateWorkflow(patch);
  };

  const entryPointOptions = agents.map((a) => a.name);

  return (
    <div className="p-4 space-y-4">
      <h3 className="text-xs font-bold text-gray-400 uppercase tracking-wide">
        Workflow Settings
      </h3>

      {/* Name */}
      <div>
        <label className="text-xs text-gray-400">Name</label>
        <input
          type="text"
          value={workflow.name}
          onChange={(e) => update({ name: e.target.value })}
          className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        />
      </div>

      {/* Description */}
      <div>
        <label className="text-xs text-gray-400">Description</label>
        <textarea
          value={workflow.description ?? ''}
          onChange={(e) => update({ description: e.target.value || undefined })}
          placeholder="Optional workflow description"
          rows={2}
          className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500 resize-y"
        />
      </div>

      {/* Version */}
      <div>
        <label className="text-xs text-gray-400">Version</label>
        <input
          type="text"
          value={workflow.version ?? ''}
          onChange={(e) => update({ version: e.target.value || undefined })}
          placeholder="e.g., 1.0.0"
          className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        />
      </div>

      {/* Entry Point */}
      <div>
        <label className="text-xs text-gray-400">Entry Point</label>
        <select
          value={workflow.entry_point}
          onChange={(e) => update({ entry_point: e.target.value })}
          className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        >
          {entryPointOptions.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>

      {/* Runtime Provider */}
      <div>
        <label className="text-xs text-gray-400">Provider</label>
        <select
          value={workflow.runtime?.provider ?? 'copilot'}
          onChange={(e) =>
            update({
              runtime: {
                ...(workflow.runtime ?? { provider: 'copilot' }),
                provider: e.target.value as 'copilot' | 'claude',
              },
            })
          }
          className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        >
          <option value="copilot">copilot</option>
          <option value="claude">claude</option>
        </select>
      </div>

      {/* Max Iterations */}
      <div>
        <label className="text-xs text-gray-400">Max Iterations</label>
        <input
          type="number"
          value={workflow.limits?.max_iterations ?? ''}
          onChange={(e) =>
            update({
              limits: {
                ...workflow.limits,
                max_iterations: e.target.value ? Number(e.target.value) : undefined,
              },
            })
          }
          placeholder="Default: 10"
          min={1}
          className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        />
      </div>

      {/* Timeout */}
      <div>
        <label className="text-xs text-gray-400">Timeout (seconds)</label>
        <input
          type="number"
          value={workflow.limits?.timeout_seconds ?? ''}
          onChange={(e) =>
            update({
              limits: {
                ...workflow.limits,
                timeout_seconds: e.target.value ? Number(e.target.value) : undefined,
              },
            })
          }
          placeholder="No timeout"
          min={1}
          className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        />
      </div>
    </div>
  );
}
