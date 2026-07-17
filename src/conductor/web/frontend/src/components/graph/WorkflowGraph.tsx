import { useCallback, useEffect, useMemo, useRef } from 'react';
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

import { useWorkflowStore, type SubworkflowContext } from '@/stores/workflow-store';
import { useViewedGraphData } from '@/hooks/use-viewed-context';
import { useDeepLink } from '@/hooks/use-deep-link';
import { buildGraphElements, type GraphNodeData, type GraphContextInput } from './graph-layout';
import { nodeKey, parseNodeKey } from '@/lib/node-id';
import { AgentNode } from './AgentNode';
import { ScriptNode } from './ScriptNode';
import { SetNode } from './SetNode';
import { GateNode } from './GateNode';
import { GroupNode } from './GroupNode';
import { WorkflowNode } from './WorkflowNode';
import { WaitNode } from './WaitNode';
import { TerminateNode } from './TerminateNode';
import { EndNode } from './EndNode';
import { StartNode } from './StartNode';
import { IngressNode } from './IngressNode';
import { EgressNode } from './EgressNode';
import { AnimatedEdge } from './AnimatedEdge';
import { WorkflowErrorBanner, WorkflowSuccessBanner } from '@/components/layout/ErrorBanner';
import { NODE_STATUS_HEX } from '@/lib/constants';
import type { NodeStatus } from '@/lib/constants';
import { Loader2, Maximize, Zap } from 'lucide-react';

