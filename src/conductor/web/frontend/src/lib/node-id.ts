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
