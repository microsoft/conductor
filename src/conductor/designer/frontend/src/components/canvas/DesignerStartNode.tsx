/**
 * Start node — marks the workflow entry point.
 */

import { Handle, Position } from '@xyflow/react';
import type { DesignerNodeData } from '@/types/designer';

interface Props {
  data: DesignerNodeData;
}

export function DesignerStartNode({ data: _data }: Props) {
  return (
    <div className="rounded-full bg-green-600 text-white px-4 py-2 text-sm font-bold shadow-lg flex items-center gap-2">
      <span>▶</span>
      <span>Start</span>
      <Handle type="source" position={Position.Bottom} className="!bg-green-400 !w-3 !h-3" />
    </div>
  );
}
