/**
 * Pure viewport-anchoring math for the workflow graph (issue #375).
 *
 * Without compensation, expanding an inline subworkflow makes the whole graph
 * appear to jump. The layout is not the problem: `layoutTopLevel` normalizes
 * each rebuild's bounding box to origin, so growing one container shifts
 * `minX`/`minY` and therefore *every* node's position — while the camera stays
 * exactly where it was. The world slides under a fixed viewport, which reads as
 * "the view reset".
 *
 * The fix is to compensate the camera rather than change the layout: find a
 * node present in both the old and new layouts, and pan by the negation of how
 * far that node moved. The anchor stays pinned on screen, everything else
 * visibly grows away from it on expand and closes back onto it on collapse.
 * Because dagre is deterministic, an expand followed by a collapse feeds it the
 * identical input graph and lands on the original view with no drift.
 *
 * Kept free of React and of any `@xyflow/react` runtime import so the anchor
 * selection, hint bookkeeping, and viewport arithmetic are unit-testable
 * without mounting a React Flow instance — the same shape as `lib/reconnect`.
 *
 * Known limitation: each toggle is compensated exactly, but a *sequence* of
 * toggles is not always invertible by a single bulk one. Expanding three
 * subworkflows individually pins three different containers, and each pin can
 * include that container's own move between the ordinary-node and
 * wide-container halves of the layout; collapsing all three at once pins one
 * node, and no node carries the summed delta needed to undo the three. That
 * path leaves a residual pan of a few tens of pixels, horizontal only, which
 * fit-view clears. Pinning one canonical node throughout would remove it, at
 * the cost of the property this exists for — that the container you clicked
 * stays under the cursor.
 */

/** Minimal structural view of a React Flow node; see `GraphNodeData`. */
export interface AnchorNode {
  id: string;
  position: { x: number; y: number };
  /** Set when the node is rendered inside a container; `position` is relative to it. */
  parentId?: string;
  data?: {
    /** Expansion key toggled by a subworkflow / iteration chevron. */
    childContextKey?: string;
    /** Expansion key toggled by a `for_each`-of-workflow group chevron. */
    groupExpansionKey?: string;
    [key: string]: unknown;
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
  /** Nodes as laid out before the rebuild. */
  prevNodes: readonly AnchorNode[];
  /** Nodes as laid out after the rebuild. */
  nextNodes: readonly AnchorNode[];
  /**
   * The single expansion key toggled since the last rebuild, if exactly one
   * changed. `null` when none changed (a topology update), or when several did
   * (expand-all) — there is no one container to pin in either case, so anchor
   * selection falls back to whatever is nearest the pane center.
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
 * container, and inline expansion *re-parents* nodes — an expanded `for_each`
 * group's iteration pills gain a `parentId` they did not have when collapsed.
 * Raw positions are therefore not comparable across a rebuild; absolute ones
 * are.
 *
 * A `parentId` pointing at a node that isn't present (or at a cycle) degrades to
 * treating the node as top-level rather than throwing: an anchor that is
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
      const parentId: string | undefined = current.parentId;
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
function ownsExpansionKey(node: AnchorNode, key: string): boolean {
  return node.data?.childContextKey === key || node.data?.groupExpansionKey === key;
}

function resolveAnchorFrom(
  { prevNodes, nextNodes, anchorKeyHint, viewport, paneSize }: AnchorInput,
  prevAbsolute: Map<string, XYPosition>,
): string | null {
  const prevById = new Map<string, AnchorNode>();
  for (const node of prevNodes) prevById.set(node.id, node);

  const shared = nextNodes.filter((node) => prevById.has(node.id));
  if (shared.length === 0) return null;

  if (anchorKeyHint !== null) {
    const hinted = shared.find(
      (node) =>
        ownsExpansionKey(node, anchorKeyHint) ||
        ownsExpansionKey(prevById.get(node.id)!, anchorKeyHint),
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
  let bestDistance = Infinity;

  for (const node of shared) {
    const pos = prevAbsolute.get(node.id);
    if (!pos) continue;
    const dx = pos.x - center.x;
    const dy = pos.y - center.y;
    const distance = dx * dx + dy * dy;
    if (distance < bestDistance || (distance === bestDistance && best !== null && node.id < best)) {
      best = node.id;
      bestDistance = distance;
    }
  }

  return best;
}

/**
 * Choose the node to pin across a rebuild, or `null` when the two layouts share
 * none (a first build or a context switch, where the caller's `fitView` paths
 * still own the camera).
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
export function resolveAnchorId(input: AnchorInput): string | null {
  return resolveAnchorFrom(input, toAbsolutePositions(input.prevNodes));
}

/**
 * The viewport that keeps the anchor node's top-left pinned on screen across a
 * rebuild, or `null` when no compensation should be applied (no shared anchor,
 * or the anchor did not move).
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
  const anchorId = resolveAnchorFrom(input, prevAbsolute);
  if (anchorId === null) return null;

  const before = prevAbsolute.get(anchorId);
  const after = toAbsolutePositions(input.nextNodes).get(anchorId);
  if (!before || !after) return null;

  const dx = after.x - before.x;
  const dy = after.y - before.y;
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
 * have scrolled far away from — as would a replay scrub, which rebuilds the
 * whole graph while deliberately leaving `expandedContexts` alone.
 */
export const MAX_STICKY_ANCHOR_REBUILDS = 2;

export interface AnchorHintInput {
  /** Expansion keys as of the previous rebuild. */
  previousKeys: ReadonlySet<string>;
  /** Expansion keys driving this rebuild. */
  currentKeys: ReadonlySet<string>;
  /** Hint carried in from the previous rebuild. */
  currentHint: string | null;
  /** Consecutive preceding rebuilds that toggled no key. */
  stickyRebuilds: number;
  /** Whether this rebuild is a switch to a different workflow context. */
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
