import { create } from 'zustand';
import type { NodeStatus, NodeType } from '@/lib/constants';
import type {
  WorkflowEvent,
  WorkflowStartedData,
  AgentStartedData,
  AgentCompletedData,
  AgentFailedData,
  AgentPromptRenderedData,
  AgentReasoningData,
  AgentToolStartData,
  AgentToolCompleteData,
  AgentTurnStartData,
  AgentMessageData,
  ScriptCompletedData,
  ScriptFailedData,
  WaitStartedData,
  WaitCompletedData,
  WaitFailedData,
  SetCompletedData,
  SetFailedData,
  GatePresentedData,
  GateResolvedData,
  GateOptionDetail,
  RouteTakenData,
  ParallelStartedData,
  ParallelAgentCompletedData,
  ParallelAgentFailedData,
  ParallelCompletedData,
  ForEachStartedData,
  ForEachItemStartedData,
  ForEachItemCompletedData,
  ForEachItemFailedData,
  ForEachCompletedData,
  AgentPausedData,
  AgentResumedData,
  DialogStartedData,
  DialogMessageData,
  DialogCompletedData,
  AgentValidatorStartData,
  AgentValidatorCompleteData,
  AgentValidationFailedData,
  SubworkflowStartedData,
  SubworkflowCompletedData,
  SubworkflowFailedData,
  IterationLimitReachedData,
  IterationLimitResolvedData,
  IterationLimitResponseTarget,
} from '@/types/events';

export interface ActivityEntry {
  type: string;
  icon: string;
  label: string;
  text: string;
  detail?: string | null;
}

export interface IterationSnapshot {
  iteration: number;
  prompt?: string;
  output?: unknown;
  elapsed?: number;
  model?: string;
  reasoning_effort?: string;
  tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  activity: ActivityEntry[];
  error_type?: string;
  error_message?: string;
}

export interface ForEachItemData {
  key: string;
  index: number;
  status: 'running' | 'completed' | 'failed';
  elapsed?: number;
  tokens?: number;
  cost_usd?: number;
  error_type?: string;
  error_message?: string;
  prompt?: string;
  output?: unknown;
  activity: ActivityEntry[];
}

export interface NodeData {
  name: string;
  status: NodeStatus;
  type: NodeType;
  elapsed?: number;
  model?: string;
  reasoning_effort?: string;
  // Context window tracking
  context_pct?: number;
  context_window_used?: number;
  context_window_max?: number;
  tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  output?: unknown;
  output_keys?: string[];
  prompt?: string;
  context_keys?: string[];
  latest_message?: string;
  iteration?: number;
  error_type?: string;
  error_message?: string;
  // Script-specific
  stdout?: string;
  stderr?: string;
  exit_code?: number;
  // Wait-specific (issue #218)
  duration_seconds?: number | null;
  waited_seconds?: number;
  requested_seconds?: number;
  reason?: string | null;
  interrupted?: boolean;
  // Set-step-specific (issue #221)
  set_output_type?: import('@/types/events').SetOutputType;
  set_output_keys?: string[];
  set_value_repr?: string;
  // Gate-specific
  options?: string[];
  option_details?: GateOptionDetail[];
  selected_option?: string;
  route?: string;
  additional_input?: string;
  // Group-specific
  success_count?: number;
  failure_count?: number;
  // For-each per-item tracking
  for_each_items?: ForEachItemData[];
  // Activity
  activity: ActivityEntry[];
  // Timestamp when the agent started (for elapsed timer on refresh)
  startedAt?: number;
  // Iteration history (snapshots of completed previous iterations)
  iterationHistory?: IterationSnapshot[];
  // Dialog-specific
  dialog_id?: string;
  dialog_messages?: Array<{ role: 'user' | 'agent'; content: string }>;
  dialog_active?: boolean;
  dialog_awaiting_response?: boolean;
  // Validator-specific (issue #220)
  validator_state?: 'running' | 'passed' | 'failed' | 'error';
  validator_passed?: boolean;
  validator_issues?: string[];
  validator_errored?: boolean;
  validator_will_retry?: boolean;
  /** Number of times the validator has run for this node (1 normally). */
  validator_attempts?: number;
  validator_cost_usd?: number | null;
  validator_model?: string | null;
  // Terminate-specific (type: terminate steps; see issue #219)
  termination_status?: 'success' | 'failed';
  termination_reason?: string;
  terminated_by?: string;
  // Provider tier (#241) — populated from workflow_started.providers when
  // the agent's resolved provider is experimental, so the graph can
  // render a badge without re-derivation.
  provider_name?: string;
  provider_tier?: 'stable' | 'experimental' | null;
}

export interface GroupProgress {
  total: number;
  completed: number;
  failed: number;
}

export interface RouteEdge {
  from: string;
  to: string;
  when?: string;
}

export interface WorkflowAgent {
  name: string;
  type?: string;
  model?: string;
  reasoning_effort?: string | null;
  /** Provider this agent will use at runtime. Drives the experimental
   *  badge in the graph (#241). */
  provider_name?: string;
}

// ProviderMetadata is defined in types/events.ts (single source of truth)
// and re-exported here for callers that import from the store.
export type { ProviderMetadata } from '@/types/events';

export interface ParallelGroup {
  name: string;
  agents: string[];
}

export interface ForEachGroup {
  name: string;
}

export type WorkflowStatus = 'pending' | 'running' | 'completed' | 'failed';
export type WsStatus = 'connecting' | 'connected' | 'disconnected' | 'reconnecting';

/** A single subworkflow execution context — isolated state for one invocation. */
export interface SubworkflowContext {
  /** Agent in the parent that triggered this subworkflow */
  parentAgent: string;
  /** Iteration number (for repeated subworkflow calls) */
  iteration: number;
  /**
   * Stable slot identifier for this context within its parent's children.
   * For sequential sub-workflow agents this is the agent name.
   * For for_each iterations it is `f"{group.name}[{key}]"`.
   * Used to disambiguate concurrent for_each iterations of the same group.
   */
  slotKey: string;
  /** The .yaml file reference */
  workflowFile: string;
  /** Resolved workflow name (from inner workflow_started) */
  workflowName: string;
  status: WorkflowStatus;
  /** Graph structure — isolated from parent */
  agents: WorkflowAgent[];
  routes: RouteEdge[];
  parallelGroups: ParallelGroup[];
  forEachGroups: ForEachGroup[];
  nodes: Record<string, NodeData>;
  groupProgress: Record<string, GroupProgress>;
  highlightedEdges: HighlightedEdge[];
  entryPoint: string | null;
  /** Nested child contexts (subworkflows within this subworkflow) */
  children: SubworkflowContext[];
  /** Counters */
  agentsCompleted: number;
  agentsTotal: number;
  totalCost: number;
  totalTokens: number;
  /** Event/activity log scoped to this context */
  eventLog: LogEntry[];
  activityLog: ActivityLogEntry[];
  workflowOutput: unknown | null;
  workflowFailure: { error_type?: string; message?: string } | null;
}

/** Breadcrumb entry for navigation */
export interface BreadcrumbEntry {
  label: string;
  /** Index path to reach this context: [] = root, [0] = first child, [0, 2] = grandchild */
  path: number[];
}

export interface HighlightedEdge {
  from: string;
  to: string;
  state: 'highlighted' | 'taken' | 'failed';
}

export type LogLevel = 'info' | 'success' | 'error' | 'warning' | 'debug';

export type ActivityLogType = 'reasoning' | 'tool-start' | 'tool-complete' | 'turn' | 'message' | 'prompt';

export interface LogEntry {
  timestamp: number;
  level: LogLevel;
  source: string;
  message: string;
  detail?: string;
}

export interface ActivityLogEntry {
  timestamp: number;
  source: string;
  type: ActivityLogType;
  message: string;
  detail?: string | null;
}

interface WorkflowState {
  // Workflow metadata
  workflowName: string;
  workflowStatus: WorkflowStatus;
  workflowStartTime: number | null;
  workflowFailure: { error_type?: string; message?: string; elapsed_seconds?: number; timeout_seconds?: number; current_agent?: string; checkpoint_path?: string; termination_reason?: string; terminated_by?: string; is_explicit?: boolean; status?: string } | null;
  workflowFailedAgent: string | null;
  workflowYaml: string | null;
  conductorVersion: string | null;
  entryPoint: string | null;

  // Explicit-termination metadata, populated for both success and failure
  // terminations (see issue #219). When present, the WorkflowSuccessBanner /
  // WorkflowErrorBanner can show the structured reason and terminate step
  // name in addition to the generic completion / failure banners.
  workflowTermination: {
    is_explicit: boolean;
    status: 'success' | 'failed';
    termination_reason?: string;
    terminated_by?: string;
  } | null;

  // Graph structure
  agents: WorkflowAgent[];
  routes: RouteEdge[];
  parallelGroups: ParallelGroup[];
  forEachGroups: ForEachGroup[];

  // Node state
  nodes: Record<string, NodeData>;
  groupProgress: Record<string, GroupProgress>;

  // Edge highlights
  highlightedEdges: HighlightedEdge[];

  // Counters
  agentsCompleted: number;
  agentsTotal: number;
  totalCost: number;
  totalTokens: number;

  // UI state
  selectedNode: string | null;
  wsStatus: WsStatus;

  // Event log (terminal-like output)
  eventLog: LogEntry[];
  activityLog: ActivityLogEntry[];
  workflowOutput: unknown | null;
  lastEventTime: number | null;
  isPaused: boolean;
  /** Set when the engine is blocked on a max-iterations gate (issue #134). */
  iterationLimitGate: IterationLimitReachedData | null;

  // --- Subworkflow depth tracking ---
  /** Current nesting depth: 0 = root workflow events are active */
  wfDepth: number;
  /** Subworkflow contexts — each child workflow gets isolated state */
  subworkflowContexts: SubworkflowContext[];
  /** The context currently being populated by child events (stack of indices into children arrays) */
  activeContextPath: number[];

  // --- Breadcrumb navigation ---
  /** Path to the currently *viewed* context ([] = root) */
  viewContextPath: number[];

  // Replay mode state
  replayMode: boolean;
  replayEvents: WorkflowEvent[];
  replayPosition: number;
  replayTotalEvents: number;
  replayPlaying: boolean;
  replaySpeed: number;

