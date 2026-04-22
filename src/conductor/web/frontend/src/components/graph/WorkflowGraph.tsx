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
import { WorkflowNode } from './WorkflowNode';
import { EndNode } from './EndNode';
import { StartNode } from './StartNode';
import { AnimatedEdge } from './AnimatedEdge';
import { WorkflowErrorBanner, WorkflowSuccessBanner } from '@/components/layout/ErrorBanner';
import { NODE_STATUS_HEX } from '@/lib/constants';
import type { NodeStatus } from '@/lib/constants';
import { Loader2, Maximize, Zap } from 'lucide-react';

const nodeTypes: NodeTypes = {
  agentNode: AgentNode,
  scriptNode: ScriptNode,
  gateNode: GateNode,
  groupNode: GroupNode,
  workflowNode: WorkflowNode,
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
        <marker id="arrow-failed" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--failed)" />
        </marker>
      </defs>
    </svg>
  );
}

export function WorkflowGraph() {
  const getViewedContext = useWorkflowStore((s) => s.getViewedContext);
  const viewContextPath = useWorkflowStore((s) => s.viewContextPath);
  const selectNode = useWorkflowStore((s) => s.selectNode);
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const wsStatus = useWorkflowStore((s) => s.wsStatus);
  const workflowFailedAgent = useWorkflowStore((s) => s.workflowFailedAgent);
  const navigateIntoSubworkflow = useWorkflowStore((s) => s.navigateIntoSubworkflow);

  // Get the data for the currently viewed context
  const viewCtx = getViewedContext();
  const { agents, routes, parallelGroups, forEachGroups, nodes: storeNodes, groupProgress, entryPoint, subworkflowContexts } = viewCtx;

  const [flowNodes, setFlowNodes, onNodesChange] = useNodesState<Node<GraphNodeData>>([]);
  const [flowEdges, setFlowEdges, onEdgesChange] = useEdgesState<Edge>([]);

  const graphBuilt = useRef(false);
  const prevViewPath = useRef<string>('');

  // Rebuild graph when context changes (breadcrumb navigation) or when agents first appear
  const viewPathKey = JSON.stringify(viewContextPath);
  useEffect(() => {
    if (agents.length === 0) {
      // Reset if navigated to empty context
      if (prevViewPath.current !== viewPathKey) {
        graphBuilt.current = false;
        prevViewPath.current = viewPathKey;
      }
      return;
    }

    // Force rebuild on context switch
    if (prevViewPath.current !== viewPathKey) {
      graphBuilt.current = false;
      prevViewPath.current = viewPathKey;
    }

    if (graphBuilt.current) return;
    graphBuilt.current = true;

    const { nodes, edges } = buildGraphElements(
      agents, routes, parallelGroups, forEachGroups, storeNodes, groupProgress, entryPoint
    );
    setFlowNodes(nodes);
    setFlowEdges(edges);
  }, [agents, routes, parallelGroups, forEachGroups, storeNodes, groupProgress, entryPoint, setFlowNodes, setFlowEdges, viewPathKey]);

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

  // Double-click on workflow agent nodes to navigate into subworkflow
  const onNodeDoubleClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      // Check if this node has a subworkflow context
      const hasSubworkflow = subworkflowContexts.some((c) => c.parentAgent === node.id);
      if (hasSubworkflow) {
        navigateIntoSubworkflow(node.id);
      }
    },
    [subworkflowContexts, navigateIntoSubworkflow],
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

  // Auto-select failed agent when workflow fails
  useEffect(() => {
    if (workflowStatus === 'failed' && workflowFailedAgent) {
      selectNode(workflowFailedAgent);
    }
  }, [workflowStatus, workflowFailedAgent, selectNode]);

  const showEmptyState = workflowStatus === 'pending' && agents.length === 0;

  // Better empty state message based on ws status
  const emptyMessage = (() => {
    switch (wsStatus) {
      case 'connecting':
        return 'Connecting to workflow\u2026';
      case 'reconnecting':
        return 'Reconnecting\u2026';
      case 'disconnected':
        return 'Connection lost. Retrying\u2026';
      default:
        return 'Waiting for workflow\u2026';
    }
  })();

  return (
    <div className="w-full h-full relative">
      <EdgeMarkers />
      {/* Workflow status banners */}
      <WorkflowErrorBanner />
      <WorkflowSuccessBanner />
      {showEmptyState && (
        <div className="absolute inset-0 z-10 flex flex-col items-center justify-center pointer-events-none">
          <div className="relative mb-3">
            <Zap className="w-8 h-8 text-[var(--accent)] opacity-20" />
            <Loader2 className="w-8 h-8 text-[var(--text-muted)] animate-spin absolute inset-0 opacity-40" />
          </div>
          <p className="text-sm text-[var(--text-muted)] animate-pulse">
            {emptyMessage}
          </p>
        </div>
      )}
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        onNodeDoubleClick={onNodeDoubleClick}
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
