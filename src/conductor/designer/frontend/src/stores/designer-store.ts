/**
 * Zustand store for the designer.
 *
 * Source of truth: a `WorkflowConfig` document.
 * ReactFlow nodes/edges are *derived* via selectors.
 * Undo/redo is snapshot-based.
 */

import { create } from 'zustand';
import type {
  WorkflowConfig,
  AgentDef,
  ParallelGroup,
  ForEachDef,
  ValidationResult,
  DesignerNode,
  DesignerEdge,
} from '@/types/designer';
import { configToGraph } from '@/lib/config-to-graph';

// ── Helpers ────────────────────────────────────────────────────────

const DEFAULT_WORKFLOW: WorkflowConfig = {
  workflow: {
    name: 'new-workflow',
    entry_point: 'agent_1',
    runtime: { provider: 'copilot' },
  },
  agents: [
    {
      name: 'agent_1',
      prompt: 'Describe what this agent should do.',
    },
  ],
};

const MAX_UNDO = 50;

// ── Store types ────────────────────────────────────────────────────

export interface NodePosition {
  x: number;
  y: number;
}

interface DesignerState {
  // Domain state (source of truth)
  config: WorkflowConfig;
  filePath: string | null;
  dirty: boolean;

  // UI state
  selectedNodeId: string | null;
  validation: ValidationResult;
  showYamlPreview: boolean;
  showValidationPanel: boolean;

  // Node positions (separate from domain — purely UI)
  nodePositions: Record<string, NodePosition>;

  // Undo stack
  undoStack: WorkflowConfig[];
  redoStack: WorkflowConfig[];

  // Derived (cached for ReactFlow)
  nodes: DesignerNode[];
  edges: DesignerEdge[];

  // Actions — workflow
  setConfig: (config: WorkflowConfig, filePath?: string | null) => void;
  updateWorkflow: (patch: Partial<WorkflowConfig['workflow']>) => void;

  // Actions — agents
  addAgent: (agent: AgentDef) => void;
  updateAgent: (name: string, patch: Partial<AgentDef>) => void;
  removeAgent: (name: string) => void;

  // Actions — parallel groups
  addParallelGroup: (group: ParallelGroup) => void;
  updateParallelGroup: (name: string, patch: Partial<ParallelGroup>) => void;
  removeParallelGroup: (name: string) => void;

  // Actions — for-each groups
  addForEachGroup: (group: ForEachDef) => void;
  updateForEachGroup: (name: string, patch: Partial<ForEachDef>) => void;
  removeForEachGroup: (name: string) => void;

  // Actions — routes (edge connections)
  addRoute: (fromName: string, toName: string, when?: string) => void;
  removeRoute: (fromName: string, toName: string) => void;

  // Actions — UI
  selectNode: (id: string | null) => void;
  setValidation: (result: ValidationResult) => void;
  toggleYamlPreview: () => void;
  toggleValidationPanel: () => void;
  updateNodePosition: (nodeId: string, pos: NodePosition) => void;

  // Actions — undo/redo
  undo: () => void;
  redo: () => void;
  canUndo: () => boolean;
  canRedo: () => boolean;

  // Actions — derived refresh
  refreshGraph: () => void;
}

// ── Helper: push undo snapshot ─────────────────────────────────────

function pushUndo(state: DesignerState): Pick<DesignerState, 'undoStack' | 'redoStack'> {
  const stack = [...state.undoStack, structuredClone(state.config)];
  if (stack.length > MAX_UNDO) stack.shift();
  return { undoStack: stack, redoStack: [] };
}

// ── Store creation ─────────────────────────────────────────────────

