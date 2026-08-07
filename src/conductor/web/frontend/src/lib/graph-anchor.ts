/**
 * Pure viewport-anchoring math for the workflow graph (issue #375).
 *
 * Without compensation, expanding an inline subworkflow makes the whole graph
 * appear to jump. The layout is not the problem: `layoutTopLevel` in
 * `graph-layout.ts` normalizes each rebuild's bounding box to origin, so
 * growing one container shifts `minX`/`minY` and therefore every node's
 * position on the canvas — while the camera stays exactly where it was. The
 * world slides under a fixed viewport, which reads as "the view reset".
 *
 * The fix is to compensate the camera rather than change the layout: find a
 * node present in both the old and new layouts, and offset the viewport
 * transform by the negation of how far that node moved. The anchor stays
 * pinned on screen, everything else visibly grows away from it on expand and
 * closes back onto it on collapse. Dagre is deterministic, so expanding a
 * container and then collapsing *that same* container feeds it the identical
 * input graph, and because the same node is pinned on both legs the deltas
 * telescope back to the original view.
 *
 * Kept free of React and of any `@xyflow/react` runtime import so the anchor
 * selection, hint bookkeeping, and viewport arithmetic are unit-testable
 * without mounting a React Flow instance — the same shape as `lib/reconnect`.
 *
 * Known limitation: each toggle is compensated exactly, but a *sequence* of
 * toggles is not always invertible by a single bulk one. Expanding three
 * subworkflows individually pins three different containers, and each pin can
 * include that container's own move as it becomes the widest node and takes
 * over `minX` from a narrower sibling; collapsing all three at once pins one
 * node, and no node carries the summed delta needed to undo the three. That
 * path leaves a residual pan of roughly 20px, horizontal in the top-to-bottom
 * layouts observed, and it accumulates — repeating the cycle repeats the
 * offset. Fit-view clears it. Pinning one canonical node throughout would
 * remove it, at the cost of the property this exists for — that the container
 * you clicked stays under the cursor.
 */

/** Minimal structural view of a React Flow node; see `GraphNodeData` in `components/graph/graph-layout.ts`. */
export interface AnchorNode {
  id: string;
  position: XYPosition;
  /** Set when the node is rendered inside a container; `position` is relative to it. */
  parentId?: string;
  data?: {
    /** Expansion key toggled by a subworkflow / iteration chevron. */
    childContextKey?: string;
    /** Expansion key toggled by a `for_each`-of-workflow group chevron. */
    groupExpansionKey?: string;
  };
}

export interface XYPosition {
  x: number;
  y: number;
}

export interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

export interface AnchorInput {
  prevNodes: readonly AnchorNode[];
  nextNodes: readonly AnchorNode[];
  /**
   * The expansion key whose container this rebuild should prefer as its anchor
   * — see {@link nextAnchorHint}. `null` when several toggled at once
   * (expand/collapse-all) or when a stale sticky hint has expired; selection
   * then falls back to the shared node nearest the pane center.
   */
  anchorKeyHint: string | null;
  /** The viewport as it stands *before* compensation. */
  viewport: Viewport;
  /** Rendered size of the graph pane, in screen pixels. */
  paneSize: { width: number; height: number };
}

/**
 * Resolve every node's canvas-absolute top-left, following `parentId` chains.
 *
 * React Flow stores a nested node's `position` relative to its parent
 * container, so a node inside an expanded container keeps a byte-identical
 * `position` across a rebuild even as its container moves. Raw positions
 * therefore under-report movement and are not comparable between layouts;
 * absolute ones are.
 *
 * A `parentId` pointing at a node that isn't present resolves the node as
 * top-level. A `parentId` cycle terminates on the `seen` set with arbitrary,
 * iteration-order-dependent offsets rather than throwing — an anchor that is
 * slightly wrong still beats a dashboard that crashes on a malformed graph.
 */
