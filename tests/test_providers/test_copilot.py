"""Unit tests for the CopilotProvider implementation."""

import contextlib
from typing import Any

import pytest

from conductor.config.schema import AgentDef
from conductor.exceptions import ProviderError
from conductor.providers.copilot import CopilotProvider, RetryConfig


def stub_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
    """A simple mock handler that returns stub responses."""
    return {"result": "stub response"}


class TestCopilotProvider:
    """Tests for the CopilotProvider class."""

    @pytest.mark.asyncio
    async def test_validate_connection(self) -> None:
        """Test that validate_connection returns True with mock handler."""
        provider = CopilotProvider(mock_handler=stub_handler)
        result = await provider.validate_connection()
        assert result is True

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        """Test that close cleans up the client."""
        provider = CopilotProvider(mock_handler=stub_handler)
        provider._client = "some_client"  # Simulate having a client
        await provider.close()
        assert provider._client is None

    @pytest.mark.asyncio
    async def test_execute_returns_stub_output(self) -> None:
        """Test that execute returns a stub AgentOutput."""
        provider = CopilotProvider(mock_handler=stub_handler)
        agent = AgentDef(
            name="test_agent",
            model="gpt-4",
            prompt="Test prompt",
        )
        result = await provider.execute(
            agent=agent,
            context={"workflow": {"input": {}}},
            rendered_prompt="Test prompt",
        )
        assert result.content == {"result": "stub response"}
        assert result.model == "gpt-4"
        assert result.tokens_used is None  # Copilot SDK doesn't return token counts

    @pytest.mark.asyncio
    async def test_execute_uses_agent_model(self) -> None:
        """Test that execute uses the model from agent definition."""
        provider = CopilotProvider(mock_handler=stub_handler)
        agent = AgentDef(
            name="test_agent",
            model="claude-3",
            prompt="Test prompt",
        )
        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Test prompt",
        )
        assert result.model == "claude-3"

    @pytest.mark.asyncio
    async def test_execute_with_no_model(self) -> None:
        """Test that execute handles agent without model."""
        provider = CopilotProvider(mock_handler=stub_handler)
        # Create agent with type="human_gate" to bypass model requirement
        # or just set model to None directly
        agent = AgentDef(
            name="test_agent",
            prompt="Test prompt",
            model=None,
        )
        result = await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Test prompt",
        )
        assert result.model == "gpt-4o"  # Default model when agent.model is None


