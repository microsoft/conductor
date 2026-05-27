/**
 * Zod schemas for workflow YAML configuration.
 * Mirrors src/conductor/config/schema.py (Pydantic → Zod).
 */
import { z } from "zod";

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

export const ReasoningEffortSchema = z.enum(["low", "medium", "high", "xhigh"]);
export type ReasoningEffort = z.infer<typeof ReasoningEffortSchema>;

export const InputDefSchema = z.object({
  type: z.enum(["string", "number", "boolean", "array", "object"]),
  required: z.boolean().default(true),
  default: z.unknown().optional(),
  description: z.string().optional(),
});
export type InputDef = z.infer<typeof InputDefSchema>;

export const OutputFieldSchema: z.ZodType<OutputField> = z.lazy(() =>
  z.object({
    type: z.enum(["string", "number", "boolean", "array", "object"]),
    description: z.string().optional(),
    items: OutputFieldSchema.optional(),
    properties: z.record(OutputFieldSchema).optional(),
  }),
);
export interface OutputField {
  type: "string" | "number" | "boolean" | "array" | "object";
  description?: string;
  items?: OutputField;
  properties?: Record<string, OutputField>;
}

export const RouteDefSchema = z.object({
  to: z.string().min(1),
  when: z.string().optional(),
  output: z.record(z.string()).optional(),
});
export type RouteDef = z.infer<typeof RouteDefSchema>;

// ---------------------------------------------------------------------------
// Parallel / for-each / gate
// ---------------------------------------------------------------------------

export const ParallelGroupSchema = z.object({
  name: z.string(),
  description: z.string().optional(),
  agents: z.array(z.string()).min(2),
  failure_mode: z
    .enum(["fail_fast", "continue_on_error", "all_or_nothing"])
    .default("fail_fast"),
  routes: z.array(RouteDefSchema).default([]),
});
export type ParallelGroup = z.infer<typeof ParallelGroupSchema>;

export const ForEachDefSchema = z.object({
  name: z.string(),
  description: z.string().optional(),
  type: z.literal("for_each"),
  source: z.string(),
  as: z.string(),
  agent: z.lazy(() => AgentDefSchema),
  max_concurrent: z.number().int().min(1).max(100).default(10),
  failure_mode: z
    .enum(["fail_fast", "continue_on_error", "all_or_nothing"])
    .default("fail_fast"),
  key_by: z.string().optional(),
  routes: z.array(RouteDefSchema).default([]),
});
export type ForEachDef = z.infer<typeof ForEachDefSchema>;

export const GateOptionSchema = z.object({
  label: z.string(),
  value: z.string(),
  route: z.string(),
  prompt_for: z.string().optional(),
});
export type GateOption = z.infer<typeof GateOptionSchema>;

// ---------------------------------------------------------------------------
// Context / limits / cost / retry / reasoning
// ---------------------------------------------------------------------------

export const ContextConfigSchema = z.object({
  mode: z.enum(["accumulate", "last_only", "explicit"]).default("accumulate"),
  max_tokens: z.number().int().positive().optional(),
  trim_strategy: z.enum(["summarize", "truncate", "drop_oldest"]).optional(),
});
export type ContextConfig = z.infer<typeof ContextConfigSchema>;

export const LimitsConfigSchema = z.object({
  max_iterations: z.number().int().min(1).max(500).default(10),
  timeout_seconds: z.number().int().positive().optional(),
});
export type LimitsConfig = z.infer<typeof LimitsConfigSchema>;

export const RetryPolicySchema = z.object({
  max_attempts: z.number().int().min(1).max(10).default(1),
  backoff: z.enum(["fixed", "exponential"]).default("exponential"),
  delay_seconds: z.number().min(0).max(300).default(2.0),
  retry_on: z
    .array(z.enum(["provider_error", "timeout"]))
    .default(["provider_error", "timeout"]),
});
export type RetryPolicy = z.infer<typeof RetryPolicySchema>;

export const ReasoningConfigSchema = z.object({
  effort: ReasoningEffortSchema,
});
export type ReasoningConfig = z.infer<typeof ReasoningConfigSchema>;

