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
  SubworkflowStartedData,
  SubworkflowCompletedData,
  SubworkflowFailedData,
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
}

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
  workflowFailure: { error_type?: string; message?: string; elapsed_seconds?: number; timeout_seconds?: number; current_agent?: string; checkpoint_path?: string } | null;
  workflowFailedAgent: string | null;
  workflowYaml: string | null;
  conductorVersion: string | null;
  entryPoint: string | null;

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
        lastEventTime: null,
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
 * When the event payload carries an explicit `subworkflow_path` (auto-stamped
 * by sub-workflow engines), that path is used to resolve the owning context —
 * essential for routing per-iteration agent events under concurrent
 * for-each-of-workflow execution, where a single shared `activeContextPath`
 * cannot represent the multiple in-flight sibling contexts. Falls back to
 * `activeContextPath` for legacy events and root-engine events.
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
  } else if (state.activeContextPath.length > 0) {
    ctx = resolveContext(state.subworkflowContexts, state.activeContextPath);
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
          agentNames.add(a.name);
        }
      }
      state.agentsTotal = agentNames.size;
    } else {
      // Child workflow — populate the active child context
      const ctx = resolveContext(state.subworkflowContexts, state.activeContextPath);
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
      const data = _data as { output?: unknown };
      state.workflowStatus = 'completed';
      state.isPaused = false;
      state.workflowOutput = data.output ?? null;
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
    const data = _data as { agent_name?: string; error_type?: string; message?: string; elapsed_seconds?: number; timeout_seconds?: number; current_agent?: string; subworkflow_path?: string[] };
    if (state.wfDepth === 0) {
      // Root workflow failed
      state.workflowStatus = 'failed';
      state.isPaused = false;
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
      state.workflowFailure = { error_type: data.error_type, message: data.message, elapsed_seconds: data.elapsed_seconds, timeout_seconds: data.timeout_seconds, current_agent: data.current_agent };
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
    const wasAtLiveEdge =
      state.viewContextPath.length === state.activeContextPath.length &&
      state.viewContextPath.every((v, i) => v === state.activeContextPath[i]);

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
      parentIndexPath = resolved?.indexPath ?? [];
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
      parentIndexPath = resolved?.indexPath ?? [];
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

    default:
      return null;
  }
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + '…';
}
