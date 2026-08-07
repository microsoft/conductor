/**
 * Tracks whether an animated camera move owns the viewport right now.
 *
 * The graph rebuild effect compensates the camera the instant a new layout is
 * built (see `lib/graph-anchor`). An animated `fitView` runs as a d3-zoom
 * transition, and d3's non-transition `zoom.transform` path — which an instant
 * `setViewport` takes — calls `selection.interrupt()`. A rebuild landing inside
 * an animation window would therefore cancel the fit at whatever frame it had
 * reached and strand the camera part-way, and the fit's promise would never
 * settle: `interrupt` dispatches `'interrupt'`, not the `'end'` event the
 * promise resolves on.
 *
 * Deep-link centering is the case that matters — it goes to considerable
 * lengths to land on a requested node, and the workflows most likely to be
 * deep-linked into are running ones, whose topology churn fires exactly the
 * extra rebuilds that would interrupt it. So an animated fit takes precedence:
 * it reframes the whole graph anyway, which makes any pending compensation moot.
 *
 * Module-scoped because there is one dashboard, and one camera, per page.
 * `now` is injectable so the logic is testable without timers.
 */

/** Timestamp (ms) until which an animated camera move owns the viewport. */
let ownedUntil = 0;

/**
 * Declare that an animated camera move is starting. Call immediately before an
 * animated `fitView`, passing the same duration (plus any delay before it).
 */
export function claimCameraForAnimation(durationMs: number, now: number = Date.now()): void {
  ownedUntil = Math.max(ownedUntil, now + durationMs);
}

/** True while an animated camera move claimed via {@link claimCameraForAnimation} is still running. */
export function isCameraAnimating(now: number = Date.now()): boolean {
  return now < ownedUntil;
}

/** Drop any outstanding claim. Exists so tests start from a known state. */
export function releaseCamera(): void {
  ownedUntil = 0;
}
