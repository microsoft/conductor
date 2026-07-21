/**
 * Context-namespaced graph node/edge identity.
 *
 * The dashboard used to render one workflow context at a time, so graph node
 * IDs could be bare agent names (unique within a single context). Inline
 * subworkflow expansion renders multiple contexts at once, where bare names
 * collide — every context has a `$start`/`$end`, and repeated subworkflow
 * iterations share identical inner agent names.
 *
 * To disambiguate, every rendered node/edge ID is namespaced by the context's
 * absolute numeric index path from the root workflow:
 *
 *   contextKey([])      -> ""        (root)
 *   contextKey([0])     -> "0"       (first subworkflow)
 *   contextKey([0, 2])  -> "0.2"     (its third child)
 *
 *   nodeKey([0, 2], "reviewer") -> "0.2::reviewer"
 *
 * Context keys contain only digits and dots, so the first `::` unambiguously
 * separates the context key from the (bare) node name — even for reserved
 * names like `$start` or synthetic `$end`.
 */

/** Canonical string key for a context's absolute numeric index path from root. */
export function contextKey(indexPath: number[]): string {
  return indexPath.join('.');
}

/** Namespaced graph node/edge id: `${contextKey}::${name}`. Root key = "". */
export function nodeKey(indexPath: number[], name: string): string {
  return `${contextKey(indexPath)}::${name}`;
}

/**
 * Parse a namespaced id back into its context index path + bare name.
 *
 * Tolerant of legacy/un-namespaced ids (no `::`): those resolve to the root
 * context so existing call sites keep working during the migration.
 */
export function parseNodeKey(id: string): { contextPath: number[]; name: string } {
  const sep = id.indexOf('::');
  if (sep === -1) {
    return { contextPath: [], name: id };
  }
  const keyPart = id.slice(0, sep);
  const name = id.slice(sep + 2);
  const contextPath = keyPart === '' ? [] : keyPart.split('.').map((s) => Number(s));
  return { contextPath, name };
}

/**
 * Expansion key for an inline-expanded `for_each`-of-workflow group container.
 *
 * A `for_each` group has *N* iteration child contexts rather than a single one,
 * so it can't be keyed by a child context path the way a sequential
 * subworkflow step is. Instead the group container is keyed by its own graph
 * node id (`${contextKey}::${groupName}`). Because this contains `::` it never
 * collides with the pure digit/dot context keys that gate individual iteration
 * inner-DAG expansion — both kinds live in the same `expandedContexts` set.
 */
export function forEachGroupKey(basePath: number[], groupName: string): string {
  return nodeKey(basePath, groupName);
}

/** True for a {@link forEachGroupKey}-style expansion key (vs. a context key). */
export function isGroupExpansionKey(key: string): boolean {
  return key.includes('::');
}

/**
 * Parse a `for_each` iteration slot key (`"${group}[${itemKey}]"`) into its
 * group name and item key. Returns `null` for anything that isn't bracketed,
 * i.e. a sequential subworkflow slot key (which equals the bare agent name).
 */
export function parseForEachSlotKey(slotKey: string): { group: string; key: string } | null {
  const open = slotKey.indexOf('[');
  if (open <= 0 || !slotKey.endsWith(']')) return null;
  return { group: slotKey.slice(0, open), key: slotKey.slice(open + 1, -1) };
}
