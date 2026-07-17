"""Unit tests for the CopilotProvider implementation."""

import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from conductor.config.schema import AgentDef, ProviderSettings, ToolOutputConfig
from conductor.exceptions import ProviderError
from conductor.providers.copilot import CopilotProvider, RetryConfig, SDKResponse


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
    async def test_validate_external_runtime_connection_error(self) -> None:
        """External runtime failures should not recommend installing a local CLI."""
        provider = CopilotProvider(
            provider_settings=ProviderSettings(name="copilot", runtime_url="localhost:3000")
        )
        provider._ensure_client_started = AsyncMock(  # type: ignore[method-assign]
            side_effect=ConnectionRefusedError("connection refused")
        )

        with pytest.raises(ProviderError) as exc_info:
            await provider.validate_connection()

        assert "external Copilot runtime" in (exc_info.value.suggestion or "")
        assert "COPILOT_PROVIDER_RUNTIME_TOKEN" in (exc_info.value.suggestion or "")

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


class TestPromptSchemaGeneration:
    """Tests for prompt-facing schema generation."""

    def test_build_prompt_schema_recurses_through_nested_fields(self) -> None:
        """Nested object properties and array items are preserved in prompt schema."""
        provider = CopilotProvider(mock_handler=stub_handler)
        agent = AgentDef(
            name="planner",
            model="gpt-4",
            prompt="Plan the work",
            output={
                "plan": {
                    "type": "object",
                    "description": "Structured research plan",
                    "properties": {
                        "questions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "areas": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "focus": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                        },
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "summary": {
                    "type": "string",
                },
            },
        )

        schema = provider._build_prompt_schema(agent.output or {})

        assert schema["plan"]["type"] == "object"
        assert schema["plan"]["properties"]["questions"]["type"] == "array"
        assert schema["plan"]["properties"]["questions"]["items"]["type"] == "string"
        areas_props = schema["plan"]["properties"]["areas"]["items"]["properties"]
        assert areas_props["name"]["type"] == "string"
        assert (
            schema["plan"]["properties"]["areas"]["items"]["properties"]["focus"]["items"]["type"]
            == "string"
        )
        assert schema["plan"]["required"] == ["questions", "areas", "sources"]
        assert schema["summary"]["description"] == "The summary field"

    @pytest.mark.asyncio
    async def test_execute_appends_nested_schema_to_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual prompt sent to Copilot includes nested schema details."""
        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))
        agent = AgentDef(
            name="planner",
            model="gpt-4",
            prompt="Plan the work",
            output={
                "plan": {
                    "type": "object",
                    "properties": {
                        "questions": {"type": "array"},
                        "areas": {"type": "array"},
                        "sources": {"type": "array"},
                    },
                },
                "summary": {"type": "string"},
            },
        )

        class _FakeSession:
            session_id = "session-123"

            async def disconnect(self) -> None:
                return None

        class _FakeClient:
            async def create_session(self, **kwargs: Any) -> _FakeSession:
                return _FakeSession()

        captured_prompt: dict[str, str] = {}

        async def _noop() -> None:
            return None

        async def _fake_send_and_wait(*args: Any, **kwargs: Any) -> SDKResponse:
            captured_prompt["value"] = args[1]
            return SDKResponse(
                content='{"plan":{"questions":[],"areas":[],"sources":[]},"summary":"done"}'
            )

        provider._client = _FakeClient()
        monkeypatch.setattr(provider, "_ensure_client_started", _noop)
        monkeypatch.setattr(provider, "_send_and_wait", _fake_send_and_wait)

        await provider.execute(agent=agent, context={}, rendered_prompt="Plan the work")

        prompt = captured_prompt["value"]
        assert '"plan"' in prompt
        assert '"properties"' in prompt
        assert '"questions"' in prompt
        assert '"areas"' in prompt
        assert '"sources"' in prompt
        assert '"required"' in prompt
        assert "Return ONLY the JSON object, no other text." in prompt


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

    def test_extract_json_with_triple_backticks_inside_string(self) -> None:
        """Triple-backticks inside a string field must not truncate the JSON.

        Mirrors the parser fix in executor.output.parse_json_output — the
        greedy fallback closes at the LAST fence in the response.
        """
        provider = CopilotProvider(mock_handler=stub_handler)

        raw = '```json\n{"code": "use ```fenced``` blocks", "n": 1}\n```'
        result = provider._extract_json(raw)

        assert result == {"code": "use ```fenced``` blocks", "n": 1}

    def test_extract_json_with_multiple_fenced_blocks_first_wins(self) -> None:
        """When the response contains multiple fenced JSON blocks, the first
        valid block wins.

        Pins the behavior for the multi-block trade-off raised in PR review:
        try each non-greedy candidate in order and return the first parse.
        """
        provider = CopilotProvider(mock_handler=stub_handler)

        raw = '```json\n{"a": 1}\n```\n\nupdated answer:\n\n```json\n{"a": 2}\n```'
        result = provider._extract_json(raw)

        assert result == {"a": 1}


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

    def test_log_parse_recovery_emits_agent_tag_when_named(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When ``agent_name`` is provided, the rendered line includes it."""
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_parse_recovery(
            attempt=1,
            max_attempts=5,
            error="boom",
            agent_name="analyzer[item_a]",
        )

        captured = capsys.readouterr().err
        assert "[analyzer[item_a]]" in captured
        assert "Parse Recovery 1/5" in captured
        # The tag must precede the recovery icon
        assert captured.index("[analyzer[item_a]]") < captured.index("🔄")

    def test_log_parse_recovery_omits_tag_when_unnamed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When ``agent_name`` is omitted, no attribution tag is emitted."""
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_parse_recovery(attempt=1, max_attempts=5, error="boom")

        captured = capsys.readouterr().err
        # No bracketed tag preceding the recovery icon
        assert "[" not in captured.split("Parse Recovery")[0]


class TestLogRecoveryAttempt:
    """Tests for idle recovery attempt logging."""

    def test_emits_agent_tag_when_named(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When ``agent_name`` is provided, the rendered line includes it."""
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_recovery_attempt(
            attempt=2,
            last_event_type="tool.execution_start",
            last_tool_call="grep",
            agent_name="analyzer[item_b]",
        )

        captured = capsys.readouterr().err
        assert "[analyzer[item_b]]" in captured
        assert "Idle Recovery" in captured
        assert "grep" in captured
        # The tag must precede the warning icon
        assert captured.index("[analyzer[item_b]]") < captured.index("⚠️")

    def test_omits_tag_when_unnamed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No attribution tag emitted when ``agent_name`` is omitted."""
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_recovery_attempt(
            attempt=1,
            last_event_type="tool.execution_start",
            last_tool_call=None,
        )

        captured = capsys.readouterr().err
        # No bracketed tag preceding the recovery icon
        assert "[" not in captured.split("Idle Recovery")[0]


class TestLogEventVerbose:
    """Tests for SDK event verbose logging and agent attribution.

    The Copilot provider renders SDK events (tool calls, reasoning, processing
    indicators, sub-agent lifecycle) directly to the console via Rich. When
    concurrent for-each or parallel iterations interleave their output, an
    optional ``agent_name`` parameter prefixes each line with ``[agent_name]``
    so consumers can attribute output to a specific iteration.
    """

    @staticmethod
    def _event(**fields: Any) -> Any:
        """Build a fake SDK event with the given ``.data`` attributes."""
        from types import SimpleNamespace

        return SimpleNamespace(data=SimpleNamespace(**fields))

    def test_tool_execution_start_renders_agent_tag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "tool.execution_start",
            self._event(tool_name="view"),
            full_mode=False,
            agent_name="processor[item_a]",
        )

        out = capsys.readouterr().err
        assert "[processor[item_a]]" in out
        assert "view" in out
        # Tag must appear before the wrench icon (between tree prefix and icon)
        assert out.index("[processor[item_a]]") < out.index("🔧")

    def test_tool_execution_start_without_agent_tag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "tool.execution_start",
            self._event(tool_name="view"),
            full_mode=False,
        )

        out = capsys.readouterr().err
        assert "view" in out
        # No magenta agent tag should appear before the icon
        assert "[processor" not in out

    def test_tool_execution_start_args_line_has_agent_tag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ``args:`` continuation line in full mode must also be tagged."""
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "tool.execution_start",
            self._event(tool_name="grep", arguments={"pattern": "needle"}),
            full_mode=True,
            agent_name="processor[item_c]",
        )

        out = capsys.readouterr().err
        # Both the tool name line and the args line should carry the tag
        assert out.count("[processor[item_c]]") >= 2
        assert "args:" in out
        # On the args line, the tag must come before the ``args:`` literal
        assert out.index("[processor[item_c]]") < out.index("args:")

    def test_tool_execution_complete_renders_agent_tag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "tool.execution_complete",
            self._event(tool_name="view", result="some result"),
            full_mode=True,
            agent_name="processor[0]",
        )

        out = capsys.readouterr().err
        # Both the completion line and the result line should carry the tag
        assert out.count("[processor[0]]") >= 2
        assert "result:" in out
        # The tag must precede the check-mark icon on the completion line
        assert out.index("[processor[0]]") < out.index("✓")

    def test_tool_execution_complete_without_agent_tag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "tool.execution_complete",
            self._event(tool_name="view", result="some result"),
            full_mode=True,
        )

        out = capsys.readouterr().err
        assert "view" in out
        assert "result:" in out
        assert "[processor" not in out

    def test_reasoning_renders_agent_tag(self, capsys: pytest.CaptureFixture[str]) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "assistant.reasoning",
            self._event(content="thinking about it"),
            full_mode=True,
            agent_name="processor[item_x]",
        )

        out = capsys.readouterr().err
        assert "[processor[item_x]]" in out
        assert "thinking about it" in out
        # Tag must precede the brain icon
        assert out.index("[processor[item_x]]") < out.index("💭")

    def test_reasoning_without_agent_tag(self, capsys: pytest.CaptureFixture[str]) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "assistant.reasoning",
            self._event(content="thinking about it"),
            full_mode=True,
        )

        out = capsys.readouterr().err
        assert "thinking about it" in out
        assert "[processor" not in out

    def test_subagent_started_does_not_shadow_agent_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``subagent.started`` previously bound a local ``agent_name``. The
        outer attribution tag must still come from the method parameter, not
        from the sub-agent name."""
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "subagent.started",
            self._event(name="sub_helper"),
            full_mode=False,
            agent_name="planner[item_a]",
        )

        out = capsys.readouterr().err
        # Outer tag from method parameter
        assert "[planner[item_a]]" in out
        # Sub-agent name still rendered as the body of the line
        assert "sub_helper" in out
        # Outer tag must precede the robot icon
        assert out.index("[planner[item_a]]") < out.index("🤖")

    def test_subagent_started_without_agent_tag(self, capsys: pytest.CaptureFixture[str]) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "subagent.started",
            self._event(name="sub_helper"),
            full_mode=False,
        )

        out = capsys.readouterr().err
        assert "sub_helper" in out
        # No outer attribution tag — the sub-agent name in the line body
        # MUST NOT be confused with an attribution tag, so explicitly check
        # there's no bracketed tag before the robot icon.
        assert "[planner" not in out

    def test_subagent_completed_does_not_shadow_agent_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "subagent.completed",
            self._event(name="sub_helper"),
            full_mode=False,
            agent_name="planner[item_b]",
        )

        out = capsys.readouterr().err
        assert "[planner[item_b]]" in out
        assert "sub_helper" in out
        # Outer tag must precede the check-mark icon
        assert out.index("[planner[item_b]]") < out.index("✓")

    def test_subagent_completed_without_agent_tag(self, capsys: pytest.CaptureFixture[str]) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "subagent.completed",
            self._event(name="sub_helper"),
            full_mode=False,
        )

        out = capsys.readouterr().err
        assert "sub_helper" in out
        assert "[planner" not in out

    def test_turn_start_renders_agent_tag(self, capsys: pytest.CaptureFixture[str]) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "assistant.turn_start",
            self._event(turn_id=3),
            full_mode=True,
            agent_name="processor[42]",
        )

        out = capsys.readouterr().err
        assert "[processor[42]]" in out
        assert "Processing" in out
        # Tag must precede the hourglass icon
        assert out.index("[processor[42]]") < out.index("⏳")

    def test_turn_start_without_agent_tag(self, capsys: pytest.CaptureFixture[str]) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)

        provider._log_event_verbose(
            "assistant.turn_start",
            self._event(turn_id=3),
            full_mode=True,
        )

        out = capsys.readouterr().err
        assert "Processing" in out
        assert "[processor" not in out


class TestFixPipeBlockingMode:
    """Tests for _fix_pipe_blocking_mode Windows platform guard."""

    def test_skips_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that _fix_pipe_blocking_mode is a no-op on Windows."""
        monkeypatch.setattr("sys.platform", "win32")
        provider = CopilotProvider(mock_handler=stub_handler)
        # Should return immediately without importing fcntl
        provider._fix_pipe_blocking_mode()

    def test_runs_on_unix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that _fix_pipe_blocking_mode does not skip on non-Windows."""
        monkeypatch.setattr("sys.platform", "linux")
        provider = CopilotProvider(mock_handler=stub_handler)
        # On actual Windows, fcntl is unavailable so the import will fail.
        # The important assertion is that the platform guard did NOT return
        # early — it proceeded past the guard and attempted the import.
        with contextlib.suppress(ModuleNotFoundError):
            provider._fix_pipe_blocking_mode()


class TestCopilotExecuteDialogTurn:
    """Tests for Copilot provider dialog-turn API (provider parity with Claude).

    The Copilot SDK is session-based and event-driven, so we mock create_session
    and synthesize the assistant.message + session.idle events that real sessions
    emit.
    """

    @staticmethod
    def _build_event(event_type: str, content: str = "", message: str = "") -> Any:
        """Build a fake SDK event with .type.value, .data.content, .data.message."""
        from unittest.mock import Mock as _Mock

        ev = _Mock()
        ev.type = _Mock()
        ev.type.value = event_type
        ev.data = _Mock()
        ev.data.content = content
        ev.data.message = message
        return ev

    async def _make_provider_with_session(
        self,
        captured: dict[str, Any],
        response_text: str = "an answer",
    ) -> CopilotProvider:
        """Build a provider whose create_session returns a session that, on send,
        invokes its event handler with assistant.message + session.idle.
        """
        from unittest.mock import AsyncMock as _AsyncMock

        provider = CopilotProvider(mock_handler=stub_handler)
        # Force the started state without invoking the real SDK
        provider._started = True

        session = _AsyncMock()
        captured_callback: dict[str, Any] = {}

        def on_event(callback: Any) -> None:
            captured_callback["cb"] = callback

        session.on = on_event

        async def send(prompt: str) -> None:
            captured["sent_prompt"] = prompt
            cb = captured_callback["cb"]
            cb(self._build_event("assistant.message", response_text))
            cb(self._build_event("session.idle"))

        session.send = send
        session.destroy = _AsyncMock()

        async def create_session(**kwargs: Any) -> Any:
            captured["create_session_kwargs"] = kwargs
            return session

        client = _AsyncMock()
        client.create_session = create_session
        provider._client = client
        return provider

    @pytest.mark.asyncio
    async def test_dialog_turn_empty_history_sends_only_current_message(self) -> None:
        captured: dict[str, Any] = {}
        provider = await self._make_provider_with_session(captured, response_text="reply")

        result = await provider.execute_dialog_turn(
            system_prompt="be helpful",
            user_message="hello",
            history=[],
        )

        assert result == "reply"
        # System prompt is sent via create_session, not embedded in the prompt body
        assert captured["create_session_kwargs"]["system_message"] == {
            "mode": "replace",
            "content": "be helpful",
        }
        # With empty history, the prompt is just the current user message line.
        assert captured["sent_prompt"] == "User: hello"

    @pytest.mark.asyncio
    async def test_dialog_turn_multi_turn_history_serialized_in_order(self) -> None:
        captured: dict[str, Any] = {}
        provider = await self._make_provider_with_session(captured)

        await provider.execute_dialog_turn(
            system_prompt="sys",
            user_message="third",
            history=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ],
        )

        # History serialized as User:/Assistant: blocks separated by blank lines,
        # with the current message appended last as a User: block.
        assert captured["sent_prompt"] == ("User: first\n\nAssistant: second\n\nUser: third")

    @pytest.mark.asyncio
    async def test_dialog_turn_model_override_used(self) -> None:
        captured: dict[str, Any] = {}
        provider = await self._make_provider_with_session(captured)

        await provider.execute_dialog_turn(
            system_prompt="sys",
            user_message="hi",
            history=None,
            model="claude-sonnet-4.5",
        )

        assert captured["create_session_kwargs"]["model"] == "claude-sonnet-4.5"

    @pytest.mark.asyncio
    async def test_dialog_turn_default_context_tier_forwarded(self) -> None:
        captured: dict[str, Any] = {}
        provider = await self._make_provider_with_session(captured)
        provider._default_context_tier = "long_context"

        await provider.execute_dialog_turn(
            system_prompt="sys",
            user_message="hi",
            history=None,
        )

        assert captured["create_session_kwargs"]["context_tier"] == "long_context"

    @pytest.mark.asyncio
    async def test_dialog_turn_no_context_tier_means_key_absent(self) -> None:
        captured: dict[str, Any] = {}
        provider = await self._make_provider_with_session(captured)

        await provider.execute_dialog_turn(
            system_prompt="sys",
            user_message="hi",
            history=None,
        )

        assert "context_tier" not in captured["create_session_kwargs"]

    @pytest.mark.asyncio
    async def test_dialog_turn_session_error_wrapped_as_provider_error(self) -> None:
        from unittest.mock import AsyncMock as _AsyncMock

        provider = CopilotProvider(mock_handler=stub_handler)
        provider._started = True

        session = _AsyncMock()
        captured_callback: dict[str, Any] = {}

        def on_event(callback: Any) -> None:
            captured_callback["cb"] = callback

        session.on = on_event

        async def send(prompt: str) -> None:
            cb = captured_callback["cb"]
            cb(self._build_event("session.error", message="internal failure"))

        session.send = send
        session.destroy = _AsyncMock()

        async def create_session(**kwargs: Any) -> Any:
            return session

        client = _AsyncMock()
        client.create_session = create_session
        provider._client = client

        with pytest.raises(ProviderError, match="Dialog turn error"):
            await provider.execute_dialog_turn(
                system_prompt="sys",
                user_message="hi",
                history=[],
            )


class TestCopilotProviderLargeOutput:
    """Tests for ``large_output`` forwarding to the Copilot SDK."""

    @staticmethod
    def _captured_create_session(provider: CopilotProvider) -> dict[str, Any]:
        """Execute with a fake client and return captured create_session kwargs."""
        captured: dict[str, Any] = {}

        class _FakeSession:
            session_id = "sess-large-output"

            async def disconnect(self) -> None:
                return None

        class _FakeClient:
            async def create_session(self, **kwargs: Any) -> _FakeSession:
                captured.update(kwargs)
                return _FakeSession()

        provider._client = _FakeClient()
        provider._mock_handler = None
        provider._started = True

        import asyncio

        async def _noop() -> None:
            return None

        async def _fake_send_and_wait(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(content='{"ok":true}')

        provider._ensure_client_started = _noop  # type: ignore[method-assign]
        provider._send_and_wait = _fake_send_and_wait  # type: ignore[method-assign]

        agent = AgentDef(name="agent", model="gpt-4o", prompt="p")
        asyncio.run(provider.execute(agent, {}, "p"))
        return captured

    def _captured_dialog_session(self, provider: CopilotProvider) -> dict[str, Any]:
        """Run a dialog turn and return the create_session kwargs it used."""
        captured: dict[str, Any] = {}

        class _AsyncMockSession:
            def __init__(self) -> None:
                self._callback: Any = None

            def on(self, callback: Any) -> None:
                self._callback = callback

            async def send(self, prompt: str) -> None:
                self._callback(_AsyncMockEvent("assistant.message", "ok"))
                self._callback(_AsyncMockEvent("session.idle"))

        class _AsyncMockEvent:
            def __init__(self, event_type: str, content: str = "") -> None:
                self.type = _AsyncMockType(event_type)
                self.data = _AsyncMockData(content)

        class _AsyncMockType:
            def __init__(self, value: str) -> None:
                self.value = value

        class _AsyncMockData:
            def __init__(self, content: str) -> None:
                self.content = content
                self.message = content

        class _Client:
            async def create_session(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return _AsyncMockSession()

        provider._client = _Client()
        provider._started = True

        import asyncio

        asyncio.run(provider.execute_dialog_turn("sys", "hi", []))
        return captured

    def test_default_config_forwards_large_output_to_create_session(self) -> None:
        """Default ToolOutputConfig produces enabled=True and default max_size_bytes."""
        provider = CopilotProvider()
        captured = self._captured_create_session(provider)
        assert captured["large_output"] == {"enabled": True, "max_size_bytes": 50000}

    def test_disabled_config_omits_large_output_key(self) -> None:
        """enabled=False means the large_output key is absent from create_session."""
        provider = CopilotProvider(tool_output=ToolOutputConfig(enabled=False))
        captured = self._captured_create_session(provider)
        assert "large_output" not in captured

    def test_spill_to_file_false_maps_to_enabled_false(self) -> None:
        """spill_to_file=False disables large_output handling entirely (SDK limitation)."""
        provider = CopilotProvider(tool_output=ToolOutputConfig(spill_to_file=False))
        captured = self._captured_create_session(provider)
        assert captured["large_output"] == {"enabled": False}

    def test_spill_dir_is_forwarded_when_set(self) -> None:
        """An explicit spill_dir becomes output_directory in SDK config."""
        provider = CopilotProvider(
            tool_output=ToolOutputConfig(spill_dir="/tmp/custom-tool-output")
        )
        captured = self._captured_create_session(provider)
        assert captured["large_output"] == {
            "enabled": True,
            "max_size_bytes": 50000,
            "output_directory": "/tmp/custom-tool-output",
        }

    def test_large_output_forwarded_to_dialog_session(self) -> None:
        """Dialog turns also receive the large_output config."""
        provider = CopilotProvider()
        captured = self._captured_dialog_session(provider)
        assert captured["large_output"] == {"enabled": True, "max_size_bytes": 50000}

    def test_large_output_disabled_omits_key_for_dialog_session(self) -> None:
        """enabled=False omits large_output from dialog create_session kwargs."""
        provider = CopilotProvider(tool_output=ToolOutputConfig(enabled=False))
        captured = self._captured_dialog_session(provider)
        assert "large_output" not in captured


class TestGetMaxPromptTokens:
    """Tests for CopilotProvider.get_max_prompt_tokens."""

    @staticmethod
    def _make_model(model_id: str, max_prompt_tokens: int) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            id=model_id,
            capabilities=SimpleNamespace(
                limits=SimpleNamespace(max_prompt_tokens=max_prompt_tokens)
            ),
        )

    @staticmethod
    def _provider_with_list_models(list_models_impl: Any) -> CopilotProvider:
        """Build a provider with the SDK short-circuit disabled and a fake client.

        Uses ``stub_handler`` for ``mock_handler`` to satisfy the constructor,
        then nulls ``_mock_handler`` so ``get_max_prompt_tokens`` falls through
        to the SDK path. ``_started=True`` skips ``_ensure_client_started``.
        """
        from types import SimpleNamespace

        provider = CopilotProvider(mock_handler=stub_handler)
        provider._mock_handler = None
        provider._client = SimpleNamespace(list_models=list_models_impl)
        provider._started = True
        return provider

    @pytest.mark.asyncio
    async def test_mock_handler_mode_returns_none(self) -> None:
        """Mock-handler mode has no SDK to query — must return None."""
        provider = CopilotProvider(mock_handler=stub_handler)
        assert await provider.get_max_prompt_tokens("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_returns_max_prompt_tokens_for_known_model(self) -> None:
        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", 128000)]

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_max_prompt_tokens("gpt-4o") == 128000

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_model(self) -> None:
        async def list_models() -> list[Any]:
            return []

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_max_prompt_tokens("anything") is None

    @pytest.mark.asyncio
    async def test_oserror_returns_none_and_does_not_cache(self) -> None:
        """A transport-level error is swallowed; the next call retries."""
        calls = 0

        async def list_models() -> list[Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("network down")
            return [self._make_model("gpt-4o", 128000)]

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_max_prompt_tokens("gpt-4o") is None
        assert await provider.get_max_prompt_tokens("gpt-4o") == 128000

    @pytest.mark.asyncio
    async def test_value_error_from_sdk_parser_returns_none(self) -> None:
        """SDK schema-parsing failures (e.g. ``ValueError`` from
        ``ModelBilling.from_dict`` when the API omits the ``multiplier``
        field) must be soft-swallowed — context-window metadata must
        never block workflow execution.

        Regression for github-copilot-sdk 0.3.0, where ``list_models()``
        eagerly parses every model and a single malformed entry (observed
        for ``claude-opus-4.7-1m-internal``) raises ``ValueError`` and
        kills the whole call.
        """

        async def list_models() -> list[Any]:
            raise ValueError("Missing required field 'multiplier' in ModelBilling")

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_max_prompt_tokens("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_alias_resolves_via_match_model_id(self) -> None:
        """Versioned-suffix aliases resolve to the SDK's listed ID."""

        async def list_models() -> list[Any]:
            return [self._make_model("claude-3-5-sonnet", 200_000)]

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_max_prompt_tokens("claude-3-5-sonnet-latest") == 200_000
        assert await provider.get_max_prompt_tokens("claude-3-5-sonnet-20241022") == 200_000


