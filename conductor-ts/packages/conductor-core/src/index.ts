/**
 * Public API surface for @conductor/core
 */

// Config
export { loadConfig, loadConfigFromString, resolveEnvVars } from "./config/loader.js";
export { validateConfig } from "./config/validator.js";
export * from "./config/schema.js";

// Engine
export { WorkflowEngine } from "./engine/workflow.js";
export { WorkflowContext } from "./engine/context.js";
export { Router } from "./engine/router.js";
export { LimitEnforcer } from "./engine/limits.js";
export { CheckpointManager } from "./engine/checkpoint.js";
export type { WorkflowResult, WorkflowEngineOptions } from "./engine/workflow.js";
export type { RouteResult } from "./engine/router.js";
export type { CheckpointData } from "./engine/checkpoint.js";

// Executors
export { TemplateRenderer } from "./executor/template.js";
export { parseOutput, extractJson } from "./executor/output.js";
export { executeScript } from "./executor/script.js";
export type { ScriptOutput } from "./executor/script.js";

// Providers
export { CopilotCliProvider } from "./providers/copilot-cli.js";
export { ClaudeProvider } from "./providers/claude.js";
export { createProvider } from "./providers/factory.js";
export type { AgentProvider, AgentOutput, ExecuteOptions, UserInputRequest, UserInputResponse } from "./providers/base.js";
export type { CopilotCliProviderOptions } from "./providers/copilot-cli.js";
export type { ClaudeProviderOptions } from "./providers/claude.js";
export type { ProviderName } from "./providers/factory.js";

// Events
export { WorkflowEventEmitter } from "./events.js";
export type { WorkflowEvent, WorkflowEventType, EventHandler } from "./events.js";

// Exceptions
export {
  ConductorError,
  ConfigurationError,
  ValidationError,
  ExecutionError,
  ProviderError,
  TemplateError,
  RouteError,
  CheckpointError,
  MaxIterationsError,
  TimeoutError,
  AgentTimeoutError,
  InterruptError,
} from "./exceptions.js";
