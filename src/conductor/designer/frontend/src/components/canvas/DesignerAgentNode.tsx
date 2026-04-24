/**
 * Designer agent node — the most common node type.
 * Shows name, model badge, and a truncated prompt preview.
 */

import { Handle, Position } from '@xyflow/react';
import type { DesignerNodeData } from '@/types/designer';
import { useDesignerStore } from '@/stores/designer-store';

interface Props {
  id: string;
  data: DesignerNodeData;
  selected?: boolean;
}

export function DesignerAgentNode({ id, data, selected }: Props) {
  const config = useDesignerStore((s) => s.config);
  const selectNode = useDesignerStore((s) => s.selectNode);
  const agent = config.agents.find((a) => a.name === data.entityName);

  const borderColor = selected ? 'border-blue-500' : 'border-gray-600';

  return (
    <div
      className={`rounded-lg border-2 ${borderColor} bg-gray-800 px-4 py-3 shadow-lg min-w-[200px] cursor-pointer`}
      onClick={() => selectNode(id)}
    >
      <Handle type="target" position={Position.Top} className="!bg-blue-500 !w-3 !h-3" />

      <div className="flex items-center gap-2 mb-1">
        <span className="text-xs font-bold uppercase tracking-wide text-blue-400">
          Agent
        </span>
        {agent?.model && (
          <span className="text-[10px] bg-gray-700 text-gray-300 px-1.5 py-0.5 rounded">
            {agent.model}
          </span>
        )}
      </div>

      <div className="text-sm font-semibold text-gray-100 truncate">{data.label}</div>

      {agent?.prompt && (
        <div className="text-xs text-gray-400 mt-1 truncate max-w-[180px]">
          {agent.prompt.slice(0, 60)}{agent.prompt.length > 60 ? '…' : ''}
        </div>
      )}

      <Handle type="source" position={Position.Bottom} className="!bg-blue-500 !w-3 !h-3" />
    </div>
  );
}
