import { useEffect, useRef, useState } from 'react';
import { useReactFlow } from '@xyflow/react';
import { useWorkflowStore } from '@/stores/workflow-store';
import type { SubworkflowContext } from '@/stores/workflow-store';

/** Parse deep-link params from the current URL. */
function getDeepLinkParams(): { subworkflowPath: string | null; agent: string | null } {
  const params = new URLSearchParams(window.location.search);
  return {
    subworkflowPath: params.get('subworkflow'),
    agent: params.get('agent'),
  };
}

type SegmentFailure =
  | { reason: 'not_found'; segment: string }
  | { reason: 'ambiguous'; segment: string; candidates: string[] };

interface ResolveResult {
  path: number[];
  failure: SegmentFailure | null;
}

/** Regex for positional index notation: `agent_name#N` (0-based). */
const POSITIONAL_RE = /^(.+)#(\d+)$/;

/**
 * Walk the subworkflow context tree to resolve a deep-link path.
 *
 * Each segment is matched against sibling contexts in priority order:
 *   1. Exact `slotKey` match — e.g. `plan_child[item-0]`
 *   2. Positional `name#N` — 0-based index among siblings sharing `parentAgent`
 *      e.g. `plan_child#0` → first iteration of plan_child
 *   3. Bare agent name — matches if **exactly one** sibling has that `parentAgent`;
 *      ambiguous when multiple siblings share the name.
 *
 * This lets external integrations deep-link into for_each iterations without
 * knowing the exact `item_key` values emitted by the engine.
 */
function resolveSubworkflowPath(
  contexts: SubworkflowContext[],
  segments: string[],
): ResolveResult {
  const path: number[] = [];
  let current = contexts;

  for (const segment of segments) {
    const idx = matchSegment(current, segment);
    if (typeof idx === 'number') {
      path.push(idx);
      current = current[idx]!.children;
      continue;
    }
    // idx is a SegmentFailure
    return { path, failure: idx };
  }

  return { path, failure: null };
}

/**
 * Match a single path segment against a list of sibling contexts.
 * Returns the matched index, or a SegmentFailure describing why it failed.
 */
function matchSegment(
  contexts: SubworkflowContext[],
  segment: string,
): number | SegmentFailure {
  // 1. Exact slotKey match (newest first for re-run tolerance)
  for (let i = contexts.length - 1; i >= 0; i--) {
    if (contexts[i]!.slotKey === segment) return i;
  }

  // 2. Positional notation: agent_name#N
  const posMatch = POSITIONAL_RE.exec(segment);
  if (posMatch) {
    const baseName = posMatch[1]!;
    const ordinal = parseInt(posMatch[2]!, 10);
    const siblings = contexts
      .map((c, i) => ({ ctx: c, index: i }))
      .filter(({ ctx }) => ctx.parentAgent === baseName);
    if (siblings.length === 0) {
      return { reason: 'not_found', segment };
    }
    if (ordinal >= siblings.length) {
      return {
        reason: 'not_found',
        segment,
      };
    }
    return siblings[ordinal]!.index;
  }

  // 3. Bare agent name — unique parentAgent match only
  const agentMatches = contexts
    .map((c, i) => ({ ctx: c, index: i }))
    .filter(({ ctx }) => ctx.parentAgent === segment);

  if (agentMatches.length === 1) {
    return agentMatches[0]!.index;
  }
  if (agentMatches.length > 1) {
    const candidates = agentMatches.map(({ ctx }) => ctx.slotKey);
    return { reason: 'ambiguous', segment, candidates };
  }

  return { reason: 'not_found', segment };
}

export interface DeepLinkError {
  message: string;
}

