import dagre from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';
import type {
  WorkflowAgent,
  RouteEdge,
  ParallelGroup,
  ForEachGroup,
  NodeData,
  GroupProgress,
  SubworkflowContext,
} from '@/stores/workflow-store';
import type { NodeType } from '@/lib/constants';
import { contextKey, nodeKey } from '@/lib/node-id';

export interface GraphNodeData {
  label: string;
  type: NodeType;
  status: string;
  groupName?: string;
  progress?: GroupProgress;
  parentAgent?: string;
  /**
   * Absolute numeric index path of the context this node belongs to (root =
   * `[]`). Together with `name` this maps a namespaced flow-node id back to the
   * store context that owns its live state. Populated by `buildGraphElements`.
   */
  contextPath?: number[];
  /** Bare node name (agent/group/`$start`) without the context namespace. */
  name?: string;
  [key: string]: unknown;
}

const NODE_WIDTH = 200;
const NODE_HEIGHT = 56;
const GROUP_PADDING_X = 20;
const GROUP_PADDING_TOP = 40;
const GROUP_PADDING_BOTTOM = 20;
const GROUP_CHILD_GAP = 12;

// Chrome around an inline-expanded subworkflow container: horizontal padding,
// a top header band (icon + name + collapse chevron), and bottom padding.
const SUBFLOW_PADDING_X = 16;
const SUBFLOW_HEADER = 46;
const SUBFLOW_PADDING_BOTTOM = 16;

/** Normalized single-context graph input for {@link buildGraphElements}. */
export interface GraphContextInput {
  agents: WorkflowAgent[];
  routes: RouteEdge[];
  parallelGroups: ParallelGroup[];
  forEachGroups: ForEachGroup[];
  nodes: Record<string, NodeData>;
  groupProgress: Record<string, GroupProgress>;
  entryPoint: string | null;
  parentAgent: string | null;
  children: SubworkflowContext[];
}

function contextToInput(c: SubworkflowContext): GraphContextInput {
  return {
    agents: c.agents,
    routes: c.routes,
    parallelGroups: c.parallelGroups,
    forEachGroups: c.forEachGroups,
    nodes: c.nodes,
    groupProgress: c.groupProgress,
    entryPoint: c.entryPoint,
    parentAgent: c.parentAgent,
    children: c.children,
  };
}

/**
 * Build React Flow nodes/edges for the viewed workflow context, rendering any
 * inline-expanded subworkflows as nested containers.
 *
 * @param base The viewed context (root store fields or a drilled-in subworkflow).
 * @param basePath Absolute numeric index path of `base` (root workflow = `[]`).
 * @param expandedContexts Context keys (see `lib/node-id`) expanded inline.
 */
export function buildGraphElements(
  base: GraphContextInput,
  basePath: number[],
  expandedContexts: Set<string>,
): { nodes: Node<GraphNodeData>[]; edges: Edge[] } {
  const { nodes, edges } = layoutContext(base, basePath, expandedContexts);
  return { nodes, edges };
}

/**
 * Collect the context keys of every inline-expandable subworkflow reachable
 * from a viewed context, walking the full context subtree (not just what is
 * currently expanded). Used by the graph's Expand/Collapse-all control.
 *
 * Only sequential `type: workflow` steps are inline-expandable in this phase:
 * their child context's `slotKey` equals the agent name and the child must have
 * at least one agent (mirrors `canExpand` in {@link layoutContext}). `for_each`
 * subworkflow iterations are intentionally excluded — their child contexts are
 * keyed `group[key]`, never match an agent name, and render only via drill-down.
 *
 * @param agents The viewed context's agents.
 * @param children The viewed context's child subworkflow contexts.
 * @param basePath Absolute numeric index path of the viewed context (root = `[]`).
 */
