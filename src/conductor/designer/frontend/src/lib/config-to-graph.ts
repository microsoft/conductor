/**
 * Convert a WorkflowConfig document into ReactFlow nodes and edges.
 *
 * This is a *derivation* — the WorkflowConfig is the source of truth,
 * and ReactFlow elements are a read-only projection used for rendering.
 */

import dagre from '@dagrejs/dagre';
import type {
  WorkflowConfig,
  DesignerNode,
  DesignerEdge,
  DesignerNodeType,
} from '@/types/designer';
import type { NodePosition } from '@/stores/designer-store';

const NODE_WIDTH = 220;
const NODE_HEIGHT = 80;
const GROUP_PADDING = 40;

/** Derive ReactFlow nodes + edges from a WorkflowConfig. */
export function configToGraph(
  config: WorkflowConfig,
  savedPositions: Record<string, NodePosition>,
): { nodes: DesignerNode[]; edges: DesignerEdge[] } {
  const nodes: DesignerNode[] = [];
  const edges: DesignerEdge[] = [];

  // Collect all entity names for routing
  const allNames = new Set<string>();

  // ── Start node ───────────────────────────────────────────────
  const startId = '__start__';
  nodes.push({
    id: startId,
    type: 'designerStart',
    position: savedPositions[startId] ?? { x: 0, y: 0 },
    data: { label: 'Start', nodeType: 'start', entityName: '__start__' },
    draggable: true,
    selectable: true,
  });

  // Edge from start → entry_point
  if (config.workflow.entry_point) {
    const targetId = entityToNodeId(config.workflow.entry_point, config);
    if (targetId) {
      edges.push({
        id: `e-start-${targetId}`,
        source: startId,
        target: targetId,
        animated: true,
        style: { stroke: '#3b82f6' },
      });
    }
  }

  // ── Agent nodes ──────────────────────────────────────────────
  for (const agent of config.agents) {
    const nodeType = resolveAgentNodeType(agent.type);
    const id = `agent-${agent.name}`;
    allNames.add(agent.name);

    nodes.push({
      id,
      type: nodeTypeToReactFlowType(nodeType),
      position: savedPositions[id] ?? { x: 0, y: 0 },
      data: {
        label: agent.name,
        nodeType,
        entityName: agent.name,
      },
      draggable: true,
      selectable: true,
    });

    // Routes → edges
    (agent.routes ?? []).forEach((route, ri) => {
      if (route.to === '$end') {
        edges.push({
          id: `e-${agent.name}-end-${ri}`,
          source: id,
          target: '__end__',
          label: route.when ?? undefined,
          style: { stroke: '#6b7280' },
        });
      } else {
        const targetId = entityToNodeId(route.to, config);
        if (targetId) {
          edges.push({
            id: `e-${agent.name}-${route.to}-${ri}`,
            source: id,
            target: targetId,
            label: route.when ?? undefined,
            style: { stroke: '#6b7280' },
          });
        }
      }
    });
  }

  // ── Parallel groups ──────────────────────────────────────────
  for (const pg of config.parallel ?? []) {
    const groupId = `parallel-${pg.name}`;
    allNames.add(pg.name);

    nodes.push({
      id: groupId,
      type: 'designerParallel',
      position: savedPositions[groupId] ?? { x: 0, y: 0 },
      data: {
        label: pg.name,
        nodeType: 'parallel',
        entityName: pg.name,
      },
      draggable: true,
      selectable: true,
      style: {
        width: NODE_WIDTH + GROUP_PADDING * 2,
        height: NODE_HEIGHT * Math.max(pg.agents.length, 1) + GROUP_PADDING * 2,
      },
    });

    // Routes from parallel group
    (pg.routes ?? []).forEach((route, ri) => {
      const targetId = route.to === '$end' ? '__end__' : entityToNodeId(route.to, config);
      if (targetId) {
        edges.push({
          id: `e-${pg.name}-${route.to}-${ri}`,
          source: groupId,
          target: targetId,
          label: route.when ?? undefined,
          style: { stroke: '#6b7280' },
        });
      }
    });
  }

  // ── For-each groups (composite node, NOT container) ──────────
  for (const fe of config.for_each ?? []) {
    const feId = `foreach-${fe.name}`;
    allNames.add(fe.name);

    nodes.push({
      id: feId,
      type: 'designerForEach',
      position: savedPositions[feId] ?? { x: 0, y: 0 },
      data: {
        label: fe.name,
        nodeType: 'for_each',
        entityName: fe.name,
      },
      draggable: true,
      selectable: true,
    });

    // Routes from for-each group
    (fe.routes ?? []).forEach((route, ri) => {
      const targetId = route.to === '$end' ? '__end__' : entityToNodeId(route.to, config);
      if (targetId) {
        edges.push({
          id: `e-${fe.name}-${route.to}-${ri}`,
          source: feId,
          target: targetId,
          label: route.when ?? undefined,
          style: { stroke: '#6b7280' },
        });
      }
    });
  }

  // ── End node ─────────────────────────────────────────────────
  // Only show End if any route targets $end
  const hasEnd = edges.some((e) => e.target === '__end__');
  if (hasEnd) {
    nodes.push({
      id: '__end__',
      type: 'designerEnd',
      position: savedPositions['__end__'] ?? { x: 0, y: 0 },
      data: { label: 'End', nodeType: 'end', entityName: '__end__' },
      draggable: true,
      selectable: false,
    });
  }

  // ── Auto-layout if no saved positions ────────────────────────
  const hasSaved = Object.keys(savedPositions).length > 0;
  if (!hasSaved) {
    applyDagreLayout(nodes, edges);
  }

  return { nodes, edges };
}

// ── Layout ─────────────────────────────────────────────────────────

function applyDagreLayout(nodes: DesignerNode[], edges: DesignerEdge[]) {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'TB', nodesep: 60, ranksep: 80 });

  for (const node of nodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  for (const node of nodes) {
    const pos = g.node(node.id);
    if (pos) {
      node.position = {
        x: pos.x - NODE_WIDTH / 2,
        y: pos.y - NODE_HEIGHT / 2,
      };
    }
  }
}

// ── Utilities ──────────────────────────────────────────────────────

function resolveAgentNodeType(type?: string | null): DesignerNodeType {
  switch (type) {
    case 'human_gate': return 'human_gate';
    case 'script': return 'script';
    case 'workflow': return 'workflow';
    default: return 'agent';
  }
}

function nodeTypeToReactFlowType(type: DesignerNodeType): string {
  switch (type) {
    case 'agent': return 'designerAgent';
    case 'human_gate': return 'designerGate';
    case 'script': return 'designerScript';
    case 'workflow': return 'designerWorkflow';
    case 'parallel': return 'designerParallel';
    case 'for_each': return 'designerForEach';
    case 'start': return 'designerStart';
    case 'end': return 'designerEnd';
  }
}

/** Map an entity name (agent, parallel group, for-each) to its node ID. */
function entityToNodeId(name: string, config: WorkflowConfig): string | null {
  if (config.agents.some((a) => a.name === name)) return `agent-${name}`;
  if ((config.parallel ?? []).some((p) => p.name === name)) return `parallel-${name}`;
  if ((config.for_each ?? []).some((f) => f.name === name)) return `foreach-${name}`;
  return null;
}
