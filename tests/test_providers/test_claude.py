"""Unit tests for the ClaudeProvider implementation.

Tests cover:
- Provider initialization with SDK version verification
- Connection validation
- Basic message execution via the Pydantic AI seam
- Structured output extraction via the Pydantic AI seam
- Temperature validation
- Error handling and wrapping via the Pydantic AI seam
- Dialog turn execution via the Pydantic AI seam
- Concurrent execution
- Model capability / max prompt token lookups
- MCP manager pooling

Obsolete tests that exercised deleted legacy internals
(multi-turn parse recovery, SDK retry delay calculations,
Anthropic block parsing, tool schema generation, etc.) were removed
in the Pydantic AI migration; equivalent behavior is covered by the
``test_pydantic_ai_*.py`` suites.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse
from pydantic_ai.models.test import TestModel

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers.claude import ClaudeProvider


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so ClaudeProvider construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _build_text_agent(text: str) -> Agent[Any, str]:
    """Build a Pydantic AI text agent backed by TestModel."""
    return Agent(model=TestModel(custom_output_text=text), output_type=str)


def _build_structured_agent(model_cls: type[BaseModel], data: dict[str, Any]) -> Agent[Any, Any]:
    """Build a Pydantic AI structured-output agent backed by TestModel."""
    return Agent(
        model=TestModel(custom_output_args=data),
        output_type=model_cls,
    )


class TestClaudeProviderInitialization:
    """Tests for ClaudeProvider initialization."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", False)
    def test_init_raises_when_sdk_not_installed(self) -> None:
        """Test that initialization raises ProviderError when SDK not available."""
        with pytest.raises(ProviderError, match="Anthropic SDK not installed"):
            ClaudeProvider()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_init_with_default_parameters(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test initialization with default parameters."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        assert provider._default_model == "claude-3-5-sonnet-latest"
        assert provider._default_max_tokens == 8192
        assert provider._timeout == 600.0
        assert provider._sdk_version == "0.77.0"
        mock_anthropic_class.assert_called_once()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_init_with_custom_parameters(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test initialization with custom parameters."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider(
            api_key="test-key",
            model="claude-3-opus-20240229",
            temperature=0.5,
            max_tokens=4096,
            timeout=300.0,
        )

        assert provider._api_key == "test-key"
        assert provider._default_model == "claude-3-opus-20240229"
        assert provider._default_temperature == 0.5
        assert provider._default_max_tokens == 4096
        assert provider._timeout == 300.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @patch("conductor.providers.claude.logger")
    def test_sdk_version_warning_old_version(
        self,
        mock_logger: Mock,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
    ) -> None:
        """Test warning when SDK version is older than 0.77.0."""
        mock_anthropic_module.__version__ = "0.76.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        ClaudeProvider()

        assert mock_logger.warning.called
        warning_calls = [call[0][0] for call in mock_logger.warning.call_args_list]
        assert any("0.76.0" in call and "older than 0.77.0" in call for call in warning_calls)

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @patch("conductor.providers.claude.logger")
    def test_sdk_version_warning_future_version(
        self,
        mock_logger: Mock,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
    ) -> None:
        """Test warning when SDK version is >= 1.0.0."""
        mock_anthropic_module.__version__ = "1.0.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        ClaudeProvider()

        assert mock_logger.warning.called
        warning_calls = [call[0][0] for call in mock_logger.warning.call_args_list]
        assert any("1.0.0" in call and ">= 1.0.0" in call for call in warning_calls)


class TestModelVerification:
    """Tests for model availability verification."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @patch("conductor.providers.claude.logger")
    @pytest.mark.asyncio
    async def test_model_verification_lists_available_models(
        self,
        mock_logger: Mock,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
    ) -> None:
        """Test that available models are listed and logged."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()

        mock_model1 = Mock()
        mock_model1.id = "claude-3-5-sonnet-latest"
        mock_model2 = Mock()
        mock_model2.id = "claude-3-opus-20240229"
        mock_client.models.list = AsyncMock(return_value=Mock(data=[mock_model1, mock_model2]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        await provider.validate_connection()

        assert mock_client.models.list.call_count == 2

        info_calls = [call[0][0] for call in mock_logger.info.call_args_list]
        assert any("Available Claude models" in call for call in info_calls)

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @patch("conductor.providers.claude.logger")
    @pytest.mark.asyncio
    async def test_model_verification_warns_unavailable_model(
        self,
        mock_logger: Mock,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
    ) -> None:
        """Test warning when requested model is not available."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()

        mock_model = Mock()
        mock_model.id = "claude-3-opus-20240229"
        mock_client.models.list = AsyncMock(return_value=Mock(data=[mock_model]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider(model="claude-sonnet-4-20250514")
        await provider.validate_connection()

        mock_logger.warning.assert_called()
        warning_calls = [call[0][0] for call in mock_logger.warning.call_args_list]
        assert any("not in the list of available models" in call for call in warning_calls)


class TestConnectionValidation:
    """Tests for connection validation."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_validate_connection_success(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test successful connection validation."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        result = await provider.validate_connection()

        assert result is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_validate_connection_failure(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test connection validation failure."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(side_effect=Exception("API key invalid"))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        result = await provider.validate_connection()

        assert result is False


class TestCloseMethod:
    """Tests for resource cleanup."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_close_clears_client(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that close() clears the client reference."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        assert provider._client is not None

        await provider.close()
        assert provider._client is None


class TestBasicExecution:
    """Tests for basic message execution via the Pydantic AI seam."""

    @pytest.mark.asyncio
    async def test_execute_simple_message(self) -> None:
        """execute() returns text output from a TestModel-backed agent."""
        provider = ClaudeProvider(api_key="test-key")

        with (
            patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=_build_text_agent("Hello, world!"),
            ) as mock_build_agent,
        ):
            agent = AgentDef(name="test", prompt="Say hello")
            result = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Say hello",
            )

        assert result.content == {"result": "Hello, world!"}
        assert result.partial is False
        assert result.model == "test"
        assert result.tokens_used is not None
        assert result.input_tokens is not None
        assert result.output_tokens is not None
        assert mock_build_agent.call_args.kwargs["agent"] is agent
        assert mock_build_agent.call_args.kwargs["rendered_prompt"] == "Say hello"

    @pytest.mark.asyncio
    async def test_execute_with_agent_model(self) -> None:
        """Agent-level model overrides the provider default in build_agent."""
        provider = ClaudeProvider(api_key="test-key")

        with (
            patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=_build_text_agent("Response"),
            ) as mock_build_agent,
        ):
            agent = AgentDef(
                name="test",
                prompt="Test",
                model="claude-3-opus-20240229",
            )
            result = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Test",
            )

        assert result.model == "test"
        assert mock_build_agent.call_args.kwargs["agent"].model == "claude-3-opus-20240229"

    @pytest.mark.asyncio
    async def test_execute_with_temperature(self) -> None:
        """Provider temperature is forwarded to build_agent defaults."""
        provider = ClaudeProvider(api_key="test-key", temperature=0.7)

        with (
            patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=_build_text_agent("Response"),
            ) as mock_build_agent,
        ):
            agent = AgentDef(name="test", prompt="Test")
            await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Test",
            )

        assert mock_build_agent.call_args.kwargs["default_temperature"] == 0.7


class TestStructuredOutput:
    """Tests for structured output via the Pydantic AI seam."""

    @pytest.mark.asyncio
    async def test_execute_with_structured_output(self) -> None:
        """execute() returns validated structured output from a Pydantic model."""
        provider = ClaudeProvider(api_key="test-key")

        class AnswerModel(BaseModel):
            answer: str
            confidence: float

        with (
            patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=_build_structured_agent(
                    AnswerModel, {"answer": "42", "confidence": 0.95}
                ),
            ),
        ):
            agent = AgentDef(
                name="test",
                prompt="Answer question",
                output={
                    "answer": OutputField(type="string"),
                    "confidence": OutputField(type="number"),
                },
            )
            result = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="What is the answer?",
            )

        assert result.content == {"answer": "42", "confidence": 0.95}
        assert result.partial is False

    @pytest.mark.asyncio
    async def test_execute_with_json_fallback(self) -> None:
        """execute() falls back to JSON parsing when the model returns text."""
        provider = ClaudeProvider(api_key="test-key")

        text_response = '```json\n{"answer": "Paris", "country": "France"}\n```'

        with (
            patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=_build_text_agent(text_response),
            ),
        ):
            agent = AgentDef(
                name="test",
                prompt="Answer",
                output={
                    "answer": OutputField(type="string"),
                    "country": OutputField(type="string"),
                },
            )
            result = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="What is the capital of France?",
            )

        assert result.content == {"answer": "Paris", "country": "France"}


class TestTemperatureValidation:
    """Tests for temperature validation behavior."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_temperature_above_1_0_raises_validation_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that provider raises ValidationError for temperature > 1.0."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        with pytest.raises(ValidationError) as exc_info:
            ClaudeProvider(temperature=1.5)

        assert "between 0.0 and 1.0" in str(exc_info.value)


class TestErrorHandling:
    """Tests for error handling and wrapping via the Pydantic AI seam."""

    @pytest.mark.asyncio
    async def test_api_error_wrapped_as_provider_error(self) -> None:
        """Test that API errors are wrapped as ProviderError."""
        provider = ClaudeProvider(api_key="test-key")

        failing_agent = _build_text_agent("never")

        async def failing_run(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("API error")

        failing_agent.run = failing_run

        with (
            patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=failing_agent,
            ),
        ):
            agent = AgentDef(name="test", prompt="Test")
            with pytest.raises(ProviderError) as exc_info:
                await provider.execute(
                    agent=agent,
                    context={},
                    rendered_prompt="Test",
                )

        assert "API error" in str(exc_info.value)
        assert exc_info.value.is_retryable is False

    @pytest.mark.asyncio
    async def test_validation_error_for_missing_output_fields(self) -> None:
        """Test that missing output fields raise ValidationError."""
        provider = ClaudeProvider(api_key="test-key")

        class AnswerModel(BaseModel):
            answer: str

        with (
            patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None),
            patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                return_value=_build_structured_agent(AnswerModel, {"answer": "42"}),
            ),
        ):
            agent = AgentDef(
                name="test",
                prompt="Test",
                output={
                    "answer": OutputField(type="string"),
                    "confidence": OutputField(type="number"),
                },
            )
            with pytest.raises(ValidationError) as exc_info:
                await provider.execute(
                    agent=agent,
                    context={},
                    rendered_prompt="Test",
                )

        assert "Missing required output field: confidence" in str(exc_info.value)


class TestConcurrentExecution:
    """Tests for concurrent execution scenarios."""

    @pytest.mark.asyncio
    async def test_concurrent_execute_calls(self) -> None:
        """Multiple concurrent execute() calls complete independently."""
        provider = ClaudeProvider(api_key="test-key")

        with patch.object(provider, "_get_mcp_manager_for_cwd", return_value=None):
            agent1 = AgentDef(name="test1", prompt="Hello 1")
            agent2 = AgentDef(name="test2", prompt="Hello 2")
            agent3 = AgentDef(name="test3", prompt="Hello 3")

            call_count = 0

            def make_agent(**kwargs: Any) -> Agent[Any, str]:
                nonlocal call_count
                call_count += 1
                return _build_text_agent(f"Response {call_count}")

            with patch(
                "conductor.providers._pydantic_ai.agent_builder.build_agent",
                side_effect=make_agent,
            ):
                results = await asyncio.gather(
                    provider.execute(agent=agent1, context={}, rendered_prompt="Hello 1"),
                    provider.execute(agent=agent2, context={}, rendered_prompt="Hello 2"),
                    provider.execute(agent=agent3, context={}, rendered_prompt="Hello 3"),
                )

        assert len(results) == 3
        assert {r.content["result"] for r in results} == {"Response 1", "Response 2", "Response 3"}
        assert all(r.tokens_used is not None for r in results)
        assert call_count == 3


class TestClaudeExecuteDialogTurn:
    """Tests for Claude provider dialog-turn API via the Pydantic AI seam."""

    @pytest.mark.asyncio
    async def test_dialog_turn_empty_history_sends_only_current_message(self) -> None:
        """Empty history -> run is called with only the current user message."""
        provider = ClaudeProvider(api_key="test-key")

        async def fake_run(*args: Any, **kwargs: Any) -> Any:
            class FakeResult:
                output = "the reply"
                usage = None

            return FakeResult()

        with (
            patch(
                "conductor.providers._pydantic_ai.agent_builder._resolve_anthropic_model"
            ) as mock_resolve_model,
            patch("pydantic_ai.Agent") as mock_agent_cls,
        ):
            mock_agent = mock_agent_cls.return_value
            mock_agent.run = AsyncMock(side_effect=fake_run)
            result = await provider.execute_dialog_turn(
                system_prompt="be a helpful assistant",
                user_message="hello",
                history=[],
            )

        assert result == "the reply"
        kwargs = mock_agent.run.call_args.kwargs
        assert kwargs["user_prompt"] == "hello"
        assert kwargs.get("message_history") == []
        assert mock_resolve_model.call_args.kwargs["agent"].model == "claude-3-5-sonnet-latest"

    @pytest.mark.asyncio
    async def test_dialog_turn_multi_turn_history_preserved_in_order(self) -> None:
        """Multi-turn history is appended in order, with current message last."""
        provider = ClaudeProvider(api_key="test-key")

        async def fake_run(*args: Any, **kwargs: Any) -> Any:
            class FakeResult:
                output = "ack"
                usage = None

            return FakeResult()

        with (
            patch(
                "conductor.providers._pydantic_ai.agent_builder._resolve_anthropic_model"
            ) as mock_resolve_model,
            patch("pydantic_ai.Agent") as mock_agent_cls,
        ):
            mock_agent = mock_agent_cls.return_value
            mock_agent.run = AsyncMock(side_effect=fake_run)
            await provider.execute_dialog_turn(
                system_prompt="sys",
                user_message="third user msg",
                history=[
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "second"},
                ],
            )

        history = mock_agent.run.call_args.kwargs["message_history"]
        assert len(history) == 2
        assert isinstance(history[0], ModelRequest)
        assert history[0].parts[0].content == "first"
        assert isinstance(history[1], ModelResponse)
        assert history[1].parts[0].content == "second"
        assert mock_resolve_model.call_args.kwargs["agent"].model == "claude-3-5-sonnet-latest"

    @pytest.mark.asyncio
    async def test_dialog_turn_model_override_used(self) -> None:
        """model arg overrides the provider default."""
        provider = ClaudeProvider(api_key="test-key")

        captured_model: Any = None

        async def fake_run(*args: Any, **kwargs: Any) -> Any:
            class FakeResult:
                output = "x"
                usage = None

            return FakeResult()

        def capture_model(*args: Any, **kwargs: Any) -> Any:
            nonlocal captured_model
            captured_model = args[0] if args else kwargs.get("agent")
            return Mock()

        with (
            patch(
                "conductor.providers._pydantic_ai.agent_builder._resolve_anthropic_model",
                side_effect=capture_model,
            ),
            patch("pydantic_ai.Agent") as mock_agent_cls,
        ):
            mock_agent = mock_agent_cls.return_value
            mock_agent.run = fake_run
            await provider.execute_dialog_turn(
                system_prompt="sys",
                user_message="hi",
                history=None,
                model="claude-3-opus-20240229",
            )

        assert captured_model.model == "claude-3-opus-20240229"

    @pytest.mark.asyncio
    async def test_dialog_turn_error_wrapped_as_provider_error(self) -> None:
        """SDK errors propagate as ProviderError, not bare exceptions."""
        provider = ClaudeProvider(api_key="test-key")

        with (
            patch("conductor.providers._pydantic_ai.agent_builder._resolve_anthropic_model"),
            patch("pydantic_ai.Agent") as mock_agent_cls,
        ):
            mock_agent = mock_agent_cls.return_value
            mock_agent.run = AsyncMock(side_effect=RuntimeError("api down"))
            with pytest.raises(ProviderError, match="api down"):
                await provider.execute_dialog_turn(
                    system_prompt="sys",
                    user_message="hi",
                    history=[],
                )

    @pytest.mark.asyncio
    async def test_dialog_turn_rejects_non_thinking_model_with_reasoning(self) -> None:
        """execute_dialog_turn() raises ValidationError for reasoning on non-thinking models."""
        provider = ClaudeProvider(api_key="test-key", default_reasoning_effort="medium")

        with (
            patch("conductor.providers._pydantic_ai.agent_builder._resolve_anthropic_model"),
            patch("pydantic_ai.Agent") as mock_agent_cls,
        ):
            mock_agent = mock_agent_cls.return_value
            mock_agent.run = AsyncMock(return_value=Mock(output="x"))
            with pytest.raises(ValidationError, match="extended thinking"):
                await provider.execute_dialog_turn(
                    system_prompt="sys",
                    user_message="hi",
                    model="claude-3-5-sonnet-latest",
                )

            mock_agent.run.assert_not_awaited()


@patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
@patch("conductor.providers.claude.AsyncAnthropic")
class TestClaudeGetMaxPromptTokens:
    """Tests for ClaudeProvider.get_max_prompt_tokens."""

    @pytest.mark.asyncio
    async def test_returns_max_input_tokens_for_known_model(
        self, mock_anthropic_class: Mock
    ) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(
                data=[
                    Mock(id="claude-sonnet-4-5", max_input_tokens=200_000),
                    Mock(id="claude-opus-4-5", max_input_tokens=200_000),
                ]
            )
        )
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        assert await provider.get_max_prompt_tokens("claude-sonnet-4-5") == 200_000

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_model(self, mock_anthropic_class: Mock) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        assert await provider.get_max_prompt_tokens("unknown-x") is None

    @pytest.mark.asyncio
    async def test_sdk_failure_returns_none_and_does_not_cache(
        self, mock_anthropic_class: Mock
    ) -> None:
        from anthropic import APIConnectionError

        err = APIConnectionError(request=Mock())

        mock_client = Mock()
        mock_client.models.list = AsyncMock(
            side_effect=[
                err,
                Mock(data=[Mock(id="claude-sonnet-4-5", max_input_tokens=200_000)]),
            ]
        )
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        assert await provider.get_max_prompt_tokens("claude-sonnet-4-5") is None
        assert await provider.get_max_prompt_tokens("claude-sonnet-4-5") == 200_000
        assert mock_client.models.list.await_count == 2

    @pytest.mark.asyncio
    async def test_unexpected_exception_propagates(self, mock_anthropic_class: Mock) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(side_effect=RuntimeError("bug"))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        with pytest.raises(RuntimeError):
            await provider.get_max_prompt_tokens("claude-sonnet-4-5")

    @pytest.mark.asyncio
    async def test_alias_resolves_via_match_model_id(self, mock_anthropic_class: Mock) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(
                data=[
                    Mock(id="claude-3-5-sonnet-20241022", max_input_tokens=200_000),
                ]
            )
        )
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        assert await provider.get_max_prompt_tokens("claude-3-5-sonnet-latest") == 200_000
        assert await provider.get_max_prompt_tokens("claude-3-5-sonnet") == 200_000

    @pytest.mark.asyncio
    async def test_caches_after_first_call(self, mock_anthropic_class: Mock) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(data=[Mock(id="claude-sonnet-4-5", max_input_tokens=200_000)])
        )
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        await provider.get_max_prompt_tokens("claude-sonnet-4-5")
        await provider.get_max_prompt_tokens("claude-sonnet-4-5")
        await provider.get_max_prompt_tokens("anything-else")

        assert mock_client.models.list.await_count == 1

    @pytest.mark.asyncio
    async def test_validate_connection_seeds_cache(self, mock_anthropic_class: Mock) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(data=[Mock(id="claude-sonnet-4-5", max_input_tokens=200_000)])
        )
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        assert await provider.validate_connection() is True
        before = mock_client.models.list.await_count
        assert await provider.get_max_prompt_tokens("claude-sonnet-4-5") == 200_000
        assert mock_client.models.list.await_count == before

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", False)
    async def test_returns_none_when_sdk_unavailable(self, mock_anthropic_class: Mock) -> None:
        with patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True):
            provider = ClaudeProvider()

        with patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", False):
            assert await provider.get_max_prompt_tokens("claude-sonnet-4-5") is None


@patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
@patch("conductor.providers.claude.AsyncAnthropic")
class TestClaudeGetModelCapabilities:
    """Tests for ClaudeProvider.get_model_capabilities (#301)."""

    @pytest.mark.asyncio
    async def test_thinking_model_reports_all_five_levels(self, mock_anthropic_class: Mock) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(data=[Mock(id="claude-sonnet-4-5", max_input_tokens=200_000)])
        )
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        caps = await provider.get_model_capabilities("claude-sonnet-4-5")
        assert caps is not None
        assert caps.supported_reasoning_efforts == ["low", "medium", "high", "xhigh", "max"]
        assert caps.default_reasoning_effort is None
        assert caps.max_prompt_tokens == 200_000
        assert caps.max_output_tokens is None
        assert caps.max_context_window_tokens is None

    @pytest.mark.asyncio
    async def test_non_thinking_model_reports_empty_list(self, mock_anthropic_class: Mock) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(
                data=[Mock(id="claude-3-5-sonnet-20241022", max_input_tokens=200_000)]
            )
        )
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        caps = await provider.get_model_capabilities("claude-3-5-sonnet-20241022")
        assert caps is not None
        assert caps.supported_reasoning_efforts == []
        assert caps.max_prompt_tokens == 200_000

    @pytest.mark.asyncio
    async def test_reasoning_fields_populated_even_when_prompt_tokens_unknown(
        self, mock_anthropic_class: Mock
    ) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        caps = await provider.get_model_capabilities("claude-opus-4-20250514")
        assert caps is not None
        assert caps.supported_reasoning_efforts == ["low", "medium", "high", "xhigh", "max"]
        assert caps.max_prompt_tokens is None

    @pytest.mark.asyncio
    async def test_reasoning_fields_populated_when_sdk_call_fails(
        self, mock_anthropic_class: Mock
    ) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(side_effect=RuntimeError("boom"))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        caps = await provider.get_model_capabilities("claude-3-opus-20240229")
        assert caps is not None
        assert caps.supported_reasoning_efforts == []
        assert caps.max_prompt_tokens is None

    @pytest.mark.asyncio
    async def test_reasoning_fields_populated_when_sdk_unavailable(
        self, mock_anthropic_class: Mock
    ) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        with patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", False):
            caps = await provider.get_model_capabilities("claude-sonnet-4-5")
        assert caps is not None
        assert caps.supported_reasoning_efforts == ["low", "medium", "high", "xhigh", "max"]
        assert caps.max_prompt_tokens is None

    @pytest.mark.asyncio
    async def test_never_raises_for_non_string_model(self, mock_anthropic_class: Mock) -> None:
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        caps = await provider.get_model_capabilities(12345)  # type: ignore[arg-type]
        assert caps is not None
        assert caps.supported_reasoning_efforts is None