export function collectExpandableContextKeys(
  agents: WorkflowAgent[],
  children: SubworkflowContext[],
  basePath: number[],
): string[] {
  const keys: string[] = [];

  const walk = (
    ctxAgents: WorkflowAgent[],
    ctxChildren: SubworkflowContext[],
    absPath: number[],
  ): void => {
    for (const a of ctxAgents) {
      if ((a.type || 'agent') !== 'workflow') continue;
      const childIdx = ctxChildren.findIndex((c) => c.slotKey === a.name);
      if (childIdx < 0) continue;
      const child = ctxChildren[childIdx]!;
      if (child.agents.length === 0) continue;
      keys.push(contextKey([...absPath, childIdx]));
      walk(child.agents, child.children, [...absPath, childIdx]);
    }
  };

  walk(agents, children, basePath);
  return keys;
}

interface ContextLayout {
  nodes: Node<GraphNodeData>[];
  edges: Edge[];
  width: number;
  height: number;
}

/**
 * Recursively lay out a single context and its inline-expanded descendants.
 *
 * Node IDs are namespaced by `absPath` (see `lib/node-id`), so the same agent
 * name in different contexts / iterations never collides. Returned node
 * positions are normalized so the context's bounding box starts at (0, 0); when
 * embedding as a container the caller re-parents and offsets the top-level
 * nodes past the container header.
 */
