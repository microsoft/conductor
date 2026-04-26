import { useEffect, useRef, useState } from 'react';
import { useReactFlow } from '@xyflow/react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { SubworkflowContext } from '@/stores/workflow-store';

/** Parse deep-link params from the current URL. */
function getDeepLinkParams(): { subworkflowPath: string | null; agent: string | null } {
  const params = new URLSearchParams(window.location.search);
  return {
    subworkflowPath: params.get('subworkflow'),
    agent: params.get('agent'),
  };
}

/** Walk the subworkflow context tree to find a path of indices for the given agent name path. */
function resolveSubworkflowPath(
  contexts: SubworkflowContext[],
  segments: string[],
): { path: number[]; failedSegment: string | null } {
  const path: number[] = [];
  let current = contexts;

  for (const segment of segments) {
    const idx = current.findIndex((c) => c.parentAgent === segment);
    if (idx === -1) {
      return { path, failedSegment: segment };
    }
    path.push(idx);
    current = current[idx]!.children;
  }

  return { path, failedSegment: null };
}

export interface DeepLinkError {
  message: string;
}

/**
 * Reads `?agent=` and `?subworkflow=` query params on initial load
 * and auto-selects / navigates to the matching node once the workflow
 * state has been replayed.
 *
 * Subworkflow paths support slash-separated nesting:
 *   ?subworkflow=planning/design  → navigate root→planning→design
 *
 * Returns an error object if the deep-link target cannot be resolved.
 * Must be rendered inside a <ReactFlow> provider so useReactFlow() works.
 */
export function useDeepLink(): DeepLinkError | null {
  const [error, setError] = useState<DeepLinkError | null>(null);
  const applied = useRef(false);
  const { fitView } = useReactFlow();

  const { subworkflowPath, agent } = getDeepLinkParams();
  const hasParams = !!(subworkflowPath || agent);

  useEffect(() => {
    if (applied.current || !hasParams) return;

    // Subscribe to store changes and apply deep-link when state is ready.
    // This avoids timing issues with useEffect + zustand selectors.
    const unsubscribe = useWorkflowStore.subscribe((state) => {
      if (applied.current) return;

      // Wait until root agents have been populated (workflow_started processed)
      if (state.agents.length === 0) return;

      applied.current = true;
      unsubscribe();

      // 1. Navigate into subworkflow path
      if (subworkflowPath) {
        const segments = subworkflowPath.split('/').filter(Boolean);
        const { path, failedSegment } = resolveSubworkflowPath(
          state.subworkflowContexts,
          segments,
        );

        if (failedSegment) {
          const resolved = segments.slice(0, path.length).join('/');
          setError({
            message: `Subworkflow "${failedSegment}" not found${resolved ? ` (resolved: ${resolved})` : ''}. It may not have started yet.`,
          });
          return;
        }

        // Apply the full navigation path at once
        useWorkflowStore.setState({ viewContextPath: path, selectedNode: null });
      }

      // 2. Select agent node
      if (agent) {
        // Determine which context to check for the agent
        const freshState = useWorkflowStore.getState();
        let agentList: { name: string }[];

        if (freshState.viewContextPath.length === 0) {
          agentList = freshState.agents;
        } else {
          // Walk the context tree to get agents at the target depth
          let ctx: SubworkflowContext | undefined;
          let contexts = freshState.subworkflowContexts;
          for (const idx of freshState.viewContextPath) {
            ctx = contexts[idx];
            if (!ctx) break;
            contexts = ctx.children;
          }
          agentList = ctx?.agents ?? [];
        }

        const agentExists = agentList.some((a) => a.name === agent);
        if (!agentExists) {
          const location = subworkflowPath || 'root workflow';
          setError({
            message: `Agent "${agent}" not found in ${location}.`,
          });
          return;
        }

        useWorkflowStore.setState({ selectedNode: agent });

        // Center the view on the node after React Flow rebuilds the graph
        setTimeout(() => {
          fitView({ nodes: [{ id: agent }], padding: 0.5, duration: 400 });
        }, 200);
      }
    });

    // Also check the current state immediately (late-joiner replay may have
    // already completed before this effect runs)
    const currentState = useWorkflowStore.getState();
    if (currentState.agents.length > 0 && !applied.current) {
      // Trigger the subscriber with current state
      useWorkflowStore.setState({});
    }

    return unsubscribe;
  }, [hasParams, subworkflowPath, agent, fitView]);

  return error;
}