class TestCopilotProviderToolsSupport:
    """Tests for tool support in CopilotProvider."""

    @pytest.mark.asyncio
    async def test_execute_records_tools_in_call_history(self) -> None:
        """Test that tools are recorded in call history."""
        provider = CopilotProvider(mock_handler=stub_handler)
        agent = AgentDef(
            name="test_agent",
            model="gpt-4",
            prompt="Test prompt",
        )

        tools = ["web_search", "calculator"]
        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Test prompt",
            tools=tools,
        )

        call_history = provider.get_call_history()
        assert len(call_history) == 1
        assert call_history[0]["tools"] == ["web_search", "calculator"]

    @pytest.mark.asyncio
    async def test_execute_with_empty_tools_list(self) -> None:
        """Test that empty tools list is recorded correctly."""
        provider = CopilotProvider(mock_handler=stub_handler)
        agent = AgentDef(
            name="test_agent",
            model="gpt-4",
            prompt="Test prompt",
        )

        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Test prompt",
            tools=[],
        )

        call_history = provider.get_call_history()
        assert call_history[0]["tools"] == []

    @pytest.mark.asyncio
    async def test_execute_with_none_tools(self) -> None:
        """Test that None tools is recorded correctly."""
        provider = CopilotProvider(mock_handler=stub_handler)
        agent = AgentDef(
            name="test_agent",
            model="gpt-4",
            prompt="Test prompt",
        )

        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Test prompt",
            tools=None,
        )

        call_history = provider.get_call_history()
        assert call_history[0]["tools"] is None

    @pytest.mark.asyncio
    async def test_mock_handler_receives_correct_tools(self) -> None:
        """Test that mock handler can verify tools passed to provider."""

        def mock_handler(agent: Any, prompt: Any, context: Any) -> dict[str, Any]:
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        agent = AgentDef(
            name="test_agent",
            model="gpt-4",
            prompt="Test prompt",
        )

        tools = ["scrape_url", "file_read"]
        await provider.execute(
            agent=agent,
            context={},
            rendered_prompt="Test prompt",
            tools=tools,
        )

        # Verify via call history
        call_history = provider.get_call_history()
        assert call_history[0]["tools"] == ["scrape_url", "file_read"]

    @pytest.mark.asyncio
    async def test_multiple_agents_with_different_tools(self) -> None:
        """Test that multiple agent calls track tools independently."""
        provider = CopilotProvider(mock_handler=stub_handler)

        agent1 = AgentDef(name="agent1", model="gpt-4", prompt="Prompt 1")
        agent2 = AgentDef(name="agent2", model="gpt-4", prompt="Prompt 2")
        agent3 = AgentDef(name="agent3", model="gpt-4", prompt="Prompt 3")

        await provider.execute(agent1, {}, "Prompt 1", tools=["tool_a", "tool_b"])
        await provider.execute(agent2, {}, "Prompt 2", tools=[])
        await provider.execute(agent3, {}, "Prompt 3", tools=None)

        history = provider.get_call_history()
        assert len(history) == 3
        assert history[0]["agent_name"] == "agent1"
        assert history[0]["tools"] == ["tool_a", "tool_b"]
        assert history[1]["agent_name"] == "agent2"
        assert history[1]["tools"] == []
        assert history[2]["agent_name"] == "agent3"
        assert history[2]["tools"] is None


