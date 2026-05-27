/**
 * Cross-reference validation for workflow configuration.
 * Mirrors src/conductor/config/validator.py
 */
import type { WorkflowConfig } from "./schema.js";
import { ConfigurationError } from "../exceptions.js";

export function validateConfig(config: WorkflowConfig, filePath?: string): void {
  const opts = filePath ? { filePath } : {};

  // Collect all defined names
  const agentNames = new Set(config.agents.map((a) => a.name));
  const parallelNames = new Set(config.parallel.map((p) => p.name));
  const forEachNames = new Set(config.for_each.map((f) => f.name));
  const allNames = new Set([...agentNames, ...parallelNames, ...forEachNames]);
  const validTargets = new Set([...allNames, "$end"]);

  // Check for duplicate names
  const allNamesList = [
    ...config.agents.map((a) => a.name),
    ...config.parallel.map((p) => p.name),
    ...config.for_each.map((f) => f.name),
  ];
  const seen = new Set<string>();
  for (const name of allNamesList) {
    if (seen.has(name)) {
      throw new ConfigurationError(
        `Duplicate name '${name}' — each agent/parallel/for_each must have a unique name`,
        opts,
      );
    }
    seen.add(name);
  }

  // Validate entry_point
  if (!allNames.has(config.workflow.entry_point)) {
    throw new ConfigurationError(
      `entry_point '${config.workflow.entry_point}' is not defined`,
      opts,
    );
  }

  // Validate agent routes
  for (const agent of config.agents) {
    for (const route of agent.routes) {
      if (!validTargets.has(route.to)) {
        throw new ConfigurationError(
          `Agent '${agent.name}' has route to unknown target '${route.to}'`,
          { ...opts, suggestion: `Valid targets: ${[...validTargets].join(", ")}` },
        );
      }
    }
    // Validate parallel group agent references
    for (const parallel of config.parallel) {
      for (const agentRef of parallel.agents) {
        if (!agentNames.has(agentRef)) {
          throw new ConfigurationError(
            `Parallel group '${parallel.name}' references unknown agent '${agentRef}'`,
            opts,
          );
        }
      }
      for (const route of parallel.routes) {
        if (!validTargets.has(route.to)) {
          throw new ConfigurationError(
            `Parallel group '${parallel.name}' has route to unknown target '${route.to}'`,
            opts,
          );
        }
      }
    }
  }

  // Validate for-each routes
  for (const fe of config.for_each) {
    for (const route of fe.routes) {
      if (!validTargets.has(route.to)) {
        throw new ConfigurationError(
          `For-each '${fe.name}' has route to unknown target '${route.to}'`,
          opts,
        );
      }
    }
  }
}
