/**
 * End node — marks workflow termination ($end route target).
 */

import { Handle, Position } from '@xyflow/react';
import type { DesignerNodeData } from '@/types/designer';

interface Props {
  data: DesignerNodeData;
}

export function DesignerEndNode({ data: _data }: Props) {
  return (
    <div className="rounded-full bg-red-600 text-white px-4 py-2 text-sm font-bold shadow-lg flex items-center gap-2">
      <Handle type="target" position={Position.Top} className="!bg-red-400 !w-3 !h-3" />
      <span>■</span>
      <span>End</span>
    </div>
  );
}
