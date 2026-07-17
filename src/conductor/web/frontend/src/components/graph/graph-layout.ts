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
import { contextKey, nodeKey, forEachGroupKey, parseForEachSlotKey } from '@/lib/node-id';

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
  /** Whether an inline-expandable node (subworkflow / for_each group) is expanded. */
  expanded?: boolean;
  /** Whether this node can be expanded inline (child DAG / iterations exist). */
  canExpand?: boolean;
  /** Context key toggled by a subworkflow / iteration node's expand chevron. */
  childContextKey?: string;
  /** Resolved child workflow name, shown as a subtitle on subworkflow nodes. */
  childName?: string;
  /**
   * Expansion key toggled by a `for_each`-of-workflow group's chevron (its own
   * node id — see `forEachGroupKey`). Present only on expandable group nodes.
   */
  groupExpansionKey?: string;
  /**
   * Absolute index path of the `for_each` iteration this node *is* (a group
   * member). Its live status comes from that child context's own `.status`,
   * not the parent's `nodes[name]`. Undefined for non-iteration nodes.
   */
  iterationContextPath?: number[];
  /** True for a `for_each` iteration member pill inside an expanded group. */
  isForEachIteration?: boolean;
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

// A collapsed for_each iteration pill occupies a fixed slot when stacked inside
// an expanded group container; expanded iterations size to their own DAG.
const ITER_PILL_WIDTH = NODE_WIDTH;
const ITER_PILL_HEIGHT = NODE_HEIGHT;
const ITER_GAP = 14;

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
 * Collect the expansion keys of every inline-expandable subworkflow reachable
 * from a viewed context, walking the full context subtree (not just what is
 * currently expanded). Used by the graph's Expand/Collapse-all control.
 *
 * Two kinds of keys are returned: a sequential `type: workflow` step yields its
 * child context key (`slotKey` equals the agent name, child must have ≥1 agent;
 * mirrors `canExpand` in {@link layoutContext}), and a `for_each`-of-workflow
 * group yields its group container key (see {@link forEachGroupKey}). Expanding
 * a group reveals its iterations as collapsed pills; individual iteration inner
 * DAGs are expanded manually, so this does not descend into them (keeping
 * Expand-all bounded on a wide fan-out).
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

    // for_each-of-workflow group containers. Expanding a group reveals its
    // iterations as collapsed pills; the individual iteration inner DAGs are
    // expanded manually (per-iteration), so Expand-all does not descend into
    // them and can't blow up on a wide fan-out.
    const seenGroups = new Set<string>();
    for (const c of ctxChildren) {
      const parsed = parseForEachSlotKey(c.slotKey);
      if (!parsed || seenGroups.has(parsed.group)) continue;
      seenGroups.add(parsed.group);
      keys.push(forEachGroupKey(absPath, parsed.group));
    }
  };

  walk(agents, children, basePath);
  return keys;
}

/**
 * Compute the expansion keys needed to reveal a target context inline, walking
 * from the root of `contexts` along `path` (the target context's absolute index
 * path). Every ancestor — and the target context itself — is expanded so its
 * inner DAG (and any deeper container it holds) renders:
 *
 *   - a sequential subworkflow step yields its context key
 *     (`contextKey(prefix)`), the same key its expand chevron toggles;
 *   - a `for_each` iteration yields BOTH its group container key
 *     (`forEachGroupKey(parentPath, group)`, so the iteration pill is revealed)
 *     AND its own context key (so the iteration's inner DAG renders).
 *
 * Used by expansion-aware deep-links: expanding this bounded set (only the
 * target's ancestor chain, never siblings) surfaces a nested agent /
 * subworkflow in the root graph without drilling in. Returns `[]` for a
 * root-level target. Walk stops early if `path` points past a materialized
 * context (nothing to expand for an unresolved tail).
 *
 * @param contexts Root subworkflow contexts (`store.subworkflowContexts`).
 * @param path Absolute numeric index path of the target context (root = `[]`).
 */
export function expansionKeysForContextPath(
  contexts: SubworkflowContext[],
  path: number[],
): string[] {
  const keys: string[] = [];
  let current = contexts;
  const prefix: number[] = [];
  for (const idx of path) {
    const ctx = current[idx];
    if (!ctx) break;
    const parentPath = [...prefix];
    prefix.push(idx);
    const parsed = parseForEachSlotKey(ctx.slotKey);
    if (parsed) keys.push(forEachGroupKey(parentPath, parsed.group));
    keys.push(contextKey(prefix));
    current = ctx.children;
  }
  return keys;
}

/**
 * Iteration child contexts belonging to a `for_each`-of-workflow group, paired
 * with their index in the parent's `children`. A for_each iteration's `slotKey`
 * is `${group}[${itemKey}]`; sequential subworkflow slot keys (bare agent
 * names) parse to `null` and are skipped.
 */
function forEachIterationContexts(
  children: SubworkflowContext[],
  groupName: string,
): { ctx: SubworkflowContext; idx: number }[] {
  const out: { ctx: SubworkflowContext; idx: number }[] = [];
  for (let idx = 0; idx < children.length; idx++) {
    const c = children[idx]!;
    const parsed = parseForEachSlotKey(c.slotKey);
    if (parsed && parsed.group === groupName) out.push({ ctx: c, idx });
  }
  return out;
}