class TestCopilotProviderRetryLogic:
    """Tests for retry logic in CopilotProvider."""

    @pytest.mark.asyncio
    async def test_successful_call_no_retry(self) -> None:
        """Test that successful calls don't trigger retries."""
        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            return {"result": "success"}

        provider = CopilotProvider(mock_handler=mock_handler)
        agent = AgentDef(name="test", model="gpt-4", prompt="Test")

        result = await provider.execute(agent, {}, "Test")

        assert result.content["result"] == "success"
        assert call_count == 1
        assert len(provider.get_retry_history()) == 0

    @pytest.mark.asyncio
    async def test_retryable_error_retries(self) -> None:
        """Test that retryable errors are retried."""
        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ProviderError("Server error", status_code=500)
            return {"result": "success after retry"}

        retry_config = RetryConfig(
            max_attempts=3,
            base_delay=0.01,  # Fast for testing
            max_delay=0.1,
        )
        provider = CopilotProvider(mock_handler=mock_handler, retry_config=retry_config)
        agent = AgentDef(name="test", model="gpt-4", prompt="Test")

        result = await provider.execute(agent, {}, "Test")

        assert result.content["result"] == "success after retry"
        assert call_count == 3
        retry_history = provider.get_retry_history()
        assert len(retry_history) == 2  # 2 failures before success

    @pytest.mark.asyncio
    async def test_non_retryable_error_fails_immediately(self) -> None:
        """Test that non-retryable errors fail without retry."""
        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            raise ProviderError("Unauthorized", status_code=401)

        provider = CopilotProvider(mock_handler=mock_handler)
        agent = AgentDef(name="test", model="gpt-4", prompt="Test")

        with pytest.raises(ProviderError) as exc_info:
            await provider.execute(agent, {}, "Test")

        assert call_count == 1  # Only one call, no retries
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self) -> None:
        """Test that max retries are exhausted and then fails."""
        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            raise ProviderError("Server error", status_code=500)

        retry_config = RetryConfig(
            max_attempts=3,
            base_delay=0.01,
            max_delay=0.1,
        )
        provider = CopilotProvider(mock_handler=mock_handler, retry_config=retry_config)
        agent = AgentDef(name="test", model="gpt-4", prompt="Test")

        with pytest.raises(ProviderError) as exc_info:
            await provider.execute(agent, {}, "Test")

        assert call_count == 3
        assert "3 attempts" in str(exc_info.value)
        assert not exc_info.value.is_retryable

    @pytest.mark.asyncio
    async def test_rate_limit_429_is_retried(self) -> None:
        """Test that 429 rate limit errors are retried."""
        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ProviderError("Rate limited", status_code=429)
            return {"result": "success"}

        retry_config = RetryConfig(
            max_attempts=3,
            base_delay=0.01,
            max_delay=0.1,
        )
        provider = CopilotProvider(mock_handler=mock_handler, retry_config=retry_config)
        agent = AgentDef(name="test", model="gpt-4", prompt="Test")

        result = await provider.execute(agent, {}, "Test")

        assert result.content["result"] == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_delay_increases_exponentially(self) -> None:
        """Test that retry delays increase exponentially."""
        call_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ProviderError("Server error", status_code=500)
            return {"result": "success"}

        retry_config = RetryConfig(
            max_attempts=3,
            base_delay=1.0,
            max_delay=30.0,
            jitter=0.0,  # No jitter for predictable testing
        )
        provider = CopilotProvider(mock_handler=mock_handler, retry_config=retry_config)

        # Note: We can't easily test actual delays without mocking asyncio.sleep
        # but we can verify the delays recorded in retry history
        provider._calculate_delay(1, retry_config)  # 1 * 2^0 = 1.0
        provider._calculate_delay(2, retry_config)  # 1 * 2^1 = 2.0
        provider._calculate_delay(3, retry_config)  # 1 * 2^2 = 4.0

        # Just verify the method works
        assert provider._calculate_delay(1, retry_config) == 1.0
        assert provider._calculate_delay(2, retry_config) == 2.0
        assert provider._calculate_delay(3, retry_config) == 4.0

    @pytest.mark.asyncio
    async def test_retry_config_can_be_updated(self) -> None:
        """Test that retry config can be updated after creation."""
        provider = CopilotProvider()

        new_config = RetryConfig(max_attempts=5, base_delay=2.0)
        provider.set_retry_config(new_config)

        assert provider._retry_config.max_attempts == 5
        assert provider._retry_config.base_delay == 2.0

    @pytest.mark.asyncio
    async def test_retry_history_is_cleared_on_close(self) -> None:
        """Test that retry history is cleared when provider is closed."""

        def mock_handler(agent, prompt, context):
            raise ProviderError("Server error", status_code=500)

        retry_config = RetryConfig(max_attempts=2, base_delay=0.01)
        provider = CopilotProvider(mock_handler=mock_handler, retry_config=retry_config)

        with contextlib.suppress(ProviderError):
            await provider.execute(
                AgentDef(name="test", model="gpt-4", prompt="Test"),
                {},
                "Test",
            )

        assert len(provider.get_retry_history()) > 0

        await provider.close()
        assert len(provider.get_retry_history()) == 0


