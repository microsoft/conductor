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
 * `'connected'` OR the very first failed connection attempt (issue #397:
 * a handshake rejected by the auth guard never reaches `'connected'` at
 * all, so timing only drops-from-connected would never fire the banner
 * for it), preserved through the connecting/reconnecting backoff churn
 * (since `wsStatus` itself oscillates and can't be timed directly), and
 * cleared once reconnected.
 */
describe('workflow-store setWsStatus — wsDisconnectedSince tracking (#330)', () => {
  it('starts the clock on the very first connecting state, even before any connection has ever succeeded', () => {
    const { setWsStatus } = useWorkflowStore.getState();
    setWsStatus('connecting');
    expect(useWorkflowStore.getState().wsDisconnectedSince).not.toBeNull();
  });

  it('preserves the same timestamp across connecting -> disconnected -> reconnecting when the socket has never once reached connected (e.g. the page loaded while the backend was already down, or the handshake was auth-rejected)', () => {
    const { setWsStatus } = useWorkflowStore.getState();
    setWsStatus('connecting');
    const firstAttempt = useWorkflowStore.getState().wsDisconnectedSince;
    expect(firstAttempt).not.toBeNull();

    setWsStatus('disconnected');
    expect(useWorkflowStore.getState().wsDisconnectedSince).toBe(firstAttempt);

    setWsStatus('reconnecting');
    expect(useWorkflowStore.getState().wsDisconnectedSince).toBe(firstAttempt);
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
        // The always-on structured JSONL event log path (EventLogSubscriber),
        // not the separate --log-file debug-output flag.
        log_file: '/tmp/conductor/conductor-root-20260101-120000-abcd1234.events.jsonl',
        bg_stderr_log: '/tmp/conductor/conductor-root-123.bg.stderr.log',
        bg_stdout_log: '/tmp/conductor/conductor-root-123.bg.stdout.log',
      },
    }));

    const state = useWorkflowStore.getState();
    expect(state.systemLogFile).toBe('/tmp/conductor/conductor-root-20260101-120000-abcd1234.events.jsonl');
    expect(state.bgStderrLog).toBe('/tmp/conductor/conductor-root-123.bg.stderr.log');
    expect(state.bgStdoutLog).toBe('/tmp/conductor/conductor-root-123.bg.stdout.log');
  });

  it('normalizes an empty log_file (the real backend default when unset) to null, and defaults bg log fields to null when absent, e.g. plain --web runs not launched via --web-bg', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'agent',
      // The backend's `RunContext.log_file` always sends a string,
      // defaulting to "" when unset — never `null` — so this is the
      // realistic "no log file" shape on the wire.
      system: { log_file: '' },
    }));

    const state = useWorkflowStore.getState();
    expect(state.systemLogFile).toBeNull();
    expect(state.bgStderrLog).toBeNull();
    expect(state.bgStdoutLog).toBeNull();
  });

  it('defaults all log fields to null when the system block is missing entirely', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'agent',
    }));

    const state = useWorkflowStore.getState();
    expect(state.systemLogFile).toBeNull();
    expect(state.bgStderrLog).toBeNull();
    expect(state.bgStdoutLog).toBeNull();
  });

  it('does not let a nested subworkflow_started event clobber the root workflow\'s captured log paths', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [{ name: 'document_review' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'document_review',
      system: {
        log_file: '/tmp/conductor/conductor-root-20260101-120000-abcd1234.events.jsonl',
        bg_stderr_log: '/tmp/conductor/conductor-root-123.bg.stderr.log',
        bg_stdout_log: '/tmp/conductor/conductor-root-123.bg.stdout.log',
      },
    }));

    processEvent(event('subworkflow_started', {
      agent_name: 'document_review',
      workflow: 'document_review.yaml',
      iteration: 1,
    }));

    // A nested workflow_started (wfDepth > 0) carries its own (irrelevant)
    // system metadata — it must not overwrite the root's log paths, which
    // are what the reconnect-warning banner actually needs to point at.
    processEvent(event('workflow_started', {
      name: 'document_review',
      agents: [],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'reviewer',
      system: {
        log_file: '/tmp/conductor/conductor-document_review-20260101-120005-deadbeef.events.jsonl',
        bg_stderr_log: '/tmp/conductor/conductor-document_review-456.bg.stderr.log',
        bg_stdout_log: '/tmp/conductor/conductor-document_review-456.bg.stdout.log',
      },
    }));

    const state = useWorkflowStore.getState();
    expect(state.systemLogFile).toBe('/tmp/conductor/conductor-root-20260101-120000-abcd1234.events.jsonl');
    expect(state.bgStderrLog).toBe('/tmp/conductor/conductor-root-123.bg.stderr.log');
    expect(state.bgStdoutLog).toBe('/tmp/conductor/conductor-root-123.bg.stdout.log');
  });
});

