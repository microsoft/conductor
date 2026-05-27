/**
 * Ports tests/test_engine/test_limits.py to TypeScript / vitest.
 *
 * TypeScript LimitEnforcer API differences:
 *  - Constructor takes LimitsConfig: { max_iterations, timeout_seconds? }
 *  - LimitEnforcer.check(iteration) — throws MaxIterationsError or TimeoutError
 *  - No separate record_execution() / start() / check_iteration() methods
 */
import { describe, expect, it } from "vitest";
import { LimitsConfigSchema } from "../../config/schema.js";
import { LimitEnforcer } from "../../engine/limits.js";
import { MaxIterationsError, TimeoutError } from "../../exceptions.js";

function makeLimits(maxIter: number, timeoutSec?: number) {
  return LimitsConfigSchema.parse({
    max_iterations: maxIter,
    timeout_seconds: timeoutSec,
  });
}

describe("LimitEnforcer defaults", () => {
  it("uses default max_iterations of 10", () => {
    const enforcer = new LimitEnforcer();
    // Should not throw for iterations 0–9
    for (let i = 0; i < 10; i++) {
      expect(() => enforcer.check(i)).not.toThrow();
    }
  });

  it("throws MaxIterationsError at exactly max_iterations", () => {
    const enforcer = new LimitEnforcer();
    expect(() => enforcer.check(10)).toThrow(MaxIterationsError);
  });
});

describe("LimitEnforcer.checkIterations", () => {
  it("does not throw when under the limit", () => {
    const enforcer = new LimitEnforcer(makeLimits(5));
    expect(() => enforcer.check(0)).not.toThrow();
    expect(() => enforcer.check(4)).not.toThrow();
  });

  it("throws MaxIterationsError at the limit", () => {
    const enforcer = new LimitEnforcer(makeLimits(3));
    expect(() => enforcer.check(3)).toThrow(MaxIterationsError);
  });

  it("MaxIterationsError message includes the limit", () => {
    const enforcer = new LimitEnforcer(makeLimits(2));
    try {
      enforcer.check(2);
    } catch (err) {
      expect(err).toBeInstanceOf(MaxIterationsError);
      if (err instanceof MaxIterationsError) {
        expect(err.message).toContain("2");
      }
    }
  });

  it("MaxIterationsError includes suggestion", () => {
    const enforcer = new LimitEnforcer(makeLimits(1));
    try {
      enforcer.check(1);
    } catch (err) {
      expect(err).toBeInstanceOf(MaxIterationsError);
      if (err instanceof MaxIterationsError) {
        expect(err.suggestion).toBeDefined();
        expect(err.suggestion).toContain("max_iterations");
      }
    }
  });

  it("throws at exact boundary (max_iterations=1)", () => {
    const enforcer = new LimitEnforcer(makeLimits(1));
    expect(() => enforcer.check(0)).not.toThrow();
    expect(() => enforcer.check(1)).toThrow(MaxIterationsError);
  });
});

describe("LimitEnforcer.checkTimeout", () => {
  it("does not throw immediately after construction", () => {
    const enforcer = new LimitEnforcer(makeLimits(10, 60));
    expect(() => enforcer.checkTimeout()).not.toThrow();
  });

  it("does not throw when no timeout_seconds configured", () => {
    const enforcer = new LimitEnforcer(makeLimits(10));
    expect(() => enforcer.checkTimeout()).not.toThrow();
  });
});

describe("LimitEnforcer.check (combined)", () => {
  it("iterates without throwing below limit", () => {
    const enforcer = new LimitEnforcer(makeLimits(5));
    for (let i = 0; i < 5; i++) {
      expect(() => enforcer.check(i)).not.toThrow();
    }
  });

  it("throws MaxIterationsError when iteration reaches limit", () => {
    const enforcer = new LimitEnforcer(makeLimits(3));
    expect(() => enforcer.check(3)).toThrow(MaxIterationsError);
  });
});
