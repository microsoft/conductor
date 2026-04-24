/**
 * Agent properties editor — editable form for agent fields.
 */

import { useCallback } from 'react';
import { useDesignerStore } from '@/stores/designer-store';
import type { AgentDef } from '@/types/designer';
import { PromptEditor } from '@/components/editors/PromptEditor';

interface Props {
  agent: AgentDef;
}

export function AgentProperties({ agent }: Props) {
  const updateAgent = useDesignerStore((s) => s.updateAgent);

  const update = useCallback(
    (patch: Partial<AgentDef>) => {
      updateAgent(agent.name, patch);
    },
    [agent.name, updateAgent],
  );

  const agentType = agent.type ?? 'agent';

  return (
    <div className="p-4 space-y-4">
      {/* Name */}
      <div>
        <label className="text-xs text-gray-400">Name</label>
        <input
          type="text"
          value={agent.name}
          onChange={(e) => update({ name: e.target.value })}
          className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        />
      </div>

      {/* Description */}
      <div>
        <label className="text-xs text-gray-400">Description</label>
        <input
          type="text"
          value={agent.description ?? ''}
          onChange={(e) => update({ description: e.target.value || undefined })}
          placeholder="Optional description"
          className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
        />
      </div>

      {/* Model (only for agent type) */}
      {agentType === 'agent' && (
        <div>
          <label className="text-xs text-gray-400">Model</label>
          <input
            type="text"
            value={agent.model ?? ''}
            onChange={(e) => update({ model: e.target.value || undefined })}
            placeholder="e.g., claude-sonnet-4"
            className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
          />
        </div>
      )}

      {/* System Prompt (only for agent type) */}
      {agentType === 'agent' && (
        <div>
          <label className="text-xs text-gray-400">System Prompt</label>
          <textarea
            value={agent.system_prompt ?? ''}
            onChange={(e) => update({ system_prompt: e.target.value || undefined })}
            placeholder="Optional system message"
            rows={3}
            className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500 resize-y font-mono"
          />
        </div>
      )}

      {/* Prompt (agent and human_gate) */}
      {(agentType === 'agent' || agentType === 'human_gate') && (
        <div>
          <label className="text-xs text-gray-400">Prompt</label>
          <PromptEditor
            value={agent.prompt ?? ''}
            onChange={(v) => update({ prompt: v })}
          />
        </div>
      )}

      {/* Command (script) */}
      {agentType === 'script' && (
        <>
          <div>
            <label className="text-xs text-gray-400">Command</label>
            <input
              type="text"
              value={agent.command ?? ''}
              onChange={(e) => update({ command: e.target.value })}
              placeholder="e.g., python script.py"
              className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
            />
          </div>
          <div>
            <label className="text-xs text-gray-400">Working Directory</label>
            <input
              type="text"
              value={agent.working_dir ?? ''}
              onChange={(e) => update({ working_dir: e.target.value || undefined })}
              placeholder="Optional"
              className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            />
          </div>
        </>
      )}

      {/* Workflow path (workflow type) */}
      {agentType === 'workflow' && (
        <>
          <div>
            <label className="text-xs text-gray-400">Workflow Path</label>
            <input
              type="text"
              value={agent.workflow ?? ''}
              onChange={(e) => update({ workflow: e.target.value })}
              placeholder="e.g., ./sub-workflow.yaml"
              className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <div>
            <label className="text-xs text-gray-400">Max Depth</label>
            <input
              type="number"
              value={agent.max_depth ?? ''}
              onChange={(e) =>
                update({ max_depth: e.target.value ? Number(e.target.value) : undefined })
              }
              placeholder="Default: 10"
              min={1}
              max={10}
              className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            />
          </div>
        </>
      )}

      {/* Gate options */}
      {agentType === 'human_gate' && agent.options && (
        <div>
          <label className="text-xs text-gray-400">Options</label>
          <div className="mt-1 space-y-1">
            {agent.options.map((opt, i) => (
              <div key={i} className="flex items-center gap-2 bg-gray-800 px-2 py-1 rounded text-sm">
                <span className="text-amber-300">{opt.label}</span>
                <span className="text-gray-500">→</span>
                <span className="text-gray-400">{opt.route}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Routes summary */}
      {agent.routes && agent.routes.length > 0 && (
        <div>
          <label className="text-xs text-gray-400">Routes</label>
          <div className="mt-1 space-y-1">
            {agent.routes.map((r, i) => (
              <div key={i} className="flex items-center gap-2 bg-gray-800 px-2 py-1 rounded text-xs">
                <span className="text-gray-300">→ {r.to}</span>
                {r.when && <span className="text-gray-500">when: {r.when}</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
