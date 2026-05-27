/**
 * Pub/sub event system for workflow execution.
 * Mirrors src/conductor/events.py
 */

export type WorkflowEventType =
  | "workflow_started"
  | "workflow_completed"
  | "workflow_failed"
  | "agent_started"
  | "agent_completed"
  | "agent_failed"
  | "agent_message"
  | "agent_reasoning"
  | "agent_tool_start"
  | "agent_tool_complete"
  | "parallel_started"
  | "parallel_completed"
  | "foreach_started"
  | "foreach_completed"
  | "checkpoint_saved"
  | "awaiting_input";

export interface WorkflowEvent {
  type: WorkflowEventType;
  timestamp: number;
  data: Record<string, unknown>;
}

export type EventHandler = (event: WorkflowEvent) => void | Promise<void>;

export class WorkflowEventEmitter {
  private handlers: EventHandler[] = [];

  subscribe(handler: EventHandler): () => void {
    this.handlers.push(handler);
    return () => {
      this.handlers = this.handlers.filter((h) => h !== handler);
    };
  }

  async emit(type: WorkflowEventType, data: Record<string, unknown> = {}): Promise<void> {
    const event: WorkflowEvent = { type, timestamp: Date.now(), data };
    // Snapshot handlers so that subscribe-during-emit doesn't affect this broadcast.
    for (const handler of [...this.handlers]) {
      try {
        await handler(event);
      } catch {
        // Error isolation: one failing handler must not prevent others from running.
      }
    }
  }
}
