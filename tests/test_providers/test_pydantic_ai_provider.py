"""Hermetic tests for ClaudeProvider after Pydantic AI rewrite.

Tests verify that ClaudeProvider.execute() and execute_dialog_turn() use the
new Pydantic AI pipeline end-to-end without network calls. They mock the
Pydantic AI model and the MCP manager resolution.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ValidationError
from conductor.providers.claude import ClaudeProvider


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so ClaudeProvider construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture
def provider() -> ClaudeProvider:
    """Return a fresh ClaudeProvider instance using a dummy API key."""
    return ClaudeProvider(api_key="test-key")


@pytest.fixture
def no_mcp_manager(provider: ClaudeProvider) -> Any:
    """Disable MCP manager resolution so execute() does not spawn tools."""
    with patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None) as mock:
        yield mock


def _build_text_agent(text: str) -> Agent[Any, str]:
    """Build a Pydantic AI text agent backed by TestModel."""
    return Agent(model=TestModel(custom_output_text=text), output_type=str)


def _build_structured_agent(model_cls: type[BaseModel], data: dict[str, Any]) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""
    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=model_cls,
    )


class TestExecuteHappyPath:
    """Tests for the normal execute() completion path."""

    async def test_execute_returns_text_agent_output(
        self, provider: ClaudeProvider, no_mcp_manager: Any
    ) -> None:
        """execute() returns text output from a TestModel-backed agent."""
        agent = AgentDef(name="greeter", model="test", prompt="say hi")
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("hello"),
        ):
            output = await provider.execute(agent, {}, "say hi")

        assert output.content == {"result": "hello"}
        assert output.partial is False
        assert output.model == "test"
        assert output.tokens_used is not None
        assert output.input_tokens is not None
        assert output.output_tokens is not None


class TestExecuteStructuredOutput:
    """Tests for the structured-output execute() path."""

    async def test_execute_returns_validated_structured_output(
        self, provider: ClaudeProvider, no_mcp_manager: Any
    ) -> None:
        """execute() returns validated structured output from a Pydantic model."""

        class AnswerModel(BaseModel):
            answer: str

        agent = AgentDef(
            name="greeter",
            model="test",
            prompt="say hi",
            output={"answer": OutputField(type="string")},
        )
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_structured_agent(AnswerModel, {"answer": "hello"}),
        ):
            output = await provider.execute(agent, {}, "say hi")

        assert output.content == {"answer": "hello"}
        assert output.partial is False


class TestExecuteInterrupt:
    """Tests for the interrupt-aware execute() path."""

    async def test_execute_with_interrupt_returns_partial_output(
        self, provider: ClaudeProvider, no_mcp_manager: Any
    ) -> None:
        """execute() returns a partial AgentOutput when interrupt fires before the run."""
        agent = AgentDef(name="interrupted", model="test", prompt="do work")
        signal = asyncio.Event()
        signal.set()

        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("partial"),
        ):
            output = await provider.execute(agent, {}, "do work", interrupt_signal=signal)

        assert output.partial is True
        assert output.content == {"result": "partial"}


class TestRetryHistory:
    """Tests for retry-history capture."""

    async def test_execute_records_retry_history(
        self, provider: ClaudeProvider, no_mcp_manager: Any
    ) -> None:
        """execute() records agent_retry events in get_retry_history()."""

        def event_callback(event_type: str, data: dict[str, Any]) -> None:
            if event_type == "agent_retry":
                pass

        agent = AgentDef(name="retry_agent", model="test", prompt="work")
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("hello"),
        ):
            await provider.execute(
                agent,
                {},
                "work",
                event_callback=event_callback,
            )

        # No retry happened in the happy path, so history should be empty.
        assert provider.get_retry_history() == []


class TestExecuteDialogTurn:
    """Tests for the dialog-turn path."""

    async def test_execute_dialog_turn_returns_text(self, provider: ClaudeProvider) -> None:
        """execute_dialog_turn() returns the Pydantic AI text response."""

        async def fake_run(*args: Any, **kwargs: Any) -> Any:
            class FakeResult:
                output = "dialog reply"

            return FakeResult()

        with patch(
            "conductor.providers._pydantic_ai.agent_builder._resolve_anthropic_model"
        ), patch("pydantic_ai.Agent") as mock_agent_cls:
            mock_agent = mock_agent_cls.return_value
            mock_agent.run = fake_run
            result = await provider.execute_dialog_turn(
                "system prompt",
                "user message",
                history=[{"role": "user", "content": "previous"}],
                model="test",
            )

        assert result == "dialog reply"


class TestExecuteDialogTurnReasoning:
    """Tests for reasoning effort validation in dialog turns."""

    async def test_dialog_turn_rejects_non_thinking_model_with_reasoning(
        self, provider: ClaudeProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """execute_dialog_turn() raises ValidationError for reasoning on non-thinking models."""
        monkeypatch.setattr(provider, "_default_reasoning_effort", "medium")

        with pytest.raises(ValidationError):
            await provider.execute_dialog_turn(
                "system",
                "user",
                model="claude-3-5-sonnet-latest",
            )
