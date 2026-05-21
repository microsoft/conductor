import dagre from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';
import type { WorkflowAgent, RouteEdge, ParallelGroup, ForEachGroup, NodeData, GroupProgress } from '@/stores/workflow-store';
import type { NodeType } from '@/lib/constants';

export interface GraphNodeData {
  label: string;
  type: NodeType;
  status: string;
  groupName?: string;
  progress?: GroupProgress;
  parentAgent?: string;
  [key: string]: unknown;
}

const NODE_WIDTH = 200;
const NODE_HEIGHT = 56;
const GROUP_PADDING_X = 20;
const GROUP_PADDING_TOP = 40;
const GROUP_PADDING_BOTTOM = 20;
const GROUP_CHILD_GAP = 12;

export function buildGraphElements(
  agents: WorkflowAgent[],
  routes: RouteEdge[],
  parallelGroups: ParallelGroup[],
  forEachGroups: ForEachGroup[],
  nodes: Record<string, NodeData>,
  groupProgress: Record<string, GroupProgress>,
  entryPoint: string | null,
  parentAgent?: string | null,
): { nodes: Node<GraphNodeData>[]; edges: Edge[] } {
  const flowNodes: Node<GraphNodeData>[] = [];
  const flowEdges: Edge[] = [];
  const agentNames = new Set<string>();
  const groupAgents = new Set<string>();

  // Track which agents belong to which group
  const agentToGroup = new Map<string, string>();

  // Identify agents in parallel groups
  for (const pg of parallelGroups) {
    for (const a of pg.agents) {
      groupAgents.add(a);
      agentToGroup.set(a, pg.name);
    }
  }

  // Create parallel group nodes (as single composite nodes for dagre)
  for (const pg of parallelGroups) {
    const nd = nodes[pg.name];
    // Calculate group dimensions based on child count
    const childCount = pg.agents.length;
    const groupWidth = NODE_WIDTH + GROUP_PADDING_X * 2;
    const groupHeight = GROUP_PADDING_TOP + childCount * NODE_HEIGHT + (childCount - 1) * GROUP_CHILD_GAP + GROUP_PADDING_BOTTOM;

    flowNodes.push({
      id: pg.name,
      type: 'groupNode',
      position: { x: 0, y: 0 },
      data: {
        label: pg.name,
        type: 'parallel_group',
        status: nd?.status || 'pending',
        groupName: pg.name,
        progress: groupProgress[pg.name],
      },
      style: { width: groupWidth, height: groupHeight },
    });

    // Create child nodes — they'll be positioned manually after dagre layout
    for (let i = 0; i < pg.agents.length; i++) {
      const agentName = pg.agents[i]!;
      const agentNd = nodes[agentName];
      flowNodes.push({
        id: agentName,
        type: 'agentNode',
        position: {
          // Position relative to parent
          x: GROUP_PADDING_X,
          y: GROUP_PADDING_TOP + i * (NODE_HEIGHT + GROUP_CHILD_GAP),
        },
        parentId: pg.name,
        extent: 'parent' as const,
        data: {
          label: agentName,
          type: 'agent',
          status: agentNd?.status || 'pending',
        },
      });
    }
    agentNames.add(pg.name);
  }

  // Create for-each group nodes
  for (const fg of forEachGroups) {
    const nd = nodes[fg.name];
    flowNodes.push({
      id: fg.name,
      type: 'groupNode',
      position: { x: 0, y: 0 },
      data: {
        label: fg.name,
        type: 'for_each_group',
        status: nd?.status || 'pending',
        groupName: fg.name,
        progress: groupProgress[fg.name],
      },
    });
    agentNames.add(fg.name);
  }

  // Create standalone agent/script/gate nodes
  for (const a of agents) {
    if (!agentNames.has(a.name) && !groupAgents.has(a.name)) {
      const nodeType = (a.type || 'agent') as NodeType;
      const nd = nodes[a.name];
      let flowNodeType = 'agentNode';
      if (nodeType === 'script') flowNodeType = 'scriptNode';
      else if (nodeType === 'set') flowNodeType = 'setNode';
      else if (nodeType === 'human_gate') flowNodeType = 'gateNode';
      else if (nodeType === 'workflow') flowNodeType = 'workflowNode';

      flowNodes.push({
        id: a.name,
        type: flowNodeType,
        position: { x: 0, y: 0 },
        data: {
          label: a.name,
          type: nodeType,
          status: nd?.status || 'pending',
        },
      });
      agentNames.add(a.name);
    }
  }

  // Check if $end is referenced
  let hasEnd = false;
  for (const r of routes) {
    if (r.to === '$end') hasEnd = true;
  }

  if (hasEnd) {
    const nd = nodes['$end'];
    const isSubworkflow = !!parentAgent;
    flowNodes.push({
      id: '$end',
      type: isSubworkflow ? 'egressNode' : 'endNode',
      position: { x: 0, y: 0 },
      data: {
        label: '$end',
        type: isSubworkflow ? 'egress' : 'end',
        status: nd?.status || 'pending',
        ...(isSubworkflow ? { parentAgent } : {}),
      },
    });
  }

  // Always add $start node if we have an entry point
  if (entryPoint) {
    const nd = nodes['$start'];
    const isSubworkflow = !!parentAgent;
    flowNodes.push({
      id: '$start',
      type: isSubworkflow ? 'ingressNode' : 'startNode',
      position: { x: 0, y: 0 },
      data: {
        label: '$start',
        type: isSubworkflow ? 'ingress' : 'start',
        status: nd?.status || 'pending',
        ...(isSubworkflow ? { parentAgent } : {}),
      },
    });

    // Add edge from $start to entry point
    flowEdges.push({
      id: '$start->$entryPoint',
      source: '$start',
      target: entryPoint,
      type: 'animatedEdge',
      data: {},
      animated: false,
    });
  }

  // Create edges — only include edges whose source and target exist as nodes.
  // Remap child nodes inside groups to the parent group node so edges
  // connect at the group boundary (children use relative positioning).
  const nodeIds = new Set(flowNodes.map((n) => n.id));
  const childToParent = new Map<string, string>();
  for (const node of flowNodes) {
    if (node.parentId) childToParent.set(node.id, node.parentId);
  }

  // Dedupe edges by (from, to). YAML route lists frequently combine a
  // conditional route with a catch-all to the same target (e.g. an "if X
  // then $end" plus a bare "to: $end" fallback). The engine evaluates
  // routes in order and the first match wins, so multiple entries between
  // the same pair represent ONE visual transition, not parallel edges.
  // Without deduping, dagre lays them as two overlapping/diverging edges
  // which render as phantom strands going off-canvas.
  // When collapsing routes with different `when` conditions, the label is
  // cleared to avoid implying only one condition applies.
  const seenPairs = new Map<string, { when: string | undefined; idx: number }>();
  for (const r of routes) {
    const from = childToParent.get(r.from) ?? r.from;
    const to = childToParent.get(r.to) ?? r.to;
    if (!nodeIds.has(from) || !nodeIds.has(to)) continue;
    // Skip self-loops created by remapping (e.g. group member → group member)
    if (from === to) continue;
    const pairKey = `${from}->${to}`;
    const existing = seenPairs.get(pairKey);
    if (existing) {
      // Multiple distinct conditions collapse — drop the label.
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

  // Classify edges as forward or back-edges using a DFS from $start.
  // Back-edges are loop-back routes (e.g. plan_reviewer → planner when
  // approved=false). Feeding them to Dagre as-is causes it to greedily
  // reverse arbitrary edges to break cycles, which scrambles the ranking
  // and produces disjointed layouts. Pre-classifying lets us pass each
  // back-edge to Dagre in REVERSED direction so the layout reflects the
  // true forward DAG, while we still render the edge in its original
  // direction.
  const backEdgeIds = findBackEdges(flowNodes, flowEdges, '$start');

  // Apply dagre layout to top-level nodes only (non-children)
  applyDagreLayout(flowNodes, flowEdges, backEdgeIds);

  return { nodes: flowNodes, edges: flowEdges };
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

function applyDagreLayout(
  flowNodes: Node<GraphNodeData>[],
  flowEdges: Edge[],
  backEdgeIds: Set<string>,
): void {
  // Use a NON-compound dagre graph — compound mode causes crashes
  // when edges cross compound boundaries
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'TB', nodesep: 50, ranksep: 70, marginx: 30, marginy: 30 });

  // Only add top-level nodes (no parentId) to dagre
  for (const node of flowNodes) {
    if (node.parentId) continue; // Skip children — they're positioned relative to parent

    const isGroup = node.type === 'groupNode';
    const w = isGroup ? (node.style?.width as number || NODE_WIDTH) : NODE_WIDTH;
    const h = isGroup ? (node.style?.height as number || NODE_HEIGHT) : NODE_HEIGHT;
    g.setNode(node.id, { width: w, height: h });
  }

  // Add edges (dagre needs valid source/target nodes). Back-edges are passed
  // in REVERSED direction so dagre ranks the underlying DAG correctly; the
  // visible flowEdges entries keep their original direction.
  for (const edge of flowEdges) {
    if (!g.hasNode(edge.source) || !g.hasNode(edge.target)) continue;
    if (backEdgeIds.has(edge.id)) {
      g.setEdge(edge.target, edge.source);
    } else {
      g.setEdge(edge.source, edge.target);
    }
  }

  dagre.layout(g);

  // Apply positions to top-level nodes only
  for (const node of flowNodes) {
    if (node.parentId) continue; // Children already have relative positions

    const dagreNode = g.node(node.id);
    if (!dagreNode) continue;

    const isGroup = node.type === 'groupNode';
    const w = isGroup ? (node.style?.width as number || NODE_WIDTH) : NODE_WIDTH;
    const h = isGroup ? (node.style?.height as number || NODE_HEIGHT) : NODE_HEIGHT;

    node.position = {
      x: dagreNode.x - w / 2,
      y: dagreNode.y - h / 2,
    };
  }
}
