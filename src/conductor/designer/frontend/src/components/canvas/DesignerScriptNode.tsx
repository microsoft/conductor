/**
 * Script node — shows command preview.
 */

import { Handle, Position } from '@xyflow/react';
import type { DesignerNodeData } from '@/types/designer';
import { useDesignerStore } from '@/stores/designer-store';

interface Props {
  id: string;
  data: DesignerNodeData;
  selected?: boolean;
}

export function DesignerScriptNode({ id, data, selected }: Props) {
  const config = useDesignerStore((s) => s.config);
  const selectNode = useDesignerStore((s) => s.selectNode);
  const agent = config.agents.find((a) => a.name === data.entityName);

  const borderColor = selected ? 'border-green-500' : 'border-gray-600';

  return (
    <div
      className={`rounded-lg border-2 ${borderColor} bg-gray-800 px-4 py-3 shadow-lg min-w-[200px] cursor-pointer`}
      onClick={() => selectNode(id)}
    >
      <Handle type="target" position={Position.Top} className="!bg-green-500 !w-3 !h-3" />

      <div className="flex items-center gap-2 mb-1">
        <span className="text-xs font-bold uppercase tracking-wide text-green-400">
          Script
        </span>
      </div>

      <div className="text-sm font-semibold text-gray-100 truncate">{data.label}</div>

      {agent?.command && (
        <div className="text-xs text-gray-400 mt-1 font-mono truncate max-w-[180px]">
          $ {agent.command}
        </div>
      )}

      <Handle type="source" position={Position.Bottom} className="!bg-green-500 !w-3 !h-3" />
    </div>
  );
}