function layoutContext(
  ctx: GraphContextInput,
  absPath: number[],
  expandedContexts: Set<string>,
  inline = false,
): ContextLayout {
  const flowNodes: Node<GraphNodeData>[] = [];
  const flowEdges: Edge[] = [];
  const agentNames = new Set<string>();
  const groupAgents = new Set<string>();
  const isSubworkflow = ctx.parentAgent != null;

  /** Namespace a bare node name into this context. */
  const nid = (name: string) => nodeKey(absPath, name);

  // Expanded subworkflow child layouts, embedded after this context's dagre.
  const embeds: { containerId: string; sub: ContextLayout }[] = [];

  const agentToGroup = new Map<string, string>();
  for (const pg of ctx.parallelGroups) {
    for (const a of pg.agents) {
      groupAgents.add(a);
      agentToGroup.set(a, pg.name);
    }
  }

  // Parallel group containers (vertical stack of children).
  for (const pg of ctx.parallelGroups) {
    const nd = ctx.nodes[pg.name];
    const childCount = pg.agents.length;
    const groupWidth = NODE_WIDTH + GROUP_PADDING_X * 2;
    const groupHeight =
      GROUP_PADDING_TOP +
      childCount * NODE_HEIGHT +
      (childCount - 1) * GROUP_CHILD_GAP +
      GROUP_PADDING_BOTTOM;

    flowNodes.push({
      id: nid(pg.name),
      type: 'groupNode',
      position: { x: 0, y: 0 },
      data: {
        label: pg.name,
        name: pg.name,
        contextPath: absPath,
        type: 'parallel_group',
        status: nd?.status || 'pending',
        groupName: pg.name,
        progress: ctx.groupProgress[pg.name],
      },
      style: { width: groupWidth, height: groupHeight },
    });

    for (let i = 0; i < pg.agents.length; i++) {
      const agentName = pg.agents[i]!;
      const agentNd = ctx.nodes[agentName];
      flowNodes.push({
        id: nid(agentName),
        type: 'agentNode',
        position: {
          x: GROUP_PADDING_X,
          y: GROUP_PADDING_TOP + i * (NODE_HEIGHT + GROUP_CHILD_GAP),
        },
        parentId: nid(pg.name),
        extent: 'parent' as const,
        data: {
          label: agentName,
          name: agentName,
          contextPath: absPath,
          type: 'agent',
          status: agentNd?.status || 'pending',
        },
      });
    }
    agentNames.add(pg.name);
  }

  // For-each group nodes.
  for (const fg of ctx.forEachGroups) {
    const nd = ctx.nodes[fg.name];
    flowNodes.push({
      id: nid(fg.name),
      type: 'groupNode',
      position: { x: 0, y: 0 },
      data: {
        label: fg.name,
        name: fg.name,
        contextPath: absPath,
        type: 'for_each_group',
        status: nd?.status || 'pending',
        groupName: fg.name,
        progress: ctx.groupProgress[fg.name],
      },
    });
    agentNames.add(fg.name);
  }

  // Standalone nodes (agent/script/set/gate/workflow/wait/terminate).
  for (const a of ctx.agents) {
    if (agentNames.has(a.name) || groupAgents.has(a.name)) continue;
    const nodeType = (a.type || 'agent') as NodeType;
    const nd = ctx.nodes[a.name];
    let flowNodeType = 'agentNode';
    if (nodeType === 'script') flowNodeType = 'scriptNode';
    else if (nodeType === 'set') flowNodeType = 'setNode';
    else if (nodeType === 'human_gate') flowNodeType = 'gateNode';
    else if (nodeType === 'workflow') flowNodeType = 'workflowNode';
    else if (nodeType === 'wait') flowNodeType = 'waitNode';
    else if (nodeType === 'terminate') flowNodeType = 'terminateNode';

    if (nodeType === 'workflow') {
      // Sequential subworkflow: slotKey === agent name. Locate its child
      // context so the node can advertise a stable expansion key and, when
      // expanded, render the child DAG inline as a container.
      const childIdx = ctx.children.findIndex((c) => c.slotKey === a.name);
      const child = childIdx >= 0 ? ctx.children[childIdx] : undefined;
      const childKey = childIdx >= 0 ? contextKey([...absPath, childIdx]) : undefined;
      const canExpand = !!child && child.agents.length > 0;
      const isExpanded = canExpand && childKey != null && expandedContexts.has(childKey);

      if (isExpanded && child) {
        const sub = layoutContext(contextToInput(child), [...absPath, childIdx], expandedContexts, true);
        const containerWidth = sub.width + SUBFLOW_PADDING_X * 2;
        const containerHeight = sub.height + SUBFLOW_HEADER + SUBFLOW_PADDING_BOTTOM;
        flowNodes.push({
          id: nid(a.name),
          type: 'workflowNode',
          position: { x: 0, y: 0 },
          data: {
            label: a.name,
            name: a.name,
            contextPath: absPath,
            type: nodeType,
            status: nd?.status || 'pending',
            expanded: true,
            canExpand: true,
            childContextKey: childKey,
            childName: child.workflowName || undefined,
          },
          style: { width: containerWidth, height: containerHeight },
        });
        embeds.push({ containerId: nid(a.name), sub });
      } else {
        flowNodes.push({
          id: nid(a.name),
          type: 'workflowNode',
          position: { x: 0, y: 0 },
          data: {
            label: a.name,
            name: a.name,
            contextPath: absPath,
            type: nodeType,
            status: nd?.status || 'pending',
            expanded: false,
            canExpand,
            childContextKey: childKey,
            childName: child?.workflowName || undefined,
          },
        });
      }
      agentNames.add(a.name);
      continue;
    }

    flowNodes.push({
      id: nid(a.name),
      type: flowNodeType,
      position: { x: 0, y: 0 },
      data: {
        label: a.name,
        name: a.name,
        contextPath: absPath,
        type: nodeType,
        status: nd?.status || 'pending',
      },
    });
    agentNames.add(a.name);
  }

  // $end (rendered as egress inside a subworkflow).
  let hasEnd = false;
  for (const r of ctx.routes) if (r.to === '$end') hasEnd = true;
  if (hasEnd) {
    const nd = ctx.nodes['$end'];
    flowNodes.push({
      id: nid('$end'),
      type: isSubworkflow ? 'egressNode' : 'endNode',
      position: { x: 0, y: 0 },
      data: {
        label: '$end',
        name: '$end',
        contextPath: absPath,
        type: isSubworkflow ? 'egress' : 'end',
        status: nd?.status || 'pending',
        // Inline, the container header already names the parent step, so the
        // "return to <parent>" label is redundant/confusing (looks like a
        // self-loop). Keep it only for the drill-down view.
        ...(isSubworkflow && !inline ? { parentAgent: ctx.parentAgent ?? undefined } : {}),
      },
    });
  }

  // $start (rendered as ingress inside a subworkflow) + entry edge.
  if (ctx.entryPoint) {
    const nd = ctx.nodes['$start'];
    flowNodes.push({
      id: nid('$start'),
      type: isSubworkflow ? 'ingressNode' : 'startNode',
      position: { x: 0, y: 0 },
      data: {
        label: '$start',
        name: '$start',
        contextPath: absPath,
        type: isSubworkflow ? 'ingress' : 'start',
        status: nd?.status || 'pending',
        // See $end: suppress the redundant parent label when rendered inline.
        ...(isSubworkflow && !inline ? { parentAgent: ctx.parentAgent ?? undefined } : {}),
      },
    });

    flowEdges.push({
      id: `${nid('$start')}->@entry`,
      source: nid('$start'),
      target: nid(ctx.entryPoint),
      type: 'animatedEdge',
      data: {},
      animated: false,
    });
  }

  // Create edges — only include edges whose source and target exist as nodes.
  // Remap child nodes inside groups to the parent group node so edges connect
  // at the group boundary (children use relative positioning).
  const nodeIds = new Set(flowNodes.map((n) => n.id));
  const childToParent = new Map<string, string>();
  for (const node of flowNodes) {
    if (node.parentId) childToParent.set(node.id, node.parentId);
  }

  // Dedupe edges by (from, to). YAML route lists frequently combine a
  // conditional route with a catch-all to the same target (e.g. an "if X then
  // $end" plus a bare "to: $end" fallback). The engine evaluates routes in
  // order and the first match wins, so multiple entries between the same pair
  // represent ONE visual transition. When collapsing routes with different
  // `when` conditions, the label is cleared to avoid implying only one applies.
  const seenPairs = new Map<string, { when: string | undefined; idx: number }>();
  for (const r of ctx.routes) {
    const from = childToParent.get(nid(r.from)) ?? nid(r.from);
    const to = childToParent.get(nid(r.to)) ?? nid(r.to);
    if (!nodeIds.has(from) || !nodeIds.has(to)) continue;
    // Skip self-loops created by remapping (e.g. group member → group member)
    if (from === to) continue;
    const pairKey = `${from}->${to}`;
    const existing = seenPairs.get(pairKey);
    if (existing) {
      if (existing.when !== r.when) {
        flowEdges[existing.idx]!.data = { when: undefined };
      }
      continue;
    }
    const idx = flowEdges.length;
    seenPairs.set(pairKey, { when: r.when, idx });
    const edgeId = `${pairKey}${r.when ? `[${r.when}]` : ''}`;
    flowEdges.push({
      id: edgeId,
      source: from,
      target: to,
      type: 'animatedEdge',
      data: { when: r.when },
      animated: false,
    });
  }

  // Classify back-edges (loop-backs) so dagre ranks the underlying forward DAG
  // correctly, then lay out this context's top-level nodes.
  const backEdgeIds = findBackEdges(flowNodes, flowEdges, nid('$start'));
  const { width, height } = layoutTopLevel(flowNodes, flowEdges, backEdgeIds);

  // Embed expanded child DAGs: re-parent each child's top-level nodes (those
  // with no parentId) into the container and offset past its header; deeper,
  // already-parented nodes keep their container-relative positions.
  for (const { containerId, sub } of embeds) {
    for (const cn of sub.nodes) {
      if (!cn.parentId) {
        cn.parentId = containerId;
        cn.extent = 'parent';
        cn.position = {
          x: cn.position.x + SUBFLOW_PADDING_X,
          y: cn.position.y + SUBFLOW_HEADER,
        };
      }
      flowNodes.push(cn);
    }
    for (const ce of sub.edges) flowEdges.push(ce);
  }

  return { nodes: flowNodes, edges: flowEdges, width, height };
}

