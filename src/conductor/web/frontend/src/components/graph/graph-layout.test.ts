import { beforeEach, describe, expect, it } from 'vitest';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { WorkflowEvent } from '@/types/events';
import { buildGraphElements, type GraphContextInput } from './graph-layout';
import { contextKey, nodeKey, parseNodeKey } from '@/lib/node-id';

function event(
  type: WorkflowEvent['type'],
  data: Record<string, unknown>,
  timestamp = Date.now() / 1000,
): WorkflowEvent {
  return { type, timestamp, data };
}

/** Assemble the root graph-context input from the current store state. */
function rootBase(): GraphContextInput {
  const s = useWorkflowStore.getState();
  return {
    agents: s.agents,
    routes: s.routes,
    parallelGroups: s.parallelGroups,
    forEachGroups: s.forEachGroups,
    nodes: s.nodes,
    groupProgress: s.groupProgress,
    entryPoint: s.entryPoint,
    parentAgent: null,
    children: s.subworkflowContexts,
  };
}

/**
 * Dispatch a root workflow that reaches a `type: workflow` step, then start
 * and populate that subworkflow's inner DAG (as the engine does via a
 * `subworkflow_path`-stamped child `workflow_started`).
 */
function seedRootWithStartedSubworkflow(): void {
  const { processEvent } = useWorkflowStore.getState();

  processEvent(
    event('workflow_started', {
      name: 'root',
      agents: [
        { name: 'planner' },
        { name: 'sub_agent', type: 'workflow' },
      ],
      routes: [
        { from: 'planner', to: 'sub_agent' },
        { from: 'sub_agent', to: '$end' },
      ],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'planner',
    }),
  );

  processEvent(
    event('subworkflow_started', {
      agent_name: 'sub_agent',
      workflow: 'sub.yaml',
      iteration: 1,
      slot_key: 'sub_agent',
      parent_path: [],
    }),
  );

  processEvent(
    event('workflow_started', {
      name: 'child-workflow',
      agents: [{ name: 'childA' }, { name: 'childB' }],
      routes: [
        { from: 'childA', to: 'childB' },
        { from: 'childB', to: '$end' },
      ],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'childA',
      subworkflow_path: ['sub_agent'],
    }),
  );
}

beforeEach(() => {
  useWorkflowStore.setState(useWorkflowStore.getInitialState(), true);
});

describe('node-id namespacing helpers', () => {
  it('round-trips root and nested ids', () => {
    expect(contextKey([])).toBe('');
    expect(contextKey([0, 2])).toBe('0.2');
    expect(nodeKey([], 'planner')).toBe('::planner');
    expect(nodeKey([0, 2], 'reviewer')).toBe('0.2::reviewer');

    expect(parseNodeKey('::planner')).toEqual({ contextPath: [], name: 'planner' });
    expect(parseNodeKey('0.2::reviewer')).toEqual({ contextPath: [0, 2], name: 'reviewer' });
    // reserved names survive the split (first `::` is the separator)
    expect(parseNodeKey(nodeKey([1], '$start'))).toEqual({ contextPath: [1], name: '$start' });
  });

  it('treats an un-namespaced id as the root context', () => {
    expect(parseNodeKey('planner')).toEqual({ contextPath: [], name: 'planner' });
  });
});

describe('buildGraphElements — namespacing', () => {
  it('namespaces every root node and edge id with the root context', () => {
    seedRootWithStartedSubworkflow();
    const { nodes, edges } = buildGraphElements(rootBase(), [], new Set());

    for (const n of nodes) {
      expect(n.id.includes('::')).toBe(true);
      expect(parseNodeKey(n.id).contextPath).toEqual([]);
    }
    expect(nodes.some((n) => n.id === nodeKey([], '$start'))).toBe(true);
    expect(nodes.some((n) => n.id === nodeKey([], 'planner'))).toBe(true);
    // Every edge endpoint must resolve to a real node id.
    const ids = new Set(nodes.map((n) => n.id));
    for (const e of edges) {
      expect(ids.has(e.source)).toBe(true);
      expect(ids.has(e.target)).toBe(true);
    }
  });
});

describe('buildGraphElements — inline subworkflow expansion', () => {
  it('renders a subworkflow node collapsed by default (no child nodes)', () => {
    seedRootWithStartedSubworkflow();
    const { nodes } = buildGraphElements(rootBase(), [], new Set());

    const wf = nodes.find((n) => n.id === nodeKey([], 'sub_agent'));
    expect(wf).toBeDefined();
    expect(wf!.type).toBe('workflowNode');
    expect(wf!.data.expanded).toBe(false);
    expect(wf!.data.canExpand).toBe(true);
    expect(wf!.data.childContextKey).toBe(contextKey([0]));

    // No child nodes are present while collapsed.
    expect(nodes.some((n) => n.id === nodeKey([0], 'childA'))).toBe(false);
  });

  it('renders the child DAG nested inside a sized container when expanded', () => {
    seedRootWithStartedSubworkflow();
    const expanded = new Set([contextKey([0])]);
    const { nodes, edges } = buildGraphElements(rootBase(), [], expanded);

    const container = nodes.find((n) => n.id === nodeKey([], 'sub_agent'));
    expect(container).toBeDefined();
    expect(container!.data.expanded).toBe(true);
    // Sized so the parent dagre pass reserves room for the child DAG.
    expect(typeof container!.style?.width).toBe('number');
    expect((container!.style!.width as number) > 0).toBe(true);
    expect((container!.style!.height as number) > 0).toBe(true);

    // Child agents render, namespaced to the child context, parented to the
    // container so React Flow draws them inside it.
    const childA = nodes.find((n) => n.id === nodeKey([0], 'childA'));
    expect(childA).toBeDefined();
    expect(childA!.parentId).toBe(nodeKey([], 'sub_agent'));
    expect(childA!.data.contextPath).toEqual([0]);

    // The child's boundary $start renders as an ingress node.
    const ingress = nodes.find((n) => n.id === nodeKey([0], '$start'));
    expect(ingress).toBeDefined();
    expect(ingress!.type).toBe('ingressNode');
    // Inline, the parent label is suppressed (the container header already
    // names the parent step) — avoids a confusing "return to <self>" read.
    expect(ingress!.data.parentAgent).toBeUndefined();

    // A child-internal edge exists and both endpoints are child-namespaced.
    const internal = edges.find(
      (e) => e.source === nodeKey([0], 'childA') && e.target === nodeKey([0], 'childB'),
    );
    expect(internal).toBeDefined();
  });

  it('keeps the parent label on boundary nodes in the drill-down (non-inline) view', () => {
    seedRootWithStartedSubworkflow();
    const child = useWorkflowStore.getState().subworkflowContexts[0]!;
    const childBase: GraphContextInput = {
      agents: child.agents,
      routes: child.routes,
      parallelGroups: child.parallelGroups,
      forEachGroups: child.forEachGroups,
      nodes: child.nodes,
      groupProgress: child.groupProgress,
      entryPoint: child.entryPoint,
      parentAgent: child.parentAgent,
      children: child.children,
    };
    // Viewed as the base context (drilled in), the boundary keeps its
    // "from/return to <parent>" label since no container header is shown.
    const { nodes } = buildGraphElements(childBase, [0], new Set());
    const ingress = nodes.find((n) => n.id === nodeKey([0], '$start'));
    expect(ingress?.type).toBe('ingressNode');
    expect(ingress?.data.parentAgent).toBe('sub_agent');
  });
});
