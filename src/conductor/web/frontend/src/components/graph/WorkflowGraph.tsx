import { useCallback, useEffect, useRef } from 'react';
import {
  ReactFlow,
  MiniMap,
  Controls,
  Background,
  BackgroundVariant,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Node,
  type Edge,
  type NodeTypes,
  type EdgeTypes,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { useWorkflowStore } from '@/stores/workflow-store';
import { buildGraphElements, type GraphNodeData } from './graph-layout';
import { AgentNode } from './AgentNode';
import { ScriptNode } from './ScriptNode';
import { GateNode } from './GateNode';
import { GroupNode } from './GroupNode';
import { EndNode } from './EndNode';
import { StartNode } from './StartNode';
import { AnimatedEdge } from './AnimatedEdge';
import { NODE_STATUS_HEX } from '@/lib/constants';
import type { NodeStatus } from '@/lib/constants';
import { Loader2, Maximize } from 'lucide-react';

const nodeTypes: NodeTypes = {
  agentNode: AgentNode,
  scriptNode: ScriptNode,
  gateNode: GateNode,
  groupNode: GroupNode,
  endNode: EndNode,
  startNode: StartNode,
};

const edgeTypes: EdgeTypes = {
  animatedEdge: AnimatedEdge,
};

const defaultEdgeOptions = {
  type: 'animatedEdge',
};

// Custom marker definitions for edge arrows
function EdgeMarkers() {
  return (
    <svg style={{ position: 'absolute', width: 0, height: 0 }}>
      <defs>
        <marker id="arrow-default" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--edge-color)" />
        </marker>
        <marker id="arrow-active" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--edge-active)" />
        </marker>
        <marker id="arrow-taken" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--edge-taken)" />
        </marker>
      </defs>
    </svg>
  );
}

export function WorkflowGraph() {
  const agents = useWorkflowStore((s) => s.agents);
  const routes = useWorkflowStore((s) => s.routes);
  const parallelGroups = useWorkflowStore((s) => s.parallelGroups);
  const forEachGroups = useWorkflowStore((s) => s.forEachGroups);
  const storeNodes = useWorkflowStore((s) => s.nodes);
  const groupProgress = useWorkflowStore((s) => s.groupProgress);
  const selectNode = useWorkflowStore((s) => s.selectNode);
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const entryPoint = useWorkflowStore((s) => s.entryPoint);

  const [flowNodes, setFlowNodes, onNodesChange] = useNodesState<Node<GraphNodeData>>([]);
  const [flowEdges, setFlowEdges, onEdgesChange] = useEdgesState<Edge>([]);

  const graphBuilt = useRef(false);

  // Build graph when agents first appear
  useEffect(() => {
    if (agents.length === 0) return;
    if (graphBuilt.current) return;
    graphBuilt.current = true;

    const { nodes, edges } = buildGraphElements(
      agents, routes, parallelGroups, forEachGroups, storeNodes, groupProgress, entryPoint
    );
    setFlowNodes(nodes);
    setFlowEdges(edges);
  }, [agents, routes, parallelGroups, forEachGroups, storeNodes, groupProgress, entryPoint, setFlowNodes, setFlowEdges]);

  // Update node data when store nodes change (status, progress, etc.)
  useEffect(() => {
    if (!graphBuilt.current) return;

    setFlowNodes((nds) =>
      nds.map((node) => {
        const storeNode = storeNodes[node.id];
        if (!storeNode) return node;

        const newStatus = storeNode.status || 'pending';
        const currentStatus = (node.data as GraphNodeData).status;

        if (newStatus !== currentStatus) {
          const newData = { ...node.data, status: newStatus } as GraphNodeData;
          // Update group progress
          if (node.data.groupName && groupProgress[node.data.groupName]) {
            newData.progress = groupProgress[node.data.groupName];
          }
          return { ...node, data: newData };
        }

        // Check group progress updates
        if (node.data.groupName && groupProgress[node.data.groupName]) {
          const currentProgress = (node.data as GraphNodeData).progress;
          const newProgress = groupProgress[node.data.groupName];
          if (
            newProgress &&
            (!currentProgress ||
              currentProgress.completed !== newProgress.completed ||
              currentProgress.failed !== newProgress.failed)
          ) {
            return { ...node, data: { ...node.data, progress: newProgress } as GraphNodeData };
          }
        }

        return node;
      })
    );
  }, [storeNodes, groupProgress, setFlowNodes]);

  // Handle node selection
  const onNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      // Don't select parallel group parent nodes (they contain clickable child nodes).
      // For-each groups are standalone nodes and should be selectable.
      if (node.type === 'groupNode') {
        const nodeData = node.data as GraphNodeData;
        if (nodeData.type !== 'for_each_group') return;
      }
      selectNode(node.id);
    },
    [selectNode],
  );

  const onPaneClick = useCallback(() => {
    selectNode(null);
  }, [selectNode]);

  // Minimap node color
  const minimapNodeColor = useCallback((node: Node) => {
    const status = ((node.data as GraphNodeData)?.status || 'pending') as NodeStatus;
    return NODE_STATUS_HEX[status] || NODE_STATUS_HEX.pending;
  }, []);

  // Update selected state on nodes
  useEffect(() => {
    setFlowNodes((nds) =>
      nds.map((n) => ({
        ...n,
        selected: n.id === selectedNode,
      })),
    );
  }, [selectedNode, setFlowNodes]);

  const showEmptyState = workflowStatus === 'pending' && agents.length === 0;

  return (
    <div className="w-full h-full relative">
      <EdgeMarkers />
      {showEmptyState && (
        <div className="absolute inset-0 z-10 flex flex-col items-center justify-center pointer-events-none">
          <Loader2 className="w-8 h-8 text-[var(--text-muted)] animate-spin mb-3 opacity-40" />
          <p className="text-sm text-[var(--text-muted)] animate-pulse">
            Waiting for workflow&hellip;
          </p>
        </div>
      )}
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        defaultEdgeOptions={defaultEdgeOptions}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.2}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable={true}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="var(--border-subtle)" />
        <MiniMap
          nodeColor={minimapNodeColor}
          maskColor="var(--minimap-mask)"
          style={{ background: 'var(--minimap-bg)' }}
          pannable
          zoomable
        />
        <Controls showInteractive={false}>
          <FitViewButton />
        </Controls>
        <FitViewKeyboardShortcut />
      </ReactFlow>
    </div>
  );
}

/** Inner component that uses useReactFlow (must be inside ReactFlow) */
function FitViewButton() {
  const { fitView } = useReactFlow();

  const handleFitView = useCallback(() => {
    fitView({ padding: 0.2, duration: 300 });
  }, [fitView]);

  return (
    <button
      onClick={handleFitView}
      className="react-flow__controls-button"
      title="Fit view (F)"
      style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}
    >
      <Maximize className="w-3.5 h-3.5" />
    </button>
  );
}

function FitViewKeyboardShortcut() {
  const { fitView } = useReactFlow();

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        fitView({ padding: 0.2, duration: 300 });
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [fitView]);

  return null;
}