/**
 * Identify back-edges via DFS from the entry node. An edge u→v is a back-edge
 * iff v is an ancestor of u in the DFS tree (i.e. v is currently on the DFS
 * stack when we visit u→v). Operates on top-level node IDs only, since edges
 * have already been remapped from group children to group parents.
 *
 * Traversal order is deterministic: outgoing edges are visited in sorted
 * target-ID order, and unreachable subgraphs are entered in sorted source-ID
 * order, so layout is stable across renders.
 */
function findBackEdges(
  flowNodes: Node<GraphNodeData>[],
  flowEdges: Edge[],
  startId: string,
): Set<string> {
  const topLevelIds = new Set(flowNodes.filter((n) => !n.parentId).map((n) => n.id));

  // Build adjacency from top-level edges. Sort targets for stability.
  const adj = new Map<string, { target: string; edgeId: string }[]>();
  for (const e of flowEdges) {
    if (!topLevelIds.has(e.source) || !topLevelIds.has(e.target)) continue;
    if (!adj.has(e.source)) adj.set(e.source, []);
    adj.get(e.source)!.push({ target: e.target, edgeId: e.id });
  }
  for (const list of adj.values()) {
    list.sort((a, b) => (a.target < b.target ? -1 : a.target > b.target ? 1 : 0));
  }

  const backEdges = new Set<string>();
  const onStack = new Set<string>();
  const visited = new Set<string>();

  const dfs = (node: string): void => {
    visited.add(node);
    onStack.add(node);
    for (const { target, edgeId } of adj.get(node) ?? []) {
      if (onStack.has(target)) {
        backEdges.add(edgeId);
      } else if (!visited.has(target)) {
        dfs(target);
      }
    }
    onStack.delete(node);
  };

  if (topLevelIds.has(startId)) dfs(startId);

  // Also DFS from any unvisited nodes that have outgoing edges, so back-edges
  // in unreachable subgraphs are still classified deterministically.
  for (const id of [...adj.keys()].sort()) {
    if (!visited.has(id)) dfs(id);
  }

  return backEdges;
}

