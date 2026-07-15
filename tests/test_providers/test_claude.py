"""Unit tests for the ClaudeProvider implementation.

Tests cover:
- Provider initialization with SDK version verification
- Connection validation
- Basic message execution
- Structured output extraction (tool-based and fallback)
- Temperature validation (SDK-enforced behavior)
- Error handling and wrapping
"""

import asyncio
import os
from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers.claude import ClaudeProvider


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

        # Check that SDK version warning was issued (may be multiple warnings)
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

        # Check that SDK version warning was issued (may be multiple warnings)
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

        # Mock models.list() response
        mock_model1 = Mock()
        mock_model1.id = "claude-3-5-sonnet-latest"
        mock_model2 = Mock()
        mock_model2.id = "claude-3-opus-20240229"
        mock_client.models.list = AsyncMock(return_value=Mock(data=[mock_model1, mock_model2]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        await provider.validate_connection()

        # Check that models were listed (called twice: once in validate_connection,
        # once in _verify_available_models)
        assert mock_client.models.list.call_count == 2

        # Check that available models were logged (at INFO level)
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

        # Mock models.list() with different models
        mock_model = Mock()
        mock_model.id = "claude-3-opus-20240229"
        mock_client.models.list = AsyncMock(return_value=Mock(data=[mock_model]))
        mock_anthropic_class.return_value = mock_client

        # Request a model that's not in the list
        provider = ClaudeProvider(model="claude-sonnet-4-20250514")
        await provider.validate_connection()

        # Check warning was logged
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
        # Mock async methods
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
        # Mock async close method
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        assert provider._client is not None

        await provider.close()
        assert provider._client is None


class TestBasicExecution:
    """Tests for basic message execution without structured output."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_execute_simple_message(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test executing a simple message without output schema."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock response
        mock_text_block = Mock()
        mock_text_block.type = "text"
        mock_text_block.text = "Hello, world!"

        mock_response = Mock()
        mock_response.content = [mock_text_block]
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(name="test", prompt="Say hello")

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Say hello",
        )

        assert result.content == {"result": "Hello, world!"}
        assert result.tokens_used == 15
        assert result.model == "claude-3-5-sonnet-latest"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_execute_with_agent_model(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that agent model overrides default."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        mock_text_block = Mock()
        mock_text_block.type = "text"
        mock_text_block.text = "Response"

        mock_response = Mock()
        mock_response.content = [mock_text_block]
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
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

        assert result.model == "claude-3-opus-20240229"

        # Verify API was called with correct model
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-3-opus-20240229"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_execute_with_temperature(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that temperature is passed to API."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        mock_text_block = Mock()
        mock_text_block.type = "text"
        mock_text_block.text = "Response"

        mock_response = Mock()
        mock_response.content = [mock_text_block]
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider(temperature=0.7)
        agent = AgentDef(name="test", prompt="Test")

        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Test",
        )

        # Verify API was called with provider temperature
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["temperature"] == 0.7


class TestStructuredOutput:
    """Tests for structured output extraction using tools.

    The ClaudeProvider uses a tool-based approach where the output schema
    is converted to a tool definition that the model must use to return
    structured data.
    """

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_execute_with_structured_output_via_tool(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test structured output extraction from tool_use blocks."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock tool_use response
        mock_tool_block = Mock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "emit_output"
        mock_tool_block.input = {"answer": "42", "confidence": 0.95}

        mock_response = Mock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = Mock(input_tokens=20, output_tokens=10)

        # Mock async messages.create
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Answer question",
            output={
                "answer": OutputField(type="string", description="The answer"),
                "confidence": OutputField(type="number", description="Confidence score"),
            },
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="What is the answer?",
        )

        assert result.content == {"answer": "42", "confidence": 0.95}

        # Verify tool was included in API call
        call_kwargs = mock_client.messages.create.call_args[1]
        assert "tools" in call_kwargs
        assert len(call_kwargs["tools"]) == 1
        assert call_kwargs["tools"][0]["name"] == "emit_output"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_execute_with_json_fallback(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test fallback JSON extraction when model returns text instead of tool_use."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock text response with JSON
        mock_text_block = Mock()
        mock_text_block.type = "text"
        mock_text_block.text = '```json\n{"answer": "Paris", "country": "France"}\n```'

        mock_response = Mock()
        mock_response.content = [mock_text_block]
        mock_response.usage = Mock(input_tokens=20, output_tokens=15)

        # Mock async messages.create
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
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
    """Tests for temperature validation behavior.

    Note: The ClaudeProvider does NOT perform its own temperature validation.
    Instead, it relies on the SDK to enforce the [0.0, 1.0] range and raises
    BadRequestError for violations. The provider catches this error and wraps
    it as a ValidationError with a clear message.

    These tests document the SDK-enforced behavior rather than testing
    provider-side validation logic.
    """

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_temperature_above_1_0_raises_validation_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that provider raises ValidationError for temperature > 1.0.

        Temperature validation happens at provider instantiation time.
        """
        mock_anthropic_module.__version__ = "0.77.0"

        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        # Temperature validation happens during __init__
        with pytest.raises(ValidationError) as exc_info:
            ClaudeProvider(temperature=1.5)  # Invalid: > 1.0

        assert "between 0.0 and 1.0" in str(exc_info.value)


class TestErrorHandling:
    """Tests for error handling and wrapping."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_api_error_wrapped_as_provider_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that API errors are wrapped as ProviderError."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(name="test", prompt="Test")

        with pytest.raises(ProviderError) as exc_info:
            await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Test",
            )

        assert "Claude API call failed" in str(exc_info.value)
        # Generic exceptions are not retryable (only specific transient errors are)
        assert exc_info.value.is_retryable is False

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_execute_with_no_client_raises_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that execute raises error if client not initialized."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        provider._client = None  # Simulate uninitialized client

        agent = AgentDef(name="test", prompt="Test")

        with pytest.raises(ProviderError, match="client not initialized"):
            await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Test",
            )

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_validation_error_for_missing_output_fields(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that missing output fields raise ValidationError."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock tool_use with incomplete output
        mock_tool_block = Mock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "emit_output"
        mock_tool_block.input = {"answer": "42"}  # Missing 'confidence'

        mock_response = Mock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Test",
            output={
                "answer": OutputField(type="string"),
                "confidence": OutputField(type="number"),  # Required but missing
            },
        )

        with pytest.raises(ValidationError) as exc_info:
            await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Test",
            )

        assert "Missing required output field: confidence" in str(exc_info.value)


class TestToolSchemaGeneration:
    """Tests for tool schema generation from output schemas."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_build_tools_for_simple_schema(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test tool generation from simple output schema."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        schema = {
            "result": OutputField(type="string", description="The result"),
            "score": OutputField(type="number", description="A score"),
        }

        tools = provider._build_tools_for_structured_output(schema)

        assert len(tools) == 1
        assert tools[0]["name"] == "emit_output"
        assert "input_schema" in tools[0]
        assert tools[0]["input_schema"]["type"] == "object"
        assert "result" in tools[0]["input_schema"]["properties"]
        assert "score" in tools[0]["input_schema"]["properties"]
        assert tools[0]["input_schema"]["properties"]["result"]["type"] == "string"
        assert tools[0]["input_schema"]["properties"]["score"]["type"] == "number"
        assert set(tools[0]["input_schema"]["required"]) == {"result", "score"}


class TestConcurrentExecution:
    """Tests for concurrent execution scenarios."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_concurrent_execute_calls(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that multiple concurrent execute() calls work correctly."""
        import asyncio

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock responses for different calls
        call_count = 0

        def create_response():
            nonlocal call_count
            call_count += 1
            mock_text_block = Mock()
            mock_text_block.type = "text"
            mock_text_block.text = f"Response {call_count}"
            mock_response = Mock()
            mock_response.content = [mock_text_block]
            mock_response.usage = Mock(input_tokens=10, output_tokens=5)
            return mock_response

        mock_client.messages.create = AsyncMock(side_effect=lambda **kwargs: create_response())
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent1 = AgentDef(name="test1", prompt="Hello 1")
        agent2 = AgentDef(name="test2", prompt="Hello 2")
        agent3 = AgentDef(name="test3", prompt="Hello 3")

        # Execute three agents concurrently
        results = await asyncio.gather(
            provider.execute(agent=agent1, context={}, rendered_prompt="Hello 1"),
            provider.execute(agent=agent2, context={}, rendered_prompt="Hello 2"),
            provider.execute(agent=agent3, context={}, rendered_prompt="Hello 3"),
        )

        # Verify all three executed successfully
        assert len(results) == 3
        assert all(result.content for result in results)
        assert all(result.tokens_used == 15 for result in results)

        # Verify all three API calls were made
        assert mock_client.messages.create.call_count == 3


class TestTextContentExtraction:
    """Tests for text content extraction with multiple blocks."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_extract_text_content_multiple_blocks(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test extraction with multiple text blocks in response."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock response with multiple text blocks
        mock_text_block1 = Mock()
        mock_text_block1.type = "text"
        mock_text_block1.text = "First part. "

        mock_text_block2 = Mock()
        mock_text_block2.type = "text"
        mock_text_block2.text = "Second part."

        mock_response = Mock()
        mock_response.content = [mock_text_block1, mock_text_block2]
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(name="test", prompt="Say hello")

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Say hello",
        )

        # Verify both text blocks are combined with newline separator
        assert result.content == {"result": "First part. \nSecond part."}


class TestParseRecovery:
    """Tests for parse recovery mechanism when JSON is malformed."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_parse_recovery_success_on_first_retry(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test parse recovery succeeds on first retry attempt."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # First response: malformed JSON
        mock_text_block1 = Mock()
        mock_text_block1.type = "text"
        mock_text_block1.text = '{"answer": "incomplete'

        mock_response1 = Mock()
        mock_response1.content = [mock_text_block1]
        mock_response1.usage = Mock(input_tokens=20, output_tokens=10)

        # Second response: valid tool_use
        mock_tool_block = Mock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "emit_output"
        mock_tool_block.input = {"answer": "42"}

        mock_response2 = Mock()
        mock_response2.content = [mock_tool_block]
        mock_response2.usage = Mock(input_tokens=25, output_tokens=12)

        # Set up mock to return different responses
        mock_client.messages.create = AsyncMock(side_effect=[mock_response1, mock_response2])
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Answer",
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="What is the answer?",
        )

        assert result.content == {"answer": "42"}
        assert mock_client.messages.create.call_count == 2

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_parse_recovery_success_with_json_fallback(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test parse recovery succeeds with JSON fallback after retry."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # First response: malformed JSON
        mock_text_block1 = Mock()
        mock_text_block1.type = "text"
        mock_text_block1.text = "answer: 42"  # Not valid JSON

        mock_response1 = Mock()
        mock_response1.content = [mock_text_block1]
        mock_response1.usage = Mock(input_tokens=20, output_tokens=10)

        # Second response: valid JSON in text
        mock_text_block2 = Mock()
        mock_text_block2.type = "text"
        mock_text_block2.text = '{"answer": "42"}'

        mock_response2 = Mock()
        mock_response2.content = [mock_text_block2]
        mock_response2.usage = Mock(input_tokens=25, output_tokens=12)

        mock_client.messages.create = AsyncMock(side_effect=[mock_response1, mock_response2])
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Answer",
            output={"answer": OutputField(type="string")},
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="What is the answer?",
        )

        assert result.content == {"answer": "42"}
        assert mock_client.messages.create.call_count == 2

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_parse_recovery_exhausted_raises_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test parse recovery raises error with detailed history after max attempts."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # All responses: malformed JSON
        mock_text_block = Mock()
        mock_text_block.type = "text"
        mock_text_block.text = "invalid json"

        mock_response = Mock()
        mock_response.content = [mock_text_block]
        mock_response.usage = Mock(input_tokens=20, output_tokens=10)

        # Return same malformed response for all attempts (1 initial + 2 retries)
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Answer",
            output={"answer": OutputField(type="string")},
        )

        with pytest.raises(
            ProviderError, match="Failed to extract valid JSON after 2 recovery attempts"
        ) as exc_info:
            await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="What is the answer?",
            )

        # Verify error includes recovery history
        assert "Recovery history" in str(exc_info.value)
        assert "Attempt 0" in str(exc_info.value)

        # Should have made 3 attempts total (initial + 2 retries)
        assert mock_client.messages.create.call_count == 3

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_no_parse_recovery_when_no_output_schema(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test parse recovery is skipped when no output schema is defined."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Response with plain text (no schema, so this is valid)
        mock_text_block = Mock()
        mock_text_block.type = "text"
        mock_text_block.text = "This is just plain text"

        mock_response = Mock()
        mock_response.content = [mock_text_block]
        mock_response.usage = Mock(input_tokens=20, output_tokens=10)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(name="test", prompt="Say hello")

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Say hello",
        )

        # Should succeed without retries
        assert result.content == {"result": "This is just plain text"}
        assert mock_client.messages.create.call_count == 1


