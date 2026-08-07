import { beforeEach, describe, expect, it } from 'vitest';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { WorkflowEvent } from '@/types/events';
import { buildGraphElements, type GraphContextInput } from '@/components/graph/graph-layout';
import { contextKey, forEachGroupKey, nodeKey } from '@/lib/node-id';
import {
  anchoredViewport,
  nextAnchorHint,
  resolveAnchorId,
  toAbsolutePositions,
  MAX_STICKY_ANCHOR_REBUILDS,
  type AnchorInput,
  type AnchorNode,
  type Viewport,
} from './graph-anchor';

function node(id: string, x: number, y: number, extra: Partial<AnchorNode> = {}): AnchorNode {
  return { id, position: { x, y }, ...extra };
}

const PANE = { width: 1000, height: 800 };
const IDENTITY: Viewport = { x: 0, y: 0, zoom: 1 };

function input(overrides: Partial<AnchorInput>): AnchorInput {
  return {
    prevNodes: [],
    nextNodes: [],
    anchorKeyHint: null,
    viewport: IDENTITY,
    paneSize: PANE,
    ...overrides,
  };
}

/** Screen-space point a flow-space position lands on under a viewport. */
function toScreen(pos: { x: number; y: number }, vp: Viewport) {
  return { x: pos.x * vp.zoom + vp.x, y: pos.y * vp.zoom + vp.y };
}

describe('toAbsolutePositions', () => {
  it('returns positions unchanged for top-level nodes', () => {
    const abs = toAbsolutePositions([node('a', 10, 20), node('b', 30, 40)]);
    expect(abs.get('a')).toEqual({ x: 10, y: 20 });
    expect(abs.get('b')).toEqual({ x: 30, y: 40 });
  });

  it('offsets a child by its parent container', () => {
    const abs = toAbsolutePositions([
      node('parent', 100, 200),
      node('child', 5, 7, { parentId: 'parent' }),
    ]);
    expect(abs.get('child')).toEqual({ x: 105, y: 207 });
  });

  it('accumulates a multi-level parent chain', () => {
    const abs = toAbsolutePositions([
      node('grandchild', 1, 2, { parentId: 'child' }),
      node('child', 10, 20, { parentId: 'parent' }),
      node('parent', 100, 200),
    ]);
    expect(abs.get('grandchild')).toEqual({ x: 111, y: 222 });
    expect(abs.get('child')).toEqual({ x: 110, y: 220 });
    expect(abs.get('parent')).toEqual({ x: 100, y: 200 });
  });

  it('resolves every node of a chain identically regardless of node order', () => {
    const nodes = [
      node('parent', 100, 200),
      node('child', 10, 20, { parentId: 'parent' }),
      node('grandchild', 1, 2, { parentId: 'child' }),
    ];
    const forward = toAbsolutePositions(nodes);
    const reversed = toAbsolutePositions([...nodes].reverse());
    for (const id of ['parent', 'child', 'grandchild']) {
      expect(reversed.get(id), id).toEqual(forward.get(id));
    }
  });

  it('resolves a deep chain correctly from either end', () => {
    // The memoized fold has no depth cap, so nesting far past anything
    // `buildGraphElements` emits still resolves exactly rather than truncating.
    const depth = 40;
    const nodes: AnchorNode[] = [];
    for (let i = 0; i < depth; i++) {
      nodes.push(i === 0 ? node('n0', 1, 1) : node(`n${i}`, 1, 1, { parentId: `n${i - 1}` }));
    }
    const leaf = `n${depth - 1}`;
    expect(toAbsolutePositions(nodes).get(leaf)).toEqual({ x: depth, y: depth });
    expect(toAbsolutePositions([...nodes].reverse()).get(leaf)).toEqual({ x: depth, y: depth });
  });

  it('treats a dangling parentId as top-level rather than throwing', () => {
    const abs = toAbsolutePositions([node('orphan', 5, 6, { parentId: 'missing' })]);
    expect(abs.get('orphan')).toEqual({ x: 5, y: 6 });
  });

  it('terminates on a parentId cycle', () => {
    const abs = toAbsolutePositions([
      node('a', 1, 1, { parentId: 'b' }),
      node('b', 2, 2, { parentId: 'a' }),
    ]);
    expect(abs.size).toBe(2);
    for (const pos of abs.values()) {
      expect(Number.isFinite(pos.x)).toBe(true);
      expect(Number.isFinite(pos.y)).toBe(true);
    }
  });
});

