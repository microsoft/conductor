/**
 * Main designer canvas — wraps ReactFlow with editing capabilities.
 */

import { useCallback, useEffect } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Connection,
  type NodeChange,
  BackgroundVariant,
} from '@xyflow/react';
import { useDesignerStore } from '@/stores/designer-store';
import { useLoadWorkflow, useValidate } from '@/hooks/useDesignerApi';
import { DesignerAgentNode } from './DesignerAgentNode';
import { DesignerGateNode } from './DesignerGateNode';
import { DesignerScriptNode } from './DesignerScriptNode';
import { DesignerWorkflowNode } from './DesignerWorkflowNode';
import { DesignerParallelGroup } from './DesignerParallelGroup';
import { DesignerForEachGroup } from './DesignerForEachGroup';
import { DesignerStartNode } from './DesignerStartNode';
import { DesignerEndNode } from './DesignerEndNode';

const nodeTypes = {
  designerAgent: DesignerAgentNode,
  designerGate: DesignerGateNode,
  designerScript: DesignerScriptNode,
  designerWorkflow: DesignerWorkflowNode,
  designerParallel: DesignerParallelGroup,
  designerForEach: DesignerForEachGroup,
  designerStart: DesignerStartNode,
  designerEnd: DesignerEndNode,
};

export function DesignerCanvas() {
  const nodes = useDesignerStore((s) => s.nodes);
  const edges = useDesignerStore((s) => s.edges);
  const config = useDesignerStore((s) => s.config);
  const addRoute = useDesignerStore((s) => s.addRoute);
  const selectNode = useDesignerStore((s) => s.selectNode);
  const updateNodePosition = useDesignerStore((s) => s.updateNodePosition);

  const loadWorkflow = useLoadWorkflow();
  const validate = useValidate();

  // Load workflow from server on mount
  useEffect(() => {
    loadWorkflow();
  }, [loadWorkflow]);

  // Validate on config changes
  useEffect(() => {
    validate(config);
  }, [config, validate]);

  // Handle new connections (edge drag)
  const onConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;

      // Extract entity names from node IDs
      const fromName = nodeIdToEntityName(connection.source);
      const toName = connection.target === '__end__'
        ? '$end'
        : nodeIdToEntityName(connection.target);

      if (fromName && toName) {
        addRoute(fromName, toName);
      }
    },
    [addRoute],
  );

  // Handle node position changes
  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      for (const change of changes) {
        if (change.type === 'position' && 'position' in change && change.position && change.id) {
          updateNodePosition(change.id, change.position);
        }
        if (change.type === 'select' && change.id) {
          if ('selected' in change && change.selected) {
            selectNode(change.id);
          }
        }
      }
    },
    [updateNodePosition, selectNode],
  );

  const onPaneClick = useCallback(() => {
    selectNode(null);
  }, [selectNode]);

  return (
    <div className="flex-1 h-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onConnect={onConnect}
        onNodesChange={onNodesChange}
        onPaneClick={onPaneClick}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        defaultEdgeOptions={{
          style: { stroke: '#6b7280', strokeWidth: 2 },
          type: 'smoothstep',
        }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#374151" />
        <Controls />
        <MiniMap
          nodeStrokeWidth={3}
          nodeColor={(node) => {
            const data = node.data as { nodeType?: string } | undefined;
            switch (data?.nodeType) {
              case 'agent': return '#3b82f6';
              case 'human_gate': return '#f59e0b';
              case 'script': return '#22c55e';
              case 'workflow': return '#a855f7';
              case 'parallel': return '#06b6d4';
              case 'for_each': return '#f97316';
              case 'start': return '#22c55e';
              case 'end': return '#ef4444';
              default: return '#6b7280';
            }
          }}
        />
      </ReactFlow>
    </div>
  );
}

/** Extract entity name from a node ID like "agent-foo" → "foo". */
function nodeIdToEntityName(nodeId: string): string | null {
  const prefixes = ['agent-', 'parallel-', 'foreach-'];
  for (const prefix of prefixes) {
    if (nodeId.startsWith(prefix)) {
      return nodeId.slice(prefix.length);
    }
  }
  return null;
}
