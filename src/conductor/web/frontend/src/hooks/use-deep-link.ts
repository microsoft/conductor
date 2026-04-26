import { useEffect, useRef } from 'react';
import { useReactFlow } from '@xyflow/react';
import { useWorkflowStore } from '@/stores/workflow-store';

/**
 * Reads `?agent=` and `?subworkflow=` query params on initial load
 * and auto-selects / navigates to the matching node once the graph
 * has been populated with data.
 *
 * Must be rendered inside a <ReactFlow> provider so useReactFlow() works.
 */
export function useDeepLink() {
  const applied = useRef(false);
  const { fitView } = useReactFlow();

  const agents = useWorkflowStore((s) => s.agents);
  const selectNode = useWorkflowStore((s) => s.selectNode);
  const navigateIntoSubworkflow = useWorkflowStore((s) => s.navigateIntoSubworkflow);
  const subworkflowContexts = useWorkflowStore((s) => s.subworkflowContexts);

  useEffect(() => {
    // Only apply once, and only after agents have loaded
    if (applied.current || agents.length === 0) return;

    const params = new URLSearchParams(window.location.search);
    const subworkflow = params.get('subworkflow');
    const agent = params.get('agent');

    if (!subworkflow && !agent) {
      applied.current = true;
      return;
    }

    // Navigate into the subworkflow first if requested
    if (subworkflow) {
      const hasContext = subworkflowContexts.some((c) => c.parentAgent === subworkflow);
      if (hasContext) {
        navigateIntoSubworkflow(subworkflow);
        applied.current = true;
        return; // context switch will re-render; agent selection in new context
                // would need a second param like ?subworkflow=X&agent=Y — not supported yet
      }
      // If subworkflow context hasn't arrived yet, wait for next render
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
      // If agent hasn't appeared yet (e.g., inside a subworkflow), wait
    }
  }, [agents, subworkflowContexts, selectNode, navigateIntoSubworkflow, fitView]);
}
