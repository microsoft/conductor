/**
 * Property panel — edit the selected node's properties.
 * Dispatches to type-specific sub-panels.
 */

import { useDesignerStore } from '@/stores/designer-store';
import { AgentProperties } from './AgentProperties';
import { WorkflowSettings } from './WorkflowSettings';

export function PropertyPanel() {
  const selectedNodeId = useDesignerStore((s) => s.selectedNodeId);
  const config = useDesignerStore((s) => s.config);
  const nodes = useDesignerStore((s) => s.nodes);

  if (!selectedNodeId) {
    return (
      <div className="w-80 bg-gray-900 border-l border-gray-700 overflow-y-auto">
        <WorkflowSettings />
      </div>
    );
  }

  const node = nodes.find((n) => n.id === selectedNodeId);
  if (!node) return null;

  const nodeType = node.data?.nodeType;
  const entityName = node.data?.entityName;

  // Find the entity in config
  const agent = config.agents.find((a) => a.name === entityName);
  const parallelGroup = (config.parallel ?? []).find((g) => g.name === entityName);
  const forEachGroup = (config.for_each ?? []).find((g) => g.name === entityName);

  return (
    <div className="w-80 bg-gray-900 border-l border-gray-700 overflow-y-auto">
      <div className="p-4 border-b border-gray-700">
        <h2 className="text-sm font-bold text-gray-300 uppercase tracking-wide">
          {nodeType === 'human_gate' ? 'Gate' : nodeType} Properties
        </h2>
        <p className="text-xs text-gray-500 mt-0.5">{entityName}</p>
      </div>

      {agent && (
        <AgentProperties agent={agent} />
      )}

      {parallelGroup && (
        <div className="p-4 space-y-3">
          <Field label="Name" value={parallelGroup.name} readOnly />
          <Field label="Failure Mode" value={parallelGroup.failure_mode} readOnly />
          <div>
            <label className="text-xs text-gray-400">Agents</label>
            <div className="mt-1 space-y-1">
              {parallelGroup.agents.map((a) => (
                <div key={a} className="text-sm bg-gray-800 px-2 py-1 rounded">{a}</div>
              ))}
            </div>
          </div>
        </div>
      )}

      {forEachGroup && (
        <div className="p-4 space-y-3">
          <Field label="Name" value={forEachGroup.name} readOnly />
          <Field label="Source" value={forEachGroup.source} readOnly />
          <Field label="Loop Variable (as)" value={forEachGroup.as} readOnly />
          <Field label="Max Concurrent" value={String(forEachGroup.max_concurrent)} readOnly />
          <Field label="Failure Mode" value={forEachGroup.failure_mode} readOnly />
          <Field label="Agent Template" value={forEachGroup.agent.name || '(inline)'} readOnly />
        </div>
      )}
    </div>
  );
}

function Field({ label, value, readOnly }: { label: string; value: string; readOnly?: boolean }) {
  return (
    <div>
      <label className="text-xs text-gray-400">{label}</label>
      <input
        type="text"
        value={value}
        readOnly={readOnly}
        className="mt-0.5 w-full bg-gray-800 border border-gray-600 rounded px-2 py-1 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
      />
    </div>
  );
}
