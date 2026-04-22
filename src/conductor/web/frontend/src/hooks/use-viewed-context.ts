/**
 * Hooks for accessing the currently viewed workflow context.
 *
 * These replace direct `getViewedContext()` calls in Zustand selectors,
 * which create new objects on every call and cause infinite re-render loops.
 * Instead, we select raw state and resolve the viewed context with useMemo.
 */
import { useMemo } from 'react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { NodeData, GroupProgress, HighlightedEdge, SubworkflowContext, WorkflowAgent, RouteEdge, ParallelGroup, ForEachGroup } from '@/stores/workflow-store';

/** Resolve a SubworkflowContext from a path of indices. */
function resolveCtx(contexts: SubworkflowContext[], path: number[]): SubworkflowContext | null {
  if (path.length === 0) return null;
  let ctx: SubworkflowContext | undefined = contexts[path[0]!];
  for (let i = 1; i < path.length && ctx; i++) {
    ctx = ctx.children[path[i]!];
  }
  return ctx ?? null;
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
    };
  }, [viewPath, rootAgents, rootRoutes, rootParallel, rootForEach, rootNodes, rootProgress, rootEntry, subContexts]);
}
