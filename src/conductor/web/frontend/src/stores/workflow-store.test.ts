import { describe, expect, it } from 'vitest';
import { useWorkflowStore } from './workflow-store';
import type { WorkflowEvent } from '@/types/events';

/**
 * Regression coverage for issue #307: an agent node adjacent to a
 * `type: workflow` sub-workflow step could render stuck on "running" even
 * though the underlying store data was already `completed`. Root cause:
 * `processEvent` cloned `nodes`/`groupProgress`/`eventLog`/`activityLog` on
 * every event for React reference-equality, but never cloned
 * `subworkflowContexts` — handlers reached via `activeTarget()` mutate
 * nested `ctx.nodes`/etc. and `subworkflow_started` pushes onto
 * `subworkflowContexts`/`children` in place, so nothing forced a fresh
 * reference for selectors depending on `subworkflowContexts`.
 */

function event(type: WorkflowEvent['type'], data: Record<string, unknown>, timestamp = Date.now() / 1000): WorkflowEvent {
  return { type, timestamp, data };
}

function resetStore() {
  useWorkflowStore.setState(useWorkflowStore.getInitialState(), true);
}

describe('workflow-store processEvent — subworkflowContexts reactivity (#307)', () => {
  it('keeps a root-level node "completed" (and produces a fresh reference) across an adjacent subworkflow_started/completed pair', () => {
    resetStore();
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [{ name: 'architect' }, { name: 'document_review' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'architect',
    }));

    // architect loops back on itself once ("x2" in the reported repro).
    processEvent(event('agent_started', { agent_name: 'architect', iteration: 1 }));
    processEvent(event('agent_completed', { agent_name: 'architect', iteration: 1 }));
    processEvent(event('agent_started', { agent_name: 'architect', iteration: 2 }));
    processEvent(event('agent_completed', { agent_name: 'architect', iteration: 2 }));

    const afterArchitect = useWorkflowStore.getState();
    expect(afterArchitect.nodes.architect?.status).toBe('completed');

    const subContextsBeforeSubworkflow = afterArchitect.subworkflowContexts;

    // Workflow moves on to the adjacent `type: workflow` step.
    processEvent(event('subworkflow_started', {
      agent_name: 'document_review',
      workflow: 'document_review.yaml',
      iteration: 1,
    }));

    const afterSubworkflowStart = useWorkflowStore.getState();
    // The bug this test guards against: subworkflowContexts must get a new
    // reference on every event that mutates it (here: the push in
    // subworkflow_started), just like nodes/groupProgress/eventLog/activityLog do.
    expect(afterSubworkflowStart.subworkflowContexts).not.toBe(subContextsBeforeSubworkflow);
    // architect's own node/status must remain untouched and correct.
    expect(afterSubworkflowStart.nodes.architect?.status).toBe('completed');
    expect(afterSubworkflowStart.nodes.document_review?.status).toBe('running');

    processEvent(event('subworkflow_completed', {
      agent_name: 'document_review',
      iteration: 1,
    }));

    const afterSubworkflowComplete = useWorkflowStore.getState();
    expect(afterSubworkflowComplete.subworkflowContexts).not.toBe(afterSubworkflowStart.subworkflowContexts);
    // architect must still show completed — never regress back to "running".
    expect(afterSubworkflowComplete.nodes.architect?.status).toBe('completed');
  });

  it('produces a fresh subworkflowContexts reference for updates to nodes nested inside a running child context', () => {
    resetStore();
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [{ name: 'document_review' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'document_review',
    }));

    processEvent(event('subworkflow_started', {
      agent_name: 'document_review',
      workflow: 'document_review.yaml',
      iteration: 1,
    }));

    const beforeChildAgent = useWorkflowStore.getState().subworkflowContexts;
    const childCtxBefore = beforeChildAgent[0];
    expect(childCtxBefore).toBeDefined();

    // An agent event stamped with a subworkflow_path routes to the nested
    // context's own nodes map via activeTarget(), mutating it in place
    // pre-fix.
    processEvent(event('agent_started', {
      agent_name: 'reviewer',
      iteration: 1,
      subworkflow_path: ['document_review'],
    }));

    const afterChildAgentStart = useWorkflowStore.getState().subworkflowContexts;
    expect(afterChildAgentStart).not.toBe(beforeChildAgent);
    expect(afterChildAgentStart[0]).not.toBe(childCtxBefore);
    expect(afterChildAgentStart[0]?.nodes.reviewer?.status).toBe('running');

    processEvent(event('agent_completed', {
      agent_name: 'reviewer',
      iteration: 1,
      subworkflow_path: ['document_review'],
    }));

    const afterChildAgentComplete = useWorkflowStore.getState().subworkflowContexts;
    expect(afterChildAgentComplete).not.toBe(afterChildAgentStart);
    expect(afterChildAgentComplete[0]?.nodes.reviewer?.status).toBe('completed');
  });
});
