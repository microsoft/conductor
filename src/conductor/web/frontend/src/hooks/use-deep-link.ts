import { useEffect, useRef, useState } from 'react';
import { useReactFlow } from '@xyflow/react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { SubworkflowContext } from '@/stores/workflow-store';
import { nodeKey } from '@/lib/node-id';
import { expansionKeysForContextPath } from '@/components/graph/graph-layout';

/** Parse deep-link params from the current URL. */
function getDeepLinkParams(): { subworkflowPath: string | null; agent: string | null } {
  const params = new URLSearchParams(window.location.search);
  return {
    subworkflowPath: params.get('subworkflow'),
    agent: params.get('agent'),
  };
}

/**
 * Walk the subworkflow context tree to find a path of indices for the given
 * URL segments.
 *
 * Each segment is matched against `slotKey` first, then `parentAgent` as a
 * fallback. This lets new URLs encode for_each iteration slot keys
 * (e.g. `plan_children_group[item-7]`) and round-trip into the right
 * concurrent iteration, while old URLs keyed by agent name (and old
 * conductor builds where `slotKey` defaults to `parentAgent`) keep working.
 *
 * For ambiguous parentAgent matches (e.g. multiple iterations of the same
 * group), the newest matching context wins — same precedence the engine
 * uses when resolving live events.
 */
function resolveSubworkflowPath(
  contexts: SubworkflowContext[],
  segments: string[],
): { path: number[]; failedSegment: string | null } {
  const path: number[] = [];
  let current = contexts;

  for (const segment of segments) {
    let idx = -1;
    // Pass 1: exact slotKey match (newest-first for re-runs / iteration loops)
    for (let i = current.length - 1; i >= 0; i--) {
      if (current[i]!.slotKey === segment) {
        idx = i;
        break;
      }
    }
    // Pass 2: legacy parentAgent fallback (newest-first)
    if (idx === -1) {
      for (let i = current.length - 1; i >= 0; i--) {
        if (current[i]!.parentAgent === segment) {
          idx = i;
          break;
        }
      }
    }
    if (idx === -1) {
      return { path, failedSegment: segment };
    }
    path.push(idx);
    current = current[idx]!.children;
  }

  return { path, failedSegment: null };
}

/** Resolve a {@link SubworkflowContext} from its absolute index path (root = `[]` → null). */
function contextAtPath(
  contexts: SubworkflowContext[],
  path: number[],
): SubworkflowContext | null {
  let current: SubworkflowContext[] = contexts;
  let ctx: SubworkflowContext | null = null;
  for (const idx of path) {
    ctx = current[idx] ?? null;
    if (!ctx) return null;
    current = ctx.children;
  }
  return ctx;
}

interface AgentMatch {
  path: number[];
  ctx: SubworkflowContext;
}

/**
 * Walk the entire subworkflow tree and collect every context whose `agents[]`
 * contains an entry matching `agentName`.
 *
 * Used for agent-only deep-links (?agent=foo, no ?subworkflow=) so that an
 * agent which lives inside a sub-workflow / for_each iteration is reachable
 * without the caller needing to construct the full slot path. External
 * notification feeds typically only know the agent name reliably.
 */
function findAgentMatches(
  contexts: SubworkflowContext[],
  agentName: string,
  basePath: number[] = [],
): AgentMatch[] {
  const matches: AgentMatch[] = [];
  for (let i = 0; i < contexts.length; i++) {
    const ctx = contexts[i]!;
    const path = [...basePath, i];
    if (ctx.agents.some((a) => a.name === agentName)) {
      matches.push({ path, ctx });
    }
    if (ctx.children.length > 0) {
      matches.push(...findAgentMatches(ctx.children, agentName, path));
    }
  }
  return matches;
}

/**
 * Pick the most relevant match among many candidates: running contexts beat
 * non-running, then deeper paths beat shallower (more specific iteration
 * wins over a parent that contains it), then newest-by-creation-order wins.
 * Mirrors the engine's "live edge" preference for live event routing.
 */
function pickBestAgentMatch(matches: AgentMatch[]): AgentMatch | null {
  if (matches.length === 0) return null;
  return [...matches].sort((a, b) => {
    const aRunning = a.ctx.status === 'running' ? 1 : 0;
    const bRunning = b.ctx.status === 'running' ? 1 : 0;
    if (aRunning !== bRunning) return bRunning - aRunning;
    if (a.path.length !== b.path.length) return b.path.length - a.path.length;
    // Same depth, same status: lexicographically-larger path = newer
    for (let i = 0; i < a.path.length; i++) {
      const ai = a.path[i]!;
      const bi = b.path[i]!;
      if (ai !== bi) return bi - ai;
    }
    return 0;
  })[0]!;
}