  // Actions
  processEvent: (event: WorkflowEvent) => void;
  replayState: (events: WorkflowEvent[]) => void;
  selectNode: (name: string | null) => void;
  setWsStatus: (status: WsStatus) => void;
  setEdgeHighlight: (from: string, to: string, state: 'highlighted' | 'taken' | 'failed') => void;
  clearEdgeHighlight: (from: string, to: string) => void;

  // Breadcrumb navigation actions
  navigateToContext: (path: number[]) => void;
  navigateUp: () => void;
  navigateIntoSubworkflow: (slotKey: string) => void;

  // Computed: get the currently viewed context's data
  getViewedContext: () => {
    workflowName: string;
    agents: WorkflowAgent[];
    routes: RouteEdge[];
    parallelGroups: ParallelGroup[];
    forEachGroups: ForEachGroup[];
    nodes: Record<string, NodeData>;
    groupProgress: Record<string, GroupProgress>;
    highlightedEdges: HighlightedEdge[];
    entryPoint: string | null;
    subworkflowContexts: SubworkflowContext[];
  };
  getBreadcrumbs: () => BreadcrumbEntry[];

  // Replay actions
  setReplayMode: (events: WorkflowEvent[]) => void;
  setReplayPosition: (position: number) => void;
  setReplayPlaying: (playing: boolean) => void;
  setReplaySpeed: (speed: number) => void;

  // WebSocket send function (set by use-websocket hook)
  _wsSend: ((data: object) => void) | null;
  setWsSend: (fn: ((data: object) => void) | null) => void;
  sendGateResponse: (agentName: string, selectedValue: string, additionalInput?: Record<string, string>) => void;
  // Dialog state
  activeDialog: { agentName: string; dialogId: string } | null;
  dialogEngaged: boolean;
  engageDialog: () => void;
  sendDialogMessage: (agentName: string, dialogId: string, message: string) => void;
  sendDialogDecline: (agentName: string, dialogId: string) => void;
  /**
   * Resolve a max-iterations gate from the dashboard (issue #198).
   *
   * Send ``additionalIterations === 0`` to stop the workflow, or any positive
   * integer to continue with N more iterations. ``gateId`` must match the
   * id from the most recent ``iteration_limit_reached`` event so the engine
   * ignores stale responses.
   *
   * ``target`` uses a discriminated union — exactly one of ``agent_name`` or
   * ``group_name`` must be set, preventing accidental dual-target payloads
   * that the engine wouldn't know how to interpret.
   */
  sendIterationLimitResponse: (
    target: IterationLimitResponseTarget,
    gateId: string,
    additionalIterations: number,
  ) => void;
}

function ensureNode(nodes: Record<string, NodeData>, name: string, type: NodeType = 'agent'): NodeData {
  if (!nodes[name]) {
    nodes[name] = { name, status: 'pending', type, activity: [] };
  }
  if (!nodes[name]!.activity) {
    nodes[name]!.activity = [];
  }
  return nodes[name]!;
}

function addActivity(nodes: Record<string, NodeData>, agentName: string, entry: ActivityEntry) {
  const nd = ensureNode(nodes, agentName);
  nd.activity.push(entry);
}

/** Create a new reference for a node to ensure React/ReactFlow detects the change. */
function replaceNode(nodes: Record<string, NodeData>, name: string): void {
  if (nodes[name]) {
    nodes[name] = { ...nodes[name]! };
  }
}

/** Add an activity entry to a for-each item's activity array. */
function addForEachItemActivity(nodes: Record<string, NodeData>, groupName: string, itemKey: string, entry: ActivityEntry): void {
  const nd = nodes[groupName];
  if (!nd?.for_each_items) return;
  const item = nd.for_each_items.find((i) => i.key === itemKey);
  if (item) {
    item.activity.push(entry);
  }
}

// ---------------------------------------------------------------------------
// Subworkflow context helpers
// ---------------------------------------------------------------------------

function createSubworkflowContext(parentAgent: string, iteration: number, workflowFile: string, slotKey?: string): SubworkflowContext {
  return {
    parentAgent,
    iteration,
    slotKey: slotKey ?? parentAgent,
    workflowFile,
    workflowName: '',
    status: 'pending',
    agents: [],
    routes: [],
    parallelGroups: [],
    forEachGroups: [],
    nodes: {},
    groupProgress: {},
    highlightedEdges: [],
    entryPoint: null,
    children: [],
    agentsCompleted: 0,
    agentsTotal: 0,
    totalCost: 0,
    totalTokens: 0,
    eventLog: [],
    activityLog: [],
    workflowOutput: null,
    workflowFailure: null,
  };
}

/** Resolve a SubworkflowContext from a path of indices (e.g. [0, 2] = first child's third child). */
function resolveContext(contexts: SubworkflowContext[], path: number[]): SubworkflowContext | null {
  if (path.length === 0) return null;
  let ctx: SubworkflowContext | undefined = contexts[path[0]!];
  for (let i = 1; i < path.length && ctx; i++) {
    ctx = ctx.children[path[i]!];
  }
  return ctx ?? null;
}

/**
 * Walk the subworkflow context tree by slot keys, returning the index path
 * (numeric, for use with resolveContext) and the resolved context.
 *
 * For each slot, matches the newest matching context to support re-runs /
 * iteration loops where the same slot key appears multiple times. Note the
 * consequence: older iterations of the same slot become unreachable via this
 * path resolver — late-arriving events targeting them must use the index
 * path captured at the time the iteration was active. Issue #145 (S3).
 *
 * Returns null if any segment cannot be matched.
 */
function resolveSlotPath(
  contexts: SubworkflowContext[],
  slotPath: string[],
): { indexPath: number[]; ctx: SubworkflowContext | null } | null {
  if (slotPath.length === 0) {
    return { indexPath: [], ctx: null };
  }
  const indexPath: number[] = [];
  let current = contexts;
  let ctx: SubworkflowContext | null = null;
  for (const slot of slotPath) {
    // Match newest matching slot (handles re-runs / iteration loops).
    let foundIdx = -1;
    for (let i = current.length - 1; i >= 0; i--) {
      if (current[i]!.slotKey === slot) {
        foundIdx = i;
        break;
      }
    }
    if (foundIdx === -1) return null;
    indexPath.push(foundIdx);
    ctx = current[foundIdx]!;
    current = ctx.children;
  }
  return { indexPath, ctx };
}

/** Find a child context by slot key within a context's children. */
function findChildContext(
  contexts: SubworkflowContext[],
  slotKey: string,
): { ctx: SubworkflowContext; index: number } | null {
  // Iterate newest-first so that re-runs of the same slot pick the latest.
  for (let i = contexts.length - 1; i >= 0; i--) {
    const c = contexts[i]!;
    if (c.slotKey === slotKey) {
      return { ctx: c, index: i };
    }
  }
  return null;
}

/** Get the nodes/routes/etc. for the currently active child context (where events should be routed). */
function _getActiveChildState(state: WorkflowState): { nodes: Record<string, NodeData>; groupProgress: Record<string, GroupProgress>; eventLog: LogEntry[]; activityLog: ActivityLogEntry[] } | null {
  if (state.activeContextPath.length === 0) return null;
  const ctx = resolveContext(state.subworkflowContexts, state.activeContextPath);
  if (!ctx) return null;
  return { nodes: ctx.nodes, groupProgress: ctx.groupProgress, eventLog: ctx.eventLog, activityLog: ctx.activityLog };
}
void _getActiveChildState; // suppress unused warning

