/**
 * Human gate node — shows gate options as badges.
 */

import { Handle, Position } from '@xyflow/react';
import type { DesignerNodeData } from '@/types/designer';
import { useDesignerStore } from '@/stores/designer-store';

interface Props {
  id: string;
  data: DesignerNodeData;
  selected?: boolean;
}

export function DesignerGateNode({ id, data, selected }: Props) {
  const config = useDesignerStore((s) => s.config);
  const selectNode = useDesignerStore((s) => s.selectNode);
  const agent = config.agents.find((a) => a.name === data.entityName);

  const borderColor = selected ? 'border-amber-400' : 'border-gray-600';

  return (
    <div
      className={`rounded-lg border-2 ${borderColor} bg-gray-800 px-4 py-3 shadow-lg min-w-[200px] cursor-pointer`}
      onClick={() => selectNode(id)}
    >
      <Handle type="target" position={Position.Top} className="!bg-amber-400 !w-3 !h-3" />

      <div className="flex items-center gap-2 mb-1">
        <span className="text-xs font-bold uppercase tracking-wide text-amber-400">
          Gate
        </span>
      </div>

      <div className="text-sm font-semibold text-gray-100 truncate">{data.label}</div>

      {agent?.options && (
        <div className="flex flex-wrap gap-1 mt-1.5">
          {agent.options.slice(0, 4).map((opt) => (
            <span
              key={opt.label}
              className="text-[10px] bg-amber-900/40 text-amber-300 px-1.5 py-0.5 rounded"
            >
              {opt.label}
            </span>
          ))}
          {agent.options.length > 4 && (
            <span className="text-[10px] text-gray-500">+{agent.options.length - 4}</span>
          )}
        </div>
      )}

      <Handle type="source" position={Position.Bottom} className="!bg-amber-400 !w-3 !h-3" />
    </div>
  );
}