/** Render a slot-key path for human-readable error messages. */
function describeLocation(contexts: SubworkflowContext[], path: number[]): string {
  const segments: string[] = [];
  let current = contexts;
  for (const idx of path) {
    const ctx = current[idx];
    if (!ctx) break;
    segments.push(ctx.slotKey || ctx.parentAgent || `[${idx}]`);
    current = ctx.children;
  }
  return segments.join('/');
}

export interface DeepLinkError {
  message: string;
}

/**
 * Reads `?agent=` and `?subworkflow=` query params on initial load
 * and reveals the matching node inline once the workflow state has been
 * replayed.
 *
 * Rather than drilling the view into the target's context, the target's
 * ancestor containers are expanded in place (root frame) so the node surfaces
 * within the full graph, then it is selected and centered. Drill-down stays
 * available via double-click / breadcrumb navigation. Expansion touches only
 * the target's ancestor chain (a sequential subworkflow contributes its context
 * key; a `for_each` iteration contributes its group key plus its own context
 * key), so a wide fan-out never auto-expands unrelated siblings.
 *
 * Subworkflow paths support slash-separated nesting. Each segment matches
 * the child context's `slotKey` first, then falls back to `parentAgent`:
 *   ?subworkflow=planning/design                    → reveal root→planning→design
 *   ?subworkflow=plan_children_group[item-7]/build  → reveal a specific
 *                                                     for_each iteration
 *
 * Agent-only links (?agent=foo, no ?subworkflow=) search transitively:
 * root agents first, then every sub-workflow context. If the agent is
 * found in exactly one place, we reveal it there. If it's found in many
 * places (e.g. the same agent ran in every for_each iteration), the
 * running > deepest > newest match wins.
 *
 * Returns an error object if the deep-link target cannot be resolved.
 * Must be rendered inside a <ReactFlow> provider so useReactFlow() works.
 */
