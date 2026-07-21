import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useWorkflowStore } from './workflow-store';
import type { WorkflowEvent } from '@/types/events';

/**
 * Regression coverage for issue #307: an agent node adjacent to a
 * `type: workflow` sub-workflow step could render stuck on "running" even
 * though the underlying store data was already `completed`, because
 * `subworkflowContexts` was never given a fresh reference on mutation (see
 * `resolveMutableContext`'s doc comment for the mechanism).
 *
 * The fix clones only the "spine" (the top-level array plus the ancestor
 * chain down to the context actually being mutated) rather than the whole
 * tree, so several tests below also assert that *unrelated* siblings and
 * untouched root-level state keep their existing references — that's the
 * perf/re-render half of the fix, not just correctness.
 */

function event(type: WorkflowEvent['type'], data: Record<string, unknown>, timestamp = Date.now() / 1000): WorkflowEvent {
  return { type, timestamp, data };
}

beforeEach(() => {
  useWorkflowStore.setState(useWorkflowStore.getInitialState(), true);
});

describe('workflow-store processEvent — subworkflowContexts reactivity (#307)', () => {
  it('keeps a root-level node "completed" (and produces a fresh reference) across an adjacent subworkflow_started/completed pair', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [{ name: 'architect' }, { name: 'document_review' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'architect',
    }));

    // architect runs twice (loops back to itself once) before handing off
    // to the sub-workflow step.
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
    // before the fix.
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

  it('propagates fresh references up through a grandchild (sub-workflow nested inside a sub-workflow)', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [{ name: 'document_review' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'document_review',
    }));

    // Parent sub-workflow "document_review" starts at the root.
    processEvent(event('subworkflow_started', {
      agent_name: 'document_review',
      workflow: 'document_review.yaml',
      iteration: 1,
      parent_path: [],
    }));

    // Grandchild sub-workflow "inner_reviewer" starts nested inside
    // "document_review" (parent_path addresses it by slot key).
    processEvent(event('subworkflow_started', {
      agent_name: 'inner_reviewer',
      workflow: 'inner_reviewer.yaml',
      iteration: 1,
      parent_path: ['document_review'],
    }));

    const beforeMutation = useWorkflowStore.getState().subworkflowContexts;
    const parentBefore = beforeMutation[0];
    const grandchildBefore = parentBefore?.children[0];
    expect(grandchildBefore).toBeDefined();

    // Mutate a node two levels deep, inside the grandchild context.
    processEvent(event('agent_started', {
      agent_name: 'deep_reviewer',
      iteration: 1,
      subworkflow_path: ['document_review', 'inner_reviewer'],
    }));

    const afterMutation = useWorkflowStore.getState().subworkflowContexts;
    const parentAfter = afterMutation[0];
    const grandchildAfter = parentAfter?.children[0];

    // Every link in the chain down to the mutated context must be fresh —
    // this exercises the recursive spine-clone, not just a single level.
    expect(afterMutation).not.toBe(beforeMutation);
    expect(parentAfter).not.toBe(parentBefore);
    expect(grandchildAfter).not.toBe(grandchildBefore);
    expect(grandchildAfter?.nodes.deep_reviewer?.status).toBe('running');
  });

  it('does not clone sibling sub-workflow contexts that a mutation does not touch', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [{ name: 'reviewer_group' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'reviewer_group',
    }));

    // Two concurrent for_each iterations of the same group start as
    // siblings, distinguished by item_key (matches how the engine
    // disambiguates concurrent for_each-of-workflow iterations).
    processEvent(event('subworkflow_started', {
      agent_name: 'reviewer_group',
      workflow: 'reviewer.yaml',
      iteration: 1,
      item_key: '0',
      parent_path: [],
    }));
    processEvent(event('subworkflow_started', {
      agent_name: 'reviewer_group',
      workflow: 'reviewer.yaml',
      iteration: 1,
      item_key: '1',
      parent_path: [],
    }));

    const beforeMutation = useWorkflowStore.getState().subworkflowContexts;
    expect(beforeMutation).toHaveLength(2);
    const sibling0Before = beforeMutation[0];
    const sibling1Before = beforeMutation[1];

    // Mutate only the second iteration (slot key "reviewer_group[1]").
    processEvent(event('agent_started', {
      agent_name: 'reviewer',
      iteration: 1,
      subworkflow_path: ['reviewer_group[1]'],
    }));

    const afterMutation = useWorkflowStore.getState().subworkflowContexts;
    // The mutated sibling gets a fresh reference...
    expect(afterMutation[1]).not.toBe(sibling1Before);
    expect(afterMutation[1]?.nodes.reviewer?.status).toBe('running');
    // ...but the untouched sibling keeps its existing reference. This is
    // the perf/re-render half of the fix: a whole-tree clone would give
    // every sibling a new reference on every event, regardless of which
    // branch actually changed.
    expect(afterMutation[0]).toBe(sibling0Before);
  });

  it('does not touch subworkflowContexts at all for events with no active sub-workflow', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [{ name: 'architect' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'architect',
    }));

    const initialContexts = useWorkflowStore.getState().subworkflowContexts;

    // Plain root-level events, none of which involve a sub-workflow, must
    // not allocate a new subworkflowContexts reference — the whole point of
    // scoping the clone to the mutated path rather than the whole tree.
    processEvent(event('agent_started', { agent_name: 'architect', iteration: 1 }));
    processEvent(event('agent_completed', { agent_name: 'architect', iteration: 1 }));

    expect(useWorkflowStore.getState().subworkflowContexts).toBe(initialContexts);
  });

  it('preserves unrelated fields on a context across a clone-and-mutate cycle', () => {
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
      iteration: 3,
      parent_path: [],
    }));

    const beforeMutation = useWorkflowStore.getState().subworkflowContexts[0];
    expect(beforeMutation?.workflowFile).toBe('document_review.yaml');
    expect(beforeMutation?.iteration).toBe(3);
    expect(beforeMutation?.parentAgent).toBe('document_review');

    processEvent(event('agent_started', {
      agent_name: 'reviewer',
      iteration: 1,
      subworkflow_path: ['document_review'],
    }));

    // A shallow-clone bug (e.g. a field manually dropped from the clone
    // helper) would silently lose one of these unrelated fields.
    const afterMutation = useWorkflowStore.getState().subworkflowContexts[0];
    expect(afterMutation?.workflowFile).toBe('document_review.yaml');
    expect(afterMutation?.iteration).toBe(3);
    expect(afterMutation?.parentAgent).toBe('document_review');
  });
});

