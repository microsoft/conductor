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

  for (const r of routes) {
    const from = childToParent.get(r.from) ?? r.from;
    const to = childToParent.get(r.to) ?? r.to;
    if (!nodeIds.has(from) || !nodeIds.has(to)) continue;
    // Skip self-loops created by remapping (e.g. group member → group member)
    if (from === to) continue;
    const edgeId = `${from}->${to}${r.when ? `[${r.when}]` : ''}`;
    flowEdges.push({
      id: edgeId,
      source: from,
      target: to,
      type: 'animatedEdge',
      data: { when: r.when },
      animated: false,
    });
  }

  // Apply dagre layout to top-level nodes only (non-children)
  applyDagreLayout(flowNodes, flowEdges);

  return { nodes: flowNodes, edges: flowEdges };
}

function applyDagreLayout(flowNodes: Node<GraphNodeData>[], flowEdges: Edge[]): void {
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

  // Add edges (dagre needs valid source/target nodes)
  for (const edge of flowEdges) {
    // Only add edge if both source and target are in dagre graph
    if (g.hasNode(edge.source) && g.hasNode(edge.target)) {
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
