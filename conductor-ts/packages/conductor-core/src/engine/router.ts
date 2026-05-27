/**
 * Route evaluation for workflow conditional branching.
 * Mirrors src/conductor/engine/router.py
 *
 * Supports:
 * - Nunjucks template conditions: {{ output.approved }}
 * - Arithmetic via expr-eval: score > 7, iteration < 5
 */
import { Parser } from "expr-eval";
import type { RouteDef } from "../config/schema.js";
import { TemplateRenderer } from "../executor/template.js";
import { RouteError } from "../exceptions.js";

export interface RouteResult {
  target: string;
  outputTransform: Record<string, string> | undefined;
  matchedRule: RouteDef | undefined;
}

const exprParser = new Parser();

export class Router {
  private readonly renderer = new TemplateRenderer();

  evaluate(
    routes: RouteDef[],
    currentOutput: Record<string, unknown>,
    context: Record<string, unknown>,
  ): RouteResult {
    const evalCtx: Record<string, unknown> = { ...context, output: currentOutput };

    for (const route of routes) {
      if (route.when === undefined) {
        return {
          target: route.to,
          outputTransform: route.output,
          matchedRule: route,
        };
      }
      if (this.evaluateCondition(route.when, evalCtx)) {
        return {
          target: route.to,
          outputTransform: route.output,
          matchedRule: route,
        };
      }
    }

    throw new RouteError(
      "No matching route found. Ensure at least one route has no 'when' clause.",
    );
  }

  private evaluateCondition(when: string, context: Record<string, unknown>): boolean {
    const trimmed = when.trim();

    // Jinja2-style: {{ expr }}
    if (trimmed.startsWith("{{") && trimmed.endsWith("}}")) {
      try {
        const rendered = this.renderer.render(trimmed, context).trim().toLowerCase();
        return rendered === "true" || rendered === "1" || rendered === "yes";
      } catch {
        return false;
      }
    }

    // Arithmetic expression via expr-eval
    try {
      const flatCtx = flattenForEval(context);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const result = exprParser.evaluate(trimmed, flatCtx as any);
      return Boolean(result);
    } catch {
      // Fall back to nunjucks rendering
      try {
        const rendered = this.renderer.render(`{{ ${trimmed} }}`, context).trim().toLowerCase();
        return rendered === "true" || rendered === "1" || rendered === "yes";
      } catch {
        return false;
      }
    }
  }
}

/**
 * Flatten nested context for expr-eval (which works on flat variable names).
 * e.g. { output: { score: 7 } } → { "output.score": 7, "score": 7 }
 */
function flattenForEval(
  obj: Record<string, unknown>,
  prefix = "",
  acc: Record<string, unknown> = {},
): Record<string, unknown> {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    acc[key] = v;
    acc[k] = v; // also available as shorthand
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      flattenForEval(v as Record<string, unknown>, key, acc);
    }
  }
  return acc;
}