describe('workflow-store processEvent — for-each agent lifecycle', () => {
  it('accepts the backend for_each_agent_started telemetry event without changing item progress', () => {
    const { processEvent } = useWorkflowStore.getState();

    // Requirement: backend-only per-item agent metadata remains a supported
    // event even though the dashboard progress model uses for_each_item_started.
    processEvent(event('for_each_agent_started', {
      group_name: 'reviews',
      agent_name: 'reviewer[0]',
      item_key: '0',
      index: 0,
      working_dir: '/workspace/review-0',
    }));

    expect(useWorkflowStore.getState().nodes.reviews).toBeUndefined();
  });
});

describe('workflow-store — eager static sub-workflow preview (dashboard expandability)', () => {
  it('seeds a pending child context from static `subworkflow` topology on workflow_started', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [
        { name: 'planner' },
        {
          name: 'sub_wf',
          type: 'workflow',
          subworkflow: {
            name: 'child-workflow',
            entry_point: 'step_one',
            agents: [{ name: 'step_one' }],
            routes: [],
            parallel_groups: [],
            for_each_groups: [],
          },
        },
      ],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'planner',
    }));

    const state = useWorkflowStore.getState();
    expect(state.subworkflowContexts).toHaveLength(1);
    const child = state.subworkflowContexts[0]!;
    expect(child.slotKey).toBe('sub_wf');
    expect(child.status).toBe('pending');
    expect(child.workflowName).toBe('child-workflow');
    expect(child.agents.map((a) => a.name)).toEqual(['step_one']);
  });

  it('recurses into nested `type: workflow` agents when seeding static previews', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [
        {
          name: 'sub_wf',
          type: 'workflow',
          subworkflow: {
            name: 'child-workflow',
            entry_point: 'mid',
            agents: [
              {
                name: 'mid',
                type: 'workflow',
                subworkflow: {
                  name: 'grandchild-workflow',
                  entry_point: 'leaf',
                  agents: [{ name: 'leaf' }],
                  routes: [],
                  parallel_groups: [],
                  for_each_groups: [],
                },
              },
            ],
            routes: [],
            parallel_groups: [],
            for_each_groups: [],
          },
        },
      ],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'sub_wf',
    }));

    const child = useWorkflowStore.getState().subworkflowContexts[0]!;
    expect(child.children).toHaveLength(1);
    const grandchild = child.children[0]!;
    expect(grandchild.slotKey).toBe('mid');
    expect(grandchild.workflowName).toBe('grandchild-workflow');
    expect(grandchild.agents.map((a) => a.name)).toEqual(['leaf']);
  });

  it('does not seed a preview when the sub-workflow could not be resolved statically', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [{ name: 'sub_wf', type: 'workflow', subworkflow: null }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'sub_wf',
    }));

    expect(useWorkflowStore.getState().subworkflowContexts).toHaveLength(0);
  });

  it('reuses the static placeholder (instead of pushing a duplicate) once subworkflow_started fires', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [
        {
          name: 'sub_wf',
          type: 'workflow',
          subworkflow: {
            name: 'child-workflow',
            entry_point: 'step_one',
            agents: [{ name: 'step_one' }],
            routes: [],
            parallel_groups: [],
            for_each_groups: [],
          },
        },
      ],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'sub_wf',
    }));

    expect(useWorkflowStore.getState().subworkflowContexts).toHaveLength(1);

    processEvent(event('subworkflow_started', {
      agent_name: 'sub_wf',
      workflow: 'child.yaml',
      iteration: 1,
    }));

    const state = useWorkflowStore.getState();
    // Still exactly one child — the placeholder was reused, not duplicated.
    expect(state.subworkflowContexts).toHaveLength(1);
    expect(state.activeContextPath).toEqual([0]);

    processEvent(event('workflow_started', {
      name: 'child-workflow',
      agents: [{ name: 'step_one' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'step_one',
    }));

    const afterChildStart = useWorkflowStore.getState();
    expect(afterChildStart.subworkflowContexts).toHaveLength(1);
    expect(afterChildStart.subworkflowContexts[0]!.status).toBe('running');
  });

  it('pushes a new context (not the completed one) on a genuine loop-back re-invocation', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [
        {
          name: 'sub_wf',
          type: 'workflow',
          subworkflow: {
            name: 'child-workflow',
            entry_point: 'step_one',
            agents: [{ name: 'step_one' }],
            routes: [],
            parallel_groups: [],
            for_each_groups: [],
          },
        },
      ],
      routes: [{ from: 'sub_wf', to: 'sub_wf' }],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'sub_wf',
    }));

    // First invocation: reuses the static placeholder (as verified above).
    processEvent(event('subworkflow_started', { agent_name: 'sub_wf', workflow: 'child.yaml', iteration: 1, parent_path: [] }));
    processEvent(event('workflow_started', {
      name: 'child-workflow',
      agents: [{ name: 'step_one' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'step_one',
    }));
    processEvent(event('workflow_completed', { output: {}, subworkflow_path: ['sub_wf'] }));
    processEvent(event('subworkflow_completed', { agent_name: 'sub_wf', elapsed: 1.0, parent_path: [] }));

    const afterFirstRun = useWorkflowStore.getState();
    expect(afterFirstRun.subworkflowContexts).toHaveLength(1);
    expect(afterFirstRun.subworkflowContexts[0]!.status).toBe('completed');

    // A loop-back routes back into `sub_wf` a second time — this must push
    // a brand-new context (preserving iteration history), not reuse/clobber
    // the now-completed one from the first pass.
    processEvent(event('subworkflow_started', { agent_name: 'sub_wf', workflow: 'child.yaml', iteration: 2, parent_path: [] }));

    const afterSecondStart = useWorkflowStore.getState();
    expect(afterSecondStart.subworkflowContexts).toHaveLength(2);
    expect(afterSecondStart.subworkflowContexts[0]!.status).toBe('completed');
    expect(afterSecondStart.subworkflowContexts[1]!.slotKey).toBe('sub_wf');
    expect(afterSecondStart.subworkflowContexts[1]!.status).toBe('pending');
  });

  it('seeds a nested static preview for a `type: workflow` agent that belongs to a parallel group', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('workflow_started', {
      name: 'root',
      agents: [
        {
          name: 'sub_wf',
          type: 'workflow',
          subworkflow: {
            name: 'child-workflow',
            entry_point: 'fan_out',
            agents: [
              { name: 'a' },
              {
                name: 'b',
                type: 'workflow',
                subworkflow: {
                  name: 'grandchild-workflow',
                  entry_point: 'leaf',
                  agents: [{ name: 'leaf' }],
                  routes: [],
                  parallel_groups: [],
                  for_each_groups: [],
                },
              },
            ],
            routes: [],
            parallel_groups: [{ name: 'fan_out', agents: ['a', 'b'] }],
            for_each_groups: [],
          },
        },
      ],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'sub_wf',
    }));

    const child = useWorkflowStore.getState().subworkflowContexts[0]!;
    // `b` is a parallel-group member AND a `type: workflow` agent with its
    // own eagerly-resolved topology — it must still get a nested preview.
    expect(child.children).toHaveLength(1);
    expect(child.children[0]!.slotKey).toBe('b');
    expect(child.children[0]!.workflowName).toBe('grandchild-workflow');
  });
});

