import { useEffect, useRef } from 'react';
import { useReactFlow } from '@xyflow/react';
import { useWorkflowStore } from '@/stores/workflow-store';

/**
 * Reads `?agent=` and `?subworkflow=` query params on initial load
 * and auto-selects / navigates to the matching node once the graph
 * has been populated with data.
 *
 * Subworkflow paths support slash-separated nesting:
 *   ?subworkflow=planning/design  → navigate root→planning→design
 *
 * Must be rendered inside a <ReactFlow> provider so useReactFlow() works.
 */
export function useDeepLink() {
  const applied = useRef(false);
  const { fitView } = useReactFlow();

  const agents = useWorkflowStore((s) => s.agents);
  const selectNode = useWorkflowStore((s) => s.selectNode);
  const navigateIntoSubworkflow = useWorkflowStore((s) => s.navigateIntoSubworkflow);

  useEffect(() => {
    // Only apply once, and only after agents have loaded
    if (applied.current || agents.length === 0) return;

    const params = new URLSearchParams(window.location.search);
    const subworkflowPath = params.get('subworkflow');
    const agent = params.get('agent');

    if (!subworkflowPath && !agent) {
      applied.current = true;
      return;
    }

    // Navigate into the subworkflow path if requested.
    // Supports slash-separated paths for nested subworkflows:
    //   ?subworkflow=planning        → one level deep
    //   ?subworkflow=planning/design → two levels deep
    if (subworkflowPath) {
      const segments = subworkflowPath.split('/').filter(Boolean);
      for (const segment of segments) {
        // Each call reads the latest store state via get(), so sequential
        // calls correctly navigate deeper into the context tree.
        navigateIntoSubworkflow(segment);
      }
      applied.current = true;

      // If there's also an agent param, select it within the subworkflow.
      // The navigateIntoSubworkflow calls above are synchronous (zustand set/get),
      // so the viewed context is already switched when we select the node.
      if (agent) {
        // Use a short delay to let React Flow rebuild the graph for the new context
        setTimeout(() => {
          selectNode(agent);
          requestAnimationFrame(() => {
            fitView({ nodes: [{ id: agent }], padding: 0.5, duration: 400 });
          });
        }, 100);
      }
      return;
    }

    // Select the agent node and center the view on it
    if (agent) {
      const agentExists = agents.some((a) => a.name === agent);
      if (agentExists) {
        selectNode(agent);
        // Allow React Flow to process the selection, then center on the node
        requestAnimationFrame(() => {
          fitView({ nodes: [{ id: agent }], padding: 0.5, duration: 400 });
        });
        applied.current = true;
      }
    }
  }, [agents, selectNode, navigateIntoSubworkflow, fitView]);
}
