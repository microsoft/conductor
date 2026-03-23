/** TypeScript types for all workflow event payloads. Mirrors events.py. */

export interface WorkflowEvent {
  type: EventType;
  timestamp: number;
  data: Record<string, unknown>;
}

export type EventType =
  | 'workflow_started'
  | 'agent_started'
  | 'agent_completed'
  | 'agent_failed'
  | 'agent_prompt_rendered'
  | 'agent_reasoning'
  | 'agent_tool_start'
  | 'agent_tool_complete'
  | 'agent_turn_start'
  | 'agent_message'
  | 'script_started'
  | 'script_completed'
  | 'script_failed'
  | 'gate_presented'
  | 'gate_resolved'
  | 'route_taken'
  | 'parallel_started'
  | 'parallel_agent_completed'
  | 'parallel_agent_failed'
  | 'parallel_completed'
  | 'for_each_started'
  | 'for_each_item_started'
  | 'for_each_item_completed'
  | 'for_each_item_failed'
  | 'for_each_completed'
  | 'workflow_completed'
  | 'workflow_failed'
  | 'checkpoint_saved'
  | 'agent_paused'
  | 'agent_resumed';

// --- Workflow lifecycle ---

export interface WorkflowStartedData {
  name: string;
  entry_point?: string;
  agents: Array<{ name: string; type?: string; model?: string }>;
  routes: Array<{ from: string; to: string; when?: string }>;
  parallel_groups?: Array<{ name: string; agents: string[] }>;
  for_each_groups?: Array<{ name: string }>;
}

export interface WorkflowCompletedData {
  elapsed?: number;
  output?: unknown;
}

export interface WorkflowFailedData {
  agent_name?: string;
  error_type?: string;
  message?: string;
}

// --- Agent lifecycle ---

export interface AgentStartedData {
  agent_name: string;
  iteration?: number;
}

export interface AgentCompletedData {
  agent_name: string;
  elapsed?: number;
  model?: string;
  tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  output?: unknown;
  output_keys?: string[];
}

export interface AgentFailedData {
  agent_name: string;
  elapsed?: number;
  error_type?: string;
  message?: string;
}

// --- Streaming events ---

export interface AgentPromptRenderedData {
  agent_name: string;
  rendered_prompt: string;
  context_keys?: string[];
}

export interface AgentReasoningData {
  agent_name: string;
  content: string;
}

export interface AgentToolStartData {
  agent_name: string;
  tool_name: string;
  arguments?: string;
}

export interface AgentToolCompleteData {
  agent_name: string;
  tool_name?: string;
  result?: string;
}

export interface AgentTurnStartData {
  agent_name: string;
  turn?: number;
}

export interface AgentMessageData {
  agent_name: string;
  content: string;
}

// --- Script lifecycle ---

export interface ScriptStartedData {
  agent_name: string;
}

export interface ScriptCompletedData {
  agent_name: string;
  elapsed?: number;
  stdout?: string;
  stderr?: string;
  exit_code?: number;
}

export interface ScriptFailedData {
  agent_name: string;
  elapsed?: number;
  error_type?: string;
  message?: string;
}

// --- Gate events ---

export interface GateOptionDetail {
  label: string;
  value: string;
  route: string;
  prompt_for?: string | null;
}

export interface GatePresentedData {
  agent_name: string;
  prompt?: string;
  options?: string[];
  option_details?: GateOptionDetail[];
}

export interface GateResolvedData {
  agent_name: string;
  selected_option?: string;
  route?: string;
  additional_input?: string;
}

// --- Route ---

export interface RouteTakenData {
  from_agent: string;
  to_agent: string;
}

// --- Parallel group ---

export interface ParallelStartedData {
  group_name: string;
  agents: string[];
}

export interface ParallelAgentCompletedData {
  group_name: string;
  agent_name: string;
  elapsed?: number;
  model?: string;
  tokens?: number;
  cost_usd?: number;
}

export interface ParallelAgentFailedData {
  group_name: string;
  agent_name: string;
  elapsed?: number;
  error_type?: string;
  message?: string;
}

export interface ParallelCompletedData {
  group_name: string;
  failure_count: number;
}

// --- For-each group ---

export interface ForEachStartedData {
  group_name: string;
  item_count: number;
}

export interface ForEachItemStartedData {
  group_name: string;
  item_key: string;
  index: number;
  item?: unknown;
}

export interface ForEachItemCompletedData {
  group_name: string;
  item_key: string;
  index: number;
  elapsed?: number;
  tokens?: number;
  cost_usd?: number;
  output?: unknown;
}

export interface ForEachItemFailedData {
  group_name: string;
  item_key: string;
  index: number;
  elapsed?: number;
  error_type?: string;
  message?: string;
}

export interface ForEachCompletedData {
  group_name: string;
  elapsed?: number;
  success_count?: number;
  failure_count?: number;
}

// --- Pause/Resume ---

export interface AgentPausedData {
  agent_name: string;
  partial_content?: string;
}

export interface AgentResumedData {
  agent_name: string;
}