class TestNestedSchemas:
    """Tests for nested object and array schemas."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_nested_object_schema(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test structured output with nested object schema."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock tool_use response with nested object
        mock_tool_block = Mock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "emit_output"
        mock_tool_block.input = {
            "person": {
                "name": "Alice",
                "age": 30,
            }
        }

        mock_response = Mock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = Mock(input_tokens=20, output_tokens=15)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Get person info",
            output={
                "person": OutputField(
                    type="object",
                    description="Person information",
                    properties={
                        "name": OutputField(type="string", description="Name"),
                        "age": OutputField(type="number", description="Age"),
                    },
                ),
            },
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Get person info",
        )

        assert result.content == {"person": {"name": "Alice", "age": 30}}

        # Verify tool schema was correctly built with nested properties
        call_kwargs = mock_client.messages.create.call_args[1]
        assert "tools" in call_kwargs
        tool_schema = call_kwargs["tools"][0]["input_schema"]
        assert "properties" in tool_schema
        assert "person" in tool_schema["properties"]
        person_schema = tool_schema["properties"]["person"]
        assert person_schema["type"] == "object"
        assert "properties" in person_schema
        assert "name" in person_schema["properties"]
        assert "age" in person_schema["properties"]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_array_schema_with_items(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test structured output with array schema and item definitions."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock tool_use response with array
        mock_tool_block = Mock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "emit_output"
        mock_tool_block.input = {"tags": ["python", "testing", "async"]}

        mock_response = Mock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = Mock(input_tokens=20, output_tokens=15)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Get tags",
            output={
                "tags": OutputField(
                    type="array",
                    description="List of tags",
                    items=OutputField(type="string", description="A tag"),
                ),
            },
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Get tags",
        )

        assert result.content == {"tags": ["python", "testing", "async"]}

        # Verify tool schema was correctly built with items
        call_kwargs = mock_client.messages.create.call_args[1]
        tool_schema = call_kwargs["tools"][0]["input_schema"]
        assert "properties" in tool_schema
        assert "tags" in tool_schema["properties"]
        tags_schema = tool_schema["properties"]["tags"]
        assert tags_schema["type"] == "array"
        assert "items" in tags_schema
        assert tags_schema["items"]["type"] == "string"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_array_of_objects_schema(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test structured output with array of objects."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock tool_use response with array of objects
        mock_tool_block = Mock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "emit_output"
        mock_tool_block.input = {
            "users": [
                {"name": "Alice", "score": 95},
                {"name": "Bob", "score": 87},
            ]
        }

        mock_response = Mock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = Mock(input_tokens=20, output_tokens=20)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Get users",
            output={
                "users": OutputField(
                    type="array",
                    description="List of users",
                    items=OutputField(
                        type="object",
                        description="User info",
                        properties={
                            "name": OutputField(type="string"),
                            "score": OutputField(type="number"),
                        },
                    ),
                ),
            },
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Get users",
        )

        assert result.content == {
            "users": [
                {"name": "Alice", "score": 95},
                {"name": "Bob", "score": 87},
            ]
        }

        # Verify tool schema was correctly built
        call_kwargs = mock_client.messages.create.call_args[1]
        tool_schema = call_kwargs["tools"][0]["input_schema"]
        users_schema = tool_schema["properties"]["users"]
        assert users_schema["type"] == "array"
        assert "items" in users_schema
        assert users_schema["items"]["type"] == "object"
        assert "properties" in users_schema["items"]
        assert "name" in users_schema["items"]["properties"]
        assert "score" in users_schema["items"]["properties"]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_deeply_nested_schema(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test structured output with deeply nested schema."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock tool_use response with deeply nested structure
        mock_tool_block = Mock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "emit_output"
        mock_tool_block.input = {
            "company": {
                "name": "TechCorp",
                "departments": [
                    {
                        "name": "Engineering",
                        "employees": [
                            {"name": "Alice", "role": "Developer"},
                        ],
                    },
                ],
            }
        }

        mock_response = Mock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = Mock(input_tokens=30, output_tokens=40)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Get company",
            output={
                "company": OutputField(
                    type="object",
                    properties={
                        "name": OutputField(type="string"),
                        "departments": OutputField(
                            type="array",
                            items=OutputField(
                                type="object",
                                properties={
                                    "name": OutputField(type="string"),
                                    "employees": OutputField(
                                        type="array",
                                        items=OutputField(
                                            type="object",
                                            properties={
                                                "name": OutputField(type="string"),
                                                "role": OutputField(type="string"),
                                            },
                                        ),
                                    ),
                                },
                            ),
                        ),
                    },
                ),
            },
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Get company",
        )

        assert result.content == {
            "company": {
                "name": "TechCorp",
                "departments": [
                    {
                        "name": "Engineering",
                        "employees": [
                            {"name": "Alice", "role": "Developer"},
                        ],
                    },
                ],
            }
        }

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_schema_depth_limit_exceeded(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that schema nesting beyond max depth raises ValidationError."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Create a schema that exceeds max depth (11 levels, max is 10)
        # Build nested structure programmatically
        def create_nested_field(depth: int) -> OutputField:
            if depth == 0:
                return OutputField(type="string")
            return OutputField(
                type="object",
                properties={"nested": create_nested_field(depth - 1)},
            )

        agent = AgentDef(
            name="test",
            prompt="Deep nesting",
            output={"root": create_nested_field(11)},  # 11 levels deep
        )

        with pytest.raises(
            ValidationError, match="Schema nesting depth exceeds maximum of 10 levels"
        ):
            await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="Test",
            )

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_nested_array_with_object_items(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test successful structured output with nested array of objects.

        Note: This test verifies the schema is built correctly and extraction works.
        Deep validation of nested object properties within arrays is handled by
        the executor.output module's validate_output function.
        """
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock tool_use response with nested array of objects
        mock_tool_block = Mock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "emit_output"
        mock_tool_block.input = {
            "items": [
                {"name": "Item1", "value": 100},
                {"name": "Item2", "value": 200},
            ]
        }

        mock_response = Mock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = Mock(input_tokens=10, output_tokens=15)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Get items",
            output={
                "items": OutputField(
                    type="array",
                    items=OutputField(
                        type="object",
                        properties={
                            "name": OutputField(type="string"),
                            "value": OutputField(type="number"),
                        },
                    ),
                )
            },
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Get items",
        )

        assert result.content == {
            "items": [
                {"name": "Item1", "value": 100},
                {"name": "Item2", "value": 200},
            ]
        }

        # Verify the schema was built with nested object properties
        call_kwargs = mock_client.messages.create.call_args[1]
        tool_schema = call_kwargs["tools"][0]["input_schema"]
        items_schema = tool_schema["properties"]["items"]["items"]
        assert "properties" in items_schema
        assert "name" in items_schema["properties"]
        assert "value" in items_schema["properties"]
        assert items_schema["required"] == ["name", "value"]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_tool_use_success_without_fallback(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test primary success path where Claude uses tool on first attempt without fallback."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock successful tool_use response on first attempt
        mock_tool_block = Mock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.name = "emit_output"
        mock_tool_block.input = {"result": "success", "count": 5}

        mock_response = Mock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = Mock(input_tokens=15, output_tokens=8)

        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="test",
            prompt="Process request",
            output={
                "result": OutputField(type="string"),
                "count": OutputField(type="number"),
            },
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Process this",
        )

        assert result.content == {"result": "success", "count": 5}
        # Should only make 1 API call (no retries)
        assert mock_client.messages.create.call_count == 1
        # Verify tool was provided in the request
        call_kwargs = mock_client.messages.create.call_args[1]
        assert "tools" in call_kwargs
        assert call_kwargs["tools"][0]["name"] == "emit_output"


