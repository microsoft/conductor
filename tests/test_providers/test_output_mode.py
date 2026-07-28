"""Tests for output_mode behavior in Copilot and Claude providers.

Tests cover:
- E1-T5: output_mode=raw skips schema injection, wraps response as {"result": ...}
- E1-T9: Parse-exhaustion raises ProviderError with is_retryable=False
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError
from conductor.providers._pydantic_ai.agent_builder import build_agent
from conductor.providers.claude import ClaudeProvider
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
        """output_mode=raw wraps a plain-text SDK response as {"result": <text>}.

        Drives the real SDK path (not the mock_handler short-circuit, which
        returns a fixed dict and bypasses the output_mode wrapping logic) so
        the wrapping is actually exercised: the model returns plain text that
        is *not* JSON, and raw mode must wrap it verbatim instead of trying to
        extract a JSON object.
        """
        from conductor.providers.copilot import SDKResponse

        provider = CopilotProvider()
        provider._started = True
        mock_session = AsyncMock()
        mock_session.session_id = "test-session"
        mock_session.disconnect = AsyncMock()
        mock_client = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=mock_session)
        provider._client = mock_client

        agent = AgentDef(name="a", prompt="p", model="gpt-4", output_mode="raw")
        with (
            patch("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True),
            patch.object(
                provider,
                "_send_and_wait",
                AsyncMock(return_value=SDKResponse(content="some raw text")),
            ),
        ):
            result, _ = await provider._execute_sdk_call(agent, "p", {})
        assert result == {"result": "some raw text"}

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
        """output_mode=envelope with output: schema extracts JSON like the default.

        Drives the real SDK path so the schema-extraction logic runs. The
        mock_handler short-circuit would return a fixed dict and never exercise
        extraction, making the assertion tautological.
        """
        from conductor.providers.copilot import SDKResponse

        provider = CopilotProvider()
        provider._started = True
        mock_session = AsyncMock()
        mock_session.session_id = "test-session"
        mock_session.disconnect = AsyncMock()
        mock_client = AsyncMock()
        mock_client.create_session = AsyncMock(return_value=mock_session)
        provider._client = mock_client

        agent = AgentDef(
            name="a",
            prompt="p",
            model="gpt-4",
            output_mode="envelope",
            output={"field": OutputField(type="string")},
        )
        with (
            patch("conductor.providers.copilot.COPILOT_SDK_AVAILABLE", True),
            patch.object(
                provider,
                "_send_and_wait",
                AsyncMock(return_value=SDKResponse(content='{"field": "value"}')),
            ),
        ):
            result, _ = await provider._execute_sdk_call(agent, "p", {})
        assert result == {"field": "value"}


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
            retry_config: Any = None,
            skill_directories: Any = None,
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


def _build_text_agent(text: str) -> Agent[Any, str]:
    """Build a Pydantic AI text agent backed by TestModel."""
    return Agent(model=TestModel(custom_output_text=text), output_type=str)


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so ClaudeProvider construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture
def claude_provider() -> ClaudeProvider:
    """Return a fresh ClaudeProvider instance using a dummy API key."""
    return ClaudeProvider(api_key="test-key")


class TestClaudeOutputModeRaw:
    """output_mode=raw with the Claude provider."""

    @pytest.mark.asyncio
    async def test_raw_agent_wraps_response_as_result(
        self, claude_provider: ClaudeProvider
    ) -> None:
        """output_mode=raw agent returns text wrapped in {"result": ...}."""
        agent = AgentDef(
            name="a",
            prompt="p",
            model="claude-3-5-sonnet-latest",
            output_mode="raw",
        )
        with patch(
            "conductor.providers._pydantic_ai.agent_builder.build_agent",
            return_value=_build_text_agent("raw output"),
        ):
            result = await claude_provider.execute(agent=agent, context={}, rendered_prompt="p")

        # Raw mode wraps text response as {"result": "..."} — matches Copilot parity
        assert result.content == {"result": "raw output"}


def test_no_final_result_tool_in_raw_agent() -> None:
    """output_mode=raw must not register the structured-output 'final_result' tool."""
    agent = AgentDef(
        name="a",
        prompt="p",
        model="claude-3-5-sonnet-latest",
        output_mode="raw",
    )
    built = build_agent(
        agent=agent,
        system_prompt="",
        rendered_prompt="p",
        api_key="test-key",
    )
    toolset = built._output_schema.toolset
    tool_names = {t.name for t in toolset._tool_defs} if toolset is not None else set()
    assert "final_result" not in tool_names


# Removed: TestClaudeParseExhaustionNotRetryable class.
# The obsolete multi-turn JSON parse recovery loop was deleted by design; Pydantic
# AI structured outputs natively steer the model using the output schema. Any
# remaining parse-exhaustion behavior is covered by the new structured output path
# in tests/test_providers/test_pydantic_ai_structured_output.py.