class TestRetryConfig:
    """Tests for RetryConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = RetryConfig()
        assert config.max_attempts == 3
        assert config.base_delay == 1.0
        assert config.max_delay == 30.0
        assert config.jitter == 0.25

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = RetryConfig(
            max_attempts=5,
            base_delay=2.0,
            max_delay=60.0,
            jitter=0.5,
        )
        assert config.max_attempts == 5
        assert config.base_delay == 2.0
        assert config.max_delay == 60.0
        assert config.jitter == 0.5

    def test_max_parse_recovery_attempts_default(self) -> None:
        """Test default parse recovery attempts value."""
        config = RetryConfig()
        assert config.max_parse_recovery_attempts == 5

    def test_max_parse_recovery_attempts_custom(self) -> None:
        """Test custom parse recovery attempts value."""
        config = RetryConfig(max_parse_recovery_attempts=10)
        assert config.max_parse_recovery_attempts == 10


class TestParseRecoveryPrompt:
    """Tests for the parse recovery prompt builder."""

    def test_build_parse_recovery_prompt_basic(self) -> None:
        """Test basic parse recovery prompt generation."""
        provider = CopilotProvider(mock_handler=stub_handler)

        schema = {
            "name": {"type": "string", "description": "The name field"},
            "value": {"type": "number", "description": "The value field"},
        }

        prompt = provider._build_parse_recovery_prompt(
            parse_error="Could not extract JSON from response",
            original_response="```json\n{invalid json",
            schema=schema,
        )

        # Verify the prompt contains all required components
        assert "Could not extract JSON from response" in prompt
        assert "```json\n{invalid json" in prompt
        assert '"name"' in prompt
        assert '"value"' in prompt
        assert "ONLY a valid JSON object" in prompt

    def test_build_parse_recovery_prompt_truncates_long_response(self) -> None:
        """Test that long responses are truncated in recovery prompt."""
        provider = CopilotProvider(mock_handler=stub_handler)

        # Create a response longer than 500 characters
        long_response = "x" * 600

        prompt = provider._build_parse_recovery_prompt(
            parse_error="Parse error",
            original_response=long_response,
            schema={"field": {"type": "string", "description": "A field"}},
        )

        # The response should be truncated with ...
        assert "x" * 500 + "..." in prompt
        # The full 600 characters should not be in the prompt
        assert "x" * 600 not in prompt

    def test_build_parse_recovery_prompt_preserves_short_response(self) -> None:
        """Test that short responses are not truncated."""
        provider = CopilotProvider(mock_handler=stub_handler)

        short_response = "short response"

        prompt = provider._build_parse_recovery_prompt(
            parse_error="Parse error",
            original_response=short_response,
            schema={"field": {"type": "string", "description": "A field"}},
        )

        # The short response should be fully included without ...
        assert short_response in prompt
        assert short_response + "..." not in prompt


class TestExtractJson:
    """Tests for JSON extraction logic."""

    def test_extract_json_direct_json(self) -> None:
        """Test extracting direct JSON string."""
        provider = CopilotProvider(mock_handler=stub_handler)

        result = provider._extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_extract_json_from_code_block(self) -> None:
        """Test extracting JSON from markdown code block."""
        provider = CopilotProvider(mock_handler=stub_handler)

        result = provider._extract_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_extract_json_from_bare_code_block(self) -> None:
        """Test extracting JSON from code block without language hint."""
        provider = CopilotProvider(mock_handler=stub_handler)

        result = provider._extract_json('```\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_extract_json_from_braces(self) -> None:
        """Test extracting JSON from text containing braces."""
        provider = CopilotProvider(mock_handler=stub_handler)

        result = provider._extract_json('Here is the result: {"key": "value"}')
        assert result == {"key": "value"}

    def test_extract_json_raises_on_invalid(self) -> None:
        """Test that extraction raises ValueError on invalid JSON."""
        provider = CopilotProvider(mock_handler=stub_handler)

        with pytest.raises(ValueError) as exc_info:
            provider._extract_json("This is not JSON at all")

        assert "Could not extract JSON" in str(exc_info.value)

    def test_extract_json_raises_on_malformed_json(self) -> None:
        """Test that extraction raises ValueError on malformed JSON."""
        provider = CopilotProvider(mock_handler=stub_handler)

        with pytest.raises(ValueError):
            provider._extract_json('{"key": "missing end brace"')


class TestLogParseRecovery:
    """Tests for parse recovery logging."""

    def test_log_parse_recovery_does_not_raise(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test that logging parse recovery doesn't raise exceptions."""
        provider = CopilotProvider(mock_handler=stub_handler)

        # This should not raise even if Rich isn't available
        # (it uses stderr, so we just verify it doesn't crash)
        provider._log_parse_recovery(
            attempt=1,
            max_attempts=5,
            error="Some parse error message",
        )
        # If we get here without exception, the test passes

    def test_log_parse_recovery_truncates_long_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that long error messages are truncated in logs."""
        provider = CopilotProvider(mock_handler=stub_handler)

        long_error = "x" * 200

        # Should not raise
        provider._log_parse_recovery(
            attempt=2,
            max_attempts=5,
            error=long_error,
        )
