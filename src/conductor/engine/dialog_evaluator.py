"""Dialog evaluator for conditional agent-user dialog triggering.

This module provides the DialogEvaluator class which uses an LLM call
to determine whether an agent should enter dialog mode based on
user-defined criteria in the trigger_prompt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef
    from conductor.providers.base import AgentProvider

logger = logging.getLogger(__name__)

EVALUATOR_SYSTEM_PROMPT = """\
You are a dialog trigger evaluator. Your job is to examine an agent's output \
and decide whether the agent should pause and start a conversation with the user.

The workflow author has defined the following criteria for triggering dialog:

--- CRITERIA ---
{trigger_prompt}
--- END CRITERIA ---

Examine the agent's output below and decide:
1. Does the output meet the criteria for triggering a dialog with the user?
2. If yes, what question or topic should the agent open the dialog with? \
Include full context — file paths, code snippets, data points, and reasoning — \
so the user has everything they need to respond meaningfully.

You MUST respond with ONLY a JSON object (no markdown, no extra text):
{{"trigger": true/false, "reason": "brief explanation", "question": "the opening \
question to ask the user with full context (only if trigger is true)"}}
"""

EVALUATOR_USER_PROMPT = """\
Agent name: {agent_name}
Agent output:
{agent_output}
"""


@dataclass
class DialogEvaluation:
    """Result of a dialog trigger evaluation.

    Attributes:
        trigger: Whether dialog should be triggered.
        reason: Explanation of why dialog was or was not triggered.
        question: The opening question for the dialog (if triggered).
    """

    trigger: bool
    reason: str
    question: str = ""


class DialogEvaluator:
    """Evaluates whether an agent should enter dialog mode.

    Uses a single LLM call to evaluate the agent's output against
    user-defined trigger criteria.
    """

    async def evaluate(
        self,
        agent: AgentDef,
        output: dict[str, Any],
        provider: AgentProvider,
    ) -> DialogEvaluation:
        """Evaluate whether an agent's output should trigger dialog.

        Args:
            agent: The agent definition with dialog config.
            output: The agent's output content.
            provider: The provider to use for the evaluation LLM call.

        Returns:
            DialogEvaluation with trigger decision and opening question.
        """
        if not agent.dialog:
            return DialogEvaluation(trigger=False, reason="No dialog config")

        return await self._run_evaluator(agent, output, provider)

    async def _run_evaluator(
        self,
        agent: AgentDef,
        output: dict[str, Any],
        provider: AgentProvider,
    ) -> DialogEvaluation:
        """Run the LLM evaluator to decide whether dialog is needed.

        Args:
            agent: The agent definition with dialog config.
            output: The agent's output content.
            provider: The provider for the LLM call.

        Returns:
            DialogEvaluation with trigger decision and opening question.
        """
        try:
            output_str = json.dumps(output, indent=2, default=str)
        except (TypeError, ValueError):
            output_str = str(output)

        system_prompt = EVALUATOR_SYSTEM_PROMPT.format(
            trigger_prompt=agent.dialog.trigger_prompt,
        )
        user_prompt = EVALUATOR_USER_PROMPT.format(
            agent_name=agent.name,
            agent_output=output_str[:4000],  # Truncate to avoid excessive tokens
        )

        try:
            result = await provider.execute_dialog_turn(
                system_prompt=system_prompt,
                user_message=user_prompt,
                history=[],
                model=agent.model,
            )
            return self._parse_evaluation(result)
        except Exception:
            logger.warning(
                "Dialog evaluation failed for agent '%s', skipping dialog",
                agent.name,
                exc_info=True,
            )
            return DialogEvaluation(
                trigger=False,
                reason="Evaluation failed",
            )

    def _parse_evaluation(self, response: str) -> DialogEvaluation:
        """Parse the evaluator LLM response into a DialogEvaluation.

        Args:
            response: Raw LLM response text.

        Returns:
            Parsed DialogEvaluation.
        """
        try:
            text = response.strip()
            # Handle markdown code blocks
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

            data = json.loads(text)
            return DialogEvaluation(
                trigger=bool(data.get("trigger", False)),
                reason=str(data.get("reason", "")),
                question=str(data.get("question", "")),
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Failed to parse dialog evaluation response: %s", response[:200])
            return DialogEvaluation(
                trigger=False,
                reason=f"Failed to parse evaluation: {response[:100]}",
            )
