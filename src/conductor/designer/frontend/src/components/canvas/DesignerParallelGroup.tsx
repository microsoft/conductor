/**
 * Parallel group node — visual container showing member agents.
 */

import { Handle, Position } from '@xyflow/react';
import type { DesignerNodeData } from '@/types/designer';
import { useDesignerStore } from '@/stores/designer-store';

interface Props {
  id: string;
  data: DesignerNodeData;
  selected?: boolean;
}

export function DesignerParallelGroup({ id, data, selected }: Props) {
  const config = useDesignerStore((s) => s.config);
  const selectNode = useDesignerStore((s) => s.selectNode);
  const group = (config.parallel ?? []).find((g) => g.name === data.entityName);

  const borderColor = selected ? 'border-cyan-500' : 'border-gray-600';

  return (
    <div
      className={`rounded-lg border-2 border-dashed ${borderColor} bg-gray-800/50 px-4 py-3 shadow-lg min-w-[220px] cursor-pointer`}
      onClick={() => selectNode(id)}
    >
      <Handle type="target" position={Position.Top} className="!bg-cyan-500 !w-3 !h-3" />

      <div className="flex items-center gap-2 mb-2">
        <span className="text-xs font-bold uppercase tracking-wide text-cyan-400">
          Parallel
        </span>
        {group?.failure_mode && group.failure_mode !== 'fail_fast' && (
          <span className="text-[10px] bg-gray-700 text-gray-300 px-1.5 py-0.5 rounded">
            {group.failure_mode}
          </span>
        )}
      </div>

      <div className="text-sm font-semibold text-gray-100 mb-2">{data.label}</div>

      {group?.agents && (
        <div className="flex flex-col gap-1">
          {group.agents.map((agentName) => (
            <div
              key={agentName}
              className="text-xs bg-gray-700/80 text-gray-300 px-2 py-1 rounded"
            >
              {agentName}
            </div>
          ))}
        </div>
      )}

      <Handle type="source" position={Position.Bottom} className="!bg-cyan-500 !w-3 !h-3" />
    </div>
  );
}