/**
 * Reads `?agent=` and `?subworkflow=` query params on initial load
 * and auto-selects / navigates to the matching node once the workflow
 * state has been replayed.
 *
 * ### Subworkflow path notation
 *
 * Paths are slash-separated:
 *   `?subworkflow=segment1/segment2`
 *
 * Each segment is matched in priority order:
 *   1. **Exact slotKey** — `plan_child[item-0]`
 *      Matches the engine-emitted `slot_key` verbatim.
 *   2. **Positional index** — `plan_child#0` (0-based)
 *      Matches the Nth iteration among siblings sharing that `parentAgent`.
 *      Useful when the caller doesn't know the `item_key` values.
 *   3. **Bare agent name** — `plan_child`
 *      Matches if exactly one sibling has that parentAgent (including when
 *      its slotKey has bracket notation). Fails as ambiguous when multiple
 *      for_each iterations exist — use exact slotKey or positional notation.
 *
 * Returns an error object if the deep-link target cannot be resolved.
 * Must be rendered inside a `<ReactFlow>` provider so `useReactFlow()` works.
 */
export function useDeepLink(): DeepLinkError | null {
  const [error, setError] = useState<DeepLinkError | null>(null);
  const applied = useRef(false);
  const { fitView } = useReactFlow();

  const { subworkflowPath, agent } = getDeepLinkParams();
  const hasParams = !!(subworkflowPath || agent);

  useEffect(() => {
    if (applied.current || !hasParams) return;

    // Subscribe to store changes and apply deep-link when state is ready.
    // This avoids timing issues with useEffect + zustand selectors.
    const unsubscribe = useWorkflowStore.subscribe((state) => {
      if (applied.current) return;

      // Wait until root agents have been populated (workflow_started processed)
      if (state.agents.length === 0) return;

      applied.current = true;
      unsubscribe();

      // 1. Navigate into subworkflow path
      if (subworkflowPath) {
        const segments = subworkflowPath.split('/').filter(Boolean);
        const { path, failure } = resolveSubworkflowPath(
          state.subworkflowContexts,
          segments,
        );

        if (failure) {
          const resolved = segments.slice(0, path.length).join('/');
          const prefix = resolved ? ` (resolved: ${resolved})` : '';
          let message: string;
          if (failure.reason === 'ambiguous') {
            const alts = failure.candidates
              .map((c, i) => `"${c}" or "${failure.segment}#${i}"`)
              .join(', ');
            message = `"${failure.segment}" is ambiguous${prefix} — multiple iterations exist. Use: ${alts}`;
          } else {
            message = `Subworkflow "${failure.segment}" not found${prefix}. It may not have started yet. Use exact slotKey or positional notation (e.g. agent#0).`;
          }
          setError({ message });
          return;
        }

        // Apply the full navigation path at once
        useWorkflowStore.setState({ viewContextPath: path, selectedNode: null });
      }

      // 2. Select agent node
      if (agent) {
        // Determine which context to check for the agent
        const freshState = useWorkflowStore.getState();
        let agentList: { name: string }[];

        if (freshState.viewContextPath.length === 0) {
          agentList = freshState.agents;
        } else {
          // Walk the context tree to get agents at the target depth
          let ctx: SubworkflowContext | undefined;
          let contexts = freshState.subworkflowContexts;
          for (const idx of freshState.viewContextPath) {
            ctx = contexts[idx];
            if (!ctx) break;
            contexts = ctx.children;
          }
          agentList = ctx?.agents ?? [];
        }

        const agentExists = agentList.some((a) => a.name === agent);
        if (!agentExists) {
          const location = subworkflowPath || 'root workflow';
          setError({
            message: `Agent "${agent}" not found in ${location}.`,
          });
          return;
        }

        useWorkflowStore.setState({ selectedNode: agent });

        // Center the view on the node after React Flow rebuilds the graph
        setTimeout(() => {
          fitView({ nodes: [{ id: agent }], padding: 0.5, duration: 400 });
        }, 200);
      }
    });

    // Also check the current state immediately (late-joiner replay may have
    // already completed before this effect runs)
    const currentState = useWorkflowStore.getState();
    if (currentState.agents.length > 0 && !applied.current) {
      // Trigger the subscriber with current state
      useWorkflowStore.setState({});
    }

    return unsubscribe;
  }, [hasParams, subworkflowPath, agent, fitView]);

  return error;
}
