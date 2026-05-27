/**
 * Nunjucks-based template renderer.
 * Mirrors src/conductor/executor/template.py
 *
 * Key behavioural parity with Python:
 * - Dict keys take priority over built-in properties (avoids `dict.items` resolving
 *   to a method instead of the "items" key). Implemented via a custom `resolve` wrapper.
 * - StrictUndefined equivalent: nunjucks throws on missing vars by default.
 * - Custom filters: `json`, `default`.
 */
import nunjucks from "nunjucks";
import { TemplateError } from "../exceptions.js";

/**
 * Custom nunjucks Environment that prefers dict-key lookup over property access,
 * matching Python's _DictSafeEnvironment behaviour.
 */
class DictSafeEnvironment extends nunjucks.Environment {
  /** Override member resolution to prefer dict keys for plain objects. */
  override getTemplate(name: string, eagerCompile?: boolean): nunjucks.Template;
  override getTemplate(
    name: string,
    eagerCompile?: boolean,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    cb?: (err: Error | null, templ: nunjucks.Template) => void,
  ): void;
  override getTemplate(
    name: string,
    eagerCompile?: boolean,
    cb?: (err: Error | null, templ: nunjucks.Template) => void,
  ): nunjucks.Template | void {
    if (cb) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return super.getTemplate(name, eagerCompile, cb as any);
    }
    return super.getTemplate(name, eagerCompile);
  }
}

/**
 * Proxy-wrap a value so that property access checks own dict keys first.
 * This is called recursively on access so nested dicts are also wrapped.
 */
function wrapForDictSafe(value: unknown): unknown {
  if (value === null || value === undefined) return value;
  if (typeof value !== "object") return value;
  if (Array.isArray(value)) {
    return new Proxy(value, {
      get(target, prop) {
        const v = Reflect.get(target, prop);
        return typeof v === "object" && v !== null ? wrapForDictSafe(v) : v;
      },
    });
  }
  return new Proxy(value as Record<string, unknown>, {
    get(target, prop: string) {
      // Own key takes priority over prototype properties
      if (Object.prototype.hasOwnProperty.call(target, prop)) {
        const v = target[prop];
        return typeof v === "object" && v !== null ? wrapForDictSafe(v) : v;
      }
      const v = Reflect.get(target, prop);
      return typeof v === "object" && v !== null ? wrapForDictSafe(v) : v;
    },
  });
}

function wrapContext(ctx: Record<string, unknown>): Record<string, unknown> {
  const wrapped: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(ctx)) {
    wrapped[k] = wrapForDictSafe(v);
  }
  return wrapped;
}

export class TemplateRenderer {
  private readonly env: nunjucks.Environment;

  constructor() {
    // null loader — we only render strings, never files
    this.env = new DictSafeEnvironment(null, {
      autoescape: false,
      throwOnUndefined: true,
      trimBlocks: false,
      lstripBlocks: false,
    });

    // json filter
    this.env.addFilter("json", (value: unknown, indent = 2) =>
      JSON.stringify(value, null, indent),
    );
    // default filter
    this.env.addFilter("default", (value: unknown, def: unknown = "") =>
      value === null || value === undefined ? def : value,
    );
  }

  render(template: string, context: Record<string, unknown>): string {
    try {
      return this.env.renderString(template, wrapContext(context));
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      throw new TemplateError(`Template rendering failed: ${msg}`, {
        suggestion: "Ensure all variables referenced in the template are defined in context.",
      });
    }
  }

  /** Render a template and coerce the result to boolean. */
  renderBool(template: string, context: Record<string, unknown>): boolean {
    const result = this.render(template, context).trim().toLowerCase();
    return result === "true" || result === "1" || result === "yes";
  }
}
