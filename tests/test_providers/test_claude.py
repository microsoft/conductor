"""Unit tests for the ClaudeProvider implementation.

Tests cover:
- Provider initialization with SDK version verification
- Connection validation
- Basic message execution
- Structured output extraction (tool-based and fallback)
- Temperature validation (SDK-enforced behavior)
- Error handling and wrapping
"""

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

        assert result.content == {"text": "Hello, world!"}
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
        assert result.content == {"text": "First part. \nSecond part."}


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
        assert result.content == {"text": "This is just plain text"}
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

        mock_client.messages.create = AsyncMock(
            side_effect=[MockRateLimitError(), mock_response]
        )

        mock_anthropic_class.return_value = mock_client

        from conductor.providers.claude import RetryConfig

        provider = ClaudeProvider(retry_config=RetryConfig(base_delay=0.01, max_delay=0.1))

        agent = AgentDef(name="test_agent", prompt="Test prompt")

        result = await provider.execute(agent, {}, "Test prompt")

        # Verify we got a successful response
        assert result.content["text"] == "Success"
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

        assert result.content["text"] == "Success"
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

        mock_client.messages.create = AsyncMock(
            side_effect=[MockRateLimitError(), mock_response]
        )

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