export function toAbsolutePositions(nodes: readonly AnchorNode[]): Map<string, XYPosition> {
  const byId = new Map<string, AnchorNode>();
  for (const node of nodes) byId.set(node.id, node);

  const resolved = new Map<string, XYPosition>();

  for (const node of nodes) {
    if (resolved.has(node.id)) continue;

    // Walk up to the highest unresolved ancestor, collecting the chain, then
    // fold back down so every node on the path is memoized in one pass. The
    // `seen` set admits each node once, so a `parentId` cycle terminates.
    const chain: AnchorNode[] = [];
    const seen = new Set<string>();
    let current: AnchorNode | undefined = node;
    let base: XYPosition = { x: 0, y: 0 };

    while (current && !seen.has(current.id)) {
      seen.add(current.id);
      chain.push(current);
      const parentId = current.parentId;
      if (parentId === undefined) break;
      const known = resolved.get(parentId);
      if (known) {
        base = known;
        break;
      }
      current = byId.get(parentId);
    }

    for (let i = chain.length - 1; i >= 0; i--) {
      const link = chain[i]!;
      base = { x: base.x + link.position.x, y: base.y + link.position.y };
      resolved.set(link.id, base);
    }
  }

  return resolved;
}

/** True when `node` is the container whose chevron toggled `key`. */
function ownsExpansionKey(node: AnchorNode | undefined, key: string): boolean {
  return node?.data?.childContextKey === key || node?.data?.groupExpansionKey === key;
}

/**
 * Choose the node to pin across a rebuild, or `null` when the two layouts share
 * no node, or when the pane has not been measured and no hint matched.
 *
 * Preference order:
 *
 * 1. The container owning `anchorKeyHint`, so the chevron the user just clicked
 *    stays exactly where they clicked it. Both key namespaces that share
 *    `expandedContexts` are checked — pure context keys (`"0.2"`) live on
 *    `childContextKey`, `for_each`-group keys (containing `::`) on
 *    `groupExpansionKey` — and both sides of the rebuild are consulted, because
 *    a group only carries `groupExpansionKey` once it is expandable.
 * 2. Otherwise the shared node nearest the center of the pane, which is the
 *    part of the graph the user is most plausibly looking at.
 *
 * Ties in step 2 are broken on node id so an ambiguous layout still anchors
 * deterministically — an anchor that flip-flops between two equidistant nodes
 * would reintroduce the jump this exists to remove.
 */
export function resolveAnchorId(
  input: AnchorInput,
  /** Precomputed `toAbsolutePositions(input.prevNodes)`, to avoid a second fold. */
  prevAbsolute: Map<string, XYPosition> = toAbsolutePositions(input.prevNodes),
): string | null {
  const { prevNodes, nextNodes, anchorKeyHint, viewport, paneSize } = input;

  const prevById = new Map<string, AnchorNode>();
  for (const node of prevNodes) prevById.set(node.id, node);

  const shared = nextNodes.filter((node) => prevById.has(node.id));
  if (shared.length === 0) return null;

  if (anchorKeyHint !== null) {
    const hinted = shared.find(
      (node) =>
        ownsExpansionKey(node, anchorKeyHint) ||
        ownsExpansionKey(prevById.get(node.id), anchorKeyHint),
    );
    if (hinted) return hinted.id;
  }

  // An unmeasured pane gives no meaningful center, so rather than anchor on
  // whatever happens to sit nearest flow-space origin, decline to anchor.
  if (paneSize.width <= 0 || paneSize.height <= 0) return null;

  // Distances are measured in the *previous* layout: the pane center describes
  // what the user is looking at now, which is the layout still on screen.
  const center: XYPosition = {
    x: (-viewport.x + paneSize.width / 2) / viewport.zoom,
    y: (-viewport.y + paneSize.height / 2) / viewport.zoom,
  };

  let best: string | null = null;
  let bestDistanceSq = Infinity;

  for (const node of shared) {
    // Unreachable in practice: `shared` is filtered on presence in `prevNodes`.
    const pos = prevAbsolute.get(node.id);
    if (!pos) continue;
    const dx = pos.x - center.x;
    const dy = pos.y - center.y;
    // Squared distance: monotonic in distance, so it ranks identically without the sqrt.
    const distanceSq = dx * dx + dy * dy;
    if (
      distanceSq < bestDistanceSq ||
      (distanceSq === bestDistanceSq && best !== null && node.id < best)
    ) {
      best = node.id;
      bestDistanceSq = distanceSq;
    }
  }

  return best;
}

