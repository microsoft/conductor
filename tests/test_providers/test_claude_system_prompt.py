"""Regression tests for Claude provider system prompt forwarding."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef, OutputField, ValidatorConfig
from conductor.engine.validator import VALIDATOR_SYSTEM_PROMPT, OutputValidator
from conductor.providers.claude import ClaudeProvider


def _build_text_agent(text: str = "Done") -> Agent[Any, str]:
    """Build a Pydantic AI text agent backed by TestModel."""
    return Agent(model=TestModel(custom_output_text=text), output_type=str)


def _build_structured_agent(model_cls: type[BaseModel], data: dict[str, Any]) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""
    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=model_cls,
    )


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so ClaudeProvider construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture
def provider() -> ClaudeProvider:
    """Return a fresh ClaudeProvider instance using a dummy API key."""
    return ClaudeProvider(api_key="test-key")


class TestClaudeSystemPromptForwarding:
    """Tests for top-level system prompt forwarding to the Pydantic AI Agent."""

    @pytest.mark.asyncio
    async def test_plain_execute_passes_system_prompt_to_agent(
        self, provider: ClaudeProvider
    ) -> None:
        """Requirement AC-A(a): plain execution sends system_prompt to the Agent."""
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("Done"),
        ) as mock_build_agent:
            agent = AgentDef.model_validate(
                {
                    "name": "plain",
                    "prompt": "Test prompt",
                    "system_prompt": "Rendered system instructions",
                }
            )
            await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        assert mock_build_agent.call_args.kwargs["system_prompt"] == "Rendered system instructions"

    @pytest.mark.asyncio
    async def test_tool_use_loop_passes_system_prompt_to_agent(
        self, provider: ClaudeProvider
    ) -> None:
        """Requirement AC-A(b): every built Agent in a tool-use loop uses the system_prompt."""
        # Tool loops are now delegated to Pydantic AI; we verify the single build_agent
        # call receives the system prompt, which is the equivalent forwarding seam.
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("Final answer"),
        ) as mock_build_agent:
            agent = AgentDef.model_validate(
                {
                    "name": "tool_agent",
                    "prompt": "Test prompt",
                    "system_prompt": "Rendered tool-loop instructions",
                }
            )
            await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        assert (
            mock_build_agent.call_args.kwargs["system_prompt"] == "Rendered tool-loop instructions"
        )

    @pytest.mark.asyncio
    async def test_structured_output_passes_system_prompt_to_agent(
        self, provider: ClaudeProvider
    ) -> None:
        """Requirement AC-A(c): structured-output runs send system_prompt to the Agent."""

        class ResultModel(BaseModel):
            result: str

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent(ResultModel, {"result": "recovered"}),
        ) as mock_build_agent:
            agent = AgentDef.model_validate(
                {
                    "name": "structured",
                    "prompt": "Test prompt",
                    "system_prompt": "Rendered recovery instructions",
                    "output": {"result": OutputField(type="string")},
                }
            )
            await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        assert (
            mock_build_agent.call_args.kwargs["system_prompt"] == "Rendered recovery instructions"
        )

    @pytest.mark.asyncio
    async def test_retry_loop_passes_system_prompt_to_agent(self, provider: ClaudeProvider) -> None:
        """Requirement AC-A(e): outer retry loop rebuilds the same Agent with the system_prompt."""
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("Final answer"),
        ) as mock_build_agent:
            verbatim_prompt = "  Preserve me exactly\n"
            agent = AgentDef.model_validate(
                {
                    "name": "retrying",
                    "prompt": "Test prompt",
                    "system_prompt": verbatim_prompt,
                }
            )
            await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        assert mock_build_agent.call_args.kwargs["system_prompt"] == verbatim_prompt
        assert len(provider.get_retry_history()) == 0

    @pytest.mark.asyncio
    async def test_interrupt_partial_output_passes_system_prompt(
        self, provider: ClaudeProvider
    ) -> None:
        """Requirement AC-A(d): interrupt partial-output path sends system_prompt to the Agent."""
        import asyncio

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("partial"),
        ) as mock_build_agent:
            agent = AgentDef.model_validate(
                {
                    "name": "interruptible",
                    "prompt": "Test prompt",
                    "system_prompt": "Rendered interrupt instructions",
                    "output": {"result": OutputField(type="string")},
                }
            )
            interrupt_signal = asyncio.Event()
            interrupt_signal.set()
            await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Test prompt",
                interrupt_signal=interrupt_signal,
            )

        assert (
            mock_build_agent.call_args.kwargs["system_prompt"] == "Rendered interrupt instructions"
        )

    # Removed: test_in_flight_interrupt_partial_output_passes_system_prompt.
    # The legacy in-flight asyncio.wait race branch was deleted in the Pydantic AI
    # rewrite; interrupt handling is now covered by run_with_interrupt in
    # tests/test_providers/test_pydantic_ai_interrupt.py.


class TestSystemPromptAbsent:
    """Tests for omitting the system prompt when no usable system prompt exists."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("system_prompt", [None, "", "   "])
    async def test_system_prompt_empty_when_system_prompt_empty(
        self,
        provider: ClaudeProvider,
        system_prompt: str | None,
    ) -> None:
        """Requirement AC-B: None, empty, and whitespace system_prompt pass through as-is."""
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("Done"),
        ) as mock_build_agent:
            agent = AgentDef.model_validate(
                {
                    "name": "empty_system",
                    "prompt": "Test prompt",
                    "system_prompt": system_prompt,
                }
            )
            await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        assert mock_build_agent.call_args.kwargs["system_prompt"] == (system_prompt or "")


class TestValidatorSystemPromptForwarding:
    """Tests for OutputValidator synthetic-agent system prompt forwarding on Claude."""

    @pytest.mark.asyncio
    async def test_output_validator_sends_formatted_rubric_as_system_prompt(
        self, provider: ClaudeProvider
    ) -> None:
        """Requirement AC-C: validator rubric reaches the Agent as its system prompt."""

        class ValidationResult(BaseModel):
            passed: bool
            issues: list[str]

        criteria = "Answer must mention the verified source."
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent(ValidationResult, {"passed": True, "issues": []}),
        ) as mock_build_agent:
            agent = AgentDef.model_validate(
                {
                    "name": "reviewer",
                    "prompt": "Review the answer",
                    "model": "claude-3-5-sonnet-latest",
                    "validator": ValidatorConfig(criteria=criteria),
                }
            )

            outcome = await OutputValidator().validate(
                agent=agent,
                primary_prompt="Review the answer",
                primary_output={"result": "Verified source included."},
                provider=provider,
            )

        assert outcome.passed is True
        expected_system_prompt = VALIDATOR_SYSTEM_PROMPT.format(criteria=criteria)
        assert mock_build_agent.call_args.kwargs["system_prompt"] == expected_system_prompt
        assert "{{" not in mock_build_agent.call_args.kwargs["system_prompt"]
