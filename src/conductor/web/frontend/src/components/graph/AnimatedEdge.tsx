import { memo, useMemo } from 'react';
import {
  BaseEdge,
  getBezierPath,
  type EdgeProps,
} from '@xyflow/react';
import { useWorkflowStore } from '@/stores/workflow-store';

export const AnimatedEdge = memo(function AnimatedEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  source,
  target,
  data,
}: EdgeProps) {
  const highlightedEdges = useWorkflowStore((s) => s.highlightedEdges);

  const edgeHighlight = useMemo(() => {
    return highlightedEdges.find((e) => e.from === source && e.to === target);
  }, [highlightedEdges, source, target]);

  const [edgePath] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });

  const hasWhen = !!(data as Record<string, unknown> | undefined)?.when;
  const isTaken = edgeHighlight?.state === 'taken';
  const isHighlighted = edgeHighlight?.state === 'highlighted';

  let strokeColor = 'var(--edge-color)';
  let strokeWidth = 2;
  let strokeDasharray: string | undefined;

  if (isTaken) {
    strokeColor = 'var(--edge-taken)';
    strokeWidth = 3;
  } else if (isHighlighted) {
    strokeColor = 'var(--edge-active)';
    strokeWidth = 3;
  }

  if (hasWhen && !isTaken && !isHighlighted) {
    strokeDasharray = '6 3';
  }

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        style={{
          stroke: strokeColor,
          strokeWidth,
          strokeDasharray,
          transition: 'stroke 0.3s ease, stroke-width 0.3s ease',
        }}
        markerEnd={`url(#arrow-${isTaken ? 'taken' : isHighlighted ? 'active' : 'default'})`}
      />
      {/* Flowing dot animation for taken edges */}
      {isTaken && (
        <circle r="3" fill="var(--edge-taken)">
          <animateMotion dur="1s" repeatCount="indefinite" path={edgePath} />
        </circle>
      )}
    </>
  );
});