/**
 * Lay out the iteration members of an expanded `for_each`-of-workflow group as
 * a vertical stack inside the group container. Each iteration is a
 * `workflowNode` — a collapsed pill by default, or (when its own context key is
 * in `expandedContexts`) an inline container embedding the iteration's DAG,
 * recursively laid out. Returned nodes/edges carry final `parentId` +
 * container-relative positions and bypass the caller's dagre pass.
 */
function layoutForEachIterations(
  iterations: { ctx: SubworkflowContext; idx: number }[],
  absPath: number[],
  groupKey: string,
  expandedContexts: Set<string>,
): { nodes: Node<GraphNodeData>[]; edges: Edge[]; width: number; height: number } {
  const nodes: Node<GraphNodeData>[] = [];
  const edges: Edge[] = [];
  let cursorY = SUBFLOW_HEADER;
  let maxWidth = ITER_PILL_WIDTH;

  for (const { ctx: iterCtx, idx } of iterations) {
    const iterPath = [...absPath, idx];
    const iterKey = contextKey(iterPath);
    const iterId = nodeKey(absPath, iterCtx.slotKey);
    const parsed = parseForEachSlotKey(iterCtx.slotKey);
    const canExpand = iterCtx.agents.length > 0;
    const isExpanded = canExpand && expandedContexts.has(iterKey);

    const data: GraphNodeData = {
      label: parsed ? parsed.key : iterCtx.slotKey,
      name: iterCtx.slotKey,
      contextPath: absPath,
      type: 'workflow',
      status: iterCtx.status || 'pending',
      canExpand,
      expanded: isExpanded,
      childContextKey: iterKey,
      childName: iterCtx.workflowName || undefined,
      iterationContextPath: iterPath,
      isForEachIteration: true,
    };

    if (isExpanded) {
      const sub = layoutContext(contextToInput(iterCtx), iterPath, expandedContexts, true);
      const w = sub.width + SUBFLOW_PADDING_X * 2;
      const h = sub.height + SUBFLOW_HEADER + SUBFLOW_PADDING_BOTTOM;
      nodes.push({
        id: iterId,
        type: 'workflowNode',
        position: { x: SUBFLOW_PADDING_X, y: cursorY },
        parentId: groupKey,
        extent: 'parent' as const,
        data,
        style: { width: w, height: h },
      });
      for (const cn of sub.nodes) {
        if (!cn.parentId) {
          cn.parentId = iterId;
          cn.extent = 'parent';
          cn.position = {
            x: cn.position.x + SUBFLOW_PADDING_X,
            y: cn.position.y + SUBFLOW_HEADER,
          };
        }
        nodes.push(cn);
      }
      for (const ce of sub.edges) edges.push(ce);
      cursorY += h + ITER_GAP;
      maxWidth = Math.max(maxWidth, w);
    } else {
      nodes.push({
        id: iterId,
        type: 'workflowNode',
        position: { x: SUBFLOW_PADDING_X, y: cursorY },
        parentId: groupKey,
        extent: 'parent' as const,
        data,
      });
      cursorY += ITER_PILL_HEIGHT + ITER_GAP;
    }
  }

  const width = maxWidth + SUBFLOW_PADDING_X * 2;
  const stacked = iterations.length > 0 ? cursorY - ITER_GAP : SUBFLOW_HEADER;
  const height = stacked + SUBFLOW_PADDING_BOTTOM;
  return { nodes, edges, width, height };
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

  // Fully-positioned nested nodes/edges (expanded for_each group members and
  // their inner DAGs) appended after this context's dagre pass. They already
  // carry final `parentId` + container-relative positions, so they bypass dagre.
  const deferredNodes: Node<GraphNodeData>[] = [];
  const deferredEdges: Edge[] = [];

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

  // For-each group nodes. A for_each-of-workflow group (its inline agent is a
  // `type: workflow` step) spawns one subworkflow child context per iteration,
  // slot-keyed `${group}[${itemKey}]`. When such a group is expanded it becomes
  // a container stacking each iteration as a collapsible subworkflow pill;
  // otherwise (and for for_each-of-agent groups) it stays a leaf progress node.
  for (const fg of ctx.forEachGroups) {
    const nd = ctx.nodes[fg.name];
    const iterations = forEachIterationContexts(ctx.children, fg.name);
    const canExpandGroup = iterations.length > 0;
    const groupKey = forEachGroupKey(absPath, fg.name);
    const isGroupExpanded = canExpandGroup && expandedContexts.has(groupKey);

    if (isGroupExpanded) {
      const built = layoutForEachIterations(iterations, absPath, groupKey, expandedContexts);
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
          expanded: true,
          canExpand: true,
          groupExpansionKey: groupKey,
        },
        style: { width: built.width, height: built.height },
      });
      for (const mn of built.nodes) deferredNodes.push(mn);
      for (const me of built.edges) deferredEdges.push(me);
    } else {
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
          expanded: false,
          canExpand: canExpandGroup,
          groupExpansionKey: canExpandGroup ? groupKey : undefined,
        },
      });
    }
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

  // Append expanded for_each members/inner DAGs. Their group container is
  // already in `flowNodes` (pushed above, sized for dagre), and each member is
  // parented to it, so ordering (parent-before-child) holds.
  for (const dn of deferredNodes) flowNodes.push(dn);
  for (const de of deferredEdges) flowEdges.push(de);

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
