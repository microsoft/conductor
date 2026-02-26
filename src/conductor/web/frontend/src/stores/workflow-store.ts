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
  RouteTakenData,
  ParallelStartedData,
  ParallelAgentCompletedData,
  ParallelAgentFailedData,
  ParallelCompletedData,
  ForEachStartedData,
  ForEachItemCompletedData,
  ForEachItemFailedData,
  ForEachCompletedData,
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
  selected_option?: string;
  route?: string;
  additional_input?: string;
  // Group-specific
  success_count?: number;
  failure_count?: number;
  // Activity
  activity: ActivityEntry[];
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
  state: 'highlighted' | 'taken';
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
  workflowFailure: { error_type?: string; message?: string } | null;
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

  // Actions
  processEvent: (event: WorkflowEvent) => void;
  replayState: (events: WorkflowEvent[]) => void;
  selectNode: (name: string | null) => void;
  setWsStatus: (status: WsStatus) => void;
  setEdgeHighlight: (from: string, to: string, state: 'highlighted' | 'taken') => void;
  clearEdgeHighlight: (from: string, to: string) => void;
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

export const useWorkflowStore = create<WorkflowState>((set) => ({
  workflowName: '',
  workflowStatus: 'pending',
  workflowStartTime: null,
  workflowFailure: null,
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

  processEvent: (event: WorkflowEvent) => {
    const handler = eventHandlers[event.type];
    if (handler) {
      set((state) => {
        const newState = { ...state, nodes: { ...state.nodes }, groupProgress: { ...state.groupProgress }, eventLog: [...state.eventLog], activityLog: [...state.activityLog] };
        handler(newState, event.data);
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
    }
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
      };
      for (const event of events) {
        const handler = eventHandlers[event.type];
        if (handler) {
          handler(newState, event.data);
        }
        const logEntry = buildLogEntry(event);
        if (logEntry) {
          newState.eventLog.push(logEntry);
        }
        const activityEntry = buildActivityLogEntry(event);
        if (activityEntry) {
          newState.activityLog.push(activityEntry);
        }
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

  setEdgeHighlight: (from: string, to: string, state: 'highlighted' | 'taken') => {
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

const eventHandlers: Record<string, (state: MutableState, data: Record<string, unknown>) => void> = {
  workflow_started: (state, _data) => {
    const data = _data as unknown as WorkflowStartedData;
    state.workflowStatus = 'running';
    state.workflowStartTime = Date.now() / 1000;
    state.workflowName = data.name || '';
    state.entryPoint = data.entry_point || null;
    state.agents = data.agents || [];
    state.routes = data.routes || [];
    state.parallelGroups = data.parallel_groups || [];
    state.forEachGroups = data.for_each_groups || [];

    // Set $start node to running
    ensureNode(state.nodes, '$start', 'start');
    state.nodes['$start']!.status = 'running';

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

  agent_started: (state, _data) => {
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
    nd.activity = [];
    // Clear stale fields from previous iteration
    nd.prompt = undefined;
    nd.output = undefined;
    nd.error_type = undefined;
    nd.error_message = undefined;
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
  },

  agent_failed: (state, _data) => {
    const data = _data as unknown as AgentFailedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
  },

  agent_prompt_rendered: (state, _data) => {
    const data = _data as unknown as AgentPromptRenderedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.prompt = data.rendered_prompt;
    nd.context_keys = data.context_keys;
  },

  agent_reasoning: (state, _data) => {
    const data = _data as unknown as AgentReasoningData;
    addActivity(state.nodes, data.agent_name, {
      type: 'reasoning',
      icon: '💭',
      label: 'thinking',
      text: data.content,
    });
  },

  agent_tool_start: (state, _data) => {
    const data = _data as unknown as AgentToolStartData;
    addActivity(state.nodes, data.agent_name, {
      type: 'tool-start',
      icon: '🔧',
      label: 'tool',
      text: data.tool_name,
      detail: data.arguments || null,
    });
  },

  agent_tool_complete: (state, _data) => {
    const data = _data as unknown as AgentToolCompleteData;
    addActivity(state.nodes, data.agent_name, {
      type: 'tool-complete',
      icon: '✓',
      label: 'result',
      text: data.tool_name || 'done',
      detail: data.result || null,
    });
  },

  agent_turn_start: (state, _data) => {
    const data = _data as unknown as AgentTurnStartData;
    addActivity(state.nodes, data.agent_name, {
      type: 'turn',
      icon: '⏳',
      label: 'turn',
      text: `Turn ${data.turn ?? '?'}`,
    });
  },

  agent_message: (state, _data) => {
    const data = _data as unknown as AgentMessageData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.latest_message = data.content;
  },

  script_started: (state, _data) => {
    const data = _data as { agent_name: string };
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'running';
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
  },

  script_failed: (state, _data) => {
    const data = _data as unknown as ScriptFailedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'failed';
    nd.elapsed = data.elapsed;
    nd.error_type = data.error_type;
    nd.error_message = data.message;
  },

  gate_presented: (state, _data) => {
    const data = _data as unknown as GatePresentedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'waiting';
    nd.options = data.options;
    nd.prompt = data.prompt;
  },

  gate_resolved: (state, _data) => {
    const data = _data as unknown as GateResolvedData;
    const nd = ensureNode(state.nodes, data.agent_name);
    nd.status = 'completed';
    state.agentsCompleted++;
    nd.selected_option = data.selected_option;
    nd.route = data.route;
    nd.additional_input = data.additional_input;
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
  },

  parallel_completed: (state, _data) => {
    const data = _data as unknown as ParallelCompletedData;
    state.agentsCompleted++;
    const nd = ensureNode(state.nodes, data.group_name, 'parallel_group');
    nd.status = data.failure_count === 0 ? 'completed' : 'failed';
  },

  for_each_started: (state, _data) => {
    const data = _data as unknown as ForEachStartedData;
    const nd = ensureNode(state.nodes, data.group_name, 'for_each_group');
    nd.status = 'running';
    if (state.groupProgress[data.group_name]) {
      state.groupProgress[data.group_name]!.total = data.item_count;
      state.groupProgress[data.group_name]!.completed = 0;
      state.groupProgress[data.group_name]!.failed = 0;
    }
  },

  for_each_item_started: (_state, _data) => {
    // No-op for now
  },

  for_each_item_completed: (state, _data) => {
    const data = _data as unknown as ForEachItemCompletedData;
    if (state.groupProgress[data.group_name]) {
      state.groupProgress[data.group_name]!.completed++;
    }
  },

  for_each_item_failed: (state, _data) => {
    const data = _data as unknown as ForEachItemFailedData;
    if (state.groupProgress[data.group_name]) {
      state.groupProgress[data.group_name]!.failed++;
    }
  },

  for_each_completed: (state, _data) => {
    const data = _data as unknown as ForEachCompletedData;
    state.agentsCompleted++;
    const nd = ensureNode(state.nodes, data.group_name, 'for_each_group');
    nd.status = (data.failure_count ?? 0) === 0 ? 'completed' : 'failed';
    nd.elapsed = data.elapsed;
    nd.success_count = data.success_count;
    nd.failure_count = data.failure_count;
  },

  workflow_completed: (state, _data) => {
    const data = _data as { output?: unknown };
    state.workflowStatus = 'completed';
    state.workflowOutput = data.output ?? null;
    if (state.nodes['$end']) {
      state.nodes['$end']!.status = 'completed';
    }
    if (state.nodes['$start']) {
      state.nodes['$start']!.status = 'completed';
    }
    // Clear flowing-dot edge animations now that workflow is done
    state.highlightedEdges = [];
  },

  workflow_failed: (state, _data) => {
    const data = _data as { agent_name?: string; error_type?: string; message?: string };
    state.workflowStatus = 'failed';
    if (data.agent_name && state.nodes[data.agent_name]) {
      state.nodes[data.agent_name]!.status = 'failed';
    }
    state.workflowFailure = { error_type: data.error_type, message: data.message };
    if (state.nodes['$start']) {
      state.nodes['$start']!.status = 'completed';
    }
    // Clear flowing-dot edge animations now that workflow is done
    state.highlightedEdges = [];
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
