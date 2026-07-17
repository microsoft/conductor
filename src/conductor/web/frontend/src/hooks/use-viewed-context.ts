/**
 * Hooks for accessing the currently viewed workflow context.
 *
 * These replace direct `getViewedContext()` calls in Zustand selectors,
 * which create new objects on every call and cause infinite re-render loops.
 * Instead, we select raw state and resolve the viewed context with useMemo.
 */
import { useMemo } from 'react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { NodeData, GroupProgress, HighlightedEdge, SubworkflowContext } from '@/stores/workflow-store';
import { parseNodeKey } from '@/lib/node-id';

/** Resolve a SubworkflowContext from a path of indices. */
function resolveCtx(contexts: SubworkflowContext[], path: number[]): SubworkflowContext | null {
  if (path.length === 0) return null;
  let ctx: SubworkflowContext | undefined = contexts[path[0]!];
  for (let i = 1; i < path.length && ctx; i++) {
    ctx = ctx.children[path[i]!];
  }
  return ctx ?? null;
}

/**
 * Resolve a graph node's live store `NodeData` from its absolute context path
 * and bare name (both stamped onto `GraphNodeData` by `buildGraphElements`).
 *
 * Node ids are namespaced by context (see `lib/node-id`) so a bare
 * `useViewedNodes()[id]` lookup no longer works once inline-expanded children
 * render alongside the base context — this resolves the owning context instead.
 */
export function useNodeLiveData(
  data: { contextPath?: number[]; name?: string } | undefined,
): NodeData | undefined {
  const rootNodes = useWorkflowStore((s) => s.nodes);
  const subContexts = useWorkflowStore((s) => s.subworkflowContexts);
  const path = data?.contextPath ?? [];
  const name = data?.name;
  const key = `${path.join('.')}::${name ?? ''}`;
  return useMemo(() => {
    if (!name) return undefined;
    if (path.length === 0) return rootNodes[name];
    const ctx = resolveCtx(subContexts, path);
    return ctx?.nodes[name];
    // `key` encodes path + name; depending on it keeps the array-typed
    // `path`/`name` out of the dependency list without staleness.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, rootNodes, subContexts]);
}

/**
 * Resolve the currently selected node's live `NodeData` from its namespaced id
 * (`selectedNode`), regardless of which context is being viewed. Node ids are
 * namespaced by context, so the selection can point into an inline-expanded
 * child or a different level than the current view.
 */
export function useSelectedNodeData(): NodeData | undefined {
  const selectedNode = useWorkflowStore((s) => s.selectedNode);
  const rootNodes = useWorkflowStore((s) => s.nodes);
  const subContexts = useWorkflowStore((s) => s.subworkflowContexts);
  return useMemo(() => {
    if (!selectedNode) return undefined;
    const { contextPath, name } = parseNodeKey(selectedNode);
    if (contextPath.length === 0) return rootNodes[name];
    const ctx = resolveCtx(subContexts, contextPath);
    return ctx?.nodes[name];
  }, [selectedNode, rootNodes, subContexts]);
}

/** Get the nodes map for the currently viewed context. */
export function useViewedNodes(): Record<string, NodeData> {
  const viewPath = useWorkflowStore((s) => s.viewContextPath);
  const rootNodes = useWorkflowStore((s) => s.nodes);
  const subContexts = useWorkflowStore((s) => s.subworkflowContexts);
  return useMemo(() => {
    if (viewPath.length === 0) return rootNodes;
    return resolveCtx(subContexts, viewPath)?.nodes ?? rootNodes;
  }, [viewPath, rootNodes, subContexts]);
}

/** Get the groupProgress map for the currently viewed context. */
export function useViewedGroupProgress(): Record<string, GroupProgress> {
  const viewPath = useWorkflowStore((s) => s.viewContextPath);
  const rootProgress = useWorkflowStore((s) => s.groupProgress);
  const subContexts = useWorkflowStore((s) => s.subworkflowContexts);
  return useMemo(() => {
    if (viewPath.length === 0) return rootProgress;
    return resolveCtx(subContexts, viewPath)?.groupProgress ?? rootProgress;
  }, [viewPath, rootProgress, subContexts]);
}

/** Get the highlightedEdges for the currently viewed context. */
export function useViewedHighlightedEdges(): HighlightedEdge[] {
  const viewPath = useWorkflowStore((s) => s.viewContextPath);
  const rootEdges = useWorkflowStore((s) => s.highlightedEdges);
  const subContexts = useWorkflowStore((s) => s.subworkflowContexts);
  return useMemo(() => {
    if (viewPath.length === 0) return rootEdges;
    return resolveCtx(subContexts, viewPath)?.highlightedEdges ?? rootEdges;
  }, [viewPath, rootEdges, subContexts]);
}

/** Get the subworkflow contexts for the currently viewed level. */
export function useViewedSubworkflowContexts(): SubworkflowContext[] {
  const viewPath = useWorkflowStore((s) => s.viewContextPath);
  const rootContexts = useWorkflowStore((s) => s.subworkflowContexts);
  return useMemo(() => {
    if (viewPath.length === 0) return rootContexts;
    return resolveCtx(rootContexts, viewPath)?.children ?? [];
  }, [viewPath, rootContexts]);
}

/** Get the full viewed context for graph building (agents, routes, etc). */
export function useViewedGraphData() {
  const viewPath = useWorkflowStore((s) => s.viewContextPath);
  const rootAgents = useWorkflowStore((s) => s.agents);
  const rootRoutes = useWorkflowStore((s) => s.routes);
  const rootParallel = useWorkflowStore((s) => s.parallelGroups);
  const rootForEach = useWorkflowStore((s) => s.forEachGroups);
  const rootNodes = useWorkflowStore((s) => s.nodes);
  const rootProgress = useWorkflowStore((s) => s.groupProgress);
  const rootEntry = useWorkflowStore((s) => s.entryPoint);
  const subContexts = useWorkflowStore((s) => s.subworkflowContexts);

  return useMemo(() => {
    if (viewPath.length === 0) {
      return {
        agents: rootAgents,
        routes: rootRoutes,
        parallelGroups: rootParallel,
        forEachGroups: rootForEach,
        nodes: rootNodes,
        groupProgress: rootProgress,
        entryPoint: rootEntry,
        subworkflowContexts: subContexts,
        parentAgent: null as string | null,
        basePath: [] as number[],
      };
    }
    const ctx = resolveCtx(subContexts, viewPath);
    if (!ctx) {
      return {
        agents: rootAgents,
        routes: rootRoutes,
        parallelGroups: rootParallel,
        forEachGroups: rootForEach,
        nodes: rootNodes,
        groupProgress: rootProgress,
        entryPoint: rootEntry,
        subworkflowContexts: subContexts,
        parentAgent: null as string | null,
        basePath: [] as number[],
      };
    }
    return {
      agents: ctx.agents,
      routes: ctx.routes,
      parallelGroups: ctx.parallelGroups,
      forEachGroups: ctx.forEachGroups,
      nodes: ctx.nodes,
      groupProgress: ctx.groupProgress,
      entryPoint: ctx.entryPoint,
      subworkflowContexts: ctx.children,
      parentAgent: ctx.parentAgent,
      basePath: viewPath,
    };
  }, [viewPath, rootAgents, rootRoutes, rootParallel, rootForEach, rootNodes, rootProgress, rootEntry, subContexts]);
}
