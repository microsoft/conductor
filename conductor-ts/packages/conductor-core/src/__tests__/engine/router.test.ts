/**
 * Ports tests/test_engine/test_router.py to TypeScript / vitest.
 *
 * Key TypeScript differences:
 *   - RouteResult uses camelCase: outputTransform, matchedRule
 *   - RouteDef uses { to, when?, output? }
 */
import { describe, expect, it } from "vitest";
import type { RouteDef } from "../../config/schema.js";
import { RouteError } from "../../exceptions.js";
import { Router } from "../../engine/router.js";

describe("RouteResult shape", () => {
  it("unconditional route populates target and matchedRule", () => {
    const router = new Router();
    const result = router.evaluate([{ to: "$end" }], {}, {});
    expect(result.target).toBe("$end");
    expect(result.matchedRule).toBeDefined();
    expect(result.matchedRule?.to).toBe("$end");
    expect(result.outputTransform).toBeUndefined();
  });
});

describe("Router - unconditional routes", () => {
  it("route without when always matches", () => {
    const router = new Router();
    const routes: RouteDef[] = [{ to: "next_agent" }];
    const result = router.evaluate(routes, { value: 1 }, {});
    expect(result.target).toBe("next_agent");
  });

  it("unconditional route to $end", () => {
    const router = new Router();
    const result = router.evaluate([{ to: "$end" }], {}, {});
    expect(result.target).toBe("$end");
  });

  it("first unconditional route wins", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "first" },
      { to: "second" },
      { to: "third" },
    ];
    const result = router.evaluate(routes, {}, {});
    expect(result.target).toBe("first");
  });
});

describe("Router - Jinja2/nunjucks conditions", () => {
  it("Jinja2 condition true routes to first", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "approved_handler", when: "{{ output.approved }}" },
      { to: "fallback" },
    ];
    const result = router.evaluate(routes, { approved: true }, {});
    expect(result.target).toBe("approved_handler");
  });

  it("Jinja2 condition false falls through to next route", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "approved_handler", when: "{{ output.approved }}" },
      { to: "fallback" },
    ];
    const result = router.evaluate(routes, { approved: false }, {});
    expect(result.target).toBe("fallback");
  });

  it("Jinja2 with nested context access", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "handler", when: "{{ output.result.success }}" },
      { to: "$end" },
    ];
    const result = router.evaluate(routes, { result: { success: true } }, {});
    expect(result.target).toBe("handler");
  });

  it("Jinja2 comparison >= routes correctly", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "high", when: "{{ output.score >= 8 }}" },
      { to: "low" },
    ];
    expect(router.evaluate(routes, { score: 9 }, {}).target).toBe("high");
    expect(router.evaluate(routes, { score: 7 }, {}).target).toBe("low");
  });

  it("Jinja2 string comparison", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "success", when: "{{ output.status == 'ok' }}" },
      { to: "error" },
    ];
    expect(router.evaluate(routes, { status: "ok" }, {}).target).toBe("success");
    expect(router.evaluate(routes, { status: "fail" }, {}).target).toBe("error");
  });
});

describe("Router - arithmetic expressions (expr-eval)", () => {
  it("arithmetic greater than", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "high", when: "score > 7" },
      { to: "low" },
    ];
    expect(router.evaluate(routes, { score: 8 }, {}).target).toBe("high");
    expect(router.evaluate(routes, { score: 6 }, {}).target).toBe("low");
  });

  it("arithmetic less than", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "continue", when: "iteration < 5" },
      { to: "$end" },
    ];
    expect(router.evaluate(routes, { iteration: 3 }, {}).target).toBe("continue");
    expect(router.evaluate(routes, { iteration: 5 }, {}).target).toBe("$end");
  });

  it("arithmetic equals", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "exact", when: "count == 10" },
      { to: "other" },
    ];
    expect(router.evaluate(routes, { count: 10 }, {}).target).toBe("exact");
    expect(router.evaluate(routes, { count: 9 }, {}).target).toBe("other");
  });
});

describe("Router - no matching route", () => {
  it("throws RouteError when no route matches", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "a", when: "{{ output.x == 'y' }}" },
      { to: "b", when: "{{ output.x == 'z' }}" },
    ];
    expect(() => router.evaluate(routes, { x: "other" }, {})).toThrow(RouteError);
  });
});

describe("Router - output transform", () => {
  it("matched route includes outputTransform when defined", () => {
    const router = new Router();
    const routes: RouteDef[] = [
      { to: "next", output: { summary: "Done" } },
    ];
    const result = router.evaluate(routes, {}, {});
    expect(result.outputTransform).toEqual({ summary: "Done" });
  });

  it("outputTransform is undefined when route has no output", () => {
    const router = new Router();
    const result = router.evaluate([{ to: "$end" }], {}, {});
    expect(result.outputTransform).toBeUndefined();
  });
});