export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  workflowName: '',
  workflowStatus: 'pending',
  workflowStartTime: null,
  workflowFailure: null,
  workflowFailedAgent: null,
  workflowTermination: null,
  workflowYaml: null,
  conductorVersion: null,
  entryPoint: null,
  agents: [],
  routes: [],
  parallelGroups: [],
  forEachGroups: [],
  nodes: {},
  groupProgress: {},
  highlightedEdges: [],
  agentsCompleted: 0,
  agentsTotal: 0,
  totalCost: 0,
  totalTokens: 0,
  selectedNode: null,
  wsStatus: 'connecting',
  eventLog: [],
  activityLog: [],
  workflowOutput: null,
  lastEventTime: null,
  isPaused: false,
  iterationLimitGate: null,
  wfDepth: 0,
  subworkflowContexts: [],
  activeContextPath: [],
  viewContextPath: [],
  replayMode: false,
  replayEvents: [],
  replayPosition: 0,
  replayTotalEvents: 0,
  replayPlaying: false,
  replaySpeed: 1,
  _wsSend: null,

  setWsSend: (fn) => {
    set({ _wsSend: fn });
  },

  sendGateResponse: (agentName, selectedValue, additionalInput) => {
    const send = useWorkflowStore.getState()._wsSend;
    if (send) {
      send({
        type: 'gate_response',
        agent_name: agentName,
        selected_value: selectedValue,
        additional_input: additionalInput || {},
      });
    }
  },

  activeDialog: null,
  dialogEngaged: false,

  engageDialog: () => {
    set({ dialogEngaged: true });
  },

  sendDialogMessage: (agentName, dialogId, message) => {
    const send = useWorkflowStore.getState()._wsSend;
    if (send) {
      send({
        type: 'dialog_message',
        agent_name: agentName,
        dialog_id: dialogId,
        content: message,
      });
      // No optimistic update — the server echoes the user message back as a
      // `dialog_message` event with role='user', and the handler below flips
      // `dialog_awaiting_response` accordingly. Keeps state transitions in one
      // place and avoids a race where the agent reply arrives before the
      // optimistic set commits.
    }
  },

  sendDialogDecline: (agentName, dialogId) => {
    const send = useWorkflowStore.getState()._wsSend;
    if (send) {
      send({
        type: 'dialog_decline',
        agent_name: agentName,
        dialog_id: dialogId,
      });
    }
  },

  sendIterationLimitResponse: (target, gateId, additionalIterations) => {
    const send = useWorkflowStore.getState()._wsSend;
    if (!send) return;
    // Clamp to non-negative integer. 0 means "stop", N>0 means "continue with N more".
    const additional = Math.max(0, Math.floor(Number(additionalIterations) || 0));
    // ``target`` is a discriminated union — exactly one branch is set, so a
    // simple `in` narrowing produces the right payload shape without risk of
    // sending both fields.
    const targetFields =
      'agent_name' in target
        ? { agent_name: target.agent_name }
        : { group_name: target.group_name };
    send({
      type: 'iteration_limit_response',
      gate_id: gateId,
      ...targetFields,
      additional_iterations: additional,
    });
  },

  processEvent: (event: WorkflowEvent) => {
    const handler = eventHandlers[event.type];
    set((state) => {
      const newState = { ...state, nodes: { ...state.nodes }, groupProgress: { ...state.groupProgress }, eventLog: [...state.eventLog], activityLog: [...state.activityLog], lastEventTime: event.timestamp };
      if (handler) {
        handler(newState, event.data, event.timestamp);
      }
      const logEntry = buildLogEntry(event);
      if (logEntry) {
        newState.eventLog.push(logEntry);
      }
      const activityEntry = buildActivityLogEntry(event);
      if (activityEntry) {
        newState.activityLog.push(activityEntry);
      }
      return newState;
    });
  },

  replayState: (events: WorkflowEvent[]) => {
    set((state) => {
      const newState: WorkflowState = {
        ...state,
        agentsCompleted: 0,
        totalCost: 0,
        totalTokens: 0,
        nodes: {},
        groupProgress: {},
        highlightedEdges: [],
        eventLog: [],
        activityLog: [],
        workflowOutput: null,
        workflowFailedAgent: null,
        workflowTermination: null,
        activeDialog: null,
        dialogEngaged: false,
        wfDepth: 0,
        subworkflowContexts: [],
        activeContextPath: [],
      };
      for (const event of events) {
        const handler = eventHandlers[event.type];
        if (handler) {
          handler(newState, event.data, event.timestamp);
        }
        const logEntry = buildLogEntry(event);
        if (logEntry) {
          newState.eventLog.push(logEntry);
        }
        const activityEntry = buildActivityLogEntry(event);
        if (activityEntry) {
          newState.activityLog.push(activityEntry);
        }
        newState.lastEventTime = event.timestamp;
      }
      return newState;
    });
  },

  selectNode: (name: string | null) => {
    set({ selectedNode: name });
  },

  setReplayMode: (events: WorkflowEvent[]) => {
    set((state) => {
      const newState: WorkflowState = {
        ...state,
        replayMode: true,
        replayEvents: events,
        replayTotalEvents: events.length,
        replayPosition: events.length,
        replayPlaying: false,
        replaySpeed: 1,
        agentsCompleted: 0,
        totalCost: 0,
        totalTokens: 0,
        nodes: {},
        groupProgress: {},
        highlightedEdges: [],
        eventLog: [],
        activityLog: [],
        workflowOutput: null,
        workflowFailedAgent: null,
        workflowTermination: null,
        activeDialog: null,
        dialogEngaged: false,
        wfDepth: 0,
        subworkflowContexts: [],
        activeContextPath: [],
        viewContextPath: [],
      };
      for (const event of events) {
        const handler = eventHandlers[event.type];
        if (handler) handler(newState, event.data, event.timestamp);
        const logEntry = buildLogEntry(event);
        if (logEntry) newState.eventLog.push(logEntry);
        const activityEntry = buildActivityLogEntry(event);
        if (activityEntry) newState.activityLog.push(activityEntry);
        newState.lastEventTime = event.timestamp;
      }
      return newState;
    });
  },

  setReplayPosition: (position: number) => {
    set((state) => {
      const events = state.replayEvents.slice(0, position);
      const newState: WorkflowState = {
        ...state,
        replayPosition: position,
        agentsCompleted: 0,
        totalCost: 0,
        totalTokens: 0,
        nodes: {},
        groupProgress: {},
        highlightedEdges: [],
        eventLog: [],
        activityLog: [],
        workflowOutput: null,
        workflowFailedAgent: null,
        workflowTermination: null,
        workflowStatus: 'pending',
        workflowStartTime: null,
        workflowName: '',
        workflowFailure: null,
        entryPoint: null,
        agents: [],
        routes: [],
        parallelGroups: [],
        forEachGroups: [],
        isPaused: false,
        iterationLimitGate: null,
        lastEventTime: null,
        activeDialog: null,
        dialogEngaged: false,
        wfDepth: 0,
        subworkflowContexts: [],
        activeContextPath: [],
        viewContextPath: [],
      };
      for (const event of events) {
        const handler = eventHandlers[event.type];
        if (handler) handler(newState, event.data, event.timestamp);
        const logEntry = buildLogEntry(event);
        if (logEntry) newState.eventLog.push(logEntry);
        const activityEntry = buildActivityLogEntry(event);
        if (activityEntry) newState.activityLog.push(activityEntry);
        newState.lastEventTime = event.timestamp;
      }
      return newState;
    });
  },

  setReplayPlaying: (playing: boolean) => {
    set({ replayPlaying: playing });
  },

  setReplaySpeed: (speed: number) => {
    set({ replaySpeed: speed });
  },

  setWsStatus: (status: WsStatus) => {
    set({ wsStatus: status });
  },

  setEdgeHighlight: (from: string, to: string, state: 'highlighted' | 'taken' | 'failed') => {
    set((prev) => ({
      highlightedEdges: [
        ...prev.highlightedEdges.filter((e) => !(e.from === from && e.to === to)),
        { from, to, state },
      ],
    }));
  },

  clearEdgeHighlight: (from: string, to: string) => {
    set((prev) => ({
      highlightedEdges: prev.highlightedEdges.filter((e) => !(e.from === from && e.to === to)),
    }));
  },

  // --- Breadcrumb navigation ---

  navigateToContext: (path: number[]) => {
    set({ viewContextPath: path, selectedNode: null });
  },

  navigateUp: () => {
    set((prev) => ({
      viewContextPath: prev.viewContextPath.slice(0, -1),
      selectedNode: null,
    }));
  },

  navigateIntoSubworkflow: (slotKey: string) => {
    const state = get();
    // Determine which context list to search in
    const viewPath = state.viewContextPath;
    let contexts: SubworkflowContext[];
    if (viewPath.length === 0) {
      contexts = state.subworkflowContexts;
    } else {
      const parent = resolveContext(state.subworkflowContexts, viewPath);
      if (!parent) return;
      contexts = parent.children;
    }
    const found = findChildContext(contexts, slotKey);
    if (found) {
      set({ viewContextPath: [...viewPath, found.index], selectedNode: null });
    }
  },

  getViewedContext: () => {
    const state = get();
    if (state.viewContextPath.length === 0) {
      return {
        workflowName: state.workflowName,
        agents: state.agents,
        routes: state.routes,
        parallelGroups: state.parallelGroups,
        forEachGroups: state.forEachGroups,
        nodes: state.nodes,
        groupProgress: state.groupProgress,
        highlightedEdges: state.highlightedEdges,
        entryPoint: state.entryPoint,
        subworkflowContexts: state.subworkflowContexts,
      };
    }
    const ctx = resolveContext(state.subworkflowContexts, state.viewContextPath);
    if (!ctx) {
      // Stale path — reset to root
      return {
        workflowName: state.workflowName,
        agents: state.agents,
        routes: state.routes,
        parallelGroups: state.parallelGroups,
        forEachGroups: state.forEachGroups,
        nodes: state.nodes,
        groupProgress: state.groupProgress,
        highlightedEdges: state.highlightedEdges,
        entryPoint: state.entryPoint,
        subworkflowContexts: state.subworkflowContexts,
      };
    }
    return {
      workflowName: ctx.workflowName,
      agents: ctx.agents,
      routes: ctx.routes,
      parallelGroups: ctx.parallelGroups,
      forEachGroups: ctx.forEachGroups,
      nodes: ctx.nodes,
      groupProgress: ctx.groupProgress,
      highlightedEdges: ctx.highlightedEdges,
      entryPoint: ctx.entryPoint,
      subworkflowContexts: ctx.children,
    };
  },

  getBreadcrumbs: () => {
    const state = get();
    const crumbs: BreadcrumbEntry[] = [{ label: state.workflowName || 'Root', path: [] }];
    let contexts = state.subworkflowContexts;
    for (let i = 0; i < state.viewContextPath.length; i++) {
      const idx = state.viewContextPath[i]!;
      const ctx = contexts[idx];
      if (!ctx) break;
      // Prefer the slot key (e.g. "plan_children_group[2]") so concurrent
      // for_each iterations are distinguishable; fall back to workflow name.
      const label = ctx.slotKey || ctx.workflowName || ctx.workflowFile || ctx.parentAgent;
      crumbs.push({ label, path: state.viewContextPath.slice(0, i + 1) });
      contexts = ctx.children;
    }
    return crumbs;
  },
}));

// --- Event handlers (mutate the passed state directly) ---

type MutableState = WorkflowState;

/** Get the nodes/groupProgress/routes/highlightedEdges for the context that should receive the event.
 *
 * Routing is keyed strictly off the engine-supplied `subworkflow_path` stamp:
 * sub-workflow engines tag every event with their depth-aware path, the root
 * engine emits no stamp, and we resolve accordingly. This avoids conflating
 * "where did this event originate" (engine state) with "where is the user
 * looking" (UI state) — earlier versions fell back to `activeContextPath`
 * for unstamped events, which incorrectly routed parent-level events such
 * as `for_each_item_started` into whichever sub-workflow the user (or a
 * prior `subworkflow_started` event) had advanced the cursor into,
 * silently dropping iterations from the parent's for-each panel.
 */