/**
 * The viewport that keeps the anchor node's top-left pinned on screen across a
 * rebuild, or `null` when no compensation should be applied: no shared anchor,
 * an unmeasured pane with no matching hint, a non-finite delta, or an anchor
 * that did not move.
 *
 * The did-not-move case is load-bearing, not just informative — it suppresses
 * the no-op `setViewport` that would otherwise fire on every status-driven
 * rebuild and interrupt any animated `fitView` in flight.
 *
 * Top-left rather than center: for the container the user just toggled, that is
 * where its header and chevron sit, so the thing under the cursor stays under
 * the cursor while the container grows down and to the right.
 *
 * Zoom is deliberately untouched — this compensates for the layout's origin
 * shift, it does not reframe the graph.
 */
export function anchoredViewport(input: AnchorInput): Viewport | null {
  const prevAbsolute = toAbsolutePositions(input.prevNodes);
  const anchorId = resolveAnchorId(input, prevAbsolute);
  if (anchorId === null) return null;

  const before = prevAbsolute.get(anchorId);
  // Unreachable in practice: the anchor is present in both node arrays.
  const after = toAbsolutePositions(input.nextNodes).get(anchorId);
  if (!before || !after) return null;

  const dx = after.x - before.x;
  const dy = after.y - before.y;
  // Finiteness is checked separately because `NaN === 0` is false, so a
  // non-finite delta would slip past the zero check into d3, where it becomes
  // an invalid CSS transform and poisons the stored viewport for every later
  // rebuild. Mirrors the `Number.isFinite` guard in `graph-layout.ts`.
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) {
    console.warn(
      `[graph-anchor] non-finite delta for anchor "${anchorId}"; skipping compensation`,
      { before, after },
    );
    return null;
  }
  if (dx === 0 && dy === 0) return null;

  const { x, y, zoom } = input.viewport;
  return { x: x - dx * zoom, y: y - dy * zoom, zoom };
}

/**
 * How many consecutive rebuilds that toggle nothing may keep reusing the last
 * clicked container as the anchor. Expanding a subworkflow whose child DAG has
 * not arrived yet grows the same container again a beat later, and pinning it
 * once more is right; holding the hint indefinitely is not, since an unrelated
 * `for_each` fanning out minutes later would still pin a container the user may
 * have scrolled far away from — as would a replay scrub, which can rebuild the
 * graph without resetting `expandedContexts`.
 */
export const MAX_STICKY_ANCHOR_REBUILDS = 2;

export interface AnchorHintInput {
  previousKeys: ReadonlySet<string>;
  currentKeys: ReadonlySet<string>;
  /** Hint carried in from the previous rebuild. */
  currentHint: string | null;
  /** Consecutive preceding rebuilds that toggled no key. */
  stickyRebuilds: number;
  contextSwitched: boolean;
}

export interface AnchorHintResult {
  hint: string | null;
  stickyRebuilds: number;
}

/**
 * Decide which container the next rebuild should prefer as its anchor.
 *
 * Exactly one key toggled means a single chevron was clicked, so that container
 * is pinned. Several toggled means expand/collapse-all, where there is no one
 * container to pin. None toggled means the rebuild came from the topology
 * changing under a running workflow, which reuses the hint already in flight up
 * to {@link MAX_STICKY_ANCHOR_REBUILDS}. A context switch drops the hint
 * outright, since the container it names is not in the incoming layout.
 */
export function nextAnchorHint({
  previousKeys,
  currentKeys,
  currentHint,
  stickyRebuilds,
  contextSwitched,
}: AnchorHintInput): AnchorHintResult {
  if (contextSwitched) return { hint: null, stickyRebuilds: 0 };

  let toggledCount = 0;
  let lastToggled: string | null = null;
  for (const key of currentKeys) {
    if (!previousKeys.has(key)) {
      toggledCount++;
      lastToggled = key;
    }
  }
  for (const key of previousKeys) {
    if (!currentKeys.has(key)) {
      toggledCount++;
      lastToggled = key;
    }
  }

  if (toggledCount === 1) return { hint: lastToggled, stickyRebuilds: 0 };
  if (toggledCount > 1) return { hint: null, stickyRebuilds: 0 };
  if (currentHint === null) return { hint: null, stickyRebuilds: 0 };

  const used = stickyRebuilds + 1;
  return { hint: used > MAX_STICKY_ANCHOR_REBUILDS ? null : currentHint, stickyRebuilds: used };
}