describe('resolveAnchorId', () => {
  it('returns null when the two layouts share no node', () => {
    const anchor = resolveAnchorId(
      input({ prevNodes: [node('a', 0, 0)], nextNodes: [node('b', 0, 0)] }),
    );
    expect(anchor).toBeNull();
  });

  it('prefers the container owning the hinted key over the one nearest the center', () => {
    // `near` sits exactly at the pane center; `toggled` is far away.
    const prevNodes = [
      node('near', 500, 400),
      node('toggled', 5000, 4000, { data: { childContextKey: '0' } }),
    ];
    const anchor = resolveAnchorId(input({ prevNodes, nextNodes: prevNodes, anchorKeyHint: '0' }));
    expect(anchor).toBe('toggled');
  });

  it('matches a for_each group hint via groupExpansionKey', () => {
    const prevNodes = [
      node('near', 500, 400),
      node('group', 5000, 4000, { data: { groupExpansionKey: '::batch' } }),
    ];
    const anchor = resolveAnchorId(
      input({ prevNodes, nextNodes: prevNodes, anchorKeyHint: '::batch' }),
    );
    expect(anchor).toBe('group');
  });

  it('matches a hint carried only by the previous layout', () => {
    // A group is only assigned `groupExpansionKey` once it is expandable, so the
    // key can be present on one side of a rebuild and absent on the other.
    const prevNodes = [
      node('near', 500, 400),
      node('group', 5000, 4000, { data: { groupExpansionKey: '::batch' } }),
    ];
    const nextNodes = [node('near', 500, 400), node('group', 5000, 4000)];
    const anchor = resolveAnchorId(input({ prevNodes, nextNodes, anchorKeyHint: '::batch' }));
    expect(anchor).toBe('group');
  });

  it('falls back to the nearest node when the hinted container is not shared', () => {
    const prevNodes = [
      node('near', 500, 400),
      node('gone', 5000, 4000, { data: { childContextKey: '0' } }),
    ];
    const nextNodes = [node('near', 500, 400)];
    const anchor = resolveAnchorId(input({ prevNodes, nextNodes, anchorKeyHint: '0' }));
    expect(anchor).toBe('near');
  });

  it('picks the shared node nearest the pane center', () => {
    const prevNodes = [node('far', 0, 0), node('close', 480, 390), node('mid', 200, 200)];
    const anchor = resolveAnchorId(input({ prevNodes, nextNodes: prevNodes }));
    expect(anchor).toBe('close');
  });

  it('measures distance in the previous layout, not the incoming one', () => {
    // The pane center describes the layout currently on screen. `a` is at the
    // center before the rebuild; after it, `b` is. The anchor must be `a`.
    const prevNodes = [node('a', 500, 400), node('b', 5000, 4000)];
    const nextNodes = [node('a', 5000, 4000), node('b', 500, 400)];
    expect(resolveAnchorId(input({ prevNodes, nextNodes }))).toBe('a');
  });

  it('accounts for pan and zoom when locating the pane center', () => {
    // At zoom 2 panned to (-1000, -800) the pane center is flow-space (750, 600).
    const viewport: Viewport = { x: -1000, y: -800, zoom: 2 };
    const prevNodes = [node('a', 0, 0), node('b', 750, 600)];
    const anchor = resolveAnchorId(input({ prevNodes, nextNodes: prevNodes, viewport }));
    expect(anchor).toBe('b');
  });

  it('breaks ties deterministically on node id', () => {
    // Both are equidistant from the center (500, 400).
    const prevNodes = [node('zulu', 400, 400), node('alpha', 600, 400)];
    const forward = resolveAnchorId(input({ prevNodes, nextNodes: prevNodes }));
    const reversed = resolveAnchorId(
      input({ prevNodes: [...prevNodes].reverse(), nextNodes: [...prevNodes].reverse() }),
    );
    expect(forward).toBe('alpha');
    expect(reversed).toBe('alpha');
  });

  it('declines to anchor on an unmeasured pane with no hint', () => {
    const prevNodes = [node('a', 0, 0), node('b', 100, 100)];
    const anchor = resolveAnchorId(
      input({ prevNodes, nextNodes: prevNodes, paneSize: { width: 0, height: 0 } }),
    );
    expect(anchor).toBeNull();
  });

  it('still honours the hint on an unmeasured pane', () => {
    const prevNodes = [node('a', 0, 0, { data: { childContextKey: '0' } })];
    const anchor = resolveAnchorId(
      input({
        prevNodes,
        nextNodes: prevNodes,
        anchorKeyHint: '0',
        paneSize: { width: 0, height: 0 },
      }),
    );
    expect(anchor).toBe('a');
  });
});