/**
 * Regression coverage for issue #330: a dashboard whose WebSocket keeps
 * failing to reconnect should eventually warn the user rather than leaving
 * `workflowStatus` looking `'running'` forever. The store tracks
 * `wsDisconnectedSince` as the timestamp of the *first* drop from
 * `'connected'`, preserved through the connecting/reconnecting backoff
 * churn (since `wsStatus` itself oscillates and can't be timed directly),
 * and cleared once reconnected.
 */
describe('workflow-store setWsStatus — wsDisconnectedSince tracking (#330)', () => {
  it('stays null through the initial connecting state before any connection has ever succeeded', () => {
    const { setWsStatus } = useWorkflowStore.getState();
    setWsStatus('connecting');
    expect(useWorkflowStore.getState().wsDisconnectedSince).toBeNull();
  });

  it('sets a timestamp on a fresh drop from connected, preserves it through connecting/reconnecting churn, and clears it on reconnect', () => {
    const { setWsStatus } = useWorkflowStore.getState();

    setWsStatus('connected');
    expect(useWorkflowStore.getState().wsDisconnectedSince).toBeNull();

    setWsStatus('disconnected');
    const firstDrop = useWorkflowStore.getState().wsDisconnectedSince;
    expect(firstDrop).not.toBeNull();

    setWsStatus('reconnecting');
    expect(useWorkflowStore.getState().wsDisconnectedSince).toBe(firstDrop);

    setWsStatus('connecting');
    expect(useWorkflowStore.getState().wsDisconnectedSince).toBe(firstDrop);

    // A failed retry attempt cycles back through disconnected/reconnecting
    // without ever having reached 'connected' again — the original
    // timestamp must NOT be reset by this churn.
    setWsStatus('disconnected');
    setWsStatus('reconnecting');
    expect(useWorkflowStore.getState().wsDisconnectedSince).toBe(firstDrop);

    setWsStatus('connected');
    expect(useWorkflowStore.getState().wsDisconnectedSince).toBeNull();
  });

  it('starts a new timestamp for a second, later disconnect', () => {
    vi.useFakeTimers();
    try {
      const { setWsStatus } = useWorkflowStore.getState();

      setWsStatus('connected');
      setWsStatus('disconnected');
      const firstDrop = useWorkflowStore.getState().wsDisconnectedSince;
      setWsStatus('connected');
      expect(useWorkflowStore.getState().wsDisconnectedSince).toBeNull();

      vi.advanceTimersByTime(5_000);

      setWsStatus('disconnected');
      const secondDrop = useWorkflowStore.getState().wsDisconnectedSince;
      expect(secondDrop).not.toBeNull();
      expect(secondDrop).not.toBe(firstDrop);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('workflow-store processEvent — system log metadata capture (#330)', () => {
  it('captures bg_stderr_log/bg_stdout_log/log_file from the root workflow_started event', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'agent',
      system: {
        log_file: '/tmp/conductor/debug.log',
        bg_stderr_log: '/tmp/conductor/conductor-root-123.bg.stderr.log',
        bg_stdout_log: '/tmp/conductor/conductor-root-123.bg.stdout.log',
      },
    }));

    const state = useWorkflowStore.getState();
    expect(state.systemLogFile).toBe('/tmp/conductor/debug.log');
    expect(state.bgStderrLog).toBe('/tmp/conductor/conductor-root-123.bg.stderr.log');
    expect(state.bgStdoutLog).toBe('/tmp/conductor/conductor-root-123.bg.stdout.log');
  });

  it('defaults log fields to null when system metadata (or its fields) are absent, e.g. plain --web runs with no --log-file', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'agent',
      system: { log_file: null },
    }));

    const state = useWorkflowStore.getState();
    expect(state.systemLogFile).toBeNull();
    expect(state.bgStderrLog).toBeNull();
    expect(state.bgStdoutLog).toBeNull();
  });
});