class TestClaudeMCPManagerPool:
    """Requirement: agents with different working_dirs get isolated MCP servers."""

    @staticmethod
    def _build_provider(
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        mcp_servers: dict[str, dict[str, str]],
    ) -> ClaudeProvider:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.messages.create = AsyncMock(return_value=Mock(content=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client
        return ClaudeProvider(mcp_servers=mcp_servers)

    @staticmethod
    def _manager_factory(instances: list[Mock]) -> type:
        class _FakeMCPManager:
            def __init__(self, tool_output: Any | None = None) -> None:
                self.connected: list[dict[str, object]] = []
                self.closed = False
                instances.append(self)

            async def connect_server(self, **kwargs: object) -> list[dict[str, object]]:
                await asyncio.sleep(0)
                self.connected.append(kwargs)
                return []

            def has_servers(self) -> bool:
                return len(self.connected) > 0

            def get_all_tools(self) -> list[dict[str, object]]:
                return []

            async def call_tool(self, name: str, arguments: dict[str, object]) -> str:
                return "ok"

            async def close(self) -> None:
                self.closed = True

        return _FakeMCPManager

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_two_cwds_create_two_managers(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        servers = {"fs": {"command": "npx", "args": []}}
        provider = self._build_provider(mock_anthropic_module, mock_anthropic_class, servers)

        instances: list[Mock] = []
        fake_cls = self._manager_factory(instances)
        with (
            patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
            patch("conductor.mcp.manager.MCPManager", fake_cls),
        ):
            manager_a = await provider._get_mcp_manager_for_cwd("/repo/a")
            manager_b = await provider._get_mcp_manager_for_cwd("/repo/b")

        assert manager_a is not manager_b
        assert len(instances) == 2
        assert instances[0].connected[0]["cwd"] == "/repo/a"
        assert instances[1].connected[0]["cwd"] == "/repo/b"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_repeated_cwd_reuses_manager(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        servers = {"fs": {"command": "npx", "args": []}}
        provider = self._build_provider(mock_anthropic_module, mock_anthropic_class, servers)

        instances: list[Mock] = []
        fake_cls = self._manager_factory(instances)
        with (
            patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
            patch("conductor.mcp.manager.MCPManager", fake_cls),
        ):
            first = await provider._get_mcp_manager_for_cwd("/repo/a")
            second = await provider._get_mcp_manager_for_cwd("/repo/a")

        assert first is second
        assert len(instances) == 1
        assert len(instances[0].connected) == 1

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_close_closes_all_managers(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        servers = {"fs": {"command": "npx", "args": []}}
        provider = self._build_provider(mock_anthropic_module, mock_anthropic_class, servers)

        instances: list[Mock] = []
        fake_cls = self._manager_factory(instances)
        with (
            patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
            patch("conductor.mcp.manager.MCPManager", fake_cls),
        ):
            await provider._get_mcp_manager_for_cwd("/repo/a")
            await provider._get_mcp_manager_for_cwd("/repo/b")
            await provider.close()
            await provider.close()

        assert len(instances) == 2
        assert all(inst.closed for inst in instances)

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_parallel_agents_no_race(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        servers = {"fs": {"command": "npx", "args": []}}
        provider = self._build_provider(mock_anthropic_module, mock_anthropic_class, servers)

        instances: list[Mock] = []
        fake_cls = self._manager_factory(instances)
        with (
            patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
            patch("conductor.mcp.manager.MCPManager", fake_cls),
        ):
            results = await asyncio.gather(
                provider._get_mcp_manager_for_cwd("/repo/a"),
                provider._get_mcp_manager_for_cwd("/repo/b"),
                provider._get_mcp_manager_for_cwd("/repo/a"),
                provider._get_mcp_manager_for_cwd("/repo/b"),
            )

        assert len(instances) == 2
        assert results[0] is results[2]
        assert results[1] is results[3]
        assert results[0] is not results[1]
        per_cwd_connects = sorted(kwargs["cwd"] for i in instances for kwargs in i.connected)
        assert per_cwd_connects == ["/repo/a", "/repo/b"]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_connect_failure_fail_open_per_cwd(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        servers = {"fs": {"command": "npx", "args": []}}
        provider = self._build_provider(mock_anthropic_module, mock_anthropic_class, servers)

        instances: list[Mock] = []
        fake_cls = self._manager_factory(instances)
        attempts: dict[str, int] = {}

        async def failing_connect(self: Mock, **kwargs: object) -> list[dict[str, object]]:
            cwd = kwargs.get("cwd")
            assert isinstance(cwd, str)
            attempts[cwd] = attempts.get(cwd, 0) + 1
            if cwd == "/repo/bad" and attempts[cwd] == 1:
                raise RuntimeError("spawn failed")
            self.connected.append(kwargs)
            return []

        with (
            patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
            patch("conductor.mcp.manager.MCPManager", fake_cls),
            patch.object(fake_cls, "connect_server", failing_connect),
        ):
            manager_bad = await provider._get_mcp_manager_for_cwd("/repo/bad")
            manager_good = await provider._get_mcp_manager_for_cwd("/repo/good")
            manager_bad_retry = await provider._get_mcp_manager_for_cwd("/repo/bad")

        assert manager_bad is not manager_good
        assert instances[0].connected == []
        assert instances[1].connected[0]["cwd"] == "/repo/good"
        assert manager_bad_retry is not manager_bad
        assert len(instances) == 3
        assert instances[2].connected[0]["cwd"] == "/repo/bad"
        assert attempts["/repo/bad"] == 2

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_no_config_returns_none(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        provider = self._build_provider(mock_anthropic_module, mock_anthropic_class, {})

        with patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True):
            result = await provider._get_mcp_manager_for_cwd("/repo/a")

        assert result is None

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_agent_without_working_dir_uses_process_cwd(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        tmp_path: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workdir = str(tmp_path)
        monkeypatch.chdir(workdir)

        servers = {"fs": {"command": "npx", "args": []}}
        provider = self._build_provider(mock_anthropic_module, mock_anthropic_class, servers)

        instances: list[Mock] = []
        fake_cls = self._manager_factory(instances)
        with (
            patch("conductor.mcp.manager.MCP_SDK_AVAILABLE", True),
            patch("conductor.mcp.manager.MCPManager", fake_cls),
        ):
            agent = AgentDef(name="a", prompt="p")
            resolved_cwd = agent.working_dir or os.getcwd()
            manager = await provider._get_mcp_manager_for_cwd(resolved_cwd)

        assert manager is not None
        assert instances[0].connected[0]["cwd"] == os.getcwd()