describe('anchoredViewport', () => {
  it('returns null when nothing is shared', () => {
    expect(
      anchoredViewport(input({ prevNodes: [node('a', 0, 0)], nextNodes: [node('b', 0, 0)] })),
    ).toBeNull();
  });

  it('returns null when the anchor did not move', () => {
    const nodes = [node('a', 10, 10)];
    expect(anchoredViewport(input({ prevNodes: nodes, nextNodes: nodes }))).toBeNull();
  });

  it('pans by the negated delta so the anchor stays put on screen', () => {
    const prevNodes = [node('a', 100, 100)];
    const nextNodes = [node('a', 130, 160)];
    const next = anchoredViewport(input({ prevNodes, nextNodes }))!;
    expect(next).toEqual({ x: -30, y: -60, zoom: 1 });
    expect(toScreen({ x: 130, y: 160 }, next)).toEqual(toScreen({ x: 100, y: 100 }, IDENTITY));
  });

  it('scales the compensation by zoom and leaves zoom untouched', () => {
    const viewport: Viewport = { x: 40, y: 90, zoom: 1.5 };
    const prevNodes = [node('a', 100, 100)];
    const nextNodes = [node('a', 120, 100)];
    const next = anchoredViewport(input({ prevNodes, nextNodes, viewport }))!;
    expect(next.zoom).toBe(1.5);
    expect(next).toEqual({ x: 40 - 30, y: 90, zoom: 1.5 });
    expect(toScreen({ x: 120, y: 100 }, next)).toEqual(toScreen({ x: 100, y: 100 }, viewport));
  });

  it('compensates a node that gained a parent container across the rebuild', () => {
    // Collapsed: the pill is top-level at (300, 300).
    const prevNodes = [node('pill', 300, 300)];
    // Expanded: it is re-parented into a container, so its own position is now
    // container-relative — absolute (250 + 20, 280 + 40) = (270, 320).
    const nextNodes = [
      node('container', 250, 280, { data: { groupExpansionKey: '::batch' } }),
      node('pill', 20, 40, { parentId: 'container' }),
    ];
    const next = anchoredViewport(input({ prevNodes, nextNodes }))!;
    expect(next).toEqual({ x: 30, y: -20, zoom: 1 });
    expect(toScreen({ x: 270, y: 320 }, next)).toEqual(toScreen({ x: 300, y: 300 }, IDENTITY));
  });

  it('returns to the original viewport across an expand then collapse', () => {
    const collapsed = [
      node('start', 0, 0),
      node('sub', 0, 120, { data: { childContextKey: '0' } }),
      node('end', 0, 240),
    ];
    // Expanding `sub` grows it, and origin normalization shifts everything.
    const expanded = [
      node('start', 40, 0),
      node('sub', 0, 130, { data: { childContextKey: '0' } }),
      node('inner', 20, 40, { parentId: 'sub' }),
      node('end', 40, 500),
    ];

    const start: Viewport = { x: 17, y: -43, zoom: 1.25 };

    const afterExpand = anchoredViewport(
      input({ prevNodes: collapsed, nextNodes: expanded, anchorKeyHint: '0', viewport: start }),
    );
    expect(afterExpand).not.toBeNull();

    const afterCollapse = anchoredViewport(
      input({
        prevNodes: expanded,
        nextNodes: collapsed,
        anchorKeyHint: '0',
        viewport: afterExpand!,
      }),
    );
    expect(afterCollapse).toEqual(start);
  });
});