const nodeTypes: NodeTypes = {
  agentNode: AgentNode,
  scriptNode: ScriptNode,
  setNode: SetNode,
  gateNode: GateNode,
  groupNode: GroupNode,
  workflowNode: WorkflowNode,
  waitNode: WaitNode,
  terminateNode: TerminateNode,
  endNode: EndNode,
  startNode: StartNode,
  ingressNode: IngressNode,
  egressNode: EgressNode,
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

/**
 * Resolve a `SubworkflowContext` from its absolute numeric index path. Returns
 * `null` for the root path (`[]`) — callers use the root store fields there.
 */
function resolveContextByPath(
  contexts: SubworkflowContext[],
  path: number[],
): SubworkflowContext | null {
  if (path.length === 0) return null;
  let ctx: SubworkflowContext | undefined = contexts[path[0]!];
  for (let i = 1; i < path.length && ctx; i++) {
    ctx = ctx.children[path[i]!];
  }
  return ctx ?? null;
}

export function WorkflowGraph() {
  const viewCtx = useViewedGraphData();
  const viewContextPath = useWorkflowStore((s) => s.viewContextPath);
  const selectNode = useWorkflowStore((s) => s.selectNode);
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const workflowStatus = useWorkflowStore((s) => s.workflowStatus);
  const wsStatus = useWorkflowStore((s) => s.wsStatus);
  const workflowFailedAgent = useWorkflowStore((s) => s.workflowFailedAgent);
  const navigateIntoSubworkflow = useWorkflowStore((s) => s.navigateIntoSubworkflow);

  // Get the data for the currently viewed context
  const {
    agents,
    routes,
    parallelGroups,
    forEachGroups,
    nodes: storeNodes,
    groupProgress,
    entryPoint,
    subworkflowContexts,
    parentAgent,
    basePath,
  } = viewCtx;

  // Root-level store slices, used to resolve an absolute-namespaced flow-node id
  // back to the store context that owns its live state (status/progress), and to
  // detect when an inline-expanded child DAG first materializes.
  const expandedContexts = useWorkflowStore((s) => s.expandedContexts);
  const rootNodes = useWorkflowStore((s) => s.nodes);
  const rootGroupProgress = useWorkflowStore((s) => s.groupProgress);
  const rootSubContexts = useWorkflowStore((s) => s.subworkflowContexts);
  const rootHighlightedEdges = useWorkflowStore((s) => s.highlightedEdges);

  const [flowNodes, setFlowNodes, onNodesChange] = useNodesState<Node<GraphNodeData>>([]);
  const [flowEdges, setFlowEdges, onEdgesChange] = useEdgesState<Edge>([]);

  const prevBuildKey = useRef<string>('');

  const viewPathKey = JSON.stringify(viewContextPath);

  // Topology signature: a full rebuild (re-layout) fires only when the set of
  // rendered nodes could change — context switch, expansion toggle, or a
  // newly-arrived expanded child DAG. Status-only updates leave this stable so
  // the layout doesn't jitter; those are applied incrementally below.
  const structureKey = useMemo(() => {
    const parts = [`${viewPathKey}#${agents.map((a) => a.name).join(',')}`];
    for (const key of [...expandedContexts].sort()) {
      const path = key.split('.').filter(Boolean).map(Number);
      const ctx = resolveContextByPath(rootSubContexts, path);
      parts.push(`${key}:${ctx?.entryPoint ?? ''}:${ctx?.agents.map((a) => a.name).join(',') ?? ''}`);
    }
    return parts.join('||');
  }, [viewPathKey, agents, expandedContexts, rootSubContexts]);

  useEffect(() => {
    if (agents.length === 0) {
      // Clear stale graph elements when navigated to an empty context
      if (prevBuildKey.current !== structureKey) {
        prevBuildKey.current = structureKey;
        setFlowNodes([]);
        setFlowEdges([]);
      }
      return;
    }

    if (prevBuildKey.current === structureKey) return;
    prevBuildKey.current = structureKey;

    const base: GraphContextInput = {
      agents,
      routes,
      parallelGroups,
      forEachGroups,
      nodes: storeNodes,
      groupProgress,
      entryPoint,
      parentAgent,
      children: subworkflowContexts,
    };
    const { nodes, edges } = buildGraphElements(base, basePath, expandedContexts);
    setFlowNodes(nodes);
    setFlowEdges(edges);
  }, [
    structureKey,
    agents,
    routes,
    parallelGroups,
    forEachGroups,
    storeNodes,
    groupProgress,
    entryPoint,
    parentAgent,
    subworkflowContexts,
    basePath,
    expandedContexts,
    setFlowNodes,
    setFlowEdges,
  ]);

  // Update node data when store nodes change (status, progress, etc.). Each
  // flow node carries its absolute `contextPath` + bare `name`, so we resolve
  // live state from the owning context — including inline-expanded children.
  useEffect(() => {
    setFlowNodes((nds) =>
      nds.map((node) => {
        const gd = node.data as GraphNodeData;
        const path = gd.contextPath ?? [];
        const ctx = path.length === 0 ? null : resolveContextByPath(rootSubContexts, path);
        const ctxNodes = path.length === 0 ? rootNodes : ctx?.nodes;
        const ctxProgress = path.length === 0 ? rootGroupProgress : ctx?.groupProgress;
        const name = gd.name ?? node.id;
        const storeNode = ctxNodes ? ctxNodes[name] : undefined;
        if (!storeNode) return node;

        let newData = gd;
        let changed = false;

        const newStatus = storeNode.status || 'pending';
        if (newStatus !== gd.status) {
          newData = { ...newData, status: newStatus };
          changed = true;
        }

        if (gd.groupName && ctxProgress && ctxProgress[gd.groupName]) {
          const newProgress = ctxProgress[gd.groupName];
          const currentProgress = newData.progress;
          if (
            newProgress &&
            (!currentProgress ||
              currentProgress.completed !== newProgress.completed ||
              currentProgress.failed !== newProgress.failed)
          ) {
            newData = { ...newData, progress: newProgress };
            changed = true;
          }
        }

        return changed ? { ...node, data: newData } : node;
      }),
    );
  }, [rootNodes, rootGroupProgress, rootSubContexts, setFlowNodes]);

  // Resolve edge highlight state per context and stamp it into edge.data. Edges
  // never cross a context boundary (both endpoints share a contextPath), so the
  // source id determines which context's highlightedEdges to consult.
  useEffect(() => {
    setFlowEdges((eds) =>
      eds.map((edge) => {
        const { contextPath, name: fromName } = parseNodeKey(edge.source);
        const toName = parseNodeKey(edge.target).name;
        const ctx = contextPath.length === 0 ? null : resolveContextByPath(rootSubContexts, contextPath);
        const highlights = contextPath.length === 0 ? rootHighlightedEdges : (ctx?.highlightedEdges ?? []);
        const match = highlights.find((h) => h.from === fromName && h.to === toName);
        const newState = match?.state;
        const cur = (edge.data as Record<string, unknown> | undefined)?.highlightState;
        if (cur === newState) return edge;
        return { ...edge, data: { ...edge.data, highlightState: newState } };
      }),
    );
  }, [rootHighlightedEdges, rootSubContexts, setFlowEdges]);

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

  // Double-click on a workflow node to drill into (focus) its subworkflow.
  // Restricted to nodes in the currently viewed context; inline-expanded
  // children are explored in place, not by focus-navigation.
  const onNodeDoubleClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      const gd = node.data as GraphNodeData;
      if (gd.type !== 'workflow') return;
      const path = gd.contextPath ?? [];
      if (path.join('.') !== viewContextPath.join('.')) return;
      const name = gd.name;
      if (name && subworkflowContexts.some((c) => c.slotKey === name || c.parentAgent === name)) {
        navigateIntoSubworkflow(name);
      }
    },
    [subworkflowContexts, navigateIntoSubworkflow, viewContextPath],
  );

  const onPaneClick = useCallback(() => {
    selectNode(null);
  }, [selectNode]);

  // Minimap node color
  const minimapNodeColor = useCallback((node: Node): string => {
    const status = ((node.data as GraphNodeData)?.status || 'pending') as NodeStatus;
    return NODE_STATUS_HEX[status] ?? NODE_STATUS_HEX.pending ?? '#6b7280';
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

  // Auto-select failed agent when workflow fails (root-level failing agent).
  useEffect(() => {
    if (workflowStatus === 'failed' && workflowFailedAgent) {
      selectNode(nodeKey([], workflowFailedAgent));
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
        <FitViewOnContextSwitch viewPathKey={viewPathKey} />
        <DeepLinkHandler />
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

/** Auto-fit viewport when navigating between workflow contexts */
function FitViewOnContextSwitch({ viewPathKey }: { viewPathKey: string }) {
  const { fitView } = useReactFlow();
  const prevKey = useRef(viewPathKey);

  useEffect(() => {
    if (prevKey.current !== viewPathKey) {
      prevKey.current = viewPathKey;
      setTimeout(() => fitView({ padding: 0.2, duration: 300 }), 50);
    }
  }, [viewPathKey, fitView]);

  return null;
}

/** Applies URL query param deep-links (?agent=X, ?subworkflow=Y) on initial load */
function DeepLinkHandler() {
  const error = useDeepLink();

  if (!error) return null;

  return (
    <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 animate-[banner-in_200ms_ease-out]">
      <div className="flex items-center gap-2 px-4 py-2 rounded-lg bg-amber-950/90 border border-amber-500/40 shadow-lg shadow-amber-500/10 backdrop-blur-sm max-w-[560px]">
        <span className="text-xs text-amber-300">⚠</span>
        <span className="text-[11px] text-amber-400/80">{error.message}</span>
        <a
          href={window.location.pathname}
          className="px-2 py-0.5 rounded text-[10px] font-medium text-amber-300 bg-amber-500/20 hover:bg-amber-500/30 transition-colors flex-shrink-0 ml-1"
        >
          Root
        </a>
      </div>
    </div>
  );
}