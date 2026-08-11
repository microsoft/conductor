import { beforeEach, describe, expect, it } from 'vitest';
import { useWorkflowStore } from './workflow-store';
import type { WorkflowEvent } from '@/types/events';

/**
 * Coverage for the `type: questions` node (issue #376).
 *
 * A questions node presents every one of its questions through the ordinary
 * gate channel under a single node name, staying `waiting` throughout. That
 * makes two things load-bearing on the client: `gate_prompt_id` (so a late
 * click can't resolve a later question, and so the form re-enables between
 * questions) and per-question outcome tracking (so going back and
 * re-answering doesn't double-count).
 */

function event(
  type: WorkflowEvent['type'],
  data: Record<string, unknown>,
  timestamp = Date.now() / 1000,
): WorkflowEvent {
  return { type, timestamp, data };
}

function node(name: string) {
  return useWorkflowStore.getState().nodes[name];
}

beforeEach(() => {
  useWorkflowStore.setState(useWorkflowStore.getInitialState(), true);
});

describe('questions node — lifecycle', () => {
  it('marks the node waiting and zeroes its counters', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(
      event('questions_presented', {
        agent_name: 'ask',
        total: 3,
        prompt: 'Answer what you can.',
        questions: [{ id: 'q1', text: 'First?' }],
      }),
    );

    const nd = node('ask');
    expect(nd?.type).toBe('questions');
    expect(nd?.status).toBe('waiting');
    expect(nd?.questions_total).toBe(3);
    expect(nd?.questions_answered_count).toBe(0);
    expect(nd?.questions_skipped_count).toBe(0);
  });

  it('completes exactly once and takes the authoritative counts', () => {
    const { processEvent } = useWorkflowStore.getState();
    processEvent(event('questions_presented', { agent_name: 'ask', total: 2 }));
    const before = useWorkflowStore.getState().agentsCompleted;

    processEvent(
      event('questions_completed', {
        agent_name: 'ask',
        outcome: 'completed',
        answered_count: 1,
        skipped_count: 1,
      }),
    );

    const nd = node('ask');
    expect(nd?.status).toBe('completed');
    expect(nd?.questions_outcome).toBe('completed');
    expect(nd?.questions_answered_count).toBe(1);
    expect(nd?.questions_skipped_count).toBe(1);
    expect(useWorkflowStore.getState().agentsCompleted).toBe(before + 1);
  });
});

describe('questions node — progress counting', () => {
  it('does not exceed the total when a question is re-answered after Back', () => {
    // Back pops the answer and emits no event, so a running +1 counter would
    // reach 3 of 2 and drive the progress bar past 100%.
    const { processEvent } = useWorkflowStore.getState();
    processEvent(event('questions_presented', { agent_name: 'ask', total: 2 }));

    processEvent(
      event('questions_answered', {
        agent_name: 'ask',
        question_id: 'q1',
        cursor: 0,
        total: 2,
        source: 'free_text',
        skipped: false,
      }),
    );
    processEvent(
      event('questions_answered', {
        agent_name: 'ask',
        question_id: 'q2',
        cursor: 1,
        total: 2,
        source: 'free_text',
        skipped: false,
      }),
    );
    // ...user goes Back and revises q2.
    processEvent(
      event('questions_answered', {
        agent_name: 'ask',
        question_id: 'q2',
        cursor: 1,
        total: 2,
        source: 'free_text',
        skipped: false,
      }),
    );

    const nd = node('ask');
    expect(nd?.questions_answered_count).toBe(2);
    expect((nd?.questions_answered_count ?? 0) + (nd?.questions_skipped_count ?? 0)).toBeLessThanOrEqual(
      nd?.questions_total ?? 0,
    );
  });

  it('moves a question between answered and skipped rather than double-counting', () => {
    const { processEvent } = useWorkflowStore.getState();
    processEvent(event('questions_presented', { agent_name: 'ask', total: 1 }));

    processEvent(
      event('questions_answered', {
        agent_name: 'ask',
        question_id: 'q1',
        cursor: 0,
        total: 1,
        source: 'skipped',
        skipped: true,
      }),
    );
    expect(node('ask')?.questions_skipped_count).toBe(1);

    processEvent(
      event('questions_answered', {
        agent_name: 'ask',
        question_id: 'q1',
        cursor: 0,
        total: 1,
        source: 'free_text',
        skipped: false,
      }),
    );

    const nd = node('ask');
    expect(nd?.questions_answered_count).toBe(1);
    expect(nd?.questions_skipped_count).toBe(0);
  });
});