function activeTarget(
  state: MutableState,
  data?: Record<string, unknown>,
): {
  nodes: Record<string, NodeData>;
  groupProgress: Record<string, GroupProgress>;
  routes: RouteEdge[];
  highlightedEdges: HighlightedEdge[];
  addCost: (cost: number) => void;
  addTokens: (tokens: number) => void;
  incrCompleted: () => void;
} {
  let ctx: SubworkflowContext | null = null;
  const subPath = data?.subworkflow_path;
  if (Array.isArray(subPath) && subPath.length > 0) {
    ctx = resolveSlotPath(state.subworkflowContexts, subPath as string[])?.ctx ?? null;
  }
  if (ctx) {
    const ctxRef = ctx;
    return {
      nodes: ctxRef.nodes,
      groupProgress: ctxRef.groupProgress,
      routes: ctxRef.routes,
      highlightedEdges: ctxRef.highlightedEdges,
      addCost: (cost: number) => { ctxRef.totalCost += cost; state.totalCost += cost; },
      addTokens: (tokens: number) => { ctxRef.totalTokens += tokens; state.totalTokens += tokens; },
      incrCompleted: () => { ctxRef.agentsCompleted++; state.agentsCompleted++; },
    };
  }
  return {
    nodes: state.nodes,
    groupProgress: state.groupProgress,
    routes: state.routes,
    highlightedEdges: state.highlightedEdges,
    addCost: (cost: number) => { state.totalCost += cost; },
    addTokens: (tokens: number) => { state.totalTokens += tokens; },
    incrCompleted: () => { state.agentsCompleted++; },
  };
}