/** Width/height a node contributes to dagre (styled containers vs. plain nodes). */
function sizeOf(node: Node<GraphNodeData>): { w: number; h: number } {
  const w = node.style?.width;
  const h = node.style?.height;
  if (typeof w === 'number' && typeof h === 'number') return { w, h };
  return { w: NODE_WIDTH, h: NODE_HEIGHT };
}

/**
 * Dagre-lay out a context's top-level nodes (those with no parentId), mutating
 * their positions and normalizing the bounding box to origin (0, 0). Returns
 * the bounding-box size so an enclosing container can be sized to fit.
 *
 * Uses a NON-compound dagre graph — compound mode crashes when edges cross
 * compound boundaries. Back-edges are fed in REVERSED direction so dagre ranks
 * the forward DAG correctly; the visible edges keep their original direction.
 */
function layoutTopLevel(
  flowNodes: Node<GraphNodeData>[],
  flowEdges: Edge[],
  backEdgeIds: Set<string>,
): { width: number; height: number } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'TB', nodesep: 50, ranksep: 70, marginx: 30, marginy: 30 });

  for (const node of flowNodes) {
    if (node.parentId) continue;
    const { w, h } = sizeOf(node);
    g.setNode(node.id, { width: w, height: h });
  }

  for (const edge of flowEdges) {
    if (!g.hasNode(edge.source) || !g.hasNode(edge.target)) continue;
    if (backEdgeIds.has(edge.id)) {
      g.setEdge(edge.target, edge.source);
    } else {
      g.setEdge(edge.source, edge.target);
    }
  }

  dagre.layout(g);

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of flowNodes) {
    if (node.parentId) continue;
    const dagreNode = g.node(node.id);
    if (!dagreNode) continue;
    const { w, h } = sizeOf(node);
    const x = dagreNode.x - w / 2;
    const y = dagreNode.y - h / 2;
    node.position = { x, y };
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + w);
    maxY = Math.max(maxY, y + h);
  }

  if (!Number.isFinite(minX)) return { width: NODE_WIDTH, height: NODE_HEIGHT };

  // Normalize so the bounding box starts at (0, 0) — required for clean
  // container embedding, harmless for the root canvas (fitView recenters).
  for (const node of flowNodes) {
    if (node.parentId) continue;
    node.position = { x: node.position.x - minX, y: node.position.y - minY };
  }

  return { width: maxX - minX, height: maxY - minY };
}
