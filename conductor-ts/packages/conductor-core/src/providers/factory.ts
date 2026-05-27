/**
 * Provider factory.
 * Mirrors src/conductor/providers/factory.py
 */
import { CopilotCliProvider, type CopilotCliProviderOptions } from "./copilot-cli.js";
import { ClaudeProvider, type ClaudeProviderOptions } from "./claude.js";
import type { AgentProvider } from "./base.js";
import { ConfigurationError } from "../exceptions.js";

export type ProviderName = "copilot" | "claude";

export interface ProviderFactoryOptions {
  copilot?: CopilotCliProviderOptions;
  claude?: ClaudeProviderOptions;
}

export function createProvider(name: ProviderName, opts: ProviderFactoryOptions = {}): AgentProvider {
  switch (name) {
    case "copilot":
      return new CopilotCliProvider(opts.copilot ?? {});
    case "claude":
      return new ClaudeProvider(opts.claude ?? {});
    default:
      throw new ConfigurationError(
        `Unknown provider '${name as string}'. Valid providers: copilot, claude`,
      );
  }
}