const eventHandlers: Record<string, (state: MutableState, data: Record<string, unknown>, timestamp?: number) => void> = {
  workflow_started: (state, _data, timestamp) => {
    const data = _data as unknown as WorkflowStartedData;

    if (state.wfDepth === 0) {
      // Root workflow — initialize as before
      state.workflowStatus = 'running';
      state.workflowStartTime = timestamp ?? Date.now() / 1000;
      state.workflowName = data.name || '';
      state.workflowYaml = (_data as Record<string, unknown>).yaml_source as string ?? null;
      state.conductorVersion = (_data as Record<string, unknown>).version as string ?? null;
      state.entryPoint = data.entry_point || null;
      state.agents = data.agents || [];
      state.routes = data.routes || [];
      state.parallelGroups = data.parallel_groups || [];
      state.forEachGroups = data.for_each_groups || [];

      ensureNode(state.nodes, '$start', 'start');
      state.nodes['$start']!.status = 'running';
      replaceNode(state.nodes, '$start');

      const groupAgents = new Set<string>();
      const agentNames = new Set<string>();

      for (const pg of state.parallelGroups) {
        for (const a of pg.agents) groupAgents.add(a);
        agentNames.add(pg.name);
        ensureNode(state.nodes, pg.name, 'parallel_group');
        state.groupProgress[pg.name] = { total: pg.agents.length, completed: 0, failed: 0 };
        for (const agentName of pg.agents) ensureNode(state.nodes, agentName, 'agent');
      }
      for (const fg of state.forEachGroups) {
        agentNames.add(fg.name);
        ensureNode(state.nodes, fg.name, 'for_each_group');
        state.groupProgress[fg.name] = { total: 0, completed: 0, failed: 0 };
      }
      for (const a of state.agents) {
        if (!agentNames.has(a.name) && !groupAgents.has(a.name)) {
          const nodeType = (a.type || 'agent') as NodeType;
          ensureNode(state.nodes, a.name, nodeType);
          if (a.model) state.nodes[a.name]!.model = a.model;
          if (a.reasoning_effort) state.nodes[a.name]!.reasoning_effort = a.reasoning_effort;
          // Decorate the node with provider tier so the graph can render
          // the experimental badge without crawling the providers block.
          if (a.provider_name) {
            state.nodes[a.name]!.provider_name = a.provider_name;
            const providerMeta = data.providers?.[a.provider_name];
            if (providerMeta?.tier) {
              state.nodes[a.name]!.provider_tier = providerMeta.tier;
            }
          }
          agentNames.add(a.name);
        }
      }
      state.agentsTotal = agentNames.size;
    } else {
      // Child workflow — populate the owning child context. Locate it via
      // the engine-supplied subworkflow_path (slot-key path) when present,
      // because under concurrent for_each iterations the global
      // activeContextPath may have been advanced past us by a sibling
      // start, which would otherwise scramble whose routes/agents land
      // in which ctx.
      const subPath = (_data as Record<string, unknown>).subworkflow_path;
      const ctx = Array.isArray(subPath) && subPath.length > 0
        ? resolveSlotPath(state.subworkflowContexts, subPath as string[])?.ctx ?? null
        : resolveContext(state.subworkflowContexts, state.activeContextPath);
      if (ctx) {
        ctx.workflowName = data.name || '';
        ctx.status = 'running';
        ctx.entryPoint = data.entry_point || null;
        ctx.agents = data.agents || [];
        ctx.routes = data.routes || [];
        ctx.parallelGroups = data.parallel_groups || [];
        ctx.forEachGroups = data.for_each_groups || [];

        ensureNode(ctx.nodes, '$start', 'start');
        ctx.nodes['$start']!.status = 'running';

        const groupAgents = new Set<string>();
        const agentNames = new Set<string>();

        for (const pg of ctx.parallelGroups) {
          for (const a of pg.agents) groupAgents.add(a);
          agentNames.add(pg.name);
          ensureNode(ctx.nodes, pg.name, 'parallel_group');
          ctx.groupProgress[pg.name] = { total: pg.agents.length, completed: 0, failed: 0 };
          for (const agentName of pg.agents) ensureNode(ctx.nodes, agentName, 'agent');
        }
        for (const fg of ctx.forEachGroups) {
          agentNames.add(fg.name);
          ensureNode(ctx.nodes, fg.name, 'for_each_group');
          ctx.groupProgress[fg.name] = { total: 0, completed: 0, failed: 0 };
        }
        for (const a of ctx.agents) {
          if (!agentNames.has(a.name) && !groupAgents.has(a.name)) {
            const nodeType = (a.type || 'agent') as NodeType;
            ensureNode(ctx.nodes, a.name, nodeType);
            if (a.model) ctx.nodes[a.name]!.model = a.model;
            if (a.reasoning_effort) ctx.nodes[a.name]!.reasoning_effort = a.reasoning_effort;
            if (a.provider_name) {
              ctx.nodes[a.name]!.provider_name = a.provider_name;
              const providerMeta = data.providers?.[a.provider_name];
              if (providerMeta?.tier) {
                ctx.nodes[a.name]!.provider_tier = providerMeta.tier;
              }
            }
            agentNames.add(a.name);
          }
        }
        ctx.agentsTotal = agentNames.size;
      }
    }
    state.wfDepth++;
  },

  agent_started: (state, _data, timestamp) => {
    const data = _data as unknown as AgentStartedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);

    // Snapshot previous iteration before clearing
    if (nd.iteration != null && (nd.output != null || nd.error_type != null)) {
      if (!nd.iterationHistory) nd.iterationHistory = [];
      nd.iterationHistory.push({
        iteration: nd.iteration,
        prompt: nd.prompt,
        output: nd.output,
        elapsed: nd.elapsed,
        model: nd.model,
        reasoning_effort: nd.reasoning_effort,
        tokens: nd.tokens,
        input_tokens: nd.input_tokens,
        output_tokens: nd.output_tokens,
        cost_usd: nd.cost_usd,
        activity: nd.activity,
        error_type: nd.error_type,
        error_message: nd.error_message,
      });
    }

    nd.status = 'running';
    nd.iteration = data.iteration;
    nd.startedAt = timestamp ?? Date.now() / 1000;
    nd.activity = [];
    if (data.context_window_max != null) {
      nd.context_window_max = data.context_window_max;
    }
    nd.prompt = undefined;
    nd.output = undefined;
    nd.error_type = undefined;
    nd.error_message = undefined;
    replaceNode(t.nodes, data.agent_name);
  },

  agent_completed: (state, _data) => {
    const data = _data as unknown as AgentCompletedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'completed';
    t.incrCompleted();
    nd.elapsed = data.elapsed;
    nd.model = data.model;
    nd.tokens = data.tokens;
    nd.input_tokens = data.input_tokens;
    nd.output_tokens = data.output_tokens;
    nd.cost_usd = data.cost_usd;
    nd.output = data.output;
    nd.output_keys = data.output_keys;
    nd.context_window_used = data.context_window_used;
    nd.context_window_max = data.context_window_max;
    if (data.context_window_used != null && data.context_window_max != null && data.context_window_max > 0) {
      nd.context_pct = Math.round((data.context_window_used / data.context_window_max) * 100);
    }
    if (data.cost_usd) t.addCost(data.cost_usd);
    if (data.tokens) t.addTokens(data.tokens);
    // Capture terminate-step metadata when present (issue #219). The engine
    // emits these on agent_completed for `status: success` terminate steps so
    // the TerminateNode can render the rendered reason in its body.
    const extra = _data as Record<string, unknown>;
    if (extra.terminated_by) {
      nd.termination_status = (extra.status as 'success' | 'failed' | undefined) ?? 'success';
      nd.termination_reason = extra.termination_reason as string | undefined;
      nd.terminated_by = extra.terminated_by as string;
    }
    replaceNode(t.nodes, data.agent_name);
  },

  agent_failed: (state, _data) => {
    const data = _data as unknown as AgentFailedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
    for (const route of t.routes) {
      if (route.to === data.agent_name) {
        t.highlightedEdges.push({ from: route.from, to: route.to, state: 'failed' });
      }
    }
    // Capture terminate-step metadata when present (issue #219). For
    // `type: terminate status: failed`, the engine emits agent_failed with
    // the rendered termination fields so the TerminateNode can render the
    // reason directly without falling back to `error_message`.
    const extra = _data as Record<string, unknown>;
    if (extra.terminated_by) {
      nd.termination_status = (extra.status as 'success' | 'failed' | undefined) ?? 'failed';
      nd.termination_reason = extra.termination_reason as string | undefined;
      nd.terminated_by = extra.terminated_by as string;
    }
    replaceNode(t.nodes, data.agent_name);
  },

  agent_prompt_rendered: (state, _data) => {
    const data = _data as unknown as AgentPromptRenderedData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.prompt = data.rendered_prompt;
    nd.context_keys = data.context_keys;
    if (itemKey) {
      addForEachItemActivity(t.nodes, data.agent_name, itemKey, {
        type: 'prompt', icon: '📝', label: 'prompt', text: 'Prompt rendered',
        detail: data.rendered_prompt?.slice(0, 500) || null,
      });
      const itemNd = t.nodes[data.agent_name];
      if (itemNd?.for_each_items) {
        const item = itemNd.for_each_items.find((i) => i.key === itemKey);
        if (item) item.prompt = data.rendered_prompt;
      }
    }
    replaceNode(t.nodes, data.agent_name);
  },

  agent_reasoning: (state, _data) => {
    const data = _data as unknown as AgentReasoningData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const t = activeTarget(state, _data);
    const entry: ActivityEntry = { type: 'reasoning', icon: '💭', label: 'thinking', text: data.content };
    addActivity(t.nodes, data.agent_name, entry);
    if (itemKey) addForEachItemActivity(t.nodes, data.agent_name, itemKey, entry);
    replaceNode(t.nodes, data.agent_name);
  },

  agent_tool_start: (state, _data) => {
    const data = _data as unknown as AgentToolStartData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const t = activeTarget(state, _data);
    const entry: ActivityEntry = { type: 'tool-start', icon: '🔧', label: 'tool', text: data.tool_name, detail: data.arguments || null };
    addActivity(t.nodes, data.agent_name, entry);
    if (itemKey) addForEachItemActivity(t.nodes, data.agent_name, itemKey, entry);
    replaceNode(t.nodes, data.agent_name);
  },

  agent_tool_complete: (state, _data) => {
    const data = _data as unknown as AgentToolCompleteData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const t = activeTarget(state, _data);
    const entry: ActivityEntry = { type: 'tool-complete', icon: '✓', label: 'result', text: data.tool_name || 'done', detail: data.result || null };
    addActivity(t.nodes, data.agent_name, entry);
    if (itemKey) addForEachItemActivity(t.nodes, data.agent_name, itemKey, entry);
    replaceNode(t.nodes, data.agent_name);
  },

  agent_turn_start: (state, _data) => {
    const data = _data as unknown as AgentTurnStartData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const t = activeTarget(state, _data);
    const entry: ActivityEntry = { type: 'turn', icon: '⏳', label: 'turn', text: `Turn ${data.turn ?? '?'}` };
    addActivity(t.nodes, data.agent_name, entry);
    if (itemKey) addForEachItemActivity(t.nodes, data.agent_name, itemKey, entry);
    replaceNode(t.nodes, data.agent_name);
  },

  agent_message: (state, _data) => {
    const data = _data as unknown as AgentMessageData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.latest_message = data.content;
    replaceNode(t.nodes, data.agent_name);
  },

  script_started: (state, _data, timestamp) => {
    const data = _data as { agent_name: string };
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'running';
    nd.startedAt = timestamp ?? Date.now() / 1000;
    replaceNode(t.nodes, data.agent_name);
  },

  script_completed: (state, _data) => {
    const data = _data as unknown as ScriptCompletedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'completed';
    t.incrCompleted();
    nd.elapsed = data.elapsed;
    nd.stdout = data.stdout;
    nd.stderr = data.stderr;
    nd.exit_code = data.exit_code;
    replaceNode(t.nodes, data.agent_name);
  },

  script_failed: (state, _data) => {
    const data = _data as unknown as ScriptFailedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
    replaceNode(t.nodes, data.agent_name);
  },

  wait_started: (state, _data, timestamp) => {
    const data = _data as unknown as WaitStartedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'running';
    nd.startedAt = timestamp ?? Date.now() / 1000;
    nd.duration_seconds = data.duration_seconds ?? null;
    nd.reason = data.reason ?? null;
    nd.iteration = data.iteration;
    replaceNode(t.nodes, data.agent_name);
  },

  wait_completed: (state, _data) => {
    const data = _data as unknown as WaitCompletedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'completed';
    t.incrCompleted();
    nd.elapsed = data.elapsed;
    nd.waited_seconds = data.waited_seconds;
    nd.requested_seconds = data.requested_seconds;
    nd.reason = data.reason ?? null;
    nd.interrupted = data.interrupted;
    replaceNode(t.nodes, data.agent_name);
  },

  wait_failed: (state, _data) => {
    const data = _data as unknown as WaitFailedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
    replaceNode(t.nodes, data.agent_name);
  },

  set_started: (state, _data, timestamp) => {
    const data = _data as { agent_name: string };
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'running';
    nd.startedAt = timestamp ?? Date.now() / 1000;
    replaceNode(t.nodes, data.agent_name);
  },

  set_completed: (state, _data) => {
    const data = _data as unknown as SetCompletedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'completed';
    t.incrCompleted();
    nd.elapsed = data.elapsed;
    nd.set_output_type = data.output_type;
    nd.set_output_keys = data.output_keys;
    nd.set_value_repr = data.value_repr;
    replaceNode(t.nodes, data.agent_name);
  },

  set_failed: (state, _data) => {
    const data = _data as unknown as SetFailedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
    replaceNode(t.nodes, data.agent_name);
  },

  gate_presented: (state, _data) => {
    const data = _data as unknown as GatePresentedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'waiting';
    nd.options = data.options;
    nd.option_details = data.option_details;
    nd.prompt = data.prompt;
    replaceNode(t.nodes, data.agent_name);
  },

  gate_resolved: (state, _data) => {
    const data = _data as unknown as GateResolvedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'completed';
    t.incrCompleted();
    nd.selected_option = data.selected_option;
    nd.route = data.route;
    nd.additional_input = data.additional_input;
    replaceNode(t.nodes, data.agent_name);
  },

  route_taken: (state, _data) => {
    const data = _data as unknown as RouteTakenData;
    const t = activeTarget(state, _data);
    t.highlightedEdges.push({ from: data.from_agent, to: data.to_agent, state: 'taken' });
  },

  parallel_started: (state, _data) => {
    const data = _data as unknown as ParallelStartedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.group_name, 'parallel_group');
    nd.status = 'running';
    if (t.groupProgress[data.group_name]) {
      t.groupProgress[data.group_name]!.total = data.agents.length;
      t.groupProgress[data.group_name]!.completed = 0;
      t.groupProgress[data.group_name]!.failed = 0;
    }
    replaceNode(t.nodes, data.group_name);
  },

  parallel_agent_completed: (state, _data) => {
    const data = _data as unknown as ParallelAgentCompletedData;
    const t = activeTarget(state, _data);
    if (t.groupProgress[data.group_name]) {
      t.groupProgress[data.group_name]!.completed++;
    }
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'completed';
    nd.elapsed = data.elapsed;
    nd.model = data.model;
    nd.tokens = data.tokens;
    nd.cost_usd = data.cost_usd;
    nd.context_window_used = data.context_window_used;
    nd.context_window_max = data.context_window_max;
    if (data.context_window_used != null && data.context_window_max != null && data.context_window_max > 0) {
      nd.context_pct = Math.round((data.context_window_used / data.context_window_max) * 100);
    }
    if (data.cost_usd) t.addCost(data.cost_usd);
    if (data.tokens) t.addTokens(data.tokens);
    replaceNode(t.nodes, data.agent_name);
    replaceNode(t.nodes, data.group_name);
  },

  parallel_agent_failed: (state, _data) => {
    const data = _data as unknown as ParallelAgentFailedData;
    const t = activeTarget(state, _data);
    if (t.groupProgress[data.group_name]) {
      t.groupProgress[data.group_name]!.failed++;
    }
    const nd = ensureNode(t.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
    replaceNode(t.nodes, data.agent_name);
    replaceNode(t.nodes, data.group_name);
  },

  parallel_completed: (state, _data) => {
    const data = _data as unknown as ParallelCompletedData;
    const t = activeTarget(state, _data);
    t.incrCompleted();
    const nd = ensureNode(t.nodes, data.group_name, 'parallel_group');
    nd.status = data.failure_count === 0 ? 'completed' : 'failed';
    replaceNode(t.nodes, data.group_name);
  },

  for_each_started: (state, _data) => {
    const data = _data as unknown as ForEachStartedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.group_name, 'for_each_group');
    nd.status = 'running';
    nd.for_each_items = [];
    if (t.groupProgress[data.group_name]) {
      t.groupProgress[data.group_name]!.total = data.item_count;
      t.groupProgress[data.group_name]!.completed = 0;
      t.groupProgress[data.group_name]!.failed = 0;
    }
    replaceNode(t.nodes, data.group_name);
  },

  for_each_item_started: (state, _data) => {
    const data = _data as unknown as ForEachItemStartedData;
    const t = activeTarget(state, _data);
    const nd = ensureNode(t.nodes, data.group_name, 'for_each_group');
    if (!nd.for_each_items) nd.for_each_items = [];
    nd.for_each_items.push({
      key: data.item_key ?? String(data.index),
      index: data.index,
      status: 'running',
      activity: [],
    });
    replaceNode(t.nodes, data.group_name);
  },

  for_each_item_completed: (state, _data) => {
    const data = _data as unknown as ForEachItemCompletedData;
    const t = activeTarget(state, _data);
    if (t.groupProgress[data.group_name]) {
      t.groupProgress[data.group_name]!.completed++;
    }
    const nd = ensureNode(t.nodes, data.group_name, 'for_each_group');
    if (nd.for_each_items) {
      const itemKey = data.item_key ?? String(data.index);
      const item = nd.for_each_items.find((i) => i.key === itemKey);
      if (item) {
        item.status = 'completed';
        item.elapsed = data.elapsed;
        item.tokens = data.tokens;
        item.cost_usd = data.cost_usd;
        item.output = data.output;
      }
    }
    replaceNode(t.nodes, data.group_name);
  },

  for_each_item_failed: (state, _data) => {
    const data = _data as unknown as ForEachItemFailedData;
    const t = activeTarget(state, _data);
    if (t.groupProgress[data.group_name]) {
      t.groupProgress[data.group_name]!.failed++;
    }
    const nd = ensureNode(t.nodes, data.group_name, 'for_each_group');
    if (nd.for_each_items) {
      const itemKey = data.item_key ?? String(data.index);
      const item = nd.for_each_items.find((i) => i.key === itemKey);
      if (item) {
        item.status = 'failed';
        item.elapsed = data.elapsed;
        item.error_type = data.error_type;
        item.error_message = data.message;
      }
    }
    replaceNode(t.nodes, data.group_name);
  },

  for_each_completed: (state, _data) => {
    const data = _data as unknown as ForEachCompletedData;
    const t = activeTarget(state, _data);
    t.incrCompleted();
    const nd = ensureNode(t.nodes, data.group_name, 'for_each_group');
    nd.status = (data.failure_count ?? 0) === 0 ? 'completed' : 'failed';
    nd.elapsed = data.elapsed;
    nd.success_count = data.success_count;
    nd.failure_count = data.failure_count;
    replaceNode(t.nodes, data.group_name);
  },

  workflow_completed: (state, _data) => {
    state.wfDepth = Math.max(0, state.wfDepth - 1);
    if (state.wfDepth === 0) {
      // Root workflow completed
      const data = _data as { output?: unknown; is_explicit?: boolean; termination_reason?: string; terminated_by?: string; status?: string };
      state.workflowStatus = 'completed';
      state.isPaused = false;
      // Clear any iteration-limit gate that wasn't paired with a resolved
      // event (defense-in-depth — see issue #134).
      state.iterationLimitGate = null;
      state.workflowOutput = data.output ?? null;
      // Explicit-termination metadata (issue #219). When the root workflow
      // ended via `type: terminate status: success`, the engine attaches
      // these fields; surface them via `workflowTermination` so the success
      // banner can show the reason and terminate step name.
      if (data.is_explicit) {
        state.workflowTermination = {
          is_explicit: true,
          status: (data.status as 'success' | 'failed' | undefined) ?? 'success',
          termination_reason: data.termination_reason,
          terminated_by: data.terminated_by,
        };
      } else {
        state.workflowTermination = null;
      }
      if (state.nodes['$end']) {
        state.nodes['$end']!.status = 'completed';
        replaceNode(state.nodes, '$end');
      }
      if (state.nodes['$start']) {
        state.nodes['$start']!.status = 'completed';
        replaceNode(state.nodes, '$start');
      }
      state.highlightedEdges = [];
    } else {
      // Child workflow completed — locate via subworkflow_path (engine-supplied
      // when running concurrently) so we don't depend on activeContextPath,
      // which under concurrency may have been advanced past us by another
      // sibling start.
      const data = _data as { output?: unknown; subworkflow_path?: string[] };
      const ctx = data.subworkflow_path
        ? resolveSlotPath(state.subworkflowContexts, data.subworkflow_path)?.ctx
        : resolveContext(state.subworkflowContexts, state.activeContextPath);
      if (ctx) {
        ctx.status = 'completed';
        ctx.workflowOutput = data.output ?? null;
        if (ctx.nodes['$end']) ctx.nodes['$end']!.status = 'completed';
        if (ctx.nodes['$start']) ctx.nodes['$start']!.status = 'completed';
        ctx.highlightedEdges = [];
      }
      // activeContextPath restoration is handled by the subsequent
      // subworkflow_completed event (which carries parent_path).
    }
  },

  workflow_failed: (state, _data) => {
    state.wfDepth = Math.max(0, state.wfDepth - 1);
    const data = _data as { agent_name?: string; error_type?: string; message?: string; elapsed_seconds?: number; timeout_seconds?: number; current_agent?: string; subworkflow_path?: string[]; is_explicit?: boolean; termination_reason?: string; terminated_by?: string; status?: string };
    if (state.wfDepth === 0) {
      // Root workflow failed
      state.workflowStatus = 'failed';
      state.isPaused = false;
      // Clear any lingering iteration-limit gate so the StatusBar doesn't
      // keep an orphan banner around (defense-in-depth — see issue #134).
      state.iterationLimitGate = null;
      state.workflowFailedAgent = data.agent_name || null;
      if (data.agent_name && state.nodes[data.agent_name]) {
        state.nodes[data.agent_name]!.status = 'failed';
        replaceNode(state.nodes, data.agent_name);
        for (const route of state.routes) {
          if (route.to === data.agent_name) {
            state.highlightedEdges.push({ from: route.from, to: route.to, state: 'failed' });
          }
        }
      }
      state.workflowFailure = {
        error_type: data.error_type,
        message: data.message,
        elapsed_seconds: data.elapsed_seconds,
        timeout_seconds: data.timeout_seconds,
        current_agent: data.current_agent,
        // Issue #219: forward termination metadata so the error banner can
        // distinguish explicit terminate-step failures from generic crashes.
        termination_reason: data.termination_reason,
        terminated_by: data.terminated_by,
        is_explicit: data.is_explicit,
        status: data.status,
      };
      if (data.is_explicit) {
        state.workflowTermination = {
          is_explicit: true,
          status: (data.status as 'success' | 'failed' | undefined) ?? 'failed',
          termination_reason: data.termination_reason,
          terminated_by: data.terminated_by,
        };
      } else {
        state.workflowTermination = null;
      }
      if (state.nodes['$start']) {
        state.nodes['$start']!.status = 'completed';
        replaceNode(state.nodes, '$start');
      }
    } else {
      const ctx = data.subworkflow_path
        ? resolveSlotPath(state.subworkflowContexts, data.subworkflow_path)?.ctx
        : resolveContext(state.subworkflowContexts, state.activeContextPath);
      if (ctx) {
        ctx.status = 'failed';
        ctx.workflowFailure = { error_type: data.error_type, message: data.message };
      }
      // activeContextPath restoration is handled by subworkflow_failed.
    }
  },

  // --- Subworkflow lifecycle ---

  subworkflow_started: (state, _data) => {
    const data = _data as unknown as SubworkflowStartedData;
    // Slot key disambiguates concurrent for_each iterations of the same
    // group. Engines emit it explicitly; older engines fall back to
    // composing it here from agent_name + item_key.
    const slotKey =
      data.slot_key ??
      (data.item_key != null ? `${data.agent_name}[${data.item_key}]` : data.agent_name);

    const ctx = createSubworkflowContext(
      data.agent_name,
      data.iteration ?? 1,
      data.workflow,
      slotKey,
    );

    // Resolve parent strictly from engine-supplied parent_path when present,
    // else fall back to the legacy "current activeContextPath is parent"
    // heuristic (correct only for serial sub-workflows).
    let parentIndexPath: number[];
    if (data.parent_path !== undefined) {
      const resolved = resolveSlotPath(state.subworkflowContexts, data.parent_path);
      if (!resolved) return; // out-of-order arrival; tolerate
      parentIndexPath = resolved.indexPath;
    } else {
      parentIndexPath = state.activeContextPath;
    }

    // Capture sticky-follow intent BEFORE mutating activeContextPath.
    //
    // Disabled: previously, when the user's view was at the engine's live
    // edge (typical at first launch, when both paths are []), starting a
    // sub-workflow would auto-advance ``viewContextPath`` into the new
    // child. That made the dashboard land inside an iteration on first
    // open and follow whichever sub-workflow most recently started during
    // a for_each fan-out. Engine progress and user view are now decoupled:
    // ``activeContextPath`` still tracks the engine's cursor (used to
    // resolve ``parent_path`` for older engines that don't stamp it), but
    // the user's view stays where it is until they navigate explicitly.
    const wasAtLiveEdge = false;

    let newActivePath: number[];
    if (parentIndexPath.length === 0) {
      state.subworkflowContexts.push(ctx);
      newActivePath = [state.subworkflowContexts.length - 1];
    } else {
      const parent = resolveContext(state.subworkflowContexts, parentIndexPath);
      if (!parent) return;
      parent.children.push(ctx);
      newActivePath = [...parentIndexPath, parent.children.length - 1];
    }
    state.activeContextPath = newActivePath;
    if (wasAtLiveEdge) {
      // Advance the user's view along with the engine — they were following
      // the live edge, so a new gate inside this child should be reachable.
      state.viewContextPath = newActivePath;
    }

    // Mark the parent-side agent node as running so the graph reflects that
    // a sub-workflow is in flight.
    if (parentIndexPath.length === 0) {
      const nd = state.nodes[data.agent_name];
      if (nd) {
        nd.status = 'running';
        replaceNode(state.nodes, data.agent_name);
      }
    } else {
      const parentCtx = resolveContext(state.subworkflowContexts, parentIndexPath);
      if (parentCtx) {
        const nd = parentCtx.nodes[data.agent_name];
        if (nd) {
          nd.status = 'running';
          replaceNode(parentCtx.nodes, data.agent_name);
        }
      }
    }
  },

  subworkflow_completed: (state, _data) => {
    const data = _data as unknown as SubworkflowCompletedData;
    // Resolve the parent context (the one that owns the agent node) by
    // engine-supplied parent_path so concurrent completions land correctly.
    let parentIndexPath: number[];
    if (data.parent_path !== undefined) {
      const resolved = resolveSlotPath(state.subworkflowContexts, data.parent_path);
      // Mirror subworkflow_started: bail on resolution miss instead of
      // silently falling back to root. A null result here means the event
      // arrived before its sibling subworkflow_started or the path is
      // inconsistent. Falling back to [] would stamp 'completed' on a
      // same-named root agent and corrupt activeContextPath below.
      if (!resolved) return;
      parentIndexPath = resolved.indexPath;
    } else {
      parentIndexPath = state.activeContextPath;
    }

    const targetNodes =
      parentIndexPath.length === 0
        ? state.nodes
        : resolveContext(state.subworkflowContexts, parentIndexPath)?.nodes;
    if (targetNodes) {
      const nd = targetNodes[data.agent_name];
      if (nd) {
        // for_each-of-workflow emits one subworkflow_completed per iteration
        // but the parent agent node is the group, completed by for_each_completed.
        // Avoid double-incrementing or overwriting the group's status here.
        if (data.item_key == null) {
          nd.status = 'completed';
          nd.elapsed = data.elapsed;
          if (parentIndexPath.length === 0) {
            state.agentsCompleted++;
          } else {
            const parentCtx = resolveContext(state.subworkflowContexts, parentIndexPath);
            if (parentCtx) parentCtx.agentsCompleted++;
          }
        }
        replaceNode(targetNodes, data.agent_name);
      }
    }

    // Restore activeContextPath to the parent so the next root-level events
    // route correctly.
    state.activeContextPath = parentIndexPath;
  },

  subworkflow_failed: (state, _data) => {
    const data = _data as unknown as SubworkflowFailedData;
    let parentIndexPath: number[];
    if (data.parent_path !== undefined) {
      const resolved = resolveSlotPath(state.subworkflowContexts, data.parent_path);
      // See subworkflow_completed: bail on resolution miss to avoid
      // stamping 'failed' on an unrelated root node and clobbering
      // activeContextPath.
      if (!resolved) return;
      parentIndexPath = resolved.indexPath;
    } else {
      parentIndexPath = state.activeContextPath;
    }

    const targetNodes =
      parentIndexPath.length === 0
        ? state.nodes
        : resolveContext(state.subworkflowContexts, parentIndexPath)?.nodes;
    if (targetNodes) {
      const nd = targetNodes[data.agent_name];
      if (nd && data.item_key == null) {
        nd.status = 'failed';
        nd.elapsed = data.elapsed;
        nd.error_type = data.error_type;
        nd.error_message = data.message;
        replaceNode(targetNodes, data.agent_name);
      }
    }
    state.activeContextPath = parentIndexPath;
  },

  checkpoint_saved: (state, _data) => {
    const data = _data as { path?: string };
    if (data.path && state.workflowFailure) {
      state.workflowFailure = { ...state.workflowFailure, checkpoint_path: data.path };
    }
  },

  agent_paused: (state, _data) => {
    const data = _data as unknown as AgentPausedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'waiting';
    nd.activity.push({
      type: 'agent_paused',
      icon: '⏸',
      label: 'Paused',
      text: 'Agent paused — click Resume to re-execute',
    });
    replaceNode(state.nodes, data.agent_name);
    state.isPaused = true;
  },

  agent_resumed: (state, _data) => {
    const data = _data as unknown as AgentResumedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'running';
    nd.activity.push({
      type: 'agent_resumed',
      icon: '▶',
      label: 'Resumed',
      text: 'Agent resumed — re-executing',
    });
    replaceNode(state.nodes, data.agent_name);
    state.isPaused = false;
  },

  iteration_limit_reached: (state, _data) => {
    const data = _data as unknown as IterationLimitReachedData;
    // Reuse the event payload directly so the slice always tracks the
    // canonical shape (no inline type drift — see types/events.ts).
    state.iterationLimitGate = data;
    const target = data.agent_name ?? data.group_name;
    if (target) {
      const nd = ensureNode(state.nodes, target);
      nd.activity.push({
        type: 'iteration_limit_reached',
        icon: '⚠',
        label: 'Iteration limit',
        text: `Reached ${data.current_iteration}/${data.max_iterations} iterations — ${
          data.skip_gates ? 'auto-stopping (--skip-gates)' : 'awaiting decision'
        }`,
      });
      replaceNode(state.nodes, target);
    } else if (typeof console !== 'undefined') {
      console.warn(
        '[workflow-store] iteration_limit_reached event missing both agent_name and group_name',
        data,
      );
    }
  },

  iteration_limit_resolved: (state, _data) => {
    const data = _data as unknown as IterationLimitResolvedData;
    state.iterationLimitGate = null;
    const target = data.agent_name ?? data.group_name;
    if (target) {
      const nd = ensureNode(state.nodes, target);
      nd.activity.push({
        type: 'iteration_limit_resolved',
        icon: data.continue_execution ? '▶' : '■',
        label: 'Iteration limit',
        text: data.aborted
          ? 'Gate aborted unexpectedly — stopping workflow'
          : data.continue_execution
            ? `Continuing with ${data.additional_iterations} more iteration(s)`
            : 'Stopping workflow',
      });
      replaceNode(state.nodes, target);
    } else if (typeof console !== 'undefined') {
      console.warn(
        '[workflow-store] iteration_limit_resolved event missing both agent_name and group_name',
        data,
      );
    }
  },

  dialog_started: (state, _data) => {
    const data = _data as unknown as DialogStartedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.dialog_id = data.dialog_id;
    nd.dialog_messages = [];
    nd.dialog_active = true;
    nd.dialog_awaiting_response = false;
    state.activeDialog = { agentName: data.agent_name, dialogId: data.dialog_id };
    state.dialogEngaged = false;
    replaceNode(state.nodes, data.agent_name);
  },

  dialog_message: (state, _data) => {
    const data = _data as unknown as DialogMessageData;
    const nd = ensureNode(state.nodes, data.agent_name);
    if (!nd.dialog_messages) nd.dialog_messages = [];
    nd.dialog_messages.push({ role: data.role, content: data.content });
    // A user message means we're now waiting on the agent; an agent message
    // means we're not. Centralizing the flag here (instead of optimistically
    // toggling it in `sendDialogMessage`) keeps the state machine single-sourced.
    if (data.role === 'user') {
      nd.dialog_awaiting_response = true;
    } else if (data.role === 'agent') {
      nd.dialog_awaiting_response = false;
    }
    replaceNode(state.nodes, data.agent_name);
  },

  dialog_completed: (state, _data) => {
    const data = _data as unknown as DialogCompletedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.dialog_active = false;
    nd.dialog_awaiting_response = false;
    state.activeDialog = null;
    state.dialogEngaged = false;
    replaceNode(state.nodes, data.agent_name);
  },

  agent_validator_start: (state, _data) => {
    const data = _data as unknown as AgentValidatorStartData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const t = activeTarget(state, _data);
    const entry: ActivityEntry = {
      type: 'validator-start',
      icon: '🔎',
      label: 'validator',
      text: 'validating output',
      detail: data.criteria_preview || null,
    };
    addActivity(t.nodes, data.agent_name, entry);
    if (itemKey != null) {
      addForEachItemActivity(t.nodes, data.agent_name, String(itemKey), entry);
    } else {
      const nd = ensureNode(t.nodes, data.agent_name);
      nd.validator_state = 'running';
      nd.validator_model = data.model ?? null;
      nd.validator_attempts = (nd.validator_attempts ?? 0) + 1;
    }
    replaceNode(t.nodes, data.agent_name);
  },

  agent_validator_complete: (state, _data) => {
    const data = _data as unknown as AgentValidatorCompleteData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const t = activeTarget(state, _data);
    const verdict = data.errored ? 'error' : data.passed ? 'passed' : 'failed';
    const entry: ActivityEntry = {
      type: 'validator-complete',
      icon: data.passed ? '✅' : data.errored ? '⚠️' : '❌',
      label: 'validator',
      text: data.errored
        ? 'validation error (treated as pass)'
        : data.passed
          ? 'validation passed'
          : 'validation failed',
      detail: data.issues && data.issues.length ? data.issues.join('\n') : null,
    };
    addActivity(t.nodes, data.agent_name, entry);
    if (itemKey != null) {
      addForEachItemActivity(t.nodes, data.agent_name, String(itemKey), entry);
    } else {
      const nd = ensureNode(t.nodes, data.agent_name);
      nd.validator_state = verdict;
      nd.validator_passed = data.passed;
      nd.validator_issues = data.issues ?? [];
      nd.validator_errored = data.errored ?? false;
      nd.validator_cost_usd = data.cost_usd ?? null;
      nd.validator_model = data.model ?? nd.validator_model ?? null;
    }
    replaceNode(t.nodes, data.agent_name);
  },

  agent_validation_failed: (state, _data) => {
    const data = _data as unknown as AgentValidationFailedData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const t = activeTarget(state, _data);
    const entry: ActivityEntry = {
      type: 'validation-failed',
      icon: '❌',
      label: 'validator',
      text: data.will_retry ? 're-running once with feedback' : 'validation failed (no retry)',
      detail: data.issues && data.issues.length ? data.issues.join('\n') : null,
    };
    addActivity(t.nodes, data.agent_name, entry);
    if (itemKey != null) {
      addForEachItemActivity(t.nodes, data.agent_name, String(itemKey), entry);
    } else {
      const nd = ensureNode(t.nodes, data.agent_name);
      nd.validator_will_retry = data.will_retry;
      nd.validator_issues = data.issues ?? [];
    }
    replaceNode(t.nodes, data.agent_name);
  },
};

