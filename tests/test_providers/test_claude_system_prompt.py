"""Regression tests for Claude provider system prompt forwarding."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField, ValidatorConfig
from conductor.engine.validator import VALIDATOR_SYSTEM_PROMPT, OutputValidator
from conductor.exceptions import ProviderError

if TYPE_CHECKING:
    from conductor.providers.claude import ClaudeProvider, RetryConfig


def _text_response(text: str = "Done") -> Mock:
    response = Mock()
    response.content = [SimpleNamespace(type="text", text=text)]
    response.usage = SimpleNamespace(input_tokens=10, output_tokens=20)
    return response


def _tool_response(
    name: str,
    input_data: dict[str, str | bool | list[str]],
    tool_id: str = "tool-1",
) -> Mock:
    response = Mock()
    response.content = [SimpleNamespace(type="tool_use", name=name, input=input_data, id=tool_id)]
    response.usage = SimpleNamespace(input_tokens=10, output_tokens=20)
    return response


def _mock_claude_provider(
    mock_anthropic_module: Mock,
    mock_anthropic_class: Mock,
    retry_config: RetryConfig | None = None,
) -> tuple[ClaudeProvider, Mock]:
    mock_anthropic_module.__version__ = "0.77.0"
    mock_client = Mock()
    mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
    mock_anthropic_class.return_value = mock_client

    from conductor.providers.claude import ClaudeProvider

    provider = ClaudeProvider(api_key="test-key", retry_config=retry_config)
    return provider, mock_client


class TestClaudeSystemPromptForwarding:
    """Tests for top-level system kwarg forwarding to Anthropic Messages API."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_plain_execute_passes_system_prompt_to_sdk(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement AC-A(a): plain Claude execution sends rendered system_prompt as system."""
        provider, mock_client = _mock_claude_provider(mock_anthropic_module, mock_anthropic_class)
        mock_client.messages.create = AsyncMock(return_value=_text_response())

        agent = AgentDef.model_validate(
            {
                "name": "plain",
                "prompt": "Test prompt",
                "system_prompt": "Rendered system instructions",
            }
        )

        await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == "Rendered system instructions"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_tool_use_loop_passes_system_prompt_on_every_sdk_call(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement AC-A(b): every SDK call in a tool-use loop sends system_prompt."""
        provider, mock_client = _mock_claude_provider(mock_anthropic_module, mock_anthropic_class)
        mock_client.messages.create = AsyncMock(
            side_effect=[
                _tool_response("lookup", {"query": "status"}),
                _text_response("Final answer"),
            ]
        )
        mock_mcp = Mock()
        mock_mcp.has_servers.return_value = True
        mock_mcp.get_all_tools.return_value = [
            {
                "name": "lookup",
                "description": "Lookup data",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
        ]
        mock_mcp.call_tool = AsyncMock(return_value="tool result")
        provider._mcp_manager = mock_mcp

        agent = AgentDef.model_validate(
            {
                "name": "tool_agent",
                "prompt": "Test prompt",
                "system_prompt": "Rendered tool-loop instructions",
            }
        )

        await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        assert mock_client.messages.create.call_count == 2
        for call in mock_client.messages.create.call_args_list:
            assert call.kwargs["system"] == "Rendered tool-loop instructions"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_parse_recovery_passes_system_prompt_on_initial_and_recovery_calls(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement AC-A(c): parse recovery retries send system_prompt on each SDK call."""
        provider, mock_client = _mock_claude_provider(mock_anthropic_module, mock_anthropic_class)
        mock_client.messages.create = AsyncMock(
            side_effect=[
                _text_response("not json"),
                _tool_response("emit_output", {"result": "recovered"}),
            ]
        )

        agent = AgentDef.model_validate(
            {
                "name": "structured",
                "prompt": "Test prompt",
                "system_prompt": "Rendered recovery instructions",
                "output": {"result": OutputField(type="string")},
            }
        )

        await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        assert mock_client.messages.create.call_count == 2
        for call in mock_client.messages.create.call_args_list:
            assert call.kwargs["system"] == "Rendered recovery instructions"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_retry_loop_passes_system_prompt_on_every_attempt(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement AC-A(e): outer retry loop resends the same system_prompt per attempt."""
        from conductor.providers.claude import RetryConfig

        retry_config = RetryConfig(max_attempts=2, base_delay=0.0, max_delay=0.0, jitter=0.0)
        provider, mock_client = _mock_claude_provider(
            mock_anthropic_module, mock_anthropic_class, retry_config=retry_config
        )
        mock_client.messages.create = AsyncMock(
            side_effect=[
                ProviderError("transient Claude failure", status_code=503),
                _text_response("Final answer"),
            ]
        )

        verbatim_prompt = "  Preserve me exactly\n"
        agent = AgentDef.model_validate(
            {
                "name": "retrying",
                "prompt": "Test prompt",
                "system_prompt": verbatim_prompt,
            }
        )

        with patch("conductor.providers.claude.asyncio.sleep", new_callable=AsyncMock):
            await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        assert mock_client.messages.create.call_count == 2
        for call in mock_client.messages.create.call_args_list:
            assert call.kwargs["system"] == verbatim_prompt
        assert len(provider.get_retry_history()) == 1

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_interrupt_partial_output_passes_system_prompt(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement AC-A(d): interrupt partial-output SDK call sends system_prompt."""
        provider, mock_client = _mock_claude_provider(mock_anthropic_module, mock_anthropic_class)
        mock_client.messages.create = AsyncMock(
            return_value=_tool_response("emit_output", {"result": "partial"})
        )
        interrupt_signal = asyncio.Event()
        interrupt_signal.set()

        agent = AgentDef.model_validate(
            {
                "name": "interruptible",
                "prompt": "Test prompt",
                "system_prompt": "Rendered interrupt instructions",
                "output": {"result": OutputField(type="string")},
            }
        )

        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Test prompt",
            interrupt_signal=interrupt_signal,
        )

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == "Rendered interrupt instructions"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_in_flight_interrupt_partial_output_passes_system_prompt(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement AC-A(f): interrupt during a blocked API call keeps system_prompt.

        Covers the in-flight race branch (asyncio.wait FIRST_COMPLETED), unlike
        the pre-set event test which only covers the between-turn branch.
        """
        provider, mock_client = _mock_claude_provider(mock_anthropic_module, mock_anthropic_class)

        call_started = asyncio.Event()
        never_release = asyncio.Event()
        interrupt_signal = asyncio.Event()
        call_number = 0

        async def create_side_effect(**kwargs: object) -> Mock:
            nonlocal call_number
            call_number += 1
            if call_number == 1:
                call_started.set()
                # Block until cancelled by the provider's interrupt race; the
                # CancelledError must propagate so the partial-output path runs.
                await never_release.wait()
            return _tool_response("emit_output", {"result": "partial"})

        mock_client.messages.create = AsyncMock(side_effect=create_side_effect)

        verbatim_prompt = "Rendered interrupt instructions\n"
        agent = AgentDef.model_validate(
            {
                "name": "interruptible",
                "prompt": "Test prompt",
                "system_prompt": verbatim_prompt,
                "output": {"result": OutputField(type="string")},
            }
        )

        execute_task = asyncio.create_task(
            provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Test prompt",
                interrupt_signal=interrupt_signal,
            )
        )

        try:
            await asyncio.wait_for(call_started.wait(), timeout=5)
            interrupt_signal.set()
            output = await asyncio.wait_for(execute_task, timeout=5)
        finally:
            if not execute_task.done():
                execute_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await execute_task

        assert output.partial is True
        assert mock_client.messages.create.call_count == 2
        partial_kwargs = mock_client.messages.create.call_args_list[1].kwargs
        assert partial_kwargs["system"] == verbatim_prompt
        assert "interrupted" in partial_kwargs["messages"][-1]["content"].lower()


class TestSystemKwargAbsent:
    """Tests for omitting the system kwarg when no usable system prompt exists."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    @pytest.mark.parametrize("system_prompt", [None, "", "   "])
    async def test_system_kwarg_absent_when_system_prompt_empty(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        system_prompt: str | None,
    ) -> None:
        """Requirement AC-B: None, empty, and whitespace system_prompt omit system kwarg."""
        provider, mock_client = _mock_claude_provider(mock_anthropic_module, mock_anthropic_class)
        mock_client.messages.create = AsyncMock(return_value=_text_response())

        agent = AgentDef.model_validate(
            {"name": "empty_system", "prompt": "Test prompt", "system_prompt": system_prompt}
        )

        await provider.execute(agent=agent, context={}, rendered_prompt="Test prompt")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert "system" not in call_kwargs


class TestValidatorSystemPromptForwarding:
    """Tests for OutputValidator synthetic-agent system prompt forwarding on Claude."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_output_validator_sends_formatted_rubric_as_system_prompt(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement AC-C: validator rubric reaches Claude as formatted system kwarg."""
        provider, mock_client = _mock_claude_provider(mock_anthropic_module, mock_anthropic_class)
        mock_client.messages.create = AsyncMock(
            return_value=_tool_response("emit_output", {"passed": True, "issues": []})
        )
        criteria = "Answer must mention the verified source."
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
        call_kwargs = mock_client.messages.create.call_args.kwargs
        # system_prompt is forwarded verbatim (no trim) for cross-provider parity.
        assert call_kwargs["system"] == VALIDATOR_SYSTEM_PROMPT.format(criteria=criteria)
        assert "{{" not in call_kwargs["system"]