describe('nextAnchorHint', () => {
  const base = {
    previousKeys: new Set<string>(),
    currentKeys: new Set<string>(),
    currentHint: null as string | null,
    stickyRebuilds: 0,
    contextSwitched: false,
  };

  it('pins the container when exactly one key is expanded', () => {
    expect(nextAnchorHint({ ...base, currentKeys: new Set(['0']) })).toEqual({
      hint: '0',
      stickyRebuilds: 0,
    });
  });

  it('pins the container when exactly one key is collapsed', () => {
    expect(
      nextAnchorHint({ ...base, previousKeys: new Set(['0', '1']), currentKeys: new Set(['1']) }),
    ).toEqual({ hint: '0', stickyRebuilds: 0 });
  });

  it('drops the hint when several keys toggle at once', () => {
    expect(nextAnchorHint({ ...base, currentKeys: new Set(['0', '1', '2']) })).toEqual({
      hint: null,
      stickyRebuilds: 0,
    });
  });

  it('drops the hint on a context switch even when a key toggled', () => {
    expect(
      nextAnchorHint({ ...base, currentKeys: new Set(['0']), contextSwitched: true }),
    ).toEqual({ hint: null, stickyRebuilds: 0 });
  });

  it('keeps the hint across a rebuild that toggles nothing', () => {
    // The expanded child's DAG arrives a beat after the chevron click and grows
    // the same container again.
    const keys = new Set(['0']);
    expect(
      nextAnchorHint({ ...base, previousKeys: keys, currentKeys: keys, currentHint: '0' }),
    ).toEqual({ hint: '0', stickyRebuilds: 1 });
  });

  it('releases the hint once the sticky budget is exhausted', () => {
    const keys = new Set(['0']);
    let state = { hint: '0' as string | null, stickyRebuilds: 0 };
    for (let i = 0; i < MAX_STICKY_ANCHOR_REBUILDS; i++) {
      state = nextAnchorHint({
        ...base,
        previousKeys: keys,
        currentKeys: keys,
        currentHint: state.hint,
        stickyRebuilds: state.stickyRebuilds,
      });
      expect(state.hint).toBe('0');
    }
    state = nextAnchorHint({
      ...base,
      previousKeys: keys,
      currentKeys: keys,
      currentHint: state.hint,
      stickyRebuilds: state.stickyRebuilds,
    });
    expect(state.hint).toBeNull();
  });

  it('stays null when there is no hint to keep', () => {
    const keys = new Set(['0']);
    expect(
      nextAnchorHint({ ...base, previousKeys: keys, currentKeys: keys, currentHint: null }),
    ).toEqual({ hint: null, stickyRebuilds: 0 });
  });

  it('resets the sticky budget when a new key is toggled', () => {
    expect(
      nextAnchorHint({ ...base, currentKeys: new Set(['1']), currentHint: '0', stickyRebuilds: 2 }),
    ).toEqual({ hint: '1', stickyRebuilds: 0 });
  });
});

// ---------------------------------------------------------------------------
// Realistic fixtures: anchor against genuine `buildGraphElements` output rather
// than hand-written positions, so the `parentId` handling is proven against the
// shapes the layout actually emits.
//
// Each fixture expands a *second* container while a first is already expanded.
// That is deliberate: expanding the only container leaves it at the layout's
// min-x/min-y, so it does not move and there is nothing to compensate — an
// assertion there would hold against a `anchoredViewport` that always returned
// null. A container that starts beside a wider sibling does move.
// ---------------------------------------------------------------------------

function event(
  type: WorkflowEvent['type'],
  data: Record<string, unknown>,
  timestamp = Date.now() / 1000,
): WorkflowEvent {
  return { type, timestamp, data };
}

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

/** Root workflow with two sequential `type: workflow` steps, both started. */
function seedTwoSubworkflows(): void {
  const { processEvent } = useWorkflowStore.getState();

  processEvent(
    event('workflow_started', {
      name: 'root',
      agents: [
        { name: 'planner' },
        { name: 'subA', type: 'workflow' },
        { name: 'subB', type: 'workflow' },
        { name: 'tail' },
      ],
      routes: [
        { from: 'planner', to: 'subA' },
        { from: 'subA', to: 'subB' },
        { from: 'subB', to: 'tail' },
        { from: 'tail', to: '$end' },
      ],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'planner',
    }),
  );

  for (const name of ['subA', 'subB']) {
    processEvent(
      event('subworkflow_started', {
        agent_name: name,
        workflow: 'sub.yaml',
        iteration: 1,
        slot_key: name,
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
        subworkflow_path: [name],
      }),
    );
  }
}

