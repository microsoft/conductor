import { beforeEach, describe, expect, it } from 'vitest';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { WorkflowEvent } from '@/types/events';
import { buildGraphElements, collectExpandableContextKeys, expansionKeysForContextPath, type GraphContextInput } from './graph-layout';
import {
  contextKey,
  nodeKey,
  parseNodeKey,
  forEachGroupKey,
  parseForEachSlotKey,
  isGroupExpansionKey,
} from '@/lib/node-id';

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

/**
 * Like {@link seedRootWithStartedSubworkflow} but the child subworkflow itself
 * contains a started `type: workflow` step (`deep_sub`), producing a two-level
 * nesting: root → sub_agent → deep_sub.
 */
function seedNestedSubworkflows(): void {
  const { processEvent } = useWorkflowStore.getState();

  processEvent(
    event('workflow_started', {
      name: 'root',
      agents: [{ name: 'planner' }, { name: 'sub_agent', type: 'workflow' }],
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
      agents: [{ name: 'childA' }, { name: 'deep_sub', type: 'workflow' }],
      routes: [
        { from: 'childA', to: 'deep_sub' },
        { from: 'deep_sub', to: '$end' },
      ],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'childA',
      subworkflow_path: ['sub_agent'],
    }),
  );

  processEvent(
    event('subworkflow_started', {
      agent_name: 'deep_sub',
      workflow: 'deep.yaml',
      iteration: 1,
      slot_key: 'deep_sub',
      parent_path: ['sub_agent'],
    }),
  );

  processEvent(
    event('workflow_started', {
      name: 'grandchild-workflow',
      agents: [{ name: 'g1' }, { name: 'g2' }],
      routes: [
        { from: 'g1', to: 'g2' },
        { from: 'g2', to: '$end' },
      ],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'g1',
      subworkflow_path: ['sub_agent', 'deep_sub'],
    }),
  );
}

/**
 * Dispatch a root workflow with a `for_each`-of-workflow group (`batch`) that
 * has fanned out into `count` started iterations, each its own child
 * subworkflow with an inner DAG (`childA → childB`). Mirrors the engine's
 * slot-keyed events (`slot_key` / `item_key` / `subworkflow_path`).
 */
