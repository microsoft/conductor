/**
 * Safety limit enforcer.
 * Mirrors src/conductor/engine/limits.py
 */
import type { LimitsConfig } from "../config/schema.js";
import { MaxIterationsError, TimeoutError } from "../exceptions.js";

export class LimitEnforcer {
  private readonly maxIterations: number;
  private readonly timeoutMs: number | undefined;
  private readonly startTime: number;

  constructor(limits: LimitsConfig = { max_iterations: 10 }) {
    this.maxIterations = limits.max_iterations;
    this.timeoutMs = limits.timeout_seconds ? limits.timeout_seconds * 1000 : undefined;
    this.startTime = Date.now();
  }

  checkIterations(current: number): void {
    if (current >= this.maxIterations) {
      throw new MaxIterationsError(
        `Workflow exceeded maximum iterations (${this.maxIterations})`,
        {
          suggestion: `Increase limits.max_iterations (current: ${this.maxIterations}) or add a '$end' route.`,
        },
      );
    }
  }

  checkTimeout(): void {
    if (!this.timeoutMs) return;
    const elapsed = Date.now() - this.startTime;
    if (elapsed >= this.timeoutMs) {
      throw new TimeoutError(
        `Workflow exceeded timeout of ${this.timeoutMs / 1000}s (elapsed: ${(elapsed / 1000).toFixed(1)}s)`,
      );
    }
  }

  check(iteration: number): void {
    this.checkIterations(iteration);
    this.checkTimeout();
  }
}