export const DialogConfigSchema = z.object({
  trigger_prompt: z.string(),
});
export type DialogConfig = z.infer<typeof DialogConfigSchema>;

// ---------------------------------------------------------------------------
// AgentDef
// ---------------------------------------------------------------------------

export const AgentDefSchema = z.object({
  name: z.string(),
  description: z.string().optional(),
  type: z
    .enum(["agent", "human_gate", "script", "workflow"])
    .optional(),
  provider: z.enum(["copilot", "claude"]).optional(),
  model: z.string().optional(),
  input: z.array(z.string()).default([]),
  tools: z.array(z.string()).nullable().optional(),
  system_prompt: z.string().optional(),
  prompt: z.string().default(""),
  output: z.record(OutputFieldSchema).optional(),
  routes: z.array(RouteDefSchema).default([]),
  options: z.array(GateOptionSchema).optional(),
  // Script fields
  command: z.string().optional(),
  args: z.array(z.string()).default([]),
  env: z.record(z.string()).default({}),
  working_dir: z.string().optional(),
  timeout: z.number().int().positive().optional(),
  // Sub-workflow fields
  workflow: z.string().optional(),
  input_mapping: z.record(z.string()).optional(),
  max_depth: z.number().int().min(1).max(10).optional(),
  // Per-agent overrides
  timeout_seconds: z.number().min(1).optional(),
  max_session_seconds: z.number().min(1).optional(),
  max_agent_iterations: z.number().int().min(1).max(500).optional(),
  retry: RetryPolicySchema.optional(),
  dialog: DialogConfigSchema.optional(),
  interactive_input: z.boolean().default(false),
  reasoning: ReasoningConfigSchema.optional(),
  skill_directories: z.array(z.string()).optional(),
});
export type AgentDef = z.infer<typeof AgentDefSchema>;

// ---------------------------------------------------------------------------
// Runtime / workflow top-level
// ---------------------------------------------------------------------------

export const RuntimeConfigSchema = z.object({
  provider: z.enum(["copilot", "claude"]).default("copilot"),
  model: z.string().optional(),
  max_agent_iterations: z.number().int().min(1).max(500).optional(),
  max_session_seconds: z.number().min(1).optional(),
  default_reasoning_effort: ReasoningEffortSchema.optional(),
  skill_directories: z.array(z.string()).optional(),
  temperature: z.number().min(0).max(2).optional(),
  mcp_servers: z.record(z.object({
    command: z.string(),
    args: z.array(z.string()).default([]),
    env: z.record(z.string()).default({}),
    tools: z.array(z.string()).optional(),
  })).optional(),
});
export type RuntimeConfig = z.infer<typeof RuntimeConfigSchema>;

/**
 * Mirrors Python WorkflowDef — the nested `workflow:` block in the YAML.
 */
export const WorkflowDefSchema = z.object({
  name: z.string(),
  description: z.string().optional(),
  version: z.string().optional(),
  entry_point: z.string(),
  runtime: RuntimeConfigSchema.default({}),
  input: z.record(InputDefSchema).default({}),
  context: ContextConfigSchema.optional(),
  limits: LimitsConfigSchema.optional(),
  metadata: z.record(z.unknown()).default({}),
  instructions: z.array(z.string()).default([]),
});
export type WorkflowDef = z.infer<typeof WorkflowDefSchema>;

/**
 * Mirrors Python WorkflowConfig — the complete YAML file.
 * Structure: { workflow: WorkflowDef, tools, agents, parallel, for_each, output }
 */
export const WorkflowConfigSchema = z.object({
  workflow: WorkflowDefSchema,
  tools: z.array(z.string()).default([]),
  agents: z.array(AgentDefSchema).default([]),
  parallel: z.array(ParallelGroupSchema).default([]),
  for_each: z.array(ForEachDefSchema).default([]),
  output: z.record(z.string()).default({}),
});
export type WorkflowConfig = z.infer<typeof WorkflowConfigSchema>;