/** Root workflow with a `for_each`-of-workflow group plus a sequential one. */
function seedForEachBesideSubworkflow(iterations = 2): void {
  const { processEvent } = useWorkflowStore.getState();

  processEvent(
    event('workflow_started', {
      name: 'root',
      agents: [{ name: 'finder' }, { name: 'wide', type: 'workflow' }, { name: 'aggregator' }],
      routes: [
        { from: 'finder', to: 'wide' },
        { from: 'wide', to: 'batch' },
        { from: 'batch', to: 'aggregator' },
        { from: 'aggregator', to: '$end' },
      ],
      parallel_groups: [],
      for_each_groups: [{ name: 'batch' }],
      entry_point: 'finder',
    }),
  );

  processEvent(
    event('subworkflow_started', {
      agent_name: 'wide',
      workflow: 'sub.yaml',
      iteration: 1,
      slot_key: 'wide',
      parent_path: [],
    }),
  );
  processEvent(
    event('workflow_started', {
      name: 'child-workflow',
      agents: [{ name: 'w1' }, { name: 'w2' }],
      routes: [
        { from: 'w1', to: 'w2' },
        { from: 'w2', to: '$end' },
      ],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'w1',
      subworkflow_path: ['wide'],
    }),
  );

  for (let i = 0; i < iterations; i++) {
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

/** Assert `anchorId`'s top-left lands on the same screen point before and after. */
function expectPinned(
  prevNodes: AnchorNode[],
  nextNodes: AnchorNode[],
  anchorId: string,
  viewport: Viewport,
  next: Viewport,
) {
  const before = toAbsolutePositions(prevNodes).get(anchorId)!;
  const after = toAbsolutePositions(nextNodes).get(anchorId)!;
  expect(before, `${anchorId} must actually move for this to test anything`).not.toEqual(after);
  expect(toScreen(after, next).x).toBeCloseTo(toScreen(before, viewport).x, 6);
  expect(toScreen(after, next).y).toBeCloseTo(toScreen(before, viewport).y, 6);
}

beforeEach(() => {
  useWorkflowStore.setState(useWorkflowStore.getInitialState(), true);
});

describe('anchoring real layouts', () => {
  it('keeps an expanded subworkflow container pinned on screen', () => {
    seedTwoSubworkflows();
    const keyA = contextKey([0]);
    const keyB = contextKey([1]);
    const containerId = nodeKey([], 'subB');

    const one = buildGraphElements(rootBase(), [], new Set([keyA])).nodes as AnchorNode[];
    const two = buildGraphElements(rootBase(), [], new Set([keyA, keyB])).nodes as AnchorNode[];

    // Sanity: this is the scenario under test — the container really grows.
    const grown = two.find((n) => n.id === containerId)!;
    expect(grown.data!.expanded).toBe(true);
    expect(typeof (grown as { style?: { width?: unknown } }).style?.width).toBe('number');

    const viewport: Viewport = { x: -120, y: -60, zoom: 0.9 };
    const next = anchoredViewport({
      prevNodes: one,
      nextNodes: two,
      anchorKeyHint: keyB,
      viewport,
      paneSize: PANE,
    });

    expect(next).not.toBeNull();
    expectPinned(one, two, containerId, viewport, next!);
  });

  it('round-trips a real expand then collapse back to the original viewport', () => {
    seedTwoSubworkflows();
    const keyA = contextKey([0]);
    const keyB = contextKey([1]);

    const one = buildGraphElements(rootBase(), [], new Set([keyA])).nodes as AnchorNode[];
    const two = buildGraphElements(rootBase(), [], new Set([keyA, keyB])).nodes as AnchorNode[];

    const start: Viewport = { x: -120, y: -60, zoom: 0.9 };
    const afterExpand = anchoredViewport({
      prevNodes: one,
      nextNodes: two,
      anchorKeyHint: keyB,
      viewport: start,
      paneSize: PANE,
    });
    expect(afterExpand).not.toBeNull();

    const afterCollapse = anchoredViewport({
      prevNodes: two,
      nextNodes: one,
      anchorKeyHint: keyB,
      viewport: afterExpand!,
      paneSize: PANE,
    });
    expect(afterCollapse).not.toBeNull();

    expect(afterCollapse!.x).toBeCloseTo(start.x, 6);
    expect(afterCollapse!.y).toBeCloseTo(start.y, 6);
    expect(afterCollapse!.zoom).toBe(start.zoom);
  });

  it('pins a for_each group whose iteration pills get re-parented on expand', () => {
    seedForEachBesideSubworkflow(2);
    const wideKey = contextKey([0]);
    const groupKey = forEachGroupKey([], 'batch');
    const groupId = nodeKey([], 'batch');

    const one = buildGraphElements(rootBase(), [], new Set([wideKey])).nodes as AnchorNode[];
    const two = buildGraphElements(rootBase(), [], new Set([wideKey, groupKey]))
      .nodes as AnchorNode[];

    // Sanity: expansion really does re-parent the iteration pills.
    const pill = two.find((n) => n.id === nodeKey([], 'batch[0]'))!;
    expect(pill.parentId).toBe(groupId);
    expect(one.some((n) => n.id === pill.id)).toBe(false);

    const viewport: Viewport = { x: 33, y: -77, zoom: 1.1 };
    const next = anchoredViewport({
      prevNodes: one,
      nextNodes: two,
      anchorKeyHint: groupKey,
      viewport,
      paneSize: PANE,
    });

    expect(next).not.toBeNull();
    expectPinned(one, two, groupId, viewport, next!);
  });
});