class TestGetModelPricing:
    """Tests for CopilotProvider.get_model_pricing (#265)."""

    @staticmethod
    def _make_model(
        model_id: str,
        *,
        batch_size: int | None = 1_000_000,
        input_price: float | None = 100.0,
        output_price: float | None = 300.0,
        cache_price: float | None = 10.0,
        multiplier: float = 1.0,
        billing: bool = True,
        token_prices: bool = True,
    ) -> Any:
        from types import SimpleNamespace

        tp = None
        if token_prices:
            tp = SimpleNamespace(
                batch_size=batch_size,
                input_price=input_price,
                output_price=output_price,
                cache_price=cache_price,
            )
        billing_obj = SimpleNamespace(multiplier=multiplier, token_prices=tp) if billing else None
        return SimpleNamespace(
            id=model_id,
            capabilities=SimpleNamespace(limits=SimpleNamespace(max_prompt_tokens=128_000)),
            billing=billing_obj,
        )

    @staticmethod
    def _provider_with_list_models(list_models_impl: Any) -> CopilotProvider:
        from types import SimpleNamespace

        provider = CopilotProvider(mock_handler=stub_handler)
        provider._mock_handler = None
        provider._client = SimpleNamespace(list_models=list_models_impl)
        provider._started = True
        return provider

    @pytest.mark.asyncio
    async def test_mock_handler_mode_returns_none(self) -> None:
        """Mock-handler mode has no SDK to query — must return None."""
        provider = CopilotProvider(mock_handler=stub_handler)
        assert await provider.get_model_pricing("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_derives_usd_pricing_from_token_prices(self) -> None:
        # batch_size 1M with input=100 credits/batch => 100 credits per 1M tokens
        # => $1.00 / Mtok at 100 credits = $1; output 300 => $3.00; cache 10 => $0.10.
        async def list_models() -> list[Any]:
            return [
                self._make_model(
                    "gpt-5.5",
                    batch_size=1_000_000,
                    input_price=100.0,
                    output_price=300.0,
                    cache_price=10.0,
                )
            ]

        provider = self._provider_with_list_models(list_models)
        pricing = await provider.get_model_pricing("gpt-5.5")
        assert pricing is not None
        assert pricing.input_per_mtok == pytest.approx(1.00)
        assert pricing.output_per_mtok == pytest.approx(3.00)
        assert pricing.cache_read_per_mtok == pytest.approx(0.10)
        assert pricing.cache_write_per_mtok == 0.0

    @pytest.mark.asyncio
    async def test_small_batch_size_scales_correctly(self) -> None:
        # batch_size 1000, input 1 credit/batch => 1 credit per 1000 tokens
        # => 1000 credits/Mtok => $10.00 / Mtok.
        async def list_models() -> list[Any]:
            return [
                self._make_model(
                    "gpt-4o",
                    batch_size=1000,
                    input_price=1.0,
                    output_price=3.0,
                    cache_price=None,
                )
            ]

        provider = self._provider_with_list_models(list_models)
        pricing = await provider.get_model_pricing("gpt-4o")
        assert pricing is not None
        assert pricing.input_per_mtok == pytest.approx(10.0)
        assert pricing.output_per_mtok == pytest.approx(30.0)
        assert pricing.cache_read_per_mtok == 0.0

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self) -> None:
        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o")]

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_model_pricing("nonexistent") is None

    @pytest.mark.asyncio
    async def test_missing_billing_returns_none(self) -> None:
        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", billing=False)]

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_model_pricing("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_missing_token_prices_returns_none(self) -> None:
        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", token_prices=False)]

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_model_pricing("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_missing_or_zero_batch_size_returns_none(self) -> None:
        async def list_none() -> list[Any]:
            return [self._make_model("gpt-4o", batch_size=None)]

        async def list_zero() -> list[Any]:
            return [self._make_model("gpt-4o", batch_size=0)]

        assert await self._provider_with_list_models(list_none).get_model_pricing("gpt-4o") is None
        assert await self._provider_with_list_models(list_zero).get_model_pricing("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_missing_input_or_output_price_returns_none(self) -> None:
        async def list_no_input() -> list[Any]:
            return [self._make_model("gpt-4o", input_price=None)]

        async def list_no_output() -> list[Any]:
            return [self._make_model("gpt-4o", output_price=None)]

        p1 = self._provider_with_list_models(list_no_input)
        p2 = self._provider_with_list_models(list_no_output)
        assert await p1.get_model_pricing("gpt-4o") is None
        assert await p2.get_model_pricing("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_missing_cache_price_defaults_to_zero(self) -> None:
        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", cache_price=None)]

        provider = self._provider_with_list_models(list_models)
        pricing = await provider.get_model_pricing("gpt-4o")
        assert pricing is not None
        assert pricing.cache_read_per_mtok == 0.0

    @pytest.mark.asyncio
    async def test_list_models_failure_returns_none(self) -> None:
        """SDK parse/transport failures are soft-swallowed — never raise (#265)."""

        async def list_models() -> list[Any]:
            raise ValueError("Missing required field 'multiplier' in ModelBilling")

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_model_pricing("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_alias_resolves_via_match_model_id(self) -> None:
        """Versioned-suffix aliases resolve to the SDK's listed ID before pricing."""

        async def list_models() -> list[Any]:
            return [self._make_model("claude-3-5-sonnet", batch_size=1_000_000, input_price=100.0)]

        provider = self._provider_with_list_models(list_models)
        pricing = await provider.get_model_pricing("claude-3-5-sonnet-latest")
        assert pricing is not None
        assert pricing.input_per_mtok == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_negative_price_returns_none(self) -> None:
        """A negative price is malformed — fall back to the table, don't emit it (#265)."""

        async def list_neg_input() -> list[Any]:
            return [self._make_model("gpt-4o", input_price=-5.0)]

        async def list_neg_output() -> list[Any]:
            return [self._make_model("gpt-4o", output_price=-1.0)]

        p_in = self._provider_with_list_models(list_neg_input)
        p_out = self._provider_with_list_models(list_neg_output)
        assert await p_in.get_model_pricing("gpt-4o") is None
        assert await p_out.get_model_pricing("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_nan_or_inf_price_returns_none(self) -> None:
        """NaN / inf prices are rejected rather than propagated as garbage cost (#265)."""

        async def list_nan() -> list[Any]:
            return [self._make_model("gpt-4o", input_price=float("nan"))]

        async def list_inf() -> list[Any]:
            return [self._make_model("gpt-4o", output_price=float("inf"))]

        assert await self._provider_with_list_models(list_nan).get_model_pricing("gpt-4o") is None
        assert await self._provider_with_list_models(list_inf).get_model_pricing("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_unconvertible_huge_int_price_returns_none(self) -> None:
        """A price int too large to convert to float is rejected, not raised (#265)."""

        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", input_price=10**400)]

        provider = self._provider_with_list_models(list_models)
        # Must not raise OverflowError — degrades to None (static-table fallback).
        assert await provider.get_model_pricing("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_zero_price_is_priced_as_free(self) -> None:
        """A genuine 0.0 rate is a free model (distinct from unpriced) and is kept."""

        async def list_models() -> list[Any]:
            return [
                self._make_model(
                    "free-model", batch_size=1_000_000, input_price=0.0, output_price=0.0
                )
            ]

        provider = self._provider_with_list_models(list_models)
        pricing = await provider.get_model_pricing("free-model")
        assert pricing is not None
        assert pricing.input_per_mtok == 0.0
        assert pricing.output_per_mtok == 0.0

    @pytest.mark.asyncio
    async def test_negative_cache_price_falls_back_to_zero(self) -> None:
        """A malformed cache price degrades to 0.0 rather than a negative cache rate."""

        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", cache_price=-3.0)]

        provider = self._provider_with_list_models(list_models)
        pricing = await provider.get_model_pricing("gpt-4o")
        assert pricing is not None
        assert pricing.cache_read_per_mtok == 0.0

    @pytest.mark.asyncio
    async def test_billing_multiplier_is_ignored(self) -> None:
        """Per-request billing.multiplier must NOT scale the per-token price (#265).

        Pins the intentional decision: token cost is billed per token via
        token_prices; the premium-request multiplier is a separate mechanism.
        """

        async def list_models() -> list[Any]:
            return [
                self._make_model("gpt-5.5", batch_size=1_000_000, input_price=100.0, multiplier=5.0)
            ]

        provider = self._provider_with_list_models(list_models)
        pricing = await provider.get_model_pricing("gpt-5.5")
        assert pricing is not None
        # $1.00/Mtok regardless of the 5x multiplier.
        assert pricing.input_per_mtok == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_malformed_models_list_returns_none(self) -> None:
        """A non-iterable / malformed models payload degrades to None (never raises)."""

        async def list_models() -> Any:
            return object()  # not iterable — the dict comprehension would raise

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_model_pricing("gpt-4o") is None


class TestReasoningEffort:
    """Tests for reasoning_effort plumbing into create_session."""

    @staticmethod
    def _make_model(model_id: str, supported: list[str] | None) -> Any:
        from types import SimpleNamespace

        # Mirrors the real github-copilot-sdk ``Model`` shape: reasoning-effort
        # fields are top level on ``Model``, NOT nested under ``capabilities``
        # (see the fix for the #301 validation-read bug).
        return SimpleNamespace(
            id=model_id,
            supported_reasoning_efforts=supported,
            capabilities=SimpleNamespace(
                limits=SimpleNamespace(max_prompt_tokens=128_000),
            ),
        )

    @staticmethod
    async def _build_provider(
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        *,
        default_reasoning_effort: str | None = None,
        list_models_impl: Any = None,
    ) -> CopilotProvider:
        """Build a provider with a fake client that captures create_session kwargs.

        The provider is in real-SDK mode (``_mock_handler`` set to ``None``)
        so the validation + plumbing path is exercised end to end.
        """

        class _FakeSession:
            session_id = "session-xyz"

            async def disconnect(self) -> None:
                return None

        class _FakeClient:
            async def create_session(self, **kwargs: Any) -> _FakeSession:
                captured["create_session_kwargs"] = kwargs
                return _FakeSession()

            async def list_models(self) -> list[Any]:
                if list_models_impl is None:
                    return []
                return await list_models_impl()

        provider = CopilotProvider(
            mock_handler=stub_handler,
            default_reasoning_effort=default_reasoning_effort,
        )
        provider._mock_handler = None
        provider._client = _FakeClient()
        provider._started = True

        async def _noop() -> None:
            return None

        async def _fake_send_and_wait(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(content='{"ok":true}')

        monkeypatch.setattr(provider, "_ensure_client_started", _noop)
        monkeypatch.setattr(provider, "_send_and_wait", _fake_send_and_wait)
        return provider

    @pytest.mark.asyncio
    async def test_per_agent_effort_forwarded_to_create_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch)
        agent = AgentDef(
            name="planner",
            model="gpt-4o",
            prompt="Plan",
            reasoning=ReasoningConfig(effort="high"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="Plan")
        assert captured["create_session_kwargs"]["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_runtime_default_used_when_agent_has_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        provider = await self._build_provider(
            captured, monkeypatch, default_reasoning_effort="medium"
        )
        agent = AgentDef(name="planner", model="gpt-4o", prompt="Plan")
        await provider.execute(agent=agent, context={}, rendered_prompt="Plan")
        assert captured["create_session_kwargs"]["reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_per_agent_effort_overrides_runtime_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.config.schema import ReasoningConfig

        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch, default_reasoning_effort="low")
        agent = AgentDef(
            name="planner",
            model="gpt-4o",
            prompt="Plan",
            reasoning=ReasoningConfig(effort="xhigh"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="Plan")
        assert captured["create_session_kwargs"]["reasoning_effort"] == "xhigh"

    @pytest.mark.asyncio
    async def test_no_effort_set_means_key_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch)
        agent = AgentDef(name="planner", model="gpt-4o", prompt="Plan")
        await provider.execute(agent=agent, context={}, rendered_prompt="Plan")
        assert "reasoning_effort" not in captured["create_session_kwargs"]

    @pytest.mark.asyncio
    async def test_validation_error_when_model_does_not_support_effort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.config.schema import ReasoningConfig
        from conductor.exceptions import ValidationError

        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", supported=["low", "medium"])]

        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch, list_models_impl=list_models)
        agent = AgentDef(
            name="planner",
            model="gpt-4o",
            prompt="Plan",
            reasoning=ReasoningConfig(effort="xhigh"),
        )
        with pytest.raises(ValidationError, match="does not support reasoning_effort"):
            await provider.execute(agent=agent, context={}, rendered_prompt="Plan")

    @pytest.mark.asyncio
    async def test_max_effort_forwarded_when_model_supports_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#299: ``max`` is forwarded to ``create_session`` on a model that
        advertises it in ``supported_reasoning_efforts``."""
        from conductor.config.schema import ReasoningConfig

        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", supported=["low", "medium", "high", "xhigh", "max"])]

        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch, list_models_impl=list_models)
        agent = AgentDef(
            name="planner",
            model="gpt-4o",
            prompt="Plan",
            reasoning=ReasoningConfig(effort="max"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="Plan")
        assert captured["create_session_kwargs"]["reasoning_effort"] == "max"

    @pytest.mark.asyncio
    async def test_max_effort_rejected_when_model_lacks_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#299: ``max`` still errors cleanly for a model whose advertised
        ``supported_reasoning_efforts`` excludes it."""
        from conductor.config.schema import ReasoningConfig
        from conductor.exceptions import ValidationError

        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", supported=["low", "medium", "high", "xhigh"])]

        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch, list_models_impl=list_models)
        agent = AgentDef(
            name="planner",
            model="gpt-4o",
            prompt="Plan",
            reasoning=ReasoningConfig(effort="max"),
        )
        with pytest.raises(ValidationError, match="does not support reasoning_effort"):
            await provider.execute(agent=agent, context={}, rendered_prompt="Plan")

    @pytest.mark.asyncio
    async def test_validation_skipped_in_mock_handler_mode(self) -> None:
        """Mock-handler mode must skip capability validation entirely."""
        provider = CopilotProvider(mock_handler=stub_handler)
        # Even an obviously bogus effort value is accepted because the SDK
        # path is short-circuited by the mock handler.
        await provider._validate_reasoning_effort_for_model("gpt-4o", "xhigh")

    @pytest.mark.asyncio
    async def test_supported_efforts_none_allows_any_effort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the SDK reports no capability metadata, validation is permissive."""
        from conductor.config.schema import ReasoningConfig

        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", supported=None)]

        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch, list_models_impl=list_models)
        agent = AgentDef(
            name="planner",
            model="gpt-4o",
            prompt="Plan",
            reasoning=ReasoningConfig(effort="xhigh"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="Plan")
        assert captured["create_session_kwargs"]["reasoning_effort"] == "xhigh"

    @pytest.mark.asyncio
    async def test_value_error_from_sdk_parser_skips_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SDK schema-parsing failures during ``list_models()`` must not block
        execution — validation is skipped permissively and the configured
        ``reasoning_effort`` is still forwarded to ``create_session``.

        Regression for github-copilot-sdk 0.3.0: ``ModelBilling.from_dict``
        raises ``ValueError("Missing required field 'multiplier' in
        ModelBilling")`` for models like ``claude-opus-4.7-1m-internal``,
        which previously leaked through the narrow except tuple in
        ``_validate_reasoning_effort_for_model`` and surfaced as a
        ``Dialog turn failed: …`` error after exhausting the retry loop.
        """
        from conductor.config.schema import ReasoningConfig

        async def list_models() -> list[Any]:
            raise ValueError("Missing required field 'multiplier' in ModelBilling")

        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch, list_models_impl=list_models)
        agent = AgentDef(
            name="planner",
            model="claude-opus-4.7-1m-internal",
            prompt="Plan",
            reasoning=ReasoningConfig(effort="xhigh"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="Plan")
        assert captured["create_session_kwargs"]["reasoning_effort"] == "xhigh"

    @pytest.mark.asyncio
    async def test_validation_error_not_retried_in_execute_with_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: ValidationError from capability check must escape unwrapped
        from _execute_with_retry after a single attempt — no retry, no sleep,
        and not re-wrapped as ProviderError.
        """
        from conductor.config.schema import ReasoningConfig
        from conductor.exceptions import ValidationError

        list_models_calls = 0

        async def list_models() -> list[Any]:
            nonlocal list_models_calls
            list_models_calls += 1
            return [self._make_model("gpt-4o", supported=["low", "medium"])]

        sleep_calls: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("conductor.providers.copilot.asyncio.sleep", fake_sleep)

        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch, list_models_impl=list_models)
        # Force a multi-attempt retry config so a successful retry-suppression
        # is unambiguous (a ProviderError-wrapped path would loop 3 times).
        provider._retry_config = RetryConfig(max_attempts=3, base_delay=0.0, max_delay=0.0)

        agent = AgentDef(
            name="planner",
            model="gpt-4o",
            prompt="Plan",
            reasoning=ReasoningConfig(effort="high"),
        )

        with pytest.raises(ValidationError, match="does not support reasoning_effort"):
            await provider.execute(agent=agent, context={}, rendered_prompt="Plan")

        # Capability check ran exactly once — no retry of the SDK call.
        assert list_models_calls == 1
        # _retry_history is only appended on the ProviderError/Exception
        # branches; the ValidationError branch must skip it entirely.
        assert provider.get_retry_history() == []
        # No backoff sleep was scheduled.
        assert sleep_calls == []

    @pytest.mark.asyncio
    async def test_validation_error_from_dialog_turn_escapes_unwrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: ValidationError from execute_dialog_turn must propagate
        unwrapped (not re-wrapped as ProviderError by the broad except clause).
        """
        from unittest.mock import AsyncMock as _AsyncMock

        from conductor.exceptions import ValidationError

        provider = CopilotProvider(
            mock_handler=stub_handler,
            default_reasoning_effort="xhigh",
        )
        provider._mock_handler = None
        provider._started = True

        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", supported=["low", "medium"])]

        # create_session must NOT be reached when validation fails.
        create_session_called = False

        async def create_session(**kwargs: Any) -> Any:
            nonlocal create_session_called
            create_session_called = True
            raise AssertionError("create_session should not be called when validation fails")

        client = _AsyncMock()
        client.create_session = create_session
        client.list_models = list_models
        provider._client = client

        async def _noop() -> None:
            return None

        monkeypatch.setattr(provider, "_ensure_client_started", _noop)

        with pytest.raises(ValidationError) as exc_info:
            await provider.execute_dialog_turn(
                system_prompt="be helpful",
                user_message="hi",
                history=[],
                model="gpt-4o",
            )

        # Original typed error preserved (not stringified into ProviderError).
        assert "does not support reasoning_effort" in str(exc_info.value)
        assert exc_info.value.suggestion is not None
        assert not create_session_called

    @pytest.mark.asyncio
    async def test_retryable_provider_error_is_still_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard against an over-broad fix: non-validation ProviderError must
        still trigger the retry loop up to max_attempts.
        """
        call_count = 0

        def mock_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            raise ProviderError("transient backend error", status_code=500)

        sleep_calls: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("conductor.providers.copilot.asyncio.sleep", fake_sleep)

        retry_config = RetryConfig(max_attempts=3, base_delay=0.0, max_delay=0.0, jitter=0.0)
        provider = CopilotProvider(mock_handler=mock_handler, retry_config=retry_config)
        agent = AgentDef(name="planner", model="gpt-4o", prompt="Plan")

        with pytest.raises(ProviderError):
            await provider.execute(agent=agent, context={}, rendered_prompt="Plan")

        assert call_count == 3
        assert len(provider.get_retry_history()) == 3
        # Two backoff sleeps between three attempts.
        assert len(sleep_calls) == 2


class TestGetModelCapabilities:
    """Tests for CopilotProvider.get_model_capabilities (#301)."""

    @staticmethod
    def _make_model(
        model_id: str,
        supported: list[str] | None,
        *,
        default_effort: str | None = None,
        max_prompt_tokens: int | None = 128_000,
        max_output_tokens: int | None = None,
        max_context_window_tokens: int | None = None,
    ) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            id=model_id,
            supported_reasoning_efforts=supported,
            default_reasoning_effort=default_effort,
            capabilities=SimpleNamespace(
                limits=SimpleNamespace(
                    max_prompt_tokens=max_prompt_tokens,
                    max_output_tokens=max_output_tokens,
                    max_context_window_tokens=max_context_window_tokens,
                ),
            ),
        )

    @staticmethod
    def _provider_with_list_models(list_models_impl: Any) -> CopilotProvider:
        class _FakeClient:
            async def list_models(self) -> Any:
                return await list_models_impl()

        provider = CopilotProvider(mock_handler=stub_handler)
        provider._mock_handler = None
        provider._client = _FakeClient()
        provider._started = True

        async def _noop() -> None:
            return None

        provider._ensure_client_started = _noop  # type: ignore[method-assign]
        return provider

    @pytest.mark.asyncio
    async def test_full_capabilities_reported(self) -> None:
        async def list_models() -> list[Any]:
            return [
                self._make_model(
                    "gpt-5.5",
                    supported=["low", "medium", "high", "xhigh"],
                    default_effort="medium",
                    max_prompt_tokens=128_000,
                    max_output_tokens=64_000,
                    max_context_window_tokens=192_000,
                )
            ]

        provider = self._provider_with_list_models(list_models)
        caps = await provider.get_model_capabilities("gpt-5.5")
        assert caps is not None
        assert caps.supported_reasoning_efforts == ["low", "medium", "high", "xhigh"]
        assert caps.default_reasoning_effort == "medium"
        assert caps.max_prompt_tokens == 128_000
        assert caps.max_output_tokens == 64_000
        assert caps.max_context_window_tokens == 192_000

    @pytest.mark.asyncio
    async def test_no_reasoning_support_reports_none_supported(self) -> None:
        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", supported=None, max_prompt_tokens=128_000)]

        provider = self._provider_with_list_models(list_models)
        caps = await provider.get_model_capabilities("gpt-4o")
        assert caps is not None
        assert caps.supported_reasoning_efforts is None
        assert caps.default_reasoning_effort is None
        assert caps.max_prompt_tokens == 128_000

    @pytest.mark.asyncio
    async def test_unmatched_model_returns_none(self) -> None:
        async def list_models() -> list[Any]:
            return [self._make_model("gpt-4o", supported=["low"])]

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_model_capabilities("totally-different") is None

    @pytest.mark.asyncio
    async def test_list_models_failure_returns_none(self) -> None:
        async def list_models() -> list[Any]:
            raise RuntimeError("boom")

        provider = self._provider_with_list_models(list_models)
        assert await provider.get_model_capabilities("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_mock_handler_mode_returns_none(self) -> None:
        provider = CopilotProvider(mock_handler=stub_handler)
        assert await provider.get_model_capabilities("gpt-4o") is None

    @pytest.mark.asyncio
    async def test_alias_resolution_via_match_model_id(self) -> None:
        """A dated/aliased requested name resolves against the SDK's base id."""

        async def list_models() -> list[Any]:
            return [
                self._make_model(
                    "claude-3-5-sonnet", supported=["low", "medium"], default_effort="low"
                )
            ]

        provider = self._provider_with_list_models(list_models)
        caps = await provider.get_model_capabilities("claude-3-5-sonnet-20241022")
        assert caps is not None
        assert caps.supported_reasoning_efforts == ["low", "medium"]


class TestContextTier:
    """Tests for context_tier plumbing into create_session."""

    @staticmethod
    async def _build_provider(
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        *,
        default_context_tier: str | None = None,
    ) -> CopilotProvider:
        """Build a real-SDK-mode provider that captures create_session kwargs."""

        class _FakeSession:
            session_id = "session-xyz"

            async def disconnect(self) -> None:
                return None

        class _FakeClient:
            async def create_session(self, **kwargs: Any) -> _FakeSession:
                captured["create_session_kwargs"] = kwargs
                return _FakeSession()

            async def list_models(self) -> list[Any]:
                return []

        provider = CopilotProvider(
            mock_handler=stub_handler,
            default_context_tier=default_context_tier,  # type: ignore[arg-type]
        )
        provider._mock_handler = None
        provider._client = _FakeClient()
        provider._started = True

        async def _noop() -> None:
            return None

        async def _fake_send_and_wait(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(content='{"ok":true}')

        monkeypatch.setattr(provider, "_ensure_client_started", _noop)
        monkeypatch.setattr(provider, "_send_and_wait", _fake_send_and_wait)
        return provider

    @pytest.mark.asyncio
    async def test_per_agent_tier_forwarded_to_create_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch)
        agent = AgentDef(
            name="analyze",
            model="claude-opus-4.8",
            prompt="Analyze",
            context_tier="long_context",
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="Analyze")
        assert captured["create_session_kwargs"]["context_tier"] == "long_context"

    @pytest.mark.asyncio
    async def test_runtime_default_used_when_agent_has_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        provider = await self._build_provider(
            captured, monkeypatch, default_context_tier="long_context"
        )
        agent = AgentDef(name="analyze", model="claude-opus-4.8", prompt="Analyze")
        await provider.execute(agent=agent, context={}, rendered_prompt="Analyze")
        assert captured["create_session_kwargs"]["context_tier"] == "long_context"

    @pytest.mark.asyncio
    async def test_per_agent_tier_overrides_runtime_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        provider = await self._build_provider(
            captured, monkeypatch, default_context_tier="long_context"
        )
        agent = AgentDef(
            name="cheap",
            model="claude-opus-4.8",
            prompt="Cheap",
            context_tier="default",
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="Cheap")
        assert captured["create_session_kwargs"]["context_tier"] == "default"

    @pytest.mark.asyncio
    async def test_no_tier_set_means_key_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch)
        agent = AgentDef(name="analyze", model="claude-opus-4.8", prompt="Analyze")
        await provider.execute(agent=agent, context={}, rendered_prompt="Analyze")
        assert "context_tier" not in captured["create_session_kwargs"]

    @pytest.mark.asyncio
    async def test_tier_and_reasoning_compose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from conductor.config.schema import ReasoningConfig

        captured: dict[str, Any] = {}
        provider = await self._build_provider(captured, monkeypatch)
        agent = AgentDef(
            name="analyze",
            model="claude-opus-4.8",
            prompt="Analyze",
            context_tier="long_context",
            reasoning=ReasoningConfig(effort="high"),
        )
        await provider.execute(agent=agent, context={}, rendered_prompt="Analyze")
        assert captured["create_session_kwargs"]["context_tier"] == "long_context"
        assert captured["create_session_kwargs"]["reasoning_effort"] == "high"


class TestCopilotProviderResolvedModel:
    """Tests for SDKResponse.resolved_model propagation into AgentOutput.model."""

    # These are mocked tests; model availability in the live Copilot environment is not required.
    # claude-sonnet-4 is used in pricing/usage tests as the canonical model and exists in the
    # Conductor pricing table, so it satisfies both propagation and cost-calculation tests.
    _RESOLVED_MODEL_FROM_SDK = "claude-sonnet-4"
    _PRICEABLE_MODEL = "gpt-4o"
    _UNPRICEABLE_RESOLVED_MODEL = "claude-sonnet-4.5"

    class _FakeSession:
        session_id = "session-fake"

        async def disconnect(self) -> None:
            return None

    class _FakeClient:
        async def create_session(self, **kwargs: Any) -> Any:
            return TestCopilotProviderResolvedModel._FakeSession()

    @pytest.mark.asyncio
    async def test_resolved_model_from_sdk_overrides_auto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SDKResponse.resolved_model propagates into AgentOutput.model for model='auto'."""
        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))
        provider._client = self._FakeClient()
        agent = AgentDef(name="a", model="auto", prompt="p")

        async def fake_send(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(
                content='{"result":"ok"}', resolved_model=self._RESOLVED_MODEL_FROM_SDK
            )

        async def noop() -> None:
            return None

        monkeypatch.setattr(provider, "_ensure_client_started", noop)
        monkeypatch.setattr(provider, "_send_and_wait", fake_send)

        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")
        assert result.model == self._RESOLVED_MODEL_FROM_SDK

    @pytest.mark.asyncio
    async def test_resolved_model_fallback_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When resolved_model is None, AgentOutput.model falls back to agent.model."""
        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))
        provider._client = self._FakeClient()
        agent = AgentDef(name="a", model="auto", prompt="p")

        async def fake_send(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(content='{"result":"ok"}', resolved_model=None)

        async def noop() -> None:
            return None

        monkeypatch.setattr(provider, "_ensure_client_started", noop)
        monkeypatch.setattr(provider, "_send_and_wait", fake_send)

        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")
        assert result.model == "auto"

    @pytest.mark.asyncio
    async def test_resolved_model_enables_auto_model_cost_calculation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolved priceable model from execute() produces non-null cost_usd."""
        from conductor.engine.usage import UsageTracker

        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))
        provider._client = self._FakeClient()
        agent = AgentDef(name="a", model="auto", prompt="p")

        async def fake_send(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(
                content='{"result":"ok"}',
                input_tokens=1000,
                output_tokens=500,
                resolved_model=self._RESOLVED_MODEL_FROM_SDK,
            )

        async def noop() -> None:
            return None

        monkeypatch.setattr(provider, "_ensure_client_started", noop)
        monkeypatch.setattr(provider, "_send_and_wait", fake_send)

        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")
        tracker = UsageTracker()
        usage = tracker.record("agent", result, elapsed=1.0)
        assert usage.cost_usd is not None
        assert usage.cost_usd > 0

    @pytest.mark.asyncio
    async def test_auto_model_without_resolved_model_remains_unpriced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """model='auto' remains unpriceable when the SDK does not report a model."""
        from conductor.engine.usage import UsageTracker

        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))
        provider._client = self._FakeClient()
        agent = AgentDef(name="a", model="auto", prompt="p")

        async def fake_send(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(
                content='{"result":"ok"}',
                input_tokens=1000,
                output_tokens=500,
                resolved_model=None,
            )

        async def noop() -> None:
            return None

        monkeypatch.setattr(provider, "_ensure_client_started", noop)
        monkeypatch.setattr(provider, "_send_and_wait", fake_send)

        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")
        usage = UsageTracker().record("agent", result, elapsed=1.0)
        assert result.model == "auto"
        assert usage.cost_usd is None

    @pytest.mark.asyncio
    async def test_explicit_priceable_model_ignores_unpriceable_resolved_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit configured models keep their pricing name over SDK aliases."""
        from conductor.engine.usage import UsageTracker

        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))
        provider._client = self._FakeClient()
        agent = AgentDef(name="a", model=self._PRICEABLE_MODEL, prompt="p")

        async def fake_send(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(
                content='{"result":"ok"}',
                input_tokens=1000,
                output_tokens=500,
                resolved_model=self._UNPRICEABLE_RESOLVED_MODEL,
            )

        async def noop() -> None:
            return None

        monkeypatch.setattr(provider, "_ensure_client_started", noop)
        monkeypatch.setattr(provider, "_send_and_wait", fake_send)

        result = await provider.execute(agent=agent, context={}, rendered_prompt="p")
        usage = UsageTracker().record("agent", result, elapsed=1.0)
        assert result.model == self._PRICEABLE_MODEL
        assert usage.cost_usd is not None
        assert usage.cost_usd > 0

    @pytest.mark.asyncio
    async def test_followup_preserves_explicit_model_over_unpriceable_resolved_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Follow-up turns keep explicit pricing names over SDK aliases."""
        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))

        async def fake_send(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(
                content='{"result":"ok"}',
                resolved_model=self._UNPRICEABLE_RESOLVED_MODEL,
            )

        monkeypatch.setattr(provider, "_send_and_wait", fake_send)

        result = await provider.send_followup(
            self._FakeSession(),
            guidance="continue",
            agent_model=self._PRICEABLE_MODEL,
        )
        assert result.model == self._PRICEABLE_MODEL

    @pytest.mark.asyncio
    async def test_followup_uses_resolved_model_for_auto_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Follow-up turns use SDK resolved model for auto-routed agents."""
        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))

        async def fake_send(*args: Any, **kwargs: Any) -> SDKResponse:
            return SDKResponse(
                content='{"result":"ok"}',
                resolved_model=self._RESOLVED_MODEL_FROM_SDK,
            )

        monkeypatch.setattr(provider, "_send_and_wait", fake_send)

        result = await provider.send_followup(
            self._FakeSession(),
            guidance="continue",
            agent_model="auto",
        )
        assert result.model == self._RESOLVED_MODEL_FROM_SDK

    @pytest.mark.asyncio
    async def test_send_and_wait_captures_model_from_usage_event(self) -> None:
        """_send_and_wait extracts event.data.model from assistant.usage into resolved_model."""
        from unittest.mock import Mock as _Mock

        provider = CopilotProvider(retry_config=RetryConfig(max_attempts=1))
        captured_cb: list[Any] = []

        # Build assistant.usage event with explicit token counts and model name.
        usage_ev = _Mock()
        usage_ev.type.value = "assistant.usage"
        usage_ev.data.input_tokens = 100
        usage_ev.data.output_tokens = 50
        usage_ev.data.cache_read_tokens = None
        usage_ev.data.cache_write_tokens = None
        usage_ev.data.model = self._RESOLVED_MODEL_FROM_SDK

        # session.idle tells _send_and_wait the turn is complete.
        idle_ev = _Mock()
        idle_ev.type.value = "session.idle"

        def on_event(callback: Any) -> None:
            captured_cb.append(callback)

        session = _Mock()
        session.on = on_event

        async def fake_send(prompt: str) -> None:
            assert captured_cb, "Expected _send_and_wait to register a session event callback"
            callback = captured_cb[0]
            for ev in (usage_ev, idle_ev):
                callback(ev)

        session.send = fake_send

        result = await provider._send_and_wait(
            session=session,
            prompt="hello",
            verbose_enabled=False,
            full_enabled=False,
        )
        assert result.resolved_model == self._RESOLVED_MODEL_FROM_SDK
