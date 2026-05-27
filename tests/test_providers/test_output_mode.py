"""Tests for output_mode behavior in Copilot and Claude providers.

Tests cover:
- E1-T5: output_mode=raw skips schema injection, wraps response as {"result": ...}
- E1-T9: Parse-exhaustion raises ProviderError with is_retryable=False
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError
from conductor.providers.copilot import CopilotProvider, RetryConfig

# ── Copilot provider tests ──────────────────────────────────────────────


def _make_copilot_handler(
    response: dict[str, Any],
) -> Any:
    """Create a mock handler that returns a fixed response."""

    def handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        return response

    return handler


class TestCopilotOutputModeRaw:
    """output_mode=raw with the Copilot provider."""

    @pytest.mark.asyncio
    async def test_raw_agent_wraps_response_as_result(self) -> None:
        """output_mode=raw agent produces {"result": ...} output."""
        provider = CopilotProvider(
            mock_handler=_make_copilot_handler({"result": "some raw text"})
        )
        agent = AgentDef(name="a", prompt="p", model="gpt-4", output_mode="raw")
        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")
        assert result.content == {"result": "some raw text"}

    @pytest.mark.asyncio
    async def test_raw_agent_no_schema_instruction_in_prompt(self) -> None:
        """output_mode=raw must not inject schema instructions into the prompt.

        Uses the SDK mock path so the full prompt-building code runs, then
        asserts the schema-injection marker is absent.
        """
        from conductor.providers.copilot import SDKResponse

        provider = CopilotProvider()
        provider._started = True

        # Mock the SDK client and session
        mock_session = AsyncMock()
        mock_session.session_id = "test-session"
        mock_session.disconnect = AsyncMock()
        mock_client = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=mock_session)
        provider._client = mock_client

        # Capture the prompt sent to _send_and_wait
        captured_prompts: list[str] = []

        async def capturing_send(session: Any, prompt: str, *args: Any, **kwargs: Any) -> Any:
            captured_prompts.append(prompt)
            return SDKResponse(content="raw text")

        agent = AgentDef(name="a", prompt="p", model="gpt-4", output_mode="raw")

        with (
            patch("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True),
            patch.object(provider, "_send_and_wait", AsyncMock(side_effect=capturing_send)),
        ):
            result, _ = await provider._execute_sdk_call(agent, "p", {})

        assert len(captured_prompts) == 1
        # The schema-injection marker must NOT be present
        assert "IMPORTANT: You MUST respond with a JSON object" not in captured_prompts[0]
        assert result == {"result": "raw text"}

    @pytest.mark.asyncio
    async def test_envelope_with_output_is_backward_compatible(self) -> None:
        """output_mode=envelope with output: schema behaves like the default."""
        provider = CopilotProvider(
            mock_handler=_make_copilot_handler({"field": "value"})
        )
        agent = AgentDef(
            name="a",
            prompt="p",
            model="gpt-4",
            output_mode="envelope",
            output={"field": OutputField(type="string")},
        )
        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")
        assert result.content == {"field": "value"}


class TestCopilotParseExhaustionNotRetryable:
    """Parse-exhaustion errors in Copilot must be is_retryable=False."""

    @pytest.mark.asyncio
    async def test_parse_exhaustion_is_not_retryable(self) -> None:
        """Parse-recovery exhaustion in _execute_sdk_call raises is_retryable=False.

        Drives through the real parse-recovery loop by mocking the SDK
        internals so _extract_json fails on every attempt.
        """
        from unittest.mock import AsyncMock, patch

        from conductor.providers.copilot import SDKResponse

        provider = CopilotProvider(
            retry_config=RetryConfig(max_parse_recovery_attempts=0),
        )
        # Bypass _ensure_client_started
        provider._started = True

        # Mock the SDK client and session
        mock_session = AsyncMock()
        mock_session.session_id = "test-session"
        mock_session.disconnect = AsyncMock()
        mock_client = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=mock_session)
        provider._client = mock_client

        non_json = SDKResponse(content="This is not valid JSON at all")

        agent = AgentDef(
            name="a",
            prompt="p",
            model="gpt-4",
            output={"field": OutputField(type="string")},
        )

        with (
            patch("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True),
            patch.object(provider, "_send_and_wait", AsyncMock(return_value=non_json)),
        ):
            with pytest.raises(ProviderError) as exc_info:
                await provider._execute_sdk_call(agent, "p", {})

            assert exc_info.value.is_retryable is False
            assert "output_mode: raw" in (exc_info.value.suggestion or "")

    @pytest.mark.asyncio
    async def test_parse_exhaustion_error_includes_500_char_prefix(self) -> None:
        """Parse-exhaustion suggestion includes first 500 chars of response."""
        provider = CopilotProvider(mock_handler=_make_copilot_handler({"result": "x"}))
        # Test the extract_json ValueError message length
        long_content = "x" * 600
        with pytest.raises(ValueError, match=r"x{500}\.\.\."):
            provider._extract_json(long_content)

    @pytest.mark.asyncio
    async def test_no_outer_retry_on_parse_exhaustion(self) -> None:
        """Verify parse-exhaustion (is_retryable=False) short-circuits the outer retry."""
        call_count = 0

        async def fake_sdk_call(
            agent: Any,
            rendered_prompt: str,
            context: Any,
            tools: Any = None,
            interrupt_signal: Any = None,
            event_callback: Any = None,
        ) -> Any:
            nonlocal call_count
            call_count += 1
            raise ProviderError(
                "Failed to parse structured output",
                is_retryable=False,
            )

        provider = CopilotProvider(
            retry_config=RetryConfig(max_attempts=3),
        )
        provider._execute_sdk_call = fake_sdk_call  # type: ignore[assignment]

        agent = AgentDef(name="a", prompt="p", model="gpt-4")
        with pytest.raises(ProviderError) as exc_info:
            await provider.execute(agent=agent, context={}, rendered_prompt="p")

        assert exc_info.value.is_retryable is False
        assert call_count == 1  # No retries — short-circuited on first attempt


# ── Claude provider tests ───────────────────────────────────────────────


def _create_text_block(text: str) -> Mock:
    block = Mock()
    block.type = "text"
    block.text = text
    return block


def _create_tool_use_block(input_dict: dict) -> Mock:
    block = Mock()
    block.type = "tool_use"
    block.id = "tool_123"
    block.name = "emit_output"
    block.input = input_dict
    return block


def _create_response(content_blocks: list, msg_id: str = "msg_1") -> Mock:
    response = Mock()
    response.id = msg_id
    response.content = content_blocks
    response.model = "claude-3-5-sonnet-latest"
    response.stop_reason = "end_turn"
    response.usage = Mock(
        input_tokens=10,
        output_tokens=20,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    response.type = "message"
    response.role = "assistant"
    return response


@patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
@patch("conductor.providers.claude.AsyncAnthropic")
@patch("conductor.providers.claude.anthropic")
class TestClaudeOutputModeRaw:
    """output_mode=raw with the Claude provider."""

    @pytest.mark.asyncio
    async def test_raw_agent_wraps_response_as_result(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """output_mode=raw agent returns text wrapped in {"result": ...}."""
        mock_anthropic_module.__version__ = "0.77.0"

        text_response = _create_response([_create_text_block("raw output")])
        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=text_response)
        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import ClaudeProvider

        provider = ClaudeProvider(api_key="test-key")
        agent = AgentDef(name="a", prompt="p", model="claude-3-5-sonnet-latest", output_mode="raw")
        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")

        # Raw mode wraps text response as {"result": "..."} — matches Copilot parity
        assert result.content == {"result": "raw output"}

    @pytest.mark.asyncio
    async def test_raw_agent_no_emit_output_tool_injected(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """output_mode=raw must not inject the emit_output tool."""
        mock_anthropic_module.__version__ = "0.77.0"

        text_response = _create_response([_create_text_block("raw output")])
        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=text_response)
        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import ClaudeProvider

        provider = ClaudeProvider(api_key="test-key")
        agent = AgentDef(name="a", prompt="p", model="claude-3-5-sonnet-latest", output_mode="raw")
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        # Verify no emit_output tool in the API call
        call_kwargs = mock_client.messages.create.call_args
        tools_arg = call_kwargs.kwargs.get("tools") if call_kwargs.kwargs else None
        # When output_mode=raw, no tools should be injected (unless MCP tools exist)
        assert tools_arg is None or not any(
            t.get("name") == "emit_output" for t in (tools_arg or [])
        )


@patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
@patch("conductor.providers.claude.AsyncAnthropic")
@patch("conductor.providers.claude.anthropic")
class TestClaudeParseExhaustionNotRetryable:
    """Parse-exhaustion errors in Claude must be is_retryable=False."""

    @pytest.mark.asyncio
    async def test_parse_exhaustion_is_not_retryable(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """After Claude parse recovery exhausts, ProviderError has is_retryable=False."""
        mock_anthropic_module.__version__ = "0.77.0"

        # Every response is text-only (no emit_output tool use) → triggers recovery
        bad_response = _create_response(
            [_create_text_block("I cannot format this as JSON")]
        )
        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=bad_response)
        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import ClaudeProvider
        from conductor.providers.claude import RetryConfig as ClaudeRetryConfig

        provider = ClaudeProvider(
            api_key="test-key",
            retry_config=ClaudeRetryConfig(max_attempts=1, max_parse_recovery_attempts=1),
        )
        agent = AgentDef(
            name="a",
            prompt="p",
            model="claude-3-5-sonnet-latest",
            output={"field": OutputField(type="string")},
        )

        with pytest.raises(ProviderError) as exc_info:
            await provider.execute(agent=agent, context={}, rendered_prompt="p")

        assert exc_info.value.is_retryable is False
        # The parse-exhaustion error is wrapped by the outer retry handler;
        # the output_mode hint appears in the wrapped message string.
        assert "output_mode: raw" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_no_outer_retry_on_parse_exhaustion(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Verify parse-exhaustion does not trigger outer retry in Claude."""
        mock_anthropic_module.__version__ = "0.77.0"

        bad_response = _create_response(
            [_create_text_block("I cannot format this as JSON")]
        )
        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=bad_response)
        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import ClaudeProvider
        from conductor.providers.claude import RetryConfig as ClaudeRetryConfig

        provider = ClaudeProvider(
            api_key="test-key",
            retry_config=ClaudeRetryConfig(max_attempts=3, max_parse_recovery_attempts=0),
        )
        agent = AgentDef(
            name="a",
            prompt="p",
            model="claude-3-5-sonnet-latest",
            output={"field": OutputField(type="string")},
        )

        with pytest.raises(ProviderError) as exc_info:
            await provider.execute(agent=agent, context={}, rendered_prompt="p")

        assert exc_info.value.is_retryable is False
        # With is_retryable=False, the outer retry loop should only call once
        assert mock_client.messages.create.call_count == 1
