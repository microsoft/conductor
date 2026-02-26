import { memo, useMemo } from 'react';
import {
  BaseEdge,
  EdgeLabelRenderer,
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

  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });

  const whenExpr = (data as Record<string, unknown> | undefined)?.when as string | undefined;
  const hasWhen = !!whenExpr;
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
      {/* Condition label for conditional edges */}
      {hasWhen && (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan"
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
              pointerEvents: 'all',
            }}
          >
            <span
              className="inline-block px-1.5 py-0.5 rounded-full text-[9px] font-mono leading-tight max-w-[140px] truncate"
              style={{
                backgroundColor: isTaken ? 'var(--edge-taken)' : 'var(--surface)',
                color: isTaken ? 'var(--bg)' : 'var(--text-muted)',
                border: `1px solid ${isTaken ? 'var(--edge-taken)' : 'var(--border)'}`,
              }}
              title={whenExpr}
            >
              {whenExpr}
            </span>
          </div>
        </EdgeLabelRenderer>
      )}
      {/* Flowing dot animation for taken edges */}
      {isTaken && (
        <circle r="3" fill="var(--edge-taken)">
          <animateMotion dur="1s" repeatCount="indefinite" path={edgePath} />
        </circle>
      )}
    </>
  );
});
