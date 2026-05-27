/**
 * Provider base interface and AgentOutput type.
 * Mirrors src/conductor/providers/base.py
 */
import type { AgentDef } from "../config/schema.js";
import type { WorkflowEventEmitter } from "../events.js";

export interface AgentOutput {
  content: string;
  model: string;
  inputTokens?: number;
  outputTokens?: number;
  reasoningContent?: string;
  toolCalls?: ToolCallRecord[];
}

export interface ToolCallRecord {
  name: string;
  arguments: unknown;
  result?: unknown;
}

export interface ExecuteOptions {
  /** Absolute paths for skill directories (SKILL.md files). */
  skillDirectories?: string[];
  /** Callback invoked when the agent needs user input. */
  onUserInputRequest?: (request: UserInputRequest) => Promise<UserInputResponse>;
  /** Max iterations for the agentic loop. */
  maxIterations?: number;
  /** Event emitter to fire progress events. */
  emitter?: WorkflowEventEmitter;
  /** AbortSignal for cancellation. */
  signal?: AbortSignal;
}

export interface UserInputRequest {
  question: string;
  choices?: string[];
  allowFreeform?: boolean;
}

export interface UserInputResponse {
  answer: string;
  wasFreeform: boolean;
}

export interface AgentProvider {
  execute(agent: AgentDef, prompt: string, opts?: ExecuteOptions): Promise<AgentOutput>;
  validateConnection(): Promise<void>;
  close(): Promise<void>;
}