export function useDeepLink(): DeepLinkError | null {
  const [error, setError] = useState<DeepLinkError | null>(null);
  const applied = useRef(false);
  const { fitView } = useReactFlow();

  const { subworkflowPath, agent } = getDeepLinkParams();
  const hasParams = !!(subworkflowPath || agent);

  useEffect(() => {
    if (applied.current || !hasParams) return;

    let debounceTimer: ReturnType<typeof setTimeout> | null = null;
    let hardTimeout: ReturnType<typeof setTimeout> | null = null;
    let unsubscribe: (() => void) | null = null;

    // Expand the target's ancestor chain inline (keeping the root frame) and
    // select it, instead of re-rooting the view into the child context.
    const revealContext = (targetPath: number[], selectedId: string | null): void => {
      const keys = expansionKeysForContextPath(
        useWorkflowStore.getState().subworkflowContexts,
        targetPath,
      );
      useWorkflowStore.getState().expandContexts(keys);
      useWorkflowStore.setState({ viewContextPath: [], selectedNode: selectedId });
    };

    // Center the view on a node after React Flow rebuilds the expanded graph.
    const centerOn = (fitId: string): void => {
      setTimeout(() => {
        fitView({ nodes: [{ id: fitId }], padding: 0.5, duration: 400 });
      }, 200);
    };

    const apply = () => {
      if (applied.current) return;
      applied.current = true;
      if (debounceTimer) clearTimeout(debounceTimer);
      if (hardTimeout) clearTimeout(hardTimeout);
      if (unsubscribe) unsubscribe();

      const state = useWorkflowStore.getState();
      if (state.agents.length === 0) {
        setError({ message: 'Workflow state did not load.' });
        return;
      }

      // Resolve subworkflow path (if provided)
      let resolvedPath: number[] = [];
      if (subworkflowPath) {
        const segments = subworkflowPath.split('/').filter(Boolean);
        const result = resolveSubworkflowPath(state.subworkflowContexts, segments);
        if (result.failedSegment) {
          const resolved = segments.slice(0, result.path.length).join('/');
          // Reveal as far as resolution got so the user sees where it stopped.
          revealContext(result.path, null);
          setError({
            message: `Subworkflow "${result.failedSegment}" not found${resolved ? ` (resolved: ${resolved})` : ''}. It may not have started yet.`,
          });
          return;
        }
        resolvedPath = result.path;
      }

      // Resolve agent (if provided)
      if (agent) {
        const agentsAtTarget =
          resolvedPath.length === 0
            ? state.agents
            : (contextAtPath(state.subworkflowContexts, resolvedPath)?.agents ?? []);

        if (agentsAtTarget.some((a) => a.name === agent)) {
          // Agent is at the requested (or root) location.
          const selectedId = nodeKey(resolvedPath, agent);
          revealContext(resolvedPath, selectedId);
          centerOn(selectedId);
          return;
        }

        // Not at the requested location. Search transitively.
        const matches = findAgentMatches(state.subworkflowContexts, agent);

        if (matches.length === 0) {
          const where = subworkflowPath || 'root workflow';
          // Even on failure, reveal any explicit subworkflow nav, otherwise
          // stay at root so sticky-follow doesn't strand the user inside a
          // stale for_each iteration during replay.
          revealContext(resolvedPath, null);
          setError({ message: `Agent "${agent}" not found in ${where}.` });
          return;
        }

        if (subworkflowPath) {
          // User asked for a specific path, agent isn't there but is
          // elsewhere — reveal the requested subworkflow and surface the
          // discovered locations so the next click is obvious.
          const locations = matches
            .slice(0, 5)
            .map((m) => describeLocation(state.subworkflowContexts, m.path))
            .join(', ');
          const more = matches.length > 5 ? `, and ${matches.length - 5} more` : '';
          revealContext(resolvedPath, null);
          setError({
            message: `Agent "${agent}" not found in ${subworkflowPath}. Found in: ${locations}${more}`,
          });
          return;
        }

        // Agent-only link: pick the best transitive match and reveal it.
        const best = pickBestAgentMatch(matches)!;
        const selectedId = nodeKey(best.path, agent);
        revealContext(best.path, selectedId);
        centerOn(selectedId);
        return;
      }

      // Subworkflow nav only, no agent: reveal it inline and center on its
      // container node (the subworkflow's node lives in its parent context).
      revealContext(resolvedPath, null);
      if (resolvedPath.length > 0) {
        const ctx = contextAtPath(state.subworkflowContexts, resolvedPath);
        if (ctx) centerOn(nodeKey(resolvedPath.slice(0, -1), ctx.slotKey));
      }
    };

    /**
     * Decide whether the current state is "ready enough" to apply the deep
     * link, or if we should keep waiting for more replayed events. Returns
     * true when the resolution is unambiguous (agent at requested location,
     * or workflow has finished so no more contexts are coming).
     */
    const isResolved = (): boolean => {
      const state = useWorkflowStore.getState();
      if (state.agents.length === 0) return false;

      // If the workflow has ended, we have all the state we'll ever get.
      if (state.workflowStatus !== 'running' && state.workflowStatus !== 'pending') {
        return true;
      }

      // For explicit subworkflow paths, wait until the path resolves.
      if (subworkflowPath) {
        const segments = subworkflowPath.split('/').filter(Boolean);
        const { failedSegment } = resolveSubworkflowPath(
          state.subworkflowContexts,
          segments,
        );
        if (failedSegment) return false;
      }

      // For agent-only links, wait until the agent appears somewhere.
      if (agent && !subworkflowPath) {
        const rootHas = state.agents.some((a) => a.name === agent);
        if (!rootHas && findAgentMatches(state.subworkflowContexts, agent).length === 0) {
          return false;
        }
      }

      return true;
    };

    // Each time the store changes, debounce 200ms then check if we can apply.
    // The debounce gives the WS replay burst a chance to finish dispatching.
    const scheduleCheck = () => {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        if (applied.current) return;
        if (isResolved()) apply();
      }, 200);
    };

    unsubscribe = useWorkflowStore.subscribe(scheduleCheck);

    // Hard cap: if 5 seconds pass and we still haven't applied (e.g. live
    // workflow that never reaches a terminal state and the agent never
    // appeared), fall through with whatever state we have so the user gets
    // a deterministic error instead of a hung UI.
    hardTimeout = setTimeout(() => {
      if (applied.current) return;
      apply();
    }, 5000);

    // Kick off an initial check in case state was already loaded before
    // this effect attached.
    scheduleCheck();

    return () => {
      if (debounceTimer) clearTimeout(debounceTimer);
      if (hardTimeout) clearTimeout(hardTimeout);
      if (unsubscribe) unsubscribe();
    };
  }, [hasParams, subworkflowPath, agent, fitView]);

  return error;
}
