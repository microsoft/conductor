export type NodeStatus = 'pending' | 'running' | 'completed' | 'failed' | 'paused' | 'idle' | 'waiting';
export type NodeType = 'agent' | 'script' | 'human_gate' | 'parallel_group' | 'for_each_group' | 'start' | 'end';

export const NODE_STATUS_HEX: Record<string, string> = {
  pending: '#6b7280',
  running: '#3b82f6',
  completed: '#22c55e',
  failed: '#ef4444',
  paused: '#f59e0b',
  idle: '#6b7280',
  waiting: '#a855f7',
};