function seedForEachSubworkflows(count = 2): void {
  const { processEvent } = useWorkflowStore.getState();

  processEvent(
    event('workflow_started', {
      name: 'root',
      agents: [{ name: 'finder' }, { name: 'aggregator' }],
      routes: [
        { from: 'finder', to: 'batch' },
        { from: 'batch', to: 'aggregator' },
        { from: 'aggregator', to: '$end' },
      ],
      parallel_groups: [],
      for_each_groups: [{ name: 'batch' }],
      entry_point: 'finder',
    }),
  );

  for (let i = 0; i < count; i++) {
    processEvent(
      event('subworkflow_started', {
        agent_name: 'batch',
        workflow: 'sub.yaml',
        iteration: i + 1,
        slot_key: `batch[${i}]`,
        item_key: String(i),
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
        subworkflow_path: [`batch[${i}]`],
      }),
    );
  }
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

describe('collectExpandableContextKeys', () => {
  it('returns nothing for a plain workflow with no subworkflows', () => {
    const { processEvent } = useWorkflowStore.getState();
    processEvent(
      event('workflow_started', {
        name: 'root',
        agents: [{ name: 'a' }, { name: 'b' }],
        routes: [
          { from: 'a', to: 'b' },
          { from: 'b', to: '$end' },
        ],
        parallel_groups: [],
        for_each_groups: [],
        entry_point: 'a',
      }),
    );
    const s = useWorkflowStore.getState();
    expect(collectExpandableContextKeys(s.agents, s.subworkflowContexts, [])).toEqual([]);
  });

  it('excludes a subworkflow step whose child DAG has not started yet', () => {
    const { processEvent } = useWorkflowStore.getState();
    // A `type: workflow` step exists, but no subworkflow_started/child
    // workflow_started has populated its inner DAG — nothing to expand.
    processEvent(
      event('workflow_started', {
        name: 'root',
        agents: [{ name: 'planner' }, { name: 'sub_agent', type: 'workflow' }],
        routes: [
          { from: 'planner', to: 'sub_agent' },
          { from: 'sub_agent', to: '$end' },
        ],
        parallel_groups: [],
        for_each_groups: [],
        entry_point: 'planner',
      }),
    );
    const s = useWorkflowStore.getState();
    expect(collectExpandableContextKeys(s.agents, s.subworkflowContexts, [])).toEqual([]);
  });

  it('collects a started sequential subworkflow regardless of expansion state', () => {
    seedRootWithStartedSubworkflow();
    const s = useWorkflowStore.getState();
    // Enumerates the data subtree, not the currently-expanded set.
    expect(s.expandedContexts.size).toBe(0);
    expect(collectExpandableContextKeys(s.agents, s.subworkflowContexts, [])).toEqual([
      contextKey([0]),
    ]);
  });

  it('recurses into nested subworkflows, returning every expandable key', () => {
    seedNestedSubworkflows();
    const s = useWorkflowStore.getState();
    expect(collectExpandableContextKeys(s.agents, s.subworkflowContexts, [])).toEqual([
      contextKey([0]),
      contextKey([0, 0]),
    ]);
  });

  it('namespaces keys relative to the provided basePath (drilled-in view)', () => {
    seedNestedSubworkflows();
    const child = useWorkflowStore.getState().subworkflowContexts[0]!;
    // Viewed as if drilled into sub_agent (basePath [0]); only deep_sub remains.
    expect(collectExpandableContextKeys(child.agents, child.children, [0])).toEqual([
      contextKey([0, 0]),
    ]);
  });
});

describe('expansionKeysForContextPath', () => {
  it('returns no keys for a root-level target', () => {
    seedNestedSubworkflows();
    const s = useWorkflowStore.getState();
    expect(expansionKeysForContextPath(s.subworkflowContexts, [])).toEqual([]);
  });

  it('expands each sequential ancestor and the target context itself', () => {
    seedNestedSubworkflows();
    const s = useWorkflowStore.getState();
    // Target a grandchild agent's context [0, 0]: reveal sub_agent then deep_sub.
    expect(expansionKeysForContextPath(s.subworkflowContexts, [0, 0])).toEqual([
      contextKey([0]),
      contextKey([0, 0]),
    ]);
    // Target the intermediate subworkflow only: just its own context key.
    expect(expansionKeysForContextPath(s.subworkflowContexts, [0])).toEqual([contextKey([0])]);
  });

  it('emits BOTH the group key and the context key for a for_each iteration', () => {
    seedForEachSubworkflows(2);
    const s = useWorkflowStore.getState();
    expect(expansionKeysForContextPath(s.subworkflowContexts, [0])).toEqual([
      forEachGroupKey([], 'batch'),
      contextKey([0]),
    ]);
    expect(expansionKeysForContextPath(s.subworkflowContexts, [1])).toEqual([
      forEachGroupKey([], 'batch'),
      contextKey([1]),
    ]);
  });

  it('stops early when the path points past a materialized context', () => {
    seedNestedSubworkflows();
    const s = useWorkflowStore.getState();
    // [0, 5] — index 5 doesn't exist under sub_agent; only [0] resolves.
    expect(expansionKeysForContextPath(s.subworkflowContexts, [0, 5])).toEqual([contextKey([0])]);
  });

  it('produces keys that actually reveal a nested sequential agent', () => {
    seedNestedSubworkflows();
    const s = useWorkflowStore.getState();
    const keys = expansionKeysForContextPath(s.subworkflowContexts, [0, 0]);
    const { nodes } = buildGraphElements(rootBase(), [], new Set(keys));
    // The grandchild agent g1 renders only when both ancestors are expanded.
    expect(nodes.some((n) => n.id === nodeKey([0, 0], 'g1'))).toBe(true);
  });

  it('produces keys that actually reveal a for_each iteration agent', () => {
    seedForEachSubworkflows(2);
    const s = useWorkflowStore.getState();
    const keys = expansionKeysForContextPath(s.subworkflowContexts, [1]);
    const { nodes } = buildGraphElements(rootBase(), [], new Set(keys));
    // childA inside iteration batch[1] renders only with group + context keys.
    expect(nodes.some((n) => n.id === nodeKey([1], 'childA'))).toBe(true);
  });
});

describe('bulk expand/collapse store actions', () => {
  it('unions keys on expand and removes them on collapse', () => {
    useWorkflowStore.getState().expandContexts(['0', '0.0']);
    expect(useWorkflowStore.getState().expandedContexts).toEqual(new Set(['0', '0.0']));

    // Union is idempotent — re-adding an existing key is a no-op.
    useWorkflowStore.getState().expandContexts(['0']);
    expect(useWorkflowStore.getState().expandedContexts).toEqual(new Set(['0', '0.0']));

    useWorkflowStore.getState().collapseContexts(['0']);
    expect(useWorkflowStore.getState().expandedContexts).toEqual(new Set(['0.0']));
  });

  it('collapse is scoped to the provided keys, preserving others', () => {
    useWorkflowStore.getState().expandContexts(['a', 'b', 'c']);
    useWorkflowStore.getState().collapseContexts(['b']);
    expect(useWorkflowStore.getState().expandedContexts).toEqual(new Set(['a', 'c']));
  });

  it('ignores empty key lists', () => {
    useWorkflowStore.getState().expandContexts(['x']);
    const before = useWorkflowStore.getState().expandedContexts;
    useWorkflowStore.getState().expandContexts([]);
    useWorkflowStore.getState().collapseContexts([]);
    // Same set reference is retained when nothing changes.
    expect(useWorkflowStore.getState().expandedContexts).toBe(before);
  });
});

describe('for_each slot/group key helpers', () => {
  it('parses for_each iteration slot keys and rejects sequential ones', () => {
    expect(parseForEachSlotKey('batch[0]')).toEqual({ group: 'batch', key: '0' });
    expect(parseForEachSlotKey('deep_dive_items[alpha]')).toEqual({
      group: 'deep_dive_items',
      key: 'alpha',
    });
    // A sequential subworkflow slot key equals the bare agent name.
    expect(parseForEachSlotKey('sub_agent')).toBeNull();
    // Leading bracket has no group name.
    expect(parseForEachSlotKey('[0]')).toBeNull();
  });

  it('builds group keys that are distinguishable from context keys', () => {
    expect(forEachGroupKey([], 'batch')).toBe('::batch');
    expect(forEachGroupKey([0, 1], 'batch')).toBe('0.1::batch');
    expect(isGroupExpansionKey(forEachGroupKey([0], 'batch'))).toBe(true);
    // Pure context keys never contain `::`, so they never look like group keys.
    expect(isGroupExpansionKey(contextKey([0, 2]))).toBe(false);
  });
});

describe('buildGraphElements — for_each-of-workflow inline expansion', () => {
  it('marks a started for_each-of-workflow group expandable but renders no members collapsed', () => {
    seedForEachSubworkflows(2);
    const { nodes } = buildGraphElements(rootBase(), [], new Set());

    const group = nodes.find((n) => n.id === nodeKey([], 'batch'));
    expect(group).toBeDefined();
    expect(group!.type).toBe('groupNode');
    expect(group!.data.type).toBe('for_each_group');
    expect(group!.data.canExpand).toBe(true);
    expect(group!.data.expanded).toBe(false);
    expect(group!.data.groupExpansionKey).toBe(forEachGroupKey([], 'batch'));

    // No iteration members while collapsed.
    expect(nodes.some((n) => n.id === nodeKey([], 'batch[0]'))).toBe(false);
  });

  it('is not expandable before any iteration has started', () => {
    // A for_each group declared but not yet fanned out (no child contexts).
    useWorkflowStore.getState().processEvent(
      event('workflow_started', {
        name: 'root',
        agents: [{ name: 'finder' }, { name: 'aggregator' }],
        routes: [
          { from: 'finder', to: 'batch' },
          { from: 'batch', to: 'aggregator' },
          { from: 'aggregator', to: '$end' },
        ],
        parallel_groups: [],
        for_each_groups: [{ name: 'batch' }],
        entry_point: 'finder',
      }),
    );
    const { nodes } = buildGraphElements(rootBase(), [], new Set());
    const group = nodes.find((n) => n.id === nodeKey([], 'batch'));
    expect(group!.data.canExpand).toBe(false);
    expect(group!.data.groupExpansionKey).toBeUndefined();
  });

  it('renders each iteration as a collapsed pill parented to the group container when expanded', () => {
    seedForEachSubworkflows(2);
    const expanded = new Set([forEachGroupKey([], 'batch')]);
    const { nodes } = buildGraphElements(rootBase(), [], expanded);

    const group = nodes.find((n) => n.id === nodeKey([], 'batch'));
    expect(group!.data.expanded).toBe(true);
    expect(typeof group!.style?.width).toBe('number');
    expect((group!.style!.width as number) > 0).toBe(true);
    expect((group!.style!.height as number) > 0).toBe(true);

    for (const key of ['batch[0]', 'batch[1]']) {
      const pill = nodes.find((n) => n.id === nodeKey([], key));
      expect(pill, key).toBeDefined();
      expect(pill!.type).toBe('workflowNode');
      expect(pill!.parentId).toBe(nodeKey([], 'batch'));
      expect(pill!.data.type).toBe('workflow');
      expect(pill!.data.isForEachIteration).toBe(true);
      expect(pill!.data.canExpand).toBe(true);
      expect(pill!.data.expanded).toBe(false);
    }
    // batch[0] is the parent's children[0], so its own context key is "0".
    const pill0 = nodes.find((n) => n.id === nodeKey([], 'batch[0]'))!;
    expect(pill0.data.childContextKey).toBe(contextKey([0]));
    expect(pill0.data.iterationContextPath).toEqual([0]);

    // Iteration inner DAGs stay hidden while the pills are collapsed.
    expect(nodes.some((n) => n.id === nodeKey([0], 'childA'))).toBe(false);
  });

  it('embeds an individual iteration inner DAG when that iteration is expanded', () => {
    seedForEachSubworkflows(2);
    const expanded = new Set([forEachGroupKey([], 'batch'), contextKey([0])]);
    const { nodes, edges } = buildGraphElements(rootBase(), [], expanded);

    const pill0 = nodes.find((n) => n.id === nodeKey([], 'batch[0]'))!;
    expect(pill0.data.expanded).toBe(true);
    expect((pill0.style!.width as number) > 0).toBe(true);

    // childA/childB render inside iteration 0, namespaced to its context [0]
    // and parented to the iteration pill.
    const childA = nodes.find((n) => n.id === nodeKey([0], 'childA'));
    expect(childA).toBeDefined();
    expect(childA!.parentId).toBe(nodeKey([], 'batch[0]'));
    expect(childA!.data.contextPath).toEqual([0]);
    const internal = edges.find(
      (e) => e.source === nodeKey([0], 'childA') && e.target === nodeKey([0], 'childB'),
    );
    expect(internal).toBeDefined();

    // The other iteration stays a collapsed pill (no inner nodes).
    expect(nodes.some((n) => n.id === nodeKey([1], 'childA'))).toBe(false);
  });

  it('collectExpandableContextKeys returns the group key, not per-iteration keys', () => {
    seedForEachSubworkflows(3);
    const s = useWorkflowStore.getState();
    expect(collectExpandableContextKeys(s.agents, s.subworkflowContexts, [])).toEqual([
      forEachGroupKey([], 'batch'),
    ]);
  });
});