describe('workflow-store — navigating to a specific historical subworkflow iteration (#365)', () => {
  function startRootWithLoopBackSubworkflow() {
    const { processEvent } = useWorkflowStore.getState();
    processEvent(event('workflow_started', {
      name: 'root',
      agents: [
        {
          name: 'sub_wf',
          type: 'workflow',
          subworkflow: {
            name: 'child-workflow',
            entry_point: 'step_one',
            agents: [{ name: 'step_one' }],
            routes: [],
            parallel_groups: [],
            for_each_groups: [],
          },
        },
      ],
      routes: [{ from: 'sub_wf', to: 'sub_wf' }],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'sub_wf',
    }));
    return processEvent;
  }

  it('resolves distinct iteration data when navigating by index instead of by slotKey', () => {
    const processEvent = startRootWithLoopBackSubworkflow();

    // Iteration 1: one agent (`step_one`), then completes.
    processEvent(event('subworkflow_started', { agent_name: 'sub_wf', workflow: 'child.yaml', iteration: 1, parent_path: [] }));
    processEvent(event('workflow_started', {
      name: 'child-workflow',
      agents: [{ name: 'step_one' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'step_one',
    }));
    processEvent(event('workflow_completed', { output: {}, subworkflow_path: ['sub_wf'] }));
    processEvent(event('subworkflow_completed', { agent_name: 'sub_wf', elapsed: 1.0, parent_path: [] }));

    // Iteration 2 (loop-back re-invocation): a *different* inner agent
    // (`step_two`), so the two iterations are distinguishable by content.
    processEvent(event('subworkflow_started', { agent_name: 'sub_wf', workflow: 'child.yaml', iteration: 2, parent_path: [] }));
    processEvent(event('workflow_started', {
      name: 'child-workflow',
      agents: [{ name: 'step_two' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'step_two',
    }));

    const { subworkflowContexts, navigateToContext, getViewedContext } = useWorkflowStore.getState();
    expect(subworkflowContexts).toHaveLength(2);
    expect(subworkflowContexts[0]!.status).toBe('completed');
    expect(subworkflowContexts[1]!.status).toBe('running');
    // Both siblings share the same slotKey — this is exactly the ambiguity
    // that `navigateIntoSubworkflow(slotKey)` cannot resolve.
    expect(subworkflowContexts[0]!.slotKey).toBe('sub_wf');
    expect(subworkflowContexts[1]!.slotKey).toBe('sub_wf');

    // Navigating by explicit index reaches iteration 1 (the older, completed
    // run), not whichever sibling is newest.
    navigateToContext([0]);
    const iter1View = useWorkflowStore.getState().getViewedContext();
    expect(iter1View.agents.map((a) => a.name)).toEqual(['step_one']);
    expect(iter1View.subworkflowContexts).toBe(subworkflowContexts[0]!.children);

    // Navigating to index 1 reaches iteration 2's distinct content.
    navigateToContext([1]);
    const iter2View = useWorkflowStore.getState().getViewedContext();
    expect(iter2View.agents.map((a) => a.name)).toEqual(['step_two']);

    // Re-confirm index 0 is still reachable and unaffected by having viewed
    // index 1 in between (no accidental "always latest" collapsing).
    navigateToContext([0]);
    expect(getViewedContext().agents.map((a) => a.name)).toEqual(['step_one']);
  });

  it('falls back to the root view for an out-of-range context index (stale click target)', () => {
    const processEvent = startRootWithLoopBackSubworkflow();
    processEvent(event('subworkflow_started', { agent_name: 'sub_wf', workflow: 'child.yaml', iteration: 1, parent_path: [] }));

    const { navigateToContext, getViewedContext } = useWorkflowStore.getState();
    // Only one sibling exists (index 0) — index 5 doesn't resolve to
    // anything, e.g. if the clicked row's context was pruned/replaced
    // between render and click.
    navigateToContext([5]);
    const view = getViewedContext();
    expect(view.workflowName).toBe(useWorkflowStore.getState().workflowName);
    expect(view.agents).toEqual(useWorkflowStore.getState().agents);
  });

  it('labels breadcrumbs with the iteration number only once a slotKey repeats across siblings', () => {
    const processEvent = startRootWithLoopBackSubworkflow();

    processEvent(event('subworkflow_started', { agent_name: 'sub_wf', workflow: 'child.yaml', iteration: 1, parent_path: [] }));

    // A single invocation so far — no disambiguation needed yet.
    useWorkflowStore.getState().navigateToContext([0]);
    const singleRunCrumbs = useWorkflowStore.getState().getBreadcrumbs();
    expect(singleRunCrumbs[singleRunCrumbs.length - 1]!.label).toBe('sub_wf');

    processEvent(event('workflow_started', {
      name: 'child-workflow',
      agents: [{ name: 'step_one' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'step_one',
    }));
    processEvent(event('workflow_completed', { output: {}, subworkflow_path: ['sub_wf'] }));
    processEvent(event('subworkflow_completed', { agent_name: 'sub_wf', elapsed: 1.0, parent_path: [] }));
    processEvent(event('subworkflow_started', { agent_name: 'sub_wf', workflow: 'child.yaml', iteration: 2, parent_path: [] }));

    // Two siblings now share the slotKey `sub_wf` — breadcrumbs for each
    // must disambiguate by iteration number.
    useWorkflowStore.getState().navigateToContext([0]);
    const iter1Crumbs = useWorkflowStore.getState().getBreadcrumbs();
    expect(iter1Crumbs[iter1Crumbs.length - 1]!.label).toBe('sub_wf (iteration 1)');

    useWorkflowStore.getState().navigateToContext([1]);
    const iter2Crumbs = useWorkflowStore.getState().getBreadcrumbs();
    expect(iter2Crumbs[iter2Crumbs.length - 1]!.label).toBe('sub_wf (iteration 2)');
  });
});

/**
 * The store-side half of the resumed-workflow-looks-stopped bug: `isPaused`,
 * `iterationLimitGate` and `activeDialog` are *global* "the engine is blocked
 * waiting on you" flags cleared only by their counterpart event — plus, for
 * `isPaused` / `iterationLimitGate` only, a root `workflow_completed` /
 * `workflow_failed`. (Nothing clears `activeDialog` on a root terminal.)
 *
 * That makes them unrecoverable from a replayed history — on resume the CLI
 * seeds the dashboard from the original run's JSONL, and the root terminal
 * events are deliberately filtered out (they would make the run look finished
 * before it starts). So a run killed while paused would latch `isPaused` on
 * for the whole resumed run, hiding the Stop button behind a Resume/Kill pair
 * that drives the live resumed engine.
 *
 * The fix lives server-side in `WebDashboard._REPLAY_INTERACTIVE_SKIP_TYPES`.
 * These tests pin the store behaviour that filter depends on: if a latch ever
 * becomes self-healing, the corresponding entry can be dropped — and if a new
 * global latch is added, it needs a new entry.
 *
 * They drive `replayState` rather than `processEvent` because `replayState` is
 * what consumes GET /api/state (`hooks/use-websocket.ts`), and GET /api/state
 * is exactly what the resume path seeds. Driving the real entry point is what
 * makes these fail if a client-side clamp ever makes the server filter moot.
 */
function rootStartedEvent() {
  return event('workflow_started', {
    name: 'root',
    agents: [{ name: 'architect_questions' }],
    routes: [],
    parallel_groups: [],
    for_each_groups: [],
    entry_point: 'architect_questions',
  });
}
describe('workflow-store — a replayed history latches the global interaction flags', () => {
  it('leaves isPaused latched when a seeded history ends on an unresolved agent_paused', () => {
    useWorkflowStore.getState().replayState([
      rootStartedEvent(),
      event('agent_started', { agent_name: 'architect_questions', iteration: 1 }),
      event('agent_paused', { agent_name: 'architect_questions', partial_content: '{}' }),
      // The resumed run re-executes the agent — which must not be mistaken
      // for a mechanism that unsticks the header.
      event('agent_started', { agent_name: 'architect_questions', iteration: 2 }),
      event('agent_completed', { agent_name: 'architect_questions', iteration: 2 }),
    ]);
    expect(useWorkflowStore.getState().isPaused).toBe(true);
  });

  it('leaves iterationLimitGate latched when a seeded history ends on an unresolved gate', () => {
    useWorkflowStore.getState().replayState([
      rootStartedEvent(),
      event('iteration_limit_reached', {
        agent_name: 'architect_questions',
        gate_id: 'g1',
        current_iteration: 10,
        max_iterations: 10,
        skip_gates: false,
      }),
      event('agent_started', { agent_name: 'architect_questions', iteration: 11 }),
    ]);
    expect(useWorkflowStore.getState().iterationLimitGate).not.toBeNull();
  });

  it('leaves activeDialog latched when a seeded history ends on an unclosed dialog', () => {
    useWorkflowStore.getState().replayState([
      rootStartedEvent(),
      event('dialog_started', { agent_name: 'architect_questions', dialog_id: 'd1' }),
      event('agent_started', { agent_name: 'architect_questions', iteration: 2 }),
    ]);
    expect(useWorkflowStore.getState().activeDialog).not.toBeNull();
  });
});

describe('workflow-store — a counterpart event clears its latch', () => {
  function startWorkflow() {
    const { processEvent } = useWorkflowStore.getState();
    processEvent(rootStartedEvent());
    return processEvent;
  }

  it('leaves isPaused set after agent_paused, and a later agent_started does not clear it', () => {
    const processEvent = startWorkflow();

    processEvent(event('agent_started', { agent_name: 'architect_questions', iteration: 1 }));
    processEvent(event('agent_paused', { agent_name: 'architect_questions', partial_content: '{}' }));
    expect(useWorkflowStore.getState().isPaused).toBe(true);

    // The resumed run re-executes the agent — which must not be mistaken for
    // a mechanism that unsticks the header.
    processEvent(event('agent_started', { agent_name: 'architect_questions', iteration: 2 }));
    processEvent(event('agent_completed', { agent_name: 'architect_questions', iteration: 2 }));
    expect(useWorkflowStore.getState().isPaused).toBe(true);

    processEvent(event('agent_resumed', { agent_name: 'architect_questions' }));
    expect(useWorkflowStore.getState().isPaused).toBe(false);
  });

  it('leaves iterationLimitGate set after iteration_limit_reached until it is explicitly resolved', () => {
    const processEvent = startWorkflow();

    processEvent(event('iteration_limit_reached', {
      agent_name: 'architect_questions',
      gate_id: 'g1',
      current_iteration: 10,
      max_iterations: 10,
      skip_gates: false,
    }));
    expect(useWorkflowStore.getState().iterationLimitGate).not.toBeNull();

    processEvent(event('agent_started', { agent_name: 'architect_questions', iteration: 11 }));
    expect(useWorkflowStore.getState().iterationLimitGate).not.toBeNull();

    processEvent(event('iteration_limit_resolved', {
      agent_name: 'architect_questions',
      continue_execution: true,
      additional_iterations: 5,
    }));
    expect(useWorkflowStore.getState().iterationLimitGate).toBeNull();
  });

  it('leaves activeDialog set after dialog_started until the dialog completes', () => {
    const processEvent = startWorkflow();

    processEvent(event('dialog_started', { agent_name: 'architect_questions', dialog_id: 'd1' }));
    expect(useWorkflowStore.getState().activeDialog).not.toBeNull();

    processEvent(event('agent_started', { agent_name: 'architect_questions', iteration: 2 }));
    expect(useWorkflowStore.getState().activeDialog).not.toBeNull();

    processEvent(event('dialog_completed', { agent_name: 'architect_questions', dialog_id: 'd1' }));
    expect(useWorkflowStore.getState().activeDialog).toBeNull();
  });
});

describe('workflow-store — context_pct clears when context_window_used is unmeasurable (#412)', () => {
  it('agent_completed with a null context_window_used clears a previous iteration\'s context_pct', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('agent_started', { agent_name: 'writer', iteration: 1 }));
    processEvent(event('agent_completed', {
      agent_name: 'writer',
      iteration: 1,
      context_window_used: 500,
      context_window_max: 1000,
    }));

    expect(useWorkflowStore.getState().nodes.writer?.context_pct).toBe(50);

    processEvent(event('agent_started', { agent_name: 'writer', iteration: 2 }));
    processEvent(event('agent_completed', {
      agent_name: 'writer',
      iteration: 2,
      context_window_used: null,
      context_window_max: 1000,
    }));

    expect(useWorkflowStore.getState().nodes.writer?.context_pct).toBeUndefined();
  });

  it('agent_started on a re-running node clears the prior context_pct', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('agent_started', { agent_name: 'writer', iteration: 1 }));
    processEvent(event('agent_completed', {
      agent_name: 'writer',
      iteration: 1,
      context_window_used: 500,
      context_window_max: 1000,
    }));
    expect(useWorkflowStore.getState().nodes.writer?.context_pct).toBe(50);

    processEvent(event('agent_started', { agent_name: 'writer', iteration: 2 }));

    expect(useWorkflowStore.getState().nodes.writer?.context_pct).toBeUndefined();
  });

  it('parallel_agent_completed with a null context_window_used clears a previous context_pct', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(event('parallel_started', { group_name: 'team', agents: ['r1'] }));
    processEvent(event('parallel_agent_completed', {
      group_name: 'team',
      agent_name: 'r1',
      context_window_used: 500,
      context_window_max: 1000,
    }));
    expect(useWorkflowStore.getState().nodes.r1?.context_pct).toBe(50);

    processEvent(event('parallel_agent_completed', {
      group_name: 'team',
      agent_name: 'r1',
      context_window_used: null,
      context_window_max: 1000,
    }));

    expect(useWorkflowStore.getState().nodes.r1?.context_pct).toBeUndefined();
  });
});

/**
 * Regression coverage for issue #397: a gate response, dialog message,
 * dialog decline, or iteration-limit response sent while the WebSocket is
 * not connected must set `wsSendFailed` (previously silently dropped with
 * no feedback at all) and must clear it again once a send actually
 * succeeds.
 */
describe('workflow-store — send actions surface a failure when not connected (#397)', () => {
  beforeEach(() => {
    useWorkflowStore.setState({ _wsSend: null, wsSendFailed: false });
  });

  it('sendGateResponse sets wsSendFailed when there is no send function', () => {
    const { sendGateResponse } = useWorkflowStore.getState();
    sendGateResponse('reviewer', 'approve');
    expect(useWorkflowStore.getState().wsSendFailed).toBe(true);
  });

  it('sendGateResponse clears wsSendFailed on a successful send', () => {
    useWorkflowStore.setState({ wsSendFailed: true, _wsSend: () => {} });
    const { sendGateResponse } = useWorkflowStore.getState();
    sendGateResponse('reviewer', 'approve');
    expect(useWorkflowStore.getState().wsSendFailed).toBe(false);
  });

  it('sendDialogMessage sets wsSendFailed when there is no send function', () => {
    const { sendDialogMessage } = useWorkflowStore.getState();
    sendDialogMessage('writer', 'dlg-1', 'hello');
    expect(useWorkflowStore.getState().wsSendFailed).toBe(true);
  });

  it('sendDialogDecline sets wsSendFailed when there is no send function', () => {
    const { sendDialogDecline } = useWorkflowStore.getState();
    sendDialogDecline('writer', 'dlg-1');
    expect(useWorkflowStore.getState().wsSendFailed).toBe(true);
  });

  it('sendIterationLimitResponse sets wsSendFailed when there is no send function', () => {
    const { sendIterationLimitResponse } = useWorkflowStore.getState();
    sendIterationLimitResponse({ agent_name: 'writer' }, 'gate-1', 2);
    expect(useWorkflowStore.getState().wsSendFailed).toBe(true);
  });
});

describe('workflow-store — wsAuthFailed (#397)', () => {
  it('setWsAuthFailed sets and clears the flag', () => {
    const { setWsAuthFailed } = useWorkflowStore.getState();
    setWsAuthFailed(true);
    expect(useWorkflowStore.getState().wsAuthFailed).toBe(true);
    setWsAuthFailed(false);
    expect(useWorkflowStore.getState().wsAuthFailed).toBe(false);
  });
});

describe('workflow-store — compaction lifecycle events appear in the activity stream', () => {
  function startWorkflow() {
    const { processEvent } = useWorkflowStore.getState();
    processEvent(event('workflow_started', {
      name: 'root',
      agents: [{ name: 'writer' }],
      routes: [],
      parallel_groups: [],
      for_each_groups: [],
      entry_point: 'writer',
    }));
    return processEvent;
  }

  it('agent_compaction_config appends an informational activity entry', () => {
    const processEvent = startWorkflow();

    processEvent(event('agent_compaction_config', {
      agent_name: 'writer',
      model: 'gpt-4o',
      context_window: 128000,
      context_window_source: 'fallback',
      output_limit: 64000,
      output_limit_source: 'default',
      trigger_tokens: 64000,
      target_tokens: 63999,
    }));

    const node = useWorkflowStore.getState().nodes.writer!;
    expect(node.activity).toHaveLength(1);
    expect(node.activity[0]).toMatchObject({
      type: 'compaction-config',
      icon: '⚙',
      label: 'compaction',
      text: 'armed (window 128000 from fallback, output limit 64000 from default, trigger 64000, target 63999)',
    });
  });

  it('agent_compaction_start appends a start activity entry', () => {
    const processEvent = startWorkflow();

    processEvent(event('agent_compaction_start', {
      agent_name: 'writer',
      strategy: 'tiered',
      model: 'gpt-4o',
      context_window: 128000,
      context_window_source: 'fallback',
      output_limit: 64000,
      output_limit_source: 'default',
      trigger_tokens: 64000,
      target_tokens: 63999,
      messages_before: 50,
      tokens_before: 65000,
    }));

    const node = useWorkflowStore.getState().nodes.writer!;
    expect(node.activity).toHaveLength(1);
    expect(node.activity[0]).toMatchObject({
      type: 'compaction-start',
      icon: '🧹',
      label: 'compacting',
      text: 'compacting context (65000 tokens, window 128000 from fallback)',
    });
  });

  it('agent_compaction_complete appends a success activity entry when errored is false', () => {
    const processEvent = startWorkflow();

    processEvent(event('agent_compaction_complete', {
      agent_name: 'writer',
      strategy: 'tiered',
      model: 'gpt-4o',
      context_window: 128000,
      context_window_source: 'fallback',
      messages_before: 50,
      messages_after: 20,
      tokens_before: 65000,
      tokens_after: 20000,
      tokens_saved: 45000,
      elapsed: 5.5,
      errored: false,
    }));

    const node = useWorkflowStore.getState().nodes.writer!;
    expect(node.activity).toHaveLength(1);
    expect(node.activity[0]).toMatchObject({
      type: 'compaction-complete',
      icon: '🧹',
      label: 'compacted',
      text: '65000 → 20000 tokens (50 → 20 messages, 5.5s, saved 45000 tokens)',
    });
  });

  it('agent_compaction_config renders the disabled note when enabled is false', () => {
    const processEvent = startWorkflow();

    processEvent(event('agent_compaction_config', {
      agent_name: 'writer',
      model: 'gpt-4o',
      context_window: 128000,
      context_window_source: 'fallback',
      output_limit: 64000,
      output_limit_source: 'default',
      enabled: false,
      disabled_reason: 'trigger below 4096 tokens',
      trigger_tokens: null,
      target_tokens: null,
    }));

    const node = useWorkflowStore.getState().nodes.writer!;
    expect(node.activity).toHaveLength(1);
    expect(node.activity[0]).toMatchObject({
      type: 'compaction-config',
      icon: '⚙',
      label: 'compaction',
      text: 'disabled: trigger below 4096 tokens',
    });
  });

  it('agent_compaction_complete renders a warning entry when tiers degraded or still over trigger', () => {
    const processEvent = startWorkflow();

    processEvent(event('agent_compaction_complete', {
      agent_name: 'writer',
      strategy: 'tiered',
      model: 'gpt-4o',
      tokens_before: 65000,
      tokens_after: 63000,
      errored: false,
      degraded_tiers: ['summarize'],
      still_over_trigger: true,
    }));

    const node = useWorkflowStore.getState().nodes.writer!;
    expect(node.activity).toHaveLength(1);
    expect(node.activity[0]).toMatchObject({
      type: 'compaction-error',
      icon: '⚠️',
      label: 'compacted with warnings',
      text: '65000 → 63000 tokens (degraded tiers: summarize; still over trigger)',
    });
  });

  it('agent_compaction_complete appends a warning activity entry when errored is true', () => {
    const processEvent = startWorkflow();

    processEvent(event('agent_compaction_complete', {
      agent_name: 'writer',
      strategy: 'tiered',
      model: 'gpt-4o',
      errored: true,
      error_type: 'ValueError',
      message: 'summarization failed',
    }));

    const node = useWorkflowStore.getState().nodes.writer!;
    expect(node.activity).toHaveLength(1);
    expect(node.activity[0]).toMatchObject({
      type: 'compaction-error',
      icon: '⚠️',
      label: 'compaction failed',
      text: 'ValueError: summarization failed',
    });
  });

  it('compaction events are strictly replay/live-state neutral', () => {
    const processEvent = startWorkflow();

    // Set some global state
    useWorkflowStore.setState({
      isPaused: true,
      activeDialog: { agentName: 'writer', dialogId: 'dlg-1' },
      iterationLimitGate: { gate_id: 'g1', agent_name: 'writer', current_iteration: 10, max_iterations: 10, skip_gates: false, agent_history: [], possible_loop: false },
    });

    const stateBefore = {
      isPaused: useWorkflowStore.getState().isPaused,
      activeDialog: useWorkflowStore.getState().activeDialog,
      iterationLimitGate: useWorkflowStore.getState().iterationLimitGate,
      workflowStatus: useWorkflowStore.getState().workflowStatus,
    };

    processEvent(event('agent_compaction_config', { agent_name: 'writer', model: 'gpt-4o', context_window: 128000, context_window_source: 'f', output_limit: 64000, output_limit_source: 'f', trigger_tokens: 64000, target_tokens: 63999 }));
    processEvent(event('agent_compaction_start', { agent_name: 'writer', strategy: 'tiered', model: 'gpt-4o', context_window: 128000, context_window_source: 'f', output_limit: 64000, output_limit_source: 'f', trigger_tokens: 64000, target_tokens: 63999 }));
    processEvent(event('agent_compaction_complete', { agent_name: 'writer', strategy: 'tiered', model: 'gpt-4o', errored: false }));

    const stateAfter = {
      isPaused: useWorkflowStore.getState().isPaused,
      activeDialog: useWorkflowStore.getState().activeDialog,
      iterationLimitGate: useWorkflowStore.getState().iterationLimitGate,
      workflowStatus: useWorkflowStore.getState().workflowStatus,
    };

    expect(stateAfter).toEqual(stateBefore);
  });
});
