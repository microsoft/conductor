"""End-to-end integration test for dialog mode.

Exercises the real `WorkflowEngine` path: agent executes → evaluator triggers →
dialog handler runs the conversation → agent re-executes with the dialog
transcript injected into its guidance section. Pure-mock all I/O so there's no
SDK or network dependency.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    DialogConfig,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.copilot import CopilotProvider


@pytest.mark.asyncio
async def test_dialog_triggers_then_re_executes_with_transcript() -> None:
    """Full integration: dialog triggers → user converses → agent re-runs with transcript.

    Asserts that:
    - the agent runs twice (initial output + post-dialog re-execution)
    - the second run's rendered prompt contains the dialog transcript
    """
    captured_prompts: list[str] = []
    handler_call_count = 0

    def mock_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        nonlocal handler_call_count
        handler_call_count += 1
        captured_prompts.append(prompt)
        if handler_call_count == 1:
            return {"answer": "I'm uncertain about the constraints."}
        return {"answer": "After our chat, the answer is X."}

    provider = CopilotProvider(mock_handler=mock_handler)

    # Mock the provider's dialog-turn API used by both the evaluator and
    # the dialog handler. The order of calls is:
    #   1. evaluator.evaluate() -> returns trigger=true JSON
    #   2. dialog handler turn 1 -> returns agent reply with terminal READY marker
    dialog_turn_responses = [
        '{"trigger": true, "reason": "uncertain", "question": "What did you mean by X?"}',
        "Got it, ready to proceed. [READY_TO_CONTINUE]",
    ]
    provider.execute_dialog_turn = AsyncMock(side_effect=dialog_turn_responses)  # type: ignore[method-assign]

    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="dialog-integration",
            entry_point="advisor",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=5),
        ),
        agents=[
            AgentDef(
                name="advisor",
                model="gpt-4",
                prompt="Advise on: {{ workflow.input.topic }}",
                output={"answer": OutputField(type="string")},
                dialog=DialogConfig(trigger_prompt="Trigger if the agent is uncertain"),
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"answer": "{{ advisor.output.answer }}"},
    )

    engine = WorkflowEngine(config, provider)

    # Engage in CLI mode: patch the dialog handler's interactive prompts.
    with (
        patch.object(
            engine._dialog_handler,
            "_ask_engagement",
            new_callable=AsyncMock,
            return_value="engage",
        ),
        patch.object(
            engine._dialog_handler,
            "_get_user_input",
            new_callable=AsyncMock,
            # First: user's content message; second: 'yes' to approve agent's continue proposal.
            side_effect=["please clarify the constraints", "yes"],
        ),
    ):
        result = await engine.run({"topic": "production rollout"})

    # The agent must have been called twice (initial + re-execute after dialog)
    assert handler_call_count == 2, (
        f"Expected agent to run twice, got {handler_call_count}. "
        "Dialog re-execution did not happen."
    )

    # Workflow output reflects the post-dialog answer
    assert result["answer"] == "After our chat, the answer is X."

    # The re-execution prompt MUST contain the dialog transcript markers
    re_execution_prompt = captured_prompts[1]
    assert "--- DIALOG WITH USER ---" in re_execution_prompt
    assert "--- END DIALOG ---" in re_execution_prompt
    assert "please clarify the constraints" in re_execution_prompt
