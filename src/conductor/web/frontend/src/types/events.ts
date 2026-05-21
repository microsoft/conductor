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
  | 'set_started'
  | 'set_completed'
  | 'set_failed'
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
  | 'subworkflow_started'
  | 'subworkflow_completed'
  | 'subworkflow_failed'
  | 'workflow_completed'
  | 'workflow_failed'
  | 'checkpoint_saved'
  | 'agent_paused'
  | 'agent_resumed'
  | 'dialog_started'
  | 'dialog_message'
  | 'dialog_completed'
  | 'iteration_limit_reached'
  | 'iteration_limit_resolved';

// --- Workflow lifecycle ---

export interface WorkflowStartedData {
  name: string;
  entry_point?: string;
  agents: Array<{ name: string; type?: string; model?: string; reasoning_effort?: string | null }>;
  routes: Array<{ from: string; to: string; when?: string }>;
  parallel_groups?: Array<{ name: string; agents: string[] }>;
  for_each_groups?: Array<{ name: string }>;
}

export interface WorkflowCompletedData {
  elapsed?: number;
  output?: unknown;
  /** Slot-key path of the completing engine (present only at depth > 0). */
  subworkflow_path?: string[];
}

export interface WorkflowFailedData {
  agent_name?: string;
  error_type?: string;
  message?: string;
  /** Slot-key path of the failing engine (present only at depth > 0). */
  subworkflow_path?: string[];
}

// --- Agent lifecycle ---

export interface AgentStartedData {
  agent_name: string;
  iteration?: number;
  context_window_max?: number;
}

export interface AgentCompletedData {
  agent_name: string;
  elapsed?: number;
  model?: string;
  tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  context_window_used?: number;
  context_window_max?: number;
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

// --- Set step lifecycle (issue #221) ---

export interface SetStartedData {
  agent_name: string;
  iteration?: number;
}

export interface SetCompletedData {
  agent_name: string;
  elapsed?: number;
  /** Effective output type used for coercion (auto/string/number/...). */
  output_type?: string;
  /** Sorted dict keys for multi-`values:` steps; empty array for scalars. */
  output_keys?: string[];
  /** Short JSON-safe preview of the bound value (truncated to ~512 chars). */
  value_repr?: string;
}

export interface SetFailedData {
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
  context_window_used?: number;
  context_window_max?: number;
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

// --- Dialog events ---

export interface DialogStartedData {
  dialog_id: string;
  agent_name: string;
  opening_question: string;
}

export interface DialogMessageData {
  dialog_id: string;
  agent_name: string;
  role: 'user' | 'agent';
  content: string;
}

export interface DialogCompletedData {
  dialog_id: string;
  agent_name: string;
  turn_count: number;
  user_dismissed?: boolean;
  user_declined?: boolean;
  agent_proposed_continue?: boolean;
}

// --- Subworkflow lifecycle ---

export interface SubworkflowStartedData {
  agent_name: string;
  iteration?: number;
  workflow: string;
  /** Slot-key path of the parent context in the recursive sub-workflow tree. */
  parent_path?: string[];
  /** Slot identifier for this child context (e.g. "agent_name" or "group[2]"). */
  slot_key?: string;
  /** for_each item key (when this start was emitted by a for_each iteration). */
  item_key?: string;
}

export interface SubworkflowCompletedData {
  agent_name: string;
  elapsed?: number;
  output?: unknown;
  parent_path?: string[];
  slot_key?: string;
  item_key?: string;
}

export interface SubworkflowFailedData {
  agent_name: string;
  elapsed?: number;
  error_type?: string;
  message?: string;
  parent_path?: string[];
  slot_key?: string;
  item_key?: string;
}

// --- Iteration limit gate ---

/**
 * Discriminated target for iteration-limit events: the Python engine emits
 * either ``agent_name`` (single-agent gate) or ``group_name`` + ``agent_count``
 * (parallel-group gate) — never both. Modeling them as a union prevents the
 * "neither/both" illegal states that an independently-optional pair would admit.
 */
export type IterationLimitTarget =
  | {
      /** Agent name (when triggered before a single agent execution). */
      agent_name: string;
      group_name?: never;
      agent_count?: never;
    }
  | {
      /** Parallel group name (when triggered before a parallel group). */
      group_name: string;
      /** Number of agents in the parallel group. */
      agent_count: number;
      agent_name?: never;
    };

/**
 * Narrowed target used in the ``iteration_limit_response`` payload sent
 * from the dashboard back to the engine. The engine already knows the
 * group's ``agent_count`` from the original ``iteration_limit_reached``
 * event, so the response only needs to identify the target. Modeling
 * this as a discriminated union prevents accidentally sending both
 * ``agent_name`` and ``group_name`` (or neither). See issue #198.
 */
export type IterationLimitResponseTarget =
  | { agent_name: string; group_name?: never }
  | { group_name: string; agent_name?: never };

export type IterationLimitReachedData = IterationLimitTarget & {
  /**
   * Unique id for this gate occurrence. The dashboard must echo this in the
   * ``iteration_limit_response`` payload so a stale or duplicated response
   * from a previous gate cannot resolve a later gate for the same target.
   * Issue #198.
   */
  gate_id: string;
  current_iteration: number;
  max_iterations: number;
  /** Last up to 5 agents executed, oldest to newest. */
  agent_history: string[];
  /**
   * Heuristic: ``true`` when the last 3 entries of ``agent_history`` are all
   * the same agent (and history has at least 3 entries). Useful for flagging
   * stuck review loops.
   */
  possible_loop: boolean;
  /**
   * When ``true``, the workflow will auto-stop without prompting the user
   * (``--skip-gates``). Subscribers should render the gate as auto-closing
   * rather than awaiting console input.
   */
  skip_gates: boolean;
};

export type IterationLimitResolvedData = (
  | { agent_name: string; group_name?: never }
  | { group_name: string; agent_name?: never }
) & {
  /** Echo of the gate_id from the corresponding ``iteration_limit_reached``. */
  gate_id?: string;
  /**
   * ``true`` when the gate was resolved by continuing (user prompt or, in
   * ``--skip-gates`` mode, the auto-decision); ``false`` when the workflow
   * stopped at the gate.
   */
  continue_execution: boolean;
  /** Additional iterations granted; ``0`` when not continuing. */
  additional_iterations: number;
  /**
   * ``true`` when the gate was resolved by an unexpected exception
   * (e.g. ``EOFError`` on non-TTY, ``KeyboardInterrupt``) rather than by a
   * user or auto decision. The dashboard can use this to distinguish a
   * crash-driven stop from a deliberate one.
   */
  aborted?: boolean;
};