// --- Build log entries from events ---

function buildLogEntry(event: WorkflowEvent): LogEntry | null {
  const ts = event.timestamp;
  const d = event.data as Record<string, unknown>;

  switch (event.type) {
    case 'workflow_started':
      return { timestamp: ts, level: 'info', source: 'workflow', message: `Workflow "${d.name || ''}" started` };

    case 'agent_started':
      return { timestamp: ts, level: 'info', source: String(d.agent_name), message: `Agent started${d.iteration != null ? ` (iteration ${d.iteration})` : ''}` };

    case 'agent_completed':
      return {
        timestamp: ts, level: 'success', source: String(d.agent_name),
        message: `Agent completed${d.elapsed != null ? ` in ${formatSec(d.elapsed as number)}` : ''}${d.tokens != null ? ` · ${(d.tokens as number).toLocaleString()} tokens` : ''}${d.cost_usd != null ? ` · $${(d.cost_usd as number).toFixed(4)}` : ''}`,
      };

    case 'agent_failed':
      return { timestamp: ts, level: 'error', source: String(d.agent_name), message: `Agent failed: ${d.message || d.error_type || 'unknown error'}` };

    case 'script_started':
      return { timestamp: ts, level: 'info', source: String(d.agent_name), message: 'Script started' };

    case 'script_completed':
      return { timestamp: ts, level: 'success', source: String(d.agent_name), message: `Script completed (exit ${d.exit_code ?? '?'})${d.elapsed != null ? ` in ${formatSec(d.elapsed as number)}` : ''}` };

    case 'script_failed':
      return { timestamp: ts, level: 'error', source: String(d.agent_name), message: `Script failed: ${d.message || d.error_type || 'unknown error'}` };

    case 'wait_started': {
      const dur = d.duration_seconds as number | null | undefined;
      const reason = d.reason as string | null | undefined;
      const durStr = typeof dur === 'number' ? formatSec(dur) : '?';
      return {
        timestamp: ts,
        level: 'info',
        source: String(d.agent_name),
        message: `Waiting ${durStr}${reason ? ` — ${reason}` : ''}`,
      };
    }

    case 'wait_completed': {
      const waited = d.waited_seconds as number | undefined;
      const interrupted = d.interrupted as boolean | undefined;
      return {
        timestamp: ts,
        level: 'success',
        source: String(d.agent_name),
        message: `Wait completed${waited != null ? ` (${formatSec(waited)})` : ''}${interrupted ? ' — interrupted' : ''}`,
      };
    }

    case 'wait_failed':
      return { timestamp: ts, level: 'error', source: String(d.agent_name), message: `Wait failed: ${d.message || d.error_type || 'unknown error'}` };

    case 'set_started':
      return { timestamp: ts, level: 'info', source: String(d.agent_name), message: 'Set started' };

    case 'set_completed': {
      const keys = (d.output_keys as string[] | undefined) ?? [];
      const summary = keys.length > 0 ? ` · ${keys.join(', ')}` : '';
      return {
        timestamp: ts,
        level: 'success',
        source: String(d.agent_name),
        message: `Set completed${summary}${d.elapsed != null ? ` in ${formatSec(d.elapsed as number)}` : ''}`,
      };
    }

    case 'set_failed':
      return { timestamp: ts, level: 'error', source: String(d.agent_name), message: `Set failed: ${d.message || d.error_type || 'unknown error'}` };

    case 'gate_presented':
      return { timestamp: ts, level: 'warning', source: String(d.agent_name), message: 'Waiting for human input…' };

    case 'gate_resolved':
      return { timestamp: ts, level: 'success', source: String(d.agent_name), message: `Gate resolved → ${d.selected_option || 'continue'}` };

    case 'route_taken':
      return { timestamp: ts, level: 'debug', source: 'router', message: `${d.from_agent} → ${d.to_agent}` };

    case 'parallel_started':
      return { timestamp: ts, level: 'info', source: String(d.group_name), message: `Parallel group started (${(d.agents as string[])?.length || '?'} agents)` };

    case 'parallel_completed':
      return {
        timestamp: ts,
        level: (d.failure_count as number) === 0 ? 'success' : 'error',
        source: String(d.group_name),
        message: `Parallel group completed${(d.failure_count as number) > 0 ? ` with ${d.failure_count} failure(s)` : ''}`,
      };

    case 'for_each_started':
      return { timestamp: ts, level: 'info', source: String(d.group_name), message: `For-each started (${d.item_count} items)` };

    case 'for_each_completed':
      return {
        timestamp: ts,
        level: ((d.failure_count as number) ?? 0) === 0 ? 'success' : 'error',
        source: String(d.group_name),
        message: `For-each completed · ${d.success_count} succeeded${(d.failure_count as number) > 0 ? ` · ${d.failure_count} failed` : ''}`,
      };

    case 'workflow_completed':
      return { timestamp: ts, level: 'success', source: 'workflow', message: `Workflow completed${d.elapsed != null ? ` in ${formatSec(d.elapsed as number)}` : ''}` };

    case 'workflow_failed':
      return { timestamp: ts, level: 'error', source: 'workflow', message: `Workflow failed: ${d.message || d.error_type || 'unknown error'}` };

    case 'checkpoint_saved':
      return { timestamp: ts, level: 'info', source: 'workflow', message: `Checkpoint saved: ${(d.path as string)?.split('/').pop() || 'unknown'}` };

    case 'agent_paused':
      return { timestamp: ts, level: 'warning', source: String(d.agent_name), message: 'Agent paused — waiting for resume' };

    case 'agent_resumed':
      return { timestamp: ts, level: 'info', source: String(d.agent_name), message: 'Agent resumed — re-executing' };

    case 'iteration_limit_reached': {
      const target = (d.agent_name ?? d.group_name ?? 'workflow') as string;
      const auto = d.skip_gates ? ' — auto-stopping (--skip-gates)' : ' — awaiting decision';
      return {
        timestamp: ts,
        level: 'warning',
        source: String(target),
        message: `Iteration limit reached (${d.current_iteration}/${d.max_iterations})${auto}`,
      };
    }

    case 'iteration_limit_resolved': {
      const target = (d.agent_name ?? d.group_name ?? 'workflow') as string;
      const continued = Boolean(d.continue_execution);
      const additional = (d.additional_iterations as number) ?? 0;
      return {
        timestamp: ts,
        level: continued ? 'info' : 'warning',
        source: String(target),
        message: continued
          ? `Iteration limit resolved — continuing with ${additional} more`
          : 'Iteration limit resolved — stopping workflow',
      };
    }

    case 'dialog_started':
      return { timestamp: ts, level: 'warning', source: String(d.agent_name), message: 'Dialog started — waiting for user…' };

    case 'dialog_completed':
      return { timestamp: ts, level: 'success', source: String(d.agent_name), message: `Dialog completed (${d.turn_count || 0} messages)` };

    case 'agent_validator_start': {
      const src = d.item_key != null ? `${d.agent_name}[${d.item_key}]` : String(d.agent_name);
      return { timestamp: ts, level: 'info', source: src, message: 'Validating output…' };
    }

    case 'agent_validator_complete': {
      const src = d.item_key != null ? `${d.agent_name}[${d.item_key}]` : String(d.agent_name);
      if (d.errored) {
        return { timestamp: ts, level: 'warning', source: src, message: 'Validator error — treated as pass' };
      }
      if (d.passed) {
        return { timestamp: ts, level: 'success', source: src, message: 'Validation passed' };
      }
      const issues = Array.isArray(d.issues) ? d.issues.length : 0;
      return { timestamp: ts, level: 'warning', source: src, message: `Validation failed (${issues} issue${issues === 1 ? '' : 's'})` };
    }

    case 'agent_validation_failed': {
      const src = d.item_key != null ? `${d.agent_name}[${d.item_key}]` : String(d.agent_name);
      const action = d.will_retry ? 're-running once with feedback' : 'no retry';
      return { timestamp: ts, level: 'warning', source: src, message: `Validation failed — ${action}` };
    }

    // Skip high-frequency streaming events from the log
    default:
      return null;
  }
}