export const useDesignerStore = create<DesignerState>((set, get) => {
  const initialGraph = configToGraph(DEFAULT_WORKFLOW, {});

  return {
    config: structuredClone(DEFAULT_WORKFLOW),
    filePath: null,
    dirty: false,
    selectedNodeId: null,
    validation: { errors: [], warnings: [] },
    showYamlPreview: false,
    showValidationPanel: true,
    nodePositions: {},
    undoStack: [],
    redoStack: [],
    nodes: initialGraph.nodes,
    edges: initialGraph.edges,

    // ── Workflow ────────────────────────────────────────────────

    setConfig: (config, filePath) => {
      set((s) => {
        const graph = configToGraph(config, s.nodePositions);
        return {
          config: structuredClone(config),
          filePath: filePath !== undefined ? filePath : s.filePath,
          dirty: false,
          nodes: graph.nodes,
          edges: graph.edges,
          undoStack: [],
          redoStack: [],
          selectedNodeId: null,
        };
      });
    },

    updateWorkflow: (patch) => {
      set((s) => {
        const undo = pushUndo(s);
        const config = {
          ...s.config,
          workflow: { ...s.config.workflow, ...patch },
        };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    // ── Agents ─────────────────────────────────────────────────

    addAgent: (agent) => {
      set((s) => {
        const undo = pushUndo(s);
        const config = {
          ...s.config,
          agents: [...s.config.agents, agent],
        };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    updateAgent: (name, patch) => {
      set((s) => {
        const undo = pushUndo(s);
        const agents = s.config.agents.map((a) =>
          a.name === name ? { ...a, ...patch } : a,
        );
        // If name changed, update entry_point and routes
        let workflow = s.config.workflow;
        if (patch.name && patch.name !== name) {
          if (workflow.entry_point === name) {
            workflow = { ...workflow, entry_point: patch.name };
          }
          // Update route targets pointing to old name
          const updatedAgents = agents.map((a) => ({
            ...a,
            routes: a.routes?.map((r) =>
              r.to === name ? { ...r, to: patch.name! } : r,
            ),
          }));
          const config = { ...s.config, workflow, agents: updatedAgents };
          const graph = configToGraph(config, s.nodePositions);
          return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
        }
        const config = { ...s.config, workflow, agents };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    removeAgent: (name) => {
      set((s) => {
        const undo = pushUndo(s);
        const agents = s.config.agents.filter((a) => a.name !== name);
        const config = { ...s.config, agents };
        const graph = configToGraph(config, s.nodePositions);
        return {
          ...undo,
          config,
          dirty: true,
          nodes: graph.nodes,
          edges: graph.edges,
          selectedNodeId: s.selectedNodeId === `agent-${name}` ? null : s.selectedNodeId,
        };
      });
    },

    // ── Parallel groups ────────────────────────────────────────

    addParallelGroup: (group) => {
      set((s) => {
        const undo = pushUndo(s);
        const config = {
          ...s.config,
          parallel: [...(s.config.parallel ?? []), group],
        };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    updateParallelGroup: (name, patch) => {
      set((s) => {
        const undo = pushUndo(s);
        const parallel = (s.config.parallel ?? []).map((g) =>
          g.name === name ? { ...g, ...patch } : g,
        );
        const config = { ...s.config, parallel };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    removeParallelGroup: (name) => {
      set((s) => {
        const undo = pushUndo(s);
        const parallel = (s.config.parallel ?? []).filter((g) => g.name !== name);
        const config = { ...s.config, parallel };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    // ── For-each groups ────────────────────────────────────────

    addForEachGroup: (group) => {
      set((s) => {
        const undo = pushUndo(s);
        const config = {
          ...s.config,
          for_each: [...(s.config.for_each ?? []), group],
        };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    updateForEachGroup: (name, patch) => {
      set((s) => {
        const undo = pushUndo(s);
        const for_each = (s.config.for_each ?? []).map((g) =>
          g.name === name ? { ...g, ...patch } : g,
        );
        const config = { ...s.config, for_each };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    removeForEachGroup: (name) => {
      set((s) => {
        const undo = pushUndo(s);
        const for_each = (s.config.for_each ?? []).filter((g) => g.name !== name);
        const config = { ...s.config, for_each };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    // ── Routes ─────────────────────────────────────────────────

    addRoute: (fromName, toName, when) => {
      set((s) => {
        const undo = pushUndo(s);
        const agents = s.config.agents.map((a) => {
          if (a.name !== fromName) return a;
          const routes = [...(a.routes ?? []), { to: toName, ...(when ? { when } : {}) }];
          return { ...a, routes };
        });
        const config = { ...s.config, agents };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    removeRoute: (fromName, toName) => {
      set((s) => {
        const undo = pushUndo(s);
        const agents = s.config.agents.map((a) => {
          if (a.name !== fromName) return a;
          const routes = (a.routes ?? []).filter((r) => r.to !== toName);
          return { ...a, routes };
        });
        const config = { ...s.config, agents };
        const graph = configToGraph(config, s.nodePositions);
        return { ...undo, config, dirty: true, nodes: graph.nodes, edges: graph.edges };
      });
    },

    // ── UI ─────────────────────────────────────────────────────

    selectNode: (id) => set({ selectedNodeId: id }),
    setValidation: (result) => set({ validation: result }),
    toggleYamlPreview: () => set((s) => ({ showYamlPreview: !s.showYamlPreview })),
    toggleValidationPanel: () =>
      set((s) => ({ showValidationPanel: !s.showValidationPanel })),

    updateNodePosition: (nodeId, pos) => {
      set((s) => ({
        nodePositions: { ...s.nodePositions, [nodeId]: pos },
      }));
    },

    // ── Undo/Redo ──────────────────────────────────────────────

    undo: () => {
      set((s) => {
        if (s.undoStack.length === 0) return s;
        const prev = s.undoStack[s.undoStack.length - 1]!;
        const undoStack = s.undoStack.slice(0, -1);
        const redoStack = [...s.redoStack, structuredClone(s.config)];
        const graph = configToGraph(prev, s.nodePositions);
        return {
          config: prev,
          undoStack,
          redoStack,
          dirty: true,
          nodes: graph.nodes,
          edges: graph.edges,
        };
      });
    },

    redo: () => {
      set((s) => {
        if (s.redoStack.length === 0) return s;
        const next = s.redoStack[s.redoStack.length - 1]!;
        const redoStack = s.redoStack.slice(0, -1);
        const undoStack = [...s.undoStack, structuredClone(s.config)];
        const graph = configToGraph(next, s.nodePositions);
        return {
          config: next,
          undoStack,
          redoStack,
          dirty: true,
          nodes: graph.nodes,
          edges: graph.edges,
        };
      });
    },

    canUndo: () => get().undoStack.length > 0,
    canRedo: () => get().redoStack.length > 0,

    refreshGraph: () => {
      set((s) => {
        const graph = configToGraph(s.config, s.nodePositions);
        return { nodes: graph.nodes, edges: graph.edges };
      });
    },
  };
});