class TestNonStreamingExecution:
    """Tests for EPIC-003: Non-streaming message execution."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_execute_api_call_basic(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test _execute_api_call() makes non-streaming API call."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock successful response
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Hello")]
        mock_response.usage = Mock(input_tokens=10, output_tokens=20)
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Call _execute_api_call directly
        messages = [{"role": "user", "content": "test"}]
        response = await provider._execute_api_call(
            messages=messages,
            model="claude-3-5-sonnet-latest",
            temperature=0.7,
            max_tokens=100,
            tools=None,
        )

        assert response == mock_response
        # Verify messages.create was called (non-streaming)
        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-3-5-sonnet-latest"
        assert call_kwargs["messages"] == messages
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["max_tokens"] == 100
        assert "tools" not in call_kwargs

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_execute_api_call_with_tools(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test _execute_api_call() includes tools when provided."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        mock_response = Mock()
        mock_response.content = []
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        tools = [{"name": "test_tool", "description": "A test tool"}]
        await provider._execute_api_call(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=None,
            max_tokens=100,
            tools=tools,
        )

        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["tools"] == tools
        # Temperature should not be in kwargs when None
        assert "temperature" not in call_kwargs

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_process_response_content_blocks_text_only(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test _process_response_content_blocks() with text blocks."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Mock response with text blocks
        mock_response = Mock()
        block1 = Mock(type="text", text="First part")
        block2 = Mock(type="text", text="Second part")
        mock_response.content = [block1, block2]

        blocks, tool_data = provider._process_response_content_blocks(mock_response)

        assert len(blocks) == 2
        assert blocks[0] == {"type": "text", "text": "First part"}
        assert blocks[1] == {"type": "text", "text": "Second part"}
        assert tool_data is None

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_process_response_content_blocks_with_tool_use(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test _process_response_content_blocks() with tool_use blocks."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Mock response with tool_use block
        mock_response = Mock()
        tool_block = Mock()
        tool_block.type = "tool_use"
        tool_block.name = "emit_output"
        tool_block.id = "tool_123"
        tool_block.input = {"answer": "42", "confidence": 0.95}
        mock_response.content = [tool_block]

        blocks, tool_data = provider._process_response_content_blocks(mock_response)

        assert len(blocks) == 1
        assert blocks[0] == {"type": "tool_use", "name": "emit_output", "id": "tool_123"}
        assert tool_data == {"answer": "42", "confidence": 0.95}

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_process_response_content_blocks_mixed(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test _process_response_content_blocks() with mixed block types."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Mock response with mixed blocks
        mock_response = Mock()
        text_block = Mock()
        text_block.type = "text"
        text_block.text = "Let me help"
        tool_block = Mock()
        tool_block.type = "tool_use"
        tool_block.name = "emit_output"
        tool_block.id = "tool_456"
        tool_block.input = {"result": "success"}
        mock_response.content = [text_block, tool_block]

        blocks, tool_data = provider._process_response_content_blocks(mock_response)

        assert len(blocks) == 2
        assert blocks[0]["type"] == "text"
        assert blocks[1]["type"] == "tool_use"
        assert tool_data == {"result": "success"}

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_extract_token_usage_with_usage_data(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test _extract_token_usage() extracts and sums tokens correctly."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Mock response with usage
        mock_response = Mock()
        mock_response.usage = Mock(input_tokens=150, output_tokens=350)

        tokens = provider._extract_token_usage(mock_response)

        assert tokens == 500  # 150 + 350

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_extract_token_usage_without_usage_data(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test _extract_token_usage() returns None when usage not available."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Mock response without usage attribute
        mock_response = Mock(spec=["content"])

        tokens = provider._extract_token_usage(mock_response)

        assert tokens is None

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_timeout_configuration_passed_to_client(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test timeout is passed to Anthropic client initialization."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        # Create provider with custom timeout
        provider = ClaudeProvider(timeout=300.0)

        assert provider._timeout == 300.0
        # Verify timeout was passed to client
        mock_anthropic_class.assert_called_once()
        call_kwargs = mock_anthropic_class.call_args[1]
        assert call_kwargs["timeout"] == 300.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_timeout_default_value(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test default timeout is 600 seconds."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        assert provider._timeout == 600.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    async def test_execute_returns_token_usage(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test execute() returns token usage in AgentOutput."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Mock response with usage
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Response text")]
        mock_response.usage = Mock(input_tokens=100, output_tokens=200)
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(
            name="test-agent",
            model="claude-3-5-sonnet-latest",
            prompt="Test prompt",
        )

        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Test prompt",
        )

        assert result.tokens_used == 300  # 100 + 200
        assert result.model == "claude-3-5-sonnet-latest"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_execute_api_call_raises_when_client_not_initialized(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test _execute_api_call() raises ProviderError when client is None."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        provider._client = None  # Simulate uninitialized client

        with pytest.raises(ProviderError, match="Claude client not initialized"):
            await provider._execute_api_call(
                messages=[{"role": "user", "content": "test"}],
                model="claude-3-5-sonnet-latest",
                temperature=None,
                max_tokens=100,
            )

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @patch("conductor.providers.claude.logger")
    @pytest.mark.asyncio
    async def test_execute_api_call_logs_non_streaming_mode(
        self, mock_logger: Mock, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test _execute_api_call() logs that it's using non-streaming mode."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        mock_response = Mock()
        mock_response.content = []
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        await provider._execute_api_call(
            messages=[{"role": "user", "content": "test"}],
            model="claude-3-5-sonnet-latest",
            temperature=0.5,
            max_tokens=1000,
        )

        # Verify debug log mentions non-streaming
        mock_logger.debug.assert_called()
        debug_calls = [call[0][0] for call in mock_logger.debug.call_args_list]
        assert any("non-streaming" in call.lower() for call in debug_calls)


class TestClaudeProviderRetryLogic:
    """Tests for retry logic and error handling."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_is_retryable_error_rate_limit(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that RateLimitError is classified as retryable."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        error = mock_anthropic_module.RateLimitError("Rate limit exceeded")
        assert provider._is_retryable_error(error) is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_is_retryable_error_timeout(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that APITimeoutError is classified as retryable."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APITimeoutError = type("APITimeoutError", (Exception,), {})
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        error = mock_anthropic_module.APITimeoutError("Request timed out")
        assert provider._is_retryable_error(error) is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_is_retryable_error_connection(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that APIConnectionError is classified as retryable."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIConnectionError = type("APIConnectionError", (Exception,), {})
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        error = mock_anthropic_module.APIConnectionError("Connection failed")
        assert provider._is_retryable_error(error) is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_is_retryable_error_5xx_status(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that 5xx status codes are classified as retryable."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockAPIStatusError(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code
                super().__init__(f"Status {status_code}")

        mock_anthropic_module.APIStatusError = MockAPIStatusError
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Test 500 Internal Server Error
        error_500 = MockAPIStatusError(500)
        assert provider._is_retryable_error(error_500) is True

        # Test 503 Service Unavailable
        error_503 = MockAPIStatusError(503)
        assert provider._is_retryable_error(error_503) is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_is_retryable_error_4xx_non_retryable(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that 4xx status codes (except 429) are non-retryable."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockAPIStatusError(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code
                super().__init__(f"Status {status_code}")

        mock_anthropic_module.APIStatusError = MockAPIStatusError
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Test 401 Unauthorized
        error_401 = MockAPIStatusError(401)
        assert provider._is_retryable_error(error_401) is False

        # Test 400 Bad Request
        error_400 = MockAPIStatusError(400)
        assert provider._is_retryable_error(error_400) is False

        # Test 404 Not Found
        error_404 = MockAPIStatusError(404)
        assert provider._is_retryable_error(error_404) is False

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_is_retryable_error_429_retryable(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that 429 status code is retryable."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockAPIStatusError(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code
                super().__init__(f"Status {status_code}")

        mock_anthropic_module.APIStatusError = MockAPIStatusError
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        error_429 = MockAPIStatusError(429)
        assert provider._is_retryable_error(error_429) is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_is_retryable_error_honors_provider_error_flag(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """A ProviderError's own is_retryable flag is honored over SDK heuristics."""
        from conductor.exceptions import ProviderError

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        # Parse-exhaustion is raised with is_retryable=False — must not retry.
        non_retryable = ProviderError("Failed to parse output", is_retryable=False)
        assert provider._is_retryable_error(non_retryable) is False

        # A ProviderError explicitly marked retryable must retry.
        retryable = ProviderError("Connection timeout", is_retryable=True)
        assert provider._is_retryable_error(retryable) is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_calculate_delay_exponential_backoff(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test exponential backoff calculation."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import RetryConfig

        provider = ClaudeProvider()
        config = RetryConfig(base_delay=1.0, max_delay=30.0, jitter=0.0)  # No jitter for testing

        # Attempt 1: base * 2^0 = 1.0
        delay_1 = provider._calculate_delay(1, config)
        assert delay_1 == 1.0

        # Attempt 2: base * 2^1 = 2.0
        delay_2 = provider._calculate_delay(2, config)
        assert delay_2 == 2.0

        # Attempt 3: base * 2^2 = 4.0
        delay_3 = provider._calculate_delay(3, config)
        assert delay_3 == 4.0

        # Attempt 5: base * 2^4 = 16.0
        delay_5 = provider._calculate_delay(5, config)
        assert delay_5 == 16.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_calculate_delay_max_cap(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that delay is capped at max_delay."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import RetryConfig

        provider = ClaudeProvider()
        config = RetryConfig(base_delay=1.0, max_delay=10.0, jitter=0.0)

        # Attempt 10: base * 2^9 = 512.0, but capped at 10.0
        delay = provider._calculate_delay(10, config)
        assert delay == 10.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_calculate_delay_with_jitter(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that jitter adds randomness to delay."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import RetryConfig

        provider = ClaudeProvider()
        config = RetryConfig(base_delay=1.0, max_delay=30.0, jitter=0.25)

        # With jitter=0.25, delay should be base_delay * (1 + 0 to 0.25)
        delay = provider._calculate_delay(1, config)
        # Base delay is 1.0, jitter can add up to 0.25
        assert 1.0 <= delay <= 1.25

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_get_retry_after_header(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test extraction of retry-after header from rate limit error."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockRateLimitError(Exception):
            def __init__(self) -> None:
                self.response = Mock()
                self.response.headers = {"retry-after": "5"}
                super().__init__("Rate limit exceeded")

        mock_anthropic_module.RateLimitError = MockRateLimitError
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        error = MockRateLimitError()
        retry_after = provider._get_retry_after(error)
        assert retry_after == 5.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_get_retry_after_capitalized_header(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test retry-after header with different capitalization."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockRateLimitError(Exception):
            def __init__(self) -> None:
                self.response = Mock()
                self.response.headers = {"Retry-After": "10"}
                super().__init__("Rate limit exceeded")

        mock_anthropic_module.RateLimitError = MockRateLimitError
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        error = MockRateLimitError()
        retry_after = provider._get_retry_after(error)
        assert retry_after == 10.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_get_retry_after_no_header(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test retry-after extraction when header is missing."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockRateLimitError(Exception):
            def __init__(self) -> None:
                self.response = Mock()
                self.response.headers = {}
                super().__init__("Rate limit exceeded")

        mock_anthropic_module.RateLimitError = MockRateLimitError
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        error = MockRateLimitError()
        retry_after = provider._get_retry_after(error)
        assert retry_after is None

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_retry_on_rate_limit_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that rate limit errors trigger retry."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockRateLimitError(Exception):
            def __init__(self) -> None:
                self.response = Mock()
                self.response.headers = {}
                super().__init__("Rate limit exceeded")

        mock_anthropic_module.RateLimitError = MockRateLimitError
        mock_anthropic_module.BadRequestError = type("BadRequestError", (Exception,), {})

        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # First call raises RateLimitError, second succeeds
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Success")]
        mock_response.usage = Mock(input_tokens=10, output_tokens=20)
        mock_response.model = "claude-3-5-sonnet-latest"

        mock_client.messages.create = AsyncMock(side_effect=[MockRateLimitError(), mock_response])

        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import RetryConfig

        provider = ClaudeProvider(retry_config=RetryConfig(base_delay=0.01, max_delay=0.1))

        agent = AgentDef(name="test_agent", prompt="Test prompt")

        result = await provider.execute(agent, {}, "Test prompt")

        # Verify we got a successful response
        assert result.content["result"] == "Success"
        # Verify retry was attempted
        assert len(provider._retry_history) == 1
        assert provider._retry_history[0]["is_retryable"] is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_retry_on_timeout_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that timeout errors trigger retry."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockAPITimeoutError(Exception):
            pass

        mock_anthropic_module.APITimeoutError = MockAPITimeoutError
        mock_anthropic_module.BadRequestError = type("BadRequestError", (Exception,), {})

        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # First call times out, second succeeds
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Success")]
        mock_response.usage = Mock(input_tokens=10, output_tokens=20)
        mock_response.model = "claude-3-5-sonnet-latest"

        mock_client.messages.create = AsyncMock(
            side_effect=[MockAPITimeoutError("Timeout"), mock_response]
        )

        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import RetryConfig

        provider = ClaudeProvider(retry_config=RetryConfig(base_delay=0.01, max_delay=0.1))

        agent = AgentDef(name="test_agent", prompt="Test prompt")

        result = await provider.execute(agent, {}, "Test prompt")

        assert result.content["result"] == "Success"
        assert len(provider._retry_history) == 1
        assert provider._retry_history[0]["is_retryable"] is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_no_retry_on_auth_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that authentication errors do NOT trigger retry."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockAPIStatusError(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code
                super().__init__(f"Status {status_code}")

        mock_anthropic_module.APIStatusError = MockAPIStatusError
        mock_anthropic_module.BadRequestError = type("BadRequestError", (Exception,), {})

        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.messages.create = AsyncMock(side_effect=MockAPIStatusError(401))

        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()

        agent = AgentDef(name="test_agent", prompt="Test prompt")

        with pytest.raises(ProviderError) as exc_info:
            await provider.execute(agent, {}, "Test prompt")

        # Verify no retries occurred (only 1 attempt)
        assert len(provider._retry_history) == 1
        assert provider._retry_history[0]["is_retryable"] is False
        assert exc_info.value.status_code == 401

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_retry_exhaustion(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that retries are exhausted after max_attempts."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockRateLimitError(Exception):
            def __init__(self) -> None:
                self.response = Mock()
                self.response.headers = {}
                super().__init__("Rate limit exceeded")

        mock_anthropic_module.RateLimitError = MockRateLimitError
        mock_anthropic_module.BadRequestError = type("BadRequestError", (Exception,), {})

        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        # Always fail
        mock_client.messages.create = AsyncMock(side_effect=MockRateLimitError())

        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import RetryConfig

        provider = ClaudeProvider(
            retry_config=RetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1)
        )

        agent = AgentDef(name="test_agent", prompt="Test prompt")

        with pytest.raises(ProviderError, match="failed after 3 attempts"):
            await provider.execute(agent, {}, "Test prompt")

        # Verify all 3 attempts were made
        assert len(provider._retry_history) == 3
        assert all(h["is_retryable"] for h in provider._retry_history)

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_retry_history_tracking(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that retry history is properly tracked."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockRateLimitError(Exception):
            def __init__(self) -> None:
                self.response = Mock()
                self.response.headers = {}
                super().__init__("Rate limit exceeded")

        mock_anthropic_module.RateLimitError = MockRateLimitError
        mock_anthropic_module.BadRequestError = type("BadRequestError", (Exception,), {})

        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Success")]
        mock_response.usage = Mock(input_tokens=10, output_tokens=20)
        mock_response.model = "claude-3-5-sonnet-latest"

        # Fail twice, then succeed
        mock_client.messages.create = AsyncMock(
            side_effect=[MockRateLimitError(), MockRateLimitError(), mock_response]
        )

        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import RetryConfig

        provider = ClaudeProvider(retry_config=RetryConfig(base_delay=0.01, max_delay=0.1))

        agent = AgentDef(name="test_agent", prompt="Test prompt")

        await provider.execute(agent, {}, "Test prompt")

        # Verify retry history
        assert len(provider._retry_history) == 2
        assert provider._retry_history[0]["attempt"] == 1
        assert provider._retry_history[0]["agent_name"] == "test_agent"
        assert provider._retry_history[0]["is_retryable"] is True
        assert "delay" in provider._retry_history[0]

        assert provider._retry_history[1]["attempt"] == 2
        assert provider._retry_history[1]["is_retryable"] is True

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_retry_respects_retry_after_header(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that retry-after header overrides calculated delay."""
        mock_anthropic_module.__version__ = "0.77.0"

        class MockRateLimitError(Exception):
            def __init__(self) -> None:
                self.response = Mock()
                self.response.headers = {"retry-after": "5"}
                super().__init__("Rate limit exceeded")

        mock_anthropic_module.RateLimitError = MockRateLimitError
        mock_anthropic_module.BadRequestError = type("BadRequestError", (Exception,), {})

        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Success")]
        mock_response.usage = Mock(input_tokens=10, output_tokens=20)
        mock_response.model = "claude-3-5-sonnet-latest"

        mock_client.messages.create = AsyncMock(side_effect=[MockRateLimitError(), mock_response])

        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import RetryConfig

        provider = ClaudeProvider(
            retry_config=RetryConfig(base_delay=1.0, max_delay=10.0, jitter=0.0)
        )

        agent = AgentDef(name="test_agent", prompt="Test prompt")

        await provider.execute(agent, {}, "Test prompt")

        # Verify retry-after header was used (delay should be 5.0)
        assert len(provider._retry_history) == 1
        assert provider._retry_history[0]["delay"] == 5.0


class TestClaudeExecuteDialogTurn:
    """Tests for Claude provider dialog-turn API (provider parity with Copilot)."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_dialog_turn_empty_history_sends_only_current_message(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Empty history -> messages list contains only the current user message."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_text_block = Mock()
        mock_text_block.text = "the reply"
        mock_response = Mock()
        mock_response.content = [mock_text_block]
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        result = await provider.execute_dialog_turn(
            system_prompt="be a helpful assistant",
            user_message="hello",
            history=[],
        )

        assert result == "the reply"
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["system"] == "be a helpful assistant"
        assert kwargs["messages"] == [{"role": "user", "content": "hello"}]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_dialog_turn_multi_turn_history_preserved_in_order(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Multi-turn history is appended in order, with current message last."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_response = Mock()
        mock_response.content = [Mock(text="ack")]
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        await provider.execute_dialog_turn(
            system_prompt="sys",
            user_message="third user msg",
            history=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ],
        )

        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["messages"] == [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third user msg"},
        ]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_dialog_turn_model_override_used(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """model arg overrides the provider default."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_response = Mock()
        mock_response.content = [Mock(text="x")]
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        await provider.execute_dialog_turn(
            system_prompt="sys",
            user_message="hi",
            history=None,
            model="claude-3-opus-20240229",
        )

        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-3-opus-20240229"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_dialog_turn_error_wrapped_as_provider_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """SDK errors propagate as ProviderError, not bare exceptions."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.messages.create = AsyncMock(side_effect=RuntimeError("api down"))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        with pytest.raises(ProviderError, match="api down"):
            await provider.execute_dialog_turn(
                system_prompt="sys",
                user_message="hi",
                history=[],
            )


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
        """An SDK exception is swallowed and not cached, so a later call retries."""
        from anthropic import APIConnectionError

        # APIConnectionError requires a request kwarg; build a minimal one.
        err = APIConnectionError(request=Mock())

        mock_client = Mock()
        # First call raises, second call succeeds — proves the failure isn't
        # cached as "no metadata" forever.
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
        """Non-SDK exceptions (programming errors) are not swallowed by the
        provider — they bubble up so the engine's outer safety net handles them."""
        mock_client = Mock()
        mock_client.models.list = AsyncMock(side_effect=RuntimeError("bug"))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        with pytest.raises(RuntimeError):
            await provider.get_max_prompt_tokens("claude-sonnet-4-5")

    @pytest.mark.asyncio
    async def test_alias_resolves_via_match_model_id(self, mock_anthropic_class: Mock) -> None:
        """``-latest`` and dated suffix aliases resolve to the SDK's listed ID."""
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
        # `-latest` strips to base, then prefix-matches the dated SDK ID.
        assert await provider.get_max_prompt_tokens("claude-3-5-sonnet-latest") == 200_000
        # The base name (no dated suffix) also matches the dated SDK ID.
        assert await provider.get_max_prompt_tokens("claude-3-5-sonnet") == 200_000

    @pytest.mark.asyncio
    async def test_caches_after_first_call(self, mock_anthropic_class: Mock) -> None:
        """Second call must hit the cache, not the SDK."""
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
        """``validate_connection()`` populates the cache so the first
        ``get_max_prompt_tokens()`` call is a pure dict lookup."""
        mock_client = Mock()
        mock_client.models.list = AsyncMock(
            return_value=Mock(data=[Mock(id="claude-sonnet-4-5", max_input_tokens=200_000)])
        )
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        assert await provider.validate_connection() is True
        # validate_connection itself called list (once for the API check, then
        # _log_available_models reuses the response). Reset the counter to
        # prove get_max_prompt_tokens doesn't add another call.
        before = mock_client.models.list.await_count
        assert await provider.get_max_prompt_tokens("claude-sonnet-4-5") == 200_000
        assert mock_client.models.list.await_count == before

    @pytest.mark.asyncio
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", False)
    async def test_returns_none_when_sdk_unavailable(self, mock_anthropic_class: Mock) -> None:
        # Need a workaround: ANTHROPIC_SDK_AVAILABLE is False so __init__
        # raises. Build an instance bypassing the init guard by patching
        # only at call time.
        with patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True):
            provider = ClaudeProvider()

        with patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", False):
            assert await provider.get_max_prompt_tokens("claude-sonnet-4-5") is None


class TestClaudeReasoningEffort:
    """Tests for extended-thinking / reasoning effort plumbing."""

    @staticmethod
    def _build_provider(
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        *,
        default_reasoning_effort: str | None = None,
        temperature: float | None = None,
    ) -> tuple[ClaudeProvider, Mock]:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        text_block = Mock()
        text_block.type = "text"
        text_block.text = "ok"

        response = Mock()
        response.content = [text_block]
        response.usage = Mock(input_tokens=1, output_tokens=1)

        mock_client.messages.create = AsyncMock(return_value=response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider(
            temperature=temperature,
            default_reasoning_effort=default_reasoning_effort,  # type: ignore[arg-type]
        )
        return provider, mock_client

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_thinking_kwarg_forwarded_with_correct_shape(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        provider, mock_client = self._build_provider(mock_anthropic_module, mock_anthropic_class)
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-opus-4-20250514",
            reasoning=ReasoningConfig(effort="medium"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        kwargs = mock_client.messages.create.call_args[1]
        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8192}

    @pytest.mark.parametrize(
        "effort,expected",
        [("low", 2048), ("medium", 8192), ("high", 16384), ("xhigh", 32768)],
    )
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_effort_to_budget_mapping(
        self,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        effort: str,
        expected: int,
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        provider, mock_client = self._build_provider(mock_anthropic_module, mock_anthropic_class)
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-sonnet-4-20250514",
            reasoning=ReasoningConfig(effort=effort),  # type: ignore[arg-type]
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        kwargs = mock_client.messages.create.call_args[1]
        assert kwargs["thinking"]["budget_tokens"] == expected
        assert kwargs["thinking"]["type"] == "enabled"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_temperature_coerced_to_one_when_thinking_enabled(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        provider, mock_client = self._build_provider(
            mock_anthropic_module, mock_anthropic_class, temperature=0.3
        )
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-opus-4-20250514",
            reasoning=ReasoningConfig(effort="low"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        kwargs = mock_client.messages.create.call_args[1]
        assert kwargs["temperature"] == 1.0
        # User-configured temperature is preserved on the provider itself.
        assert provider._default_temperature == 0.3

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_max_tokens_bumped_above_budget(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        provider, mock_client = self._build_provider(mock_anthropic_module, mock_anthropic_class)
        # Default max_tokens=8192. xhigh budget=32768. Effective must be
        # >= 32768 + 4096 = 36864.
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-opus-4-20250514",
            reasoning=ReasoningConfig(effort="xhigh"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        kwargs = mock_client.messages.create.call_args[1]
        assert kwargs["max_tokens"] >= 32768 + 4096
        assert kwargs["max_tokens"] > kwargs["thinking"]["budget_tokens"]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_validation_error_on_non_thinking_model(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        provider, _ = self._build_provider(mock_anthropic_module, mock_anthropic_class)
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-3-5-sonnet-latest",
            reasoning=ReasoningConfig(effort="medium"),
        )
        with pytest.raises(ValidationError, match="extended thinking"):
            await provider.execute(agent=agent, context={}, rendered_prompt="p")

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_no_validation_error_on_thinking_model(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        provider, mock_client = self._build_provider(mock_anthropic_module, mock_anthropic_class)
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-opus-4-20250514",
            reasoning=ReasoningConfig(effort="high"),
        )
        # Should not raise.
        await provider.execute(agent=agent, context={}, rendered_prompt="p")
        assert "thinking" in mock_client.messages.create.call_args[1]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_runtime_default_used_when_agent_unset(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        provider, mock_client = self._build_provider(
            mock_anthropic_module,
            mock_anthropic_class,
            default_reasoning_effort="low",
        )
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-sonnet-4-20250514",
            # No per-agent reasoning configured.
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        kwargs = mock_client.messages.create.call_args[1]
        assert kwargs["thinking"]["budget_tokens"] == 2048

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_per_agent_reasoning_overrides_runtime_default(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        provider, mock_client = self._build_provider(
            mock_anthropic_module,
            mock_anthropic_class,
            default_reasoning_effort="low",
        )
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-sonnet-4-20250514",
            reasoning=ReasoningConfig(effort="high"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        kwargs = mock_client.messages.create.call_args[1]
        assert kwargs["thinking"]["budget_tokens"] == 16384

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_thinking_blocks_emit_agent_reasoning_event(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        thinking_block = Mock(spec=["type", "thinking"])
        thinking_block.type = "thinking"
        thinking_block.thinking = "Let me reason step by step..."

        text_block = Mock(spec=["type", "text"])
        text_block.type = "text"
        text_block.text = "Final answer"

        response = Mock()
        response.content = [thinking_block, text_block]
        response.usage = Mock(input_tokens=10, output_tokens=20)

        mock_client.messages.create = AsyncMock(return_value=response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-opus-4-20250514",
            reasoning=ReasoningConfig(effort="medium"),
        )

        events: list[tuple[str, dict]] = []

        def cb(event_type: str, data: dict) -> None:
            events.append((event_type, data))

        await provider.execute(agent=agent, context={}, rendered_prompt="p", event_callback=cb)

        reasoning_events = [e for e in events if e[0] == "agent_reasoning"]
        assert len(reasoning_events) == 1
        assert reasoning_events[0][1]["content"] == "Let me reason step by step..."

        message_events = [e for e in events if e[0] == "agent_message"]
        assert len(message_events) == 1
        assert message_events[0][1]["content"] == "Final answer"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_no_thinking_kwarg_when_reasoning_unset(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        provider, mock_client = self._build_provider(mock_anthropic_module, mock_anthropic_class)
        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-opus-4-20250514",
            # No reasoning config and no runtime default.
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        kwargs = mock_client.messages.create.call_args[1]
        assert "thinking" not in kwargs


class TestClaudeReasoningEffortRegressions:
    """Regression tests for fixes applied on feat/reasoning-effort.

    Covers:
    - Fix #1: thinking / redacted_thinking blocks must be echoed back in the
      assistant message replay across agentic-loop iterations, otherwise the
      Anthropic API rejects iteration 2+ with a 400 when reasoning + tool_use
      are combined.
    - Fix #3: ``execute_dialog_turn`` raises ``ValidationError`` (not
      ``ProviderError``) for non-thinking models when
      ``default_reasoning_effort`` is configured.
    - Fix #5 / coverage: ``thinking`` kwarg is forwarded through the
      parse-recovery path (not just the bare agentic-loop path).
    """

    @staticmethod
    def _build_provider_with_responses(
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        responses: list[Mock],
    ) -> tuple[ClaudeProvider, Mock]:
        """Build a ClaudeProvider whose messages.create returns ``responses`` in order."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.messages.create = AsyncMock(side_effect=responses)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        return provider, mock_client

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_thinking_block_preserved_in_agentic_loop_replay(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Regression for Fix #1 (CRITICAL).

        When a Claude response combines a ``thinking`` block with a ``tool_use``
        block, the next iteration of the agentic loop must replay BOTH blocks
        in the assistant message, in original order, with the thinking block
        serialized as ``{"type": "thinking", "thinking": ..., "signature": ...}``.
        Dropping the thinking block (or its signature) causes the Anthropic API
        to reject the next request with a 400.

        Also asserts ``thinking`` kwarg is forwarded on EVERY API call within
        the loop — not only the first.
        """
        from conductor.config.schema import ReasoningConfig

        # Iteration 1: thinking + tool_use → triggers MCP tool execution.
        thinking_block = Mock(spec=["type", "thinking", "signature"])
        thinking_block.type = "thinking"
        thinking_block.thinking = "Let me consider what tool to call..."
        thinking_block.signature = "sig-abc123"

        tool_use_block = Mock(spec=["type", "id", "name", "input"])
        tool_use_block.type = "tool_use"
        tool_use_block.id = "toolu_01"
        tool_use_block.name = "search"
        tool_use_block.input = {"query": "weather"}

        response_iter1 = Mock()
        response_iter1.content = [thinking_block, tool_use_block]
        response_iter1.usage = Mock(input_tokens=10, output_tokens=20)

        # Iteration 2: text only → loop terminates.
        text_block = Mock(spec=["type", "text"])
        text_block.type = "text"
        text_block.text = "It is sunny."

        response_iter2 = Mock()
        response_iter2.content = [text_block]
        response_iter2.usage = Mock(input_tokens=15, output_tokens=5)

        provider, mock_client = self._build_provider_with_responses(
            mock_anthropic_module, mock_anthropic_class, [response_iter1, response_iter2]
        )

        # Stub MCP manager so the tool_use is actually executed and the loop
        # advances to iteration 2 (without an MCP manager the loop bails out).
        # Injected into the cwd pool under the current process cwd, which is
        # what the agent (working_dir=None) resolves to.
        mock_mcp = Mock()
        mock_mcp.has_servers = Mock(return_value=False)  # don't add tools to the request
        mock_mcp.call_tool = AsyncMock(return_value="sunny")
        provider._mcp_managers[os.getcwd()] = mock_mcp

        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-opus-4-20250514",
            reasoning=ReasoningConfig(effort="medium"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        # Both API calls must have happened.
        assert mock_client.messages.create.await_count == 2

        # Thinking kwarg present on BOTH calls (not just the first).
        first_kwargs = mock_client.messages.create.call_args_list[0].kwargs
        second_kwargs = mock_client.messages.create.call_args_list[1].kwargs
        expected_thinking = {"type": "enabled", "budget_tokens": 8192}
        assert first_kwargs["thinking"] == expected_thinking
        assert second_kwargs["thinking"] == expected_thinking

        # The second call's messages must echo the assistant turn with both
        # blocks serialized in original order.
        replayed_messages = second_kwargs["messages"]
        assistant_turns = [m for m in replayed_messages if m["role"] == "assistant"]
        assert len(assistant_turns) == 1, "Exactly one assistant replay expected"
        assistant_content = assistant_turns[0]["content"]

        assert assistant_content == [
            {
                "type": "thinking",
                "thinking": "Let me consider what tool to call...",
                "signature": "sig-abc123",
            },
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "search",
                "input": {"query": "weather"},
            },
        ]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_redacted_thinking_block_preserved_in_agentic_loop_replay(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Regression for Fix #1: ``redacted_thinking`` blocks (no signature, has ``data``)
        must also be echoed in the assistant replay as
        ``{"type": "redacted_thinking", "data": ...}``.
        """
        from conductor.config.schema import ReasoningConfig

        redacted_block = Mock(spec=["type", "data"])
        redacted_block.type = "redacted_thinking"
        redacted_block.data = "REDACTED_PAYLOAD"

        tool_use_block = Mock(spec=["type", "id", "name", "input"])
        tool_use_block.type = "tool_use"
        tool_use_block.id = "toolu_02"
        tool_use_block.name = "search"
        tool_use_block.input = {"q": "x"}

        response_iter1 = Mock()
        response_iter1.content = [redacted_block, tool_use_block]
        response_iter1.usage = Mock(input_tokens=5, output_tokens=5)

        text_block = Mock(spec=["type", "text"])
        text_block.type = "text"
        text_block.text = "done"
        response_iter2 = Mock()
        response_iter2.content = [text_block]
        response_iter2.usage = Mock(input_tokens=1, output_tokens=1)

        provider, mock_client = self._build_provider_with_responses(
            mock_anthropic_module, mock_anthropic_class, [response_iter1, response_iter2]
        )

        mock_mcp = Mock()
        mock_mcp.has_servers = Mock(return_value=False)
        mock_mcp.call_tool = AsyncMock(return_value="ok")
        provider._mcp_managers[os.getcwd()] = mock_mcp

        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-opus-4-20250514",
            reasoning=ReasoningConfig(effort="low"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="p")

        assert mock_client.messages.create.await_count == 2
        replayed_messages = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        assistant_turns = [m for m in replayed_messages if m["role"] == "assistant"]
        assert assistant_turns[0]["content"] == [
            {"type": "redacted_thinking", "data": "REDACTED_PAYLOAD"},
            {
                "type": "tool_use",
                "id": "toolu_02",
                "name": "search",
                "input": {"q": "x"},
            },
        ]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_dialog_turn_raises_validation_error_for_non_thinking_model(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Regression for Fix #3.

        ``execute_dialog_turn`` must raise ``ValidationError`` (not silently
        drop the reasoning request, and not wrap it as ``ProviderError``)
        when ``default_reasoning_effort`` is set but the resolved model does
        not support extended thinking. Mirrors ``_resolve_thinking_for_agent``.
        """
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        # messages.create should NOT be called — the validation must trip
        # first. Setting it as AsyncMock guards against silent fallthrough.
        mock_client.messages.create = AsyncMock(return_value=Mock(content=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider(default_reasoning_effort="high")  # type: ignore[arg-type]

        with pytest.raises(ValidationError, match="extended thinking"):
            await provider.execute_dialog_turn(
                system_prompt="sys",
                user_message="hi",
                model="claude-3-5-sonnet-latest",
            )

        # And ensure no API call was made (no silent dropping of reasoning).
        mock_client.messages.create.assert_not_awaited()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_dialog_turn_succeeds_for_thinking_model_with_default_effort(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Companion to Fix #3: the same ``default_reasoning_effort`` setting
        must work on a thinking-capable model and forward the ``thinking``
        kwarg with the correct budget.
        """
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        text_block = Mock(spec=["type", "text"])
        text_block.type = "text"
        text_block.text = "hello"
        response = Mock()
        response.content = [text_block]
        mock_client.messages.create = AsyncMock(return_value=response)
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider(default_reasoning_effort="medium")  # type: ignore[arg-type]

        result = await provider.execute_dialog_turn(
            system_prompt="sys",
            user_message="hi",
            model="claude-opus-4-20250514",
        )

        assert result == "hello"
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8192}
        # max_tokens must accommodate the thinking budget.
        assert kwargs["max_tokens"] >= 8192 + 4096

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_thinking_kwarg_forwarded_through_parse_recovery_path(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Regression for Fix #5 / coverage gap.

        Agents with an ``output:`` schema route through
        ``_execute_with_parse_recovery`` rather than the bare ``_execute_api_call``
        path. Verify that ``thinking`` is forwarded on that path so reasoning
        is not silently dropped for structured-output agents.
        """
        from conductor.config.schema import ReasoningConfig

        # emit_output tool_use → parse-recovery happy path (no recovery needed,
        # but still goes through _execute_with_parse_recovery).
        emit_block = Mock(spec=["type", "name", "input", "id"])
        emit_block.type = "tool_use"
        emit_block.name = "emit_output"
        emit_block.id = "toolu_emit"
        emit_block.input = {"answer": "42"}

        response = Mock()
        response.content = [emit_block]
        response.usage = Mock(input_tokens=5, output_tokens=5)

        provider, mock_client = self._build_provider_with_responses(
            mock_anthropic_module, mock_anthropic_class, [response]
        )

        agent = AgentDef(
            name="t",
            prompt="p",
            model="claude-opus-4-20250514",
            reasoning=ReasoningConfig(effort="high"),
            output={"answer": OutputField(type="string")},
        )
        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")

        assert result.content == {"answer": "42"}
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 16384}
        # temperature must be coerced to 1.0 when thinking is enabled.
        assert kwargs["temperature"] == 1.0

    # TODO: cover _execute_api_call interrupt-race branch (interrupt_signal set
    # mid-call) — requires racing asyncio.Event with the mocked API call and is
    # exercised indirectly today via the agentic-loop tests above.
    # TODO: cover _request_partial_output path with thinking forwarded — this
    # is a fourth messages.create site reachable only via mid-agent interrupt
    # and partial-output flow; mocking complexity is prohibitive for a unit
    # test (would need a full asyncio interrupt fixture).


class TestClaudeMCPManagerPool:
    """Requirement: agents with different working_dirs must get isolated MCP servers.

    Covers the MCPManager pool keyed by resolved cwd (agent-mcp-working-dir
    todo 4): two cwds → two managers, repeated cwd → reuse, close() closes
    all, parallel agents with different cwds never share a manager, a
    connect failure for one cwd does not break another (fail-open), and the
    resolved cwd is forwarded to ``MCPManager.connect_server(cwd=...)``.
    """

    @staticmethod
    def _build_provider(
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
        mcp_servers: dict[str, dict[str, str]],
    ) -> ClaudeProvider:
        """Build a ClaudeProvider with a mocked Anthropic client and MCP config."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_client.messages.create = AsyncMock(return_value=Mock(content=[]))
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client
        return ClaudeProvider(mcp_servers=mcp_servers)

    @staticmethod
    def _manager_factory(instances: list[Mock]) -> type:
        """Return a fake MCPManager class whose instances are tracked in ``instances``."""

        class _FakeMCPManager:
            def __init__(self) -> None:
                self.connected: list[dict[str, object]] = []
                self.closed = False
                instances.append(self)  # type: ignore[arg-type]

            async def connect_server(self, **kwargs: object) -> list[dict[str, object]]:
                self.connected.append(kwargs)
                return []

            def has_servers(self) -> bool:
                return True

            def get_all_tools(self) -> list[dict[str, object]]:
                return []

            async def call_tool(self, name: str, arguments: dict[str, object]) -> str:
                return "ok"

            async def close(self) -> None:
                self.closed = True

        return _FakeMCPManager  # type: ignore[return-value]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_two_cwds_create_two_managers(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement: two distinct resolved cwds → two distinct pool entries.

        Each cwd needs its own MCPManager because stdio MCP servers are
        spawned per-manager with that manager's cwd; sharing one manager
        across cwds would silently run tools in the wrong directory.
        """
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
        # The cwd must be forwarded to connect_server for each pool entry.
        assert instances[0].connected[0]["cwd"] == "/repo/a"
        assert instances[1].connected[0]["cwd"] == "/repo/b"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_repeated_cwd_reuses_manager(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement: repeated resolution of the same cwd reuses the manager.

        Spawning a fresh MCP server process per agent execution would be
        prohibitively expensive; the pool must return the already-connected
        manager for a cwd seen before (no per-agent spawn/teardown).
        """
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
        # connect_server ran exactly once per configured server (no reconnect).
        assert len(instances[0].connected) == 1

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_close_closes_all_managers(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement: provider.close() closes every pooled manager (idempotent).

        MCP server subprocesses are tied to the provider lifetime; close()
        must iterate the whole pool and shut down each manager, and a second
        close() must be a safe no-op.
        """
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
            await provider.close()  # idempotent: must not raise

        assert len(instances) == 2
        assert all(inst.closed for inst in instances)

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_parallel_agents_no_race(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement: parallel agents with different cwds never share a manager.

        Concurrent first-use of the pool must not race: each cwd ends up with
        exactly one manager, and two agents resolving the same cwd observe the
        same instance (lock-guarded lazy connect).
        """
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

        # Exactly two managers total — one per cwd — despite concurrent access.
        assert len(instances) == 2
        assert results[0] is results[2]
        assert results[1] is results[3]
        assert results[0] is not results[1]

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_connect_failure_fail_open_per_cwd(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement: a connect failure for one cwd must not break another cwd.

        Fail-open per-server connect is existing behavior (the error is
        logged and remaining servers still connect). The pool must preserve
        it across pool keys: cwd A failing to connect leaves cwd B fully
        functional, and the failed key is not cached so a later retry can
        succeed.
        """
        servers = {"fs": {"command": "npx", "args": []}}
        provider = self._build_provider(mock_anthropic_module, mock_anthropic_class, servers)

        instances: list[Mock] = []
        fake_cls = self._manager_factory(instances)

        async def failing_connect(self: Mock, **kwargs: object) -> list[dict[str, object]]:
            if kwargs.get("cwd") == "/repo/bad":
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

        # Both keys resolve to *some* manager object (the helper never raises),
        # but the good cwd actually connected while the bad one did not.
        assert manager_bad is not manager_good
        assert instances[0].connected == []  # /repo/bad: connect raised
        assert instances[1].connected[0]["cwd"] == "/repo/good"

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_pool_no_config_returns_none(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Requirement: no runtime.mcp_servers configured → no pool entries.

        When the workflow declares no MCP servers the helper must return None
        and never construct an MCPManager, regardless of the agent's cwd.
        """
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
        """Requirement: agent.working_dir=None falls back to os.getcwd() as pool key.

        Agents that do not declare working_dir must behave exactly as before
        the pool existed: their MCP servers spawn in the conductor process's
        current working directory (precedence agent > runtime > cwd).
        """
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
            agent = AgentDef(name="a", prompt="p")  # working_dir=None
            resolved_cwd = agent.working_dir or os.getcwd()
            manager = await provider._get_mcp_manager_for_cwd(resolved_cwd)

        assert manager is not None
        assert instances[0].connected[0]["cwd"] == os.getcwd()
