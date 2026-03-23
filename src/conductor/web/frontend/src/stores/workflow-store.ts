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

  // Actions
  processEvent: (event: WorkflowEvent) => void;
  replayState: (events: WorkflowEvent[]) => void;
  selectNode: (name: string | null) => void;
  setWsStatus: (status: WsStatus) => void;
  setEdgeHighlight: (from: string, to: string, state: 'highlighted' | 'taken' | 'failed') => void;
  clearEdgeHighlight: (from: string, to: string) => void;

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

export const useWorkflowStore = create<WorkflowState>((set) => ({
  workflowName: '',
  workflowStatus: 'pending',
  workflowStartTime: null,
  workflowFailure: null,
  workflowFailedAgent: null,
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
        // Track timestamp of the last replayed event
        newState.lastEventTime = event.timestamp;
      }
      return newState;
    });
  },

  selectNode: (name: string | null) => {
    set({ selectedNode: name });
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
}));

// --- Event handlers (mutate the passed state directly) ---

type MutableState = WorkflowState;

const eventHandlers: Record<string, (state: MutableState, data: Record<string, unknown>, timestamp?: number) => void> = {
  workflow_started: (state, _data, timestamp) => {
    const data = _data as unknown as WorkflowStartedData;
    state.workflowStatus = 'running';
    state.workflowStartTime = timestamp ?? Date.now() / 1000;
    state.workflowName = data.name || '';
    state.entryPoint = data.entry_point || null;
    state.agents = data.agents || [];
    state.routes = data.routes || [];
    state.parallelGroups = data.parallel_groups || [];
    state.forEachGroups = data.for_each_groups || [];

    // Set $start node to running
    ensureNode(state.nodes, '$start', 'start');
    state.nodes['$start']!.status = 'running';
    replaceNode(state.nodes, '$start');

    const groupAgents = new Set<string>();
    const agentNames = new Set<string>();

    // Register parallel group agents
    for (const pg of state.parallelGroups) {
      for (const a of pg.agents) {
        groupAgents.add(a);
      }
      agentNames.add(pg.name);
      ensureNode(state.nodes, pg.name, 'parallel_group');
      state.groupProgress[pg.name] = { total: pg.agents.length, completed: 0, failed: 0 };
      for (const agentName of pg.agents) {
        ensureNode(state.nodes, agentName, 'agent');
      }
    }

    // Register for-each groups
    for (const fg of state.forEachGroups) {
      agentNames.add(fg.name);
      ensureNode(state.nodes, fg.name, 'for_each_group');
      state.groupProgress[fg.name] = { total: 0, completed: 0, failed: 0 };
    }

    // Register standalone agents
    for (const a of state.agents) {
      if (!agentNames.has(a.name) && !groupAgents.has(a.name)) {
        const nodeType = (a.type || 'agent') as NodeType;
        ensureNode(state.nodes, a.name, nodeType);
        if (a.model) state.nodes[a.name]!.model = a.model;
        agentNames.add(a.name);
      }
    }

    state.agentsTotal = agentNames.size;
  },

  agent_started: (state, _data, timestamp) => {
    const data = _data as unknown as AgentStartedData;
    const nd = ensureNode(state.nodes, data.agent_name);

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
    // Clear stale fields from previous iteration
    nd.prompt = undefined;
    nd.output = undefined;
    nd.error_type = undefined;
    nd.error_message = undefined;
    replaceNode(state.nodes, data.agent_name);
  },

  agent_completed: (state, _data) => {
    const data = _data as unknown as AgentCompletedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'completed';
    state.agentsCompleted++;
    nd.elapsed = data.elapsed;
    nd.model = data.model;
    nd.tokens = data.tokens;
    nd.input_tokens = data.input_tokens;
    nd.output_tokens = data.output_tokens;
    nd.cost_usd = data.cost_usd;
    nd.output = data.output;
    nd.output_keys = data.output_keys;
    if (data.cost_usd) state.totalCost += data.cost_usd;
    if (data.tokens) state.totalTokens += data.tokens;
    replaceNode(state.nodes, data.agent_name);
  },

  agent_failed: (state, _data) => {
    const data = _data as unknown as AgentFailedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
    // Highlight edges leading to the failed agent in red
    for (const route of state.routes) {
      if (route.to === data.agent_name) {
        state.highlightedEdges = [
          ...state.highlightedEdges.filter(
            (e) => !(e.from === route.from && e.to === route.to)
          ),
          { from: route.from, to: route.to, state: 'failed' },
        ];
      }
    }
    replaceNode(state.nodes, data.agent_name);
  },

  agent_prompt_rendered: (state, _data) => {
    const data = _data as unknown as AgentPromptRenderedData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.prompt = data.rendered_prompt;
    nd.context_keys = data.context_keys;
    // Route to per-item data when item_key is present (for-each)
    if (itemKey) {
      addForEachItemActivity(state.nodes, data.agent_name, itemKey, {
        type: 'prompt',
        icon: '📝',
        label: 'prompt',
        text: 'Prompt rendered',
        detail: data.rendered_prompt?.slice(0, 500) || null,
      });
      const itemNd = state.nodes[data.agent_name];
      if (itemNd?.for_each_items) {
        const item = itemNd.for_each_items.find((i) => i.key === itemKey);
        if (item) item.prompt = data.rendered_prompt;
      }
    }
    replaceNode(state.nodes, data.agent_name);
  },

  agent_reasoning: (state, _data) => {
    const data = _data as unknown as AgentReasoningData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const entry: ActivityEntry = {
      type: 'reasoning',
      icon: '💭',
      label: 'thinking',
      text: data.content,
    };
    addActivity(state.nodes, data.agent_name, entry);
    if (itemKey) {
      addForEachItemActivity(state.nodes, data.agent_name, itemKey, entry);
    }
    replaceNode(state.nodes, data.agent_name);
  },

  agent_tool_start: (state, _data) => {
    const data = _data as unknown as AgentToolStartData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const entry: ActivityEntry = {
      type: 'tool-start',
      icon: '🔧',
      label: 'tool',
      text: data.tool_name,
      detail: data.arguments || null,
    };
    addActivity(state.nodes, data.agent_name, entry);
    if (itemKey) {
      addForEachItemActivity(state.nodes, data.agent_name, itemKey, entry);
    }
    replaceNode(state.nodes, data.agent_name);
  },

  agent_tool_complete: (state, _data) => {
    const data = _data as unknown as AgentToolCompleteData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const entry: ActivityEntry = {
      type: 'tool-complete',
      icon: '✓',
      label: 'result',
      text: data.tool_name || 'done',
      detail: data.result || null,
    };
    addActivity(state.nodes, data.agent_name, entry);
    if (itemKey) {
      addForEachItemActivity(state.nodes, data.agent_name, itemKey, entry);
    }
    replaceNode(state.nodes, data.agent_name);
  },

  agent_turn_start: (state, _data) => {
    const data = _data as unknown as AgentTurnStartData;
    const itemKey = (_data as Record<string, unknown>).item_key as string | undefined;
    const entry: ActivityEntry = {
      type: 'turn',
      icon: '⏳',
      label: 'turn',
      text: `Turn ${data.turn ?? '?'}`,
    };
    addActivity(state.nodes, data.agent_name, entry);
    if (itemKey) {
      addForEachItemActivity(state.nodes, data.agent_name, itemKey, entry);
    }
    replaceNode(state.nodes, data.agent_name);
  },

  agent_message: (state, _data) => {
    const data = _data as unknown as AgentMessageData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.latest_message = data.content;
    replaceNode(state.nodes, data.agent_name);
  },

  script_started: (state, _data, timestamp) => {
    const data = _data as { agent_name: string };
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'running';
    nd.startedAt = timestamp ?? Date.now() / 1000;
    replaceNode(state.nodes, data.agent_name);
  },

  script_completed: (state, _data) => {
    const data = _data as unknown as ScriptCompletedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'completed';
    state.agentsCompleted++;
    nd.elapsed = data.elapsed;
    nd.stdout = data.stdout;
    nd.stderr = data.stderr;
    nd.exit_code = data.exit_code;
    replaceNode(state.nodes, data.agent_name);
  },

  script_failed: (state, _data) => {
    const data = _data as unknown as ScriptFailedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
    replaceNode(state.nodes, data.agent_name);
  },

  gate_presented: (state, _data) => {
    const data = _data as unknown as GatePresentedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'waiting';
    nd.options = data.options;
    nd.option_details = data.option_details;
    nd.prompt = data.prompt;
    replaceNode(state.nodes, data.agent_name);
  },

  gate_resolved: (state, _data) => {
    const data = _data as unknown as GateResolvedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'completed';
    state.agentsCompleted++;
    nd.selected_option = data.selected_option;
    nd.route = data.route;
    nd.additional_input = data.additional_input;
    replaceNode(state.nodes, data.agent_name);
  },

  route_taken: (state, _data) => {
    const data = _data as unknown as RouteTakenData;
    // Set edge highlight — the component will handle animation timing
    state.highlightedEdges = [
      ...state.highlightedEdges.filter(
        (e) => !(e.from === data.from_agent && e.to === data.to_agent)
      ),
      { from: data.from_agent, to: data.to_agent, state: 'taken' },
    ];
  },

  parallel_started: (state, _data) => {
    const data = _data as unknown as ParallelStartedData;
    const nd = ensureNode(state.nodes, data.group_name, 'parallel_group');
    nd.status = 'running';
    if (state.groupProgress[data.group_name]) {
      state.groupProgress[data.group_name]!.total = data.agents.length;
      state.groupProgress[data.group_name]!.completed = 0;
      state.groupProgress[data.group_name]!.failed = 0;
    }
    replaceNode(state.nodes, data.group_name);
  },

  parallel_agent_completed: (state, _data) => {
    const data = _data as unknown as ParallelAgentCompletedData;
    if (state.groupProgress[data.group_name]) {
      state.groupProgress[data.group_name]!.completed++;
    }
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'completed';
    nd.elapsed = data.elapsed;
    nd.model = data.model;
    nd.tokens = data.tokens;
    nd.cost_usd = data.cost_usd;
    if (data.cost_usd) state.totalCost += data.cost_usd;
    if (data.tokens) state.totalTokens += data.tokens;
    replaceNode(state.nodes, data.agent_name);
    replaceNode(state.nodes, data.group_name);
  },

  parallel_agent_failed: (state, _data) => {
    const data = _data as unknown as ParallelAgentFailedData;
    if (state.groupProgress[data.group_name]) {
      state.groupProgress[data.group_name]!.failed++;
    }
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
    replaceNode(state.nodes, data.agent_name);
    replaceNode(state.nodes, data.group_name);
  },

  parallel_completed: (state, _data) => {
    const data = _data as unknown as ParallelCompletedData;
    state.agentsCompleted++;
    const nd = ensureNode(state.nodes, data.group_name, 'parallel_group');
    nd.status = data.failure_count === 0 ? 'completed' : 'failed';
    replaceNode(state.nodes, data.group_name);
  },

  for_each_started: (state, _data) => {
    const data = _data as unknown as ForEachStartedData;
    const nd = ensureNode(state.nodes, data.group_name, 'for_each_group');
    nd.status = 'running';
    nd.for_each_items = [];
    if (state.groupProgress[data.group_name]) {
      state.groupProgress[data.group_name]!.total = data.item_count;
      state.groupProgress[data.group_name]!.completed = 0;
      state.groupProgress[data.group_name]!.failed = 0;
    }
    replaceNode(state.nodes, data.group_name);
  },

  for_each_item_started: (state, _data) => {
    const data = _data as unknown as ForEachItemStartedData;
    const nd = ensureNode(state.nodes, data.group_name, 'for_each_group');
    if (!nd.for_each_items) nd.for_each_items = [];
    nd.for_each_items.push({
      key: data.item_key ?? String(data.index),
      index: data.index,
      status: 'running',
      activity: [],
    });
    replaceNode(state.nodes, data.group_name);
  },

  for_each_item_completed: (state, _data) => {
    const data = _data as unknown as ForEachItemCompletedData;
    if (state.groupProgress[data.group_name]) {
      state.groupProgress[data.group_name]!.completed++;
    }
    const nd = ensureNode(state.nodes, data.group_name, 'for_each_group');
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
    replaceNode(state.nodes, data.group_name);
  },

  for_each_item_failed: (state, _data) => {
    const data = _data as unknown as ForEachItemFailedData;
    if (state.groupProgress[data.group_name]) {
      state.groupProgress[data.group_name]!.failed++;
    }
    const nd = ensureNode(state.nodes, data.group_name, 'for_each_group');
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
    replaceNode(state.nodes, data.group_name);
  },

  for_each_completed: (state, _data) => {
    const data = _data as unknown as ForEachCompletedData;
    state.agentsCompleted++;
    const nd = ensureNode(state.nodes, data.group_name, 'for_each_group');
    nd.status = (data.failure_count ?? 0) === 0 ? 'completed' : 'failed';
    nd.elapsed = data.elapsed;
    nd.success_count = data.success_count;
    nd.failure_count = data.failure_count;
    replaceNode(state.nodes, data.group_name);
  },

  workflow_completed: (state, _data) => {
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
    // Clear flowing-dot edge animations now that workflow is done
    state.highlightedEdges = [];
  },

  workflow_failed: (state, _data) => {
    const data = _data as { agent_name?: string; error_type?: string; message?: string; elapsed_seconds?: number; timeout_seconds?: number; current_agent?: string };
    state.workflowStatus = 'failed';
    state.isPaused = false;
    state.workflowFailedAgent = data.agent_name || null;
    if (data.agent_name && state.nodes[data.agent_name]) {
      state.nodes[data.agent_name]!.status = 'failed';
      replaceNode(state.nodes, data.agent_name);
      // Highlight edges leading to the failed agent in red
      for (const route of state.routes) {
        if (route.to === data.agent_name) {
          state.highlightedEdges = [
            ...state.highlightedEdges.filter(
              (e) => !(e.from === route.from && e.to === route.to)
            ),
            { from: route.from, to: route.to, state: 'failed' },
          ];
        }
      }
    }
    state.workflowFailure = { error_type: data.error_type, message: data.message, elapsed_seconds: data.elapsed_seconds, timeout_seconds: data.timeout_seconds, current_agent: data.current_agent };
    if (state.nodes['$start']) {
      state.nodes['$start']!.status = 'completed';
      replaceNode(state.nodes, '$start');
    }
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
