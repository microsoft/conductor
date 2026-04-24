/**
 * Sub-workflow node — shows workflow path reference.
 */

import { Handle, Position } from '@xyflow/react';
import type { DesignerNodeData } from '@/types/designer';
import { useDesignerStore } from '@/stores/designer-store';

interface Props {
  id: string;
  data: DesignerNodeData;
  selected?: boolean;
}

export function DesignerWorkflowNode({ id, data, selected }: Props) {
  const config = useDesignerStore((s) => s.config);
  const selectNode = useDesignerStore((s) => s.selectNode);
  const agent = config.agents.find((a) => a.name === data.entityName);

  const borderColor = selected ? 'border-purple-500' : 'border-gray-600';

  return (
    <div
      className={`rounded-lg border-2 ${borderColor} bg-gray-800 px-4 py-3 shadow-lg min-w-[200px] cursor-pointer`}
      onClick={() => selectNode(id)}
    >
      <Handle type="target" position={Position.Top} className="!bg-purple-500 !w-3 !h-3" />

      <div className="flex items-center gap-2 mb-1">
        <span className="text-xs font-bold uppercase tracking-wide text-purple-400">
          Workflow
        </span>
      </div>

      <div className="text-sm font-semibold text-gray-100 truncate">{data.label}</div>

      {agent?.workflow && (
        <div className="text-xs text-gray-400 mt-1 truncate max-w-[180px]">
          📁 {agent.workflow}
        </div>
      )}

      <Handle type="source" position={Position.Bottom} className="!bg-purple-500 !w-3 !h-3" />
    </div>
  );
}