function formatSec(s: number): string {
  if (s < 1) return `${(s * 1000).toFixed(0)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const sec = (s % 60).toFixed(0);
  return `${m}m ${sec}s`;
}

function buildActivityLogEntry(event: WorkflowEvent): ActivityLogEntry | null {
  const ts = event.timestamp;
  const d = event.data as Record<string, unknown>;

  switch (event.type) {
    case 'agent_started':
      return { timestamp: ts, source: String(d.agent_name), type: 'turn', message: `Agent started${d.iteration != null ? ` (iteration ${d.iteration})` : ''}` };

    case 'agent_prompt_rendered':
      return {
        timestamp: ts, source: String(d.agent_name), type: 'prompt',
        message: 'Prompt rendered',
        detail: truncate(String(d.rendered_prompt || ''), 500),
      };

    case 'agent_reasoning':
      return { timestamp: ts, source: String(d.agent_name), type: 'reasoning', message: String(d.content || '') };

    case 'agent_tool_start':
      return {
        timestamp: ts, source: String(d.agent_name), type: 'tool-start',
        message: `→ ${d.tool_name}`,
        detail: d.arguments ? truncate(String(d.arguments), 300) : null,
      };

    case 'agent_tool_complete':
      return {
        timestamp: ts, source: String(d.agent_name), type: 'tool-complete',
        message: `← ${d.tool_name || 'done'}`,
        detail: d.result ? truncate(String(d.result), 300) : null,
      };

    case 'agent_turn_start':
      return { timestamp: ts, source: String(d.agent_name), type: 'turn', message: `Turn ${d.turn ?? '?'}` };

    case 'agent_message':
      return { timestamp: ts, source: String(d.agent_name), type: 'message', message: truncate(String(d.content || ''), 500) };

    case 'agent_completed':
      return {
        timestamp: ts, source: String(d.agent_name), type: 'turn',
        message: `Completed${d.elapsed != null ? ` in ${formatSec(d.elapsed as number)}` : ''}${d.tokens != null ? ` · ${(d.tokens as number).toLocaleString()} tokens` : ''}`,
      };

    case 'agent_failed':
      return { timestamp: ts, source: String(d.agent_name), type: 'turn', message: `Failed: ${d.message || d.error_type || 'unknown'}` };

    case 'script_started':
      return { timestamp: ts, source: String(d.agent_name), type: 'turn', message: 'Script started' };

    case 'script_completed':
      return {
        timestamp: ts, source: String(d.agent_name), type: 'tool-complete',
        message: `Script completed (exit ${d.exit_code ?? '?'})`,
        detail: d.stdout ? truncate(String(d.stdout), 300) : null,
      };

    case 'script_failed':
      return { timestamp: ts, source: String(d.agent_name), type: 'turn', message: `Script failed: ${d.message || d.error_type || 'unknown'}` };

    case 'wait_started': {
      const dur = d.duration_seconds as number | null | undefined;
      const reason = d.reason as string | null | undefined;
      const durStr = typeof dur === 'number' ? formatSec(dur) : '?';
      return {
        timestamp: ts,
        source: String(d.agent_name),
        type: 'turn',
        message: `Waiting ${durStr}${reason ? ` — ${reason}` : ''}`,
      };
    }

    case 'wait_completed': {
      const waited = d.waited_seconds as number | undefined;
      const interrupted = d.interrupted as boolean | undefined;
      return {
        timestamp: ts,
        source: String(d.agent_name),
        type: 'tool-complete',
        message: `Wait completed${waited != null ? ` (${formatSec(waited)})` : ''}${interrupted ? ' — interrupted' : ''}`,
      };
    }

    case 'wait_failed':
      return { timestamp: ts, source: String(d.agent_name), type: 'turn', message: `Wait failed: ${d.message || d.error_type || 'unknown'}` };

    case 'set_started':
      return { timestamp: ts, source: String(d.agent_name), type: 'turn', message: 'Set started' };

    case 'set_completed': {
      const keys = (d.output_keys as string[] | undefined) ?? [];
      const summary = keys.length > 0 ? ` (${keys.join(', ')})` : '';
      return {
        timestamp: ts,
        source: String(d.agent_name),
        type: 'tool-complete',
        message: `Set completed${summary}`,
        detail: d.value_repr ? truncate(String(d.value_repr), 300) : null,
      };
    }

    case 'set_failed':
      return { timestamp: ts, source: String(d.agent_name), type: 'turn', message: `Set failed: ${d.message || d.error_type || 'unknown'}` };

    default:
      return null;
  }
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + '…';
}
