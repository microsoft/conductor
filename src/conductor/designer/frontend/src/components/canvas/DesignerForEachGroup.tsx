/**
 * For-each group node — composite node showing loop config.
 * NOT a container — rendered as a single node with loop metadata.
 */

import { Handle, Position } from '@xyflow/react';
import type { DesignerNodeData } from '@/types/designer';
import { useDesignerStore } from '@/stores/designer-store';

interface Props {
  id: string;
  data: DesignerNodeData;
  selected?: boolean;
}

export function DesignerForEachGroup({ id, data, selected }: Props) {
  const config = useDesignerStore((s) => s.config);
  const selectNode = useDesignerStore((s) => s.selectNode);
  const group = (config.for_each ?? []).find((g) => g.name === data.entityName);

  const borderColor = selected ? 'border-orange-500' : 'border-gray-600';

  return (
    <div
      className={`rounded-lg border-2 ${borderColor} bg-gray-800 px-4 py-3 shadow-lg min-w-[220px] cursor-pointer`}
      onClick={() => selectNode(id)}
    >
      <Handle type="target" position={Position.Top} className="!bg-orange-500 !w-3 !h-3" />

      <div className="flex items-center gap-2 mb-1">
        <span className="text-xs font-bold uppercase tracking-wide text-orange-400">
          For Each
        </span>
        {group && (
          <span className="text-[10px] bg-gray-700 text-gray-300 px-1.5 py-0.5 rounded">
            max {group.max_concurrent}
          </span>
        )}
      </div>

      <div className="text-sm font-semibold text-gray-100">{data.label}</div>

      {group && (
        <div className="mt-1.5 space-y-0.5">
          <div className="text-xs text-gray-400">
            <span className="text-orange-300">source:</span> {group.source}
          </div>
          <div className="text-xs text-gray-400">
            <span className="text-orange-300">as:</span> {group.as}
          </div>
          <div className="text-xs text-gray-400">
            <span className="text-orange-300">agent:</span> {group.agent.name || '(inline)'}
          </div>
        </div>
      )}

      <Handle type="source" position={Position.Bottom} className="!bg-orange-500 !w-3 !h-3" />
    </div>
  );
}
