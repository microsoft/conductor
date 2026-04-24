/** Designer-specific TypeScript types.
 *
 * The store's source of truth is a WorkflowConfig-shaped document.
 * ReactFlow nodes/edges are *derived* from this document — they are
 * never the canonical model.
 */

import type { Node, Edge } from '@xyflow/react';

// ── Workflow domain types (mirrors Pydantic schema) ────────────────

export interface OutputField {
  type: 'string' | 'number' | 'boolean' | 'array' | 'object';
  description?: string;
  items?: OutputField;
  properties?: Record<string, OutputField>;
}

export interface RouteDef {
  to: string;
  when?: string;
  output?: Record<string, string>;
}

export interface GateOption {
  label: string;
  route: string;
  description?: string;
}

export interface RetryPolicy {
  max_attempts: number;
  backoff: 'fixed' | 'exponential';
  delay_seconds: number;
  retry_on: ('provider_error' | 'timeout')[];
}

export interface AgentDef {
  name: string;
  description?: string;
  type?: 'agent' | 'human_gate' | 'script' | 'workflow' | null;
  provider?: 'copilot' | 'claude' | null;
  model?: string;
  input?: string[];
  tools?: string[] | null;
  system_prompt?: string;
  prompt?: string;
  output?: Record<string, OutputField>;
  routes?: RouteDef[];
  options?: GateOption[];
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  working_dir?: string;
  timeout?: number;
  workflow?: string;
  input_mapping?: Record<string, string>;
  max_depth?: number;
  max_session_seconds?: number;
  max_agent_iterations?: number;
  retry?: RetryPolicy;
}

export interface ParallelGroup {
  name: string;
  description?: string;
  agents: string[];
  failure_mode: 'fail_fast' | 'continue_on_error' | 'all_or_nothing';
  routes?: RouteDef[];
}

export interface ForEachDef {
  name: string;
  description?: string;
  type: 'for_each';
  source: string;
  as: string;
  agent: AgentDef;
  max_concurrent: number;
  failure_mode: 'fail_fast' | 'continue_on_error' | 'all_or_nothing';
  key_by?: string;
  routes?: RouteDef[];
}

export interface RuntimeConfig {
  provider: 'copilot' | 'openai-agents' | 'claude';
  default_model?: string;
  temperature?: number;
  max_tokens?: number;
  timeout?: number;
  max_session_seconds?: number;
  max_agent_iterations?: number;
}

export interface LimitsConfig {
  max_iterations?: number;
  timeout_seconds?: number;
}

export interface WorkflowDef {
  name: string;
  description?: string;
  version?: string;
  entry_point: string;
  runtime?: RuntimeConfig;
  input?: Record<string, unknown>;
  limits?: LimitsConfig;
  metadata?: Record<string, unknown>;
}

export interface WorkflowConfig {
  workflow: WorkflowDef;
  tools?: string[];
  agents: AgentDef[];
  parallel?: ParallelGroup[];
  for_each?: ForEachDef[];
  output?: Record<string, string>;
}

// ── Designer UI types ──────────────────────────────────────────────

export type DesignerNodeType =
  | 'agent'
  | 'human_gate'
  | 'script'
  | 'workflow'
  | 'parallel'
  | 'for_each'
  | 'start'
  | 'end';

/** Extra data stored on each ReactFlow node. */
export interface DesignerNodeData extends Record<string, unknown> {
  label: string;
  nodeType: DesignerNodeType;
  /** The name of the agent/group this node represents. */
  entityName: string;
}

export type DesignerNode = Node<DesignerNodeData>;
export type DesignerEdge = Edge;

/** Validation result from the backend. */
export interface ValidationResult {
  errors: string[];
  warnings: string[];
}