describe('questions node — rejection banner', () => {
  it('survives the gate_presented that immediately follows it', () => {
    // The engine emits questions_answer_rejected then re-presents the same
    // question, so clearing the reason on gate_presented would make the
    // banner unreachable.
    const { processEvent } = useWorkflowStore.getState();
    processEvent(event('questions_presented', { agent_name: 'ask', total: 1 }));

    processEvent(
      event('questions_answer_rejected', {
        agent_name: 'ask',
        question_id: 'q1',
        reason: 'This question is required and cannot be skipped.',
      }),
    );
    processEvent(
      event('gate_presented', {
        agent_name: 'ask',
        options: ['__skip__'],
        option_details: [{ label: 'Skip', value: '__skip__', route: '' }],
        prompt: 'Q1',
        prompt_id: 'ask:run:2',
      }),
    );

    expect(node('ask')?.questions_reject_reason).toContain('required');
  });

  it('clears once an answer is accepted', () => {
    const { processEvent } = useWorkflowStore.getState();
    processEvent(event('questions_presented', { agent_name: 'ask', total: 1 }));
    processEvent(
      event('questions_answer_rejected', {
        agent_name: 'ask',
        question_id: 'q1',
        reason: 'Required.',
      }),
    );

    processEvent(
      event('questions_answered', {
        agent_name: 'ask',
        question_id: 'q1',
        cursor: 0,
        total: 1,
        source: 'free_text',
        skipped: false,
      }),
    );

    expect(node('ask')?.questions_reject_reason).toBeNull();
  });
});

describe('gate prompt_id round-trip', () => {
  it('stores the token from gate_presented', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(
      event('gate_presented', {
        agent_name: 'ask',
        options: ['a'],
        option_details: [{ label: 'A', value: 'a', route: '' }],
        prompt: 'Q',
        prompt_id: 'ask:run:3',
      }),
    );

    expect(node('ask')?.gate_prompt_id).toBe('ask:run:3');
  });

  it('sends the token back with the response', () => {
    const sent: Record<string, unknown>[] = [];
    useWorkflowStore.setState({
      _wsSend: (msg: object) => {
        sent.push(msg as Record<string, unknown>);
      },
    });

    useWorkflowStore.getState().sendGateResponse('ask', 'a', { answer: 'x' }, 'ask:run:3');

    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      type: 'gate_response',
      agent_name: 'ask',
      selected_value: 'a',
      additional_input: { answer: 'x' },
      prompt_id: 'ask:run:3',
    });
  });

  it('sends null when there is no token, so standalone gates still resolve', () => {
    const sent: Record<string, unknown>[] = [];
    useWorkflowStore.setState({
      _wsSend: (msg: object) => {
        sent.push(msg as Record<string, unknown>);
      },
    });

    useWorkflowStore.getState().sendGateResponse('approval', 'approve');

    expect(sent[0]).toMatchObject({ prompt_id: null });
  });

  it('is null for a gate that carries no token', () => {
    const { processEvent } = useWorkflowStore.getState();

    processEvent(
      event('gate_presented', {
        agent_name: 'approval',
        options: ['approve'],
        option_details: [{ label: 'Approve', value: 'approve', route: 'next' }],
        prompt: 'Approve?',
      }),
    );

    expect(node('approval')?.gate_prompt_id).toBeNull();
  });
});
