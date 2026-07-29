"""Schema-shape recovery tests for the Claude provider (issue #343).

Claude had the same gap as Copilot: ``_execute_with_parse_recovery`` returned
as soon as *any* content could be extracted, without checking that the content
matched the declared schema.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField, RetryPolicy
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers.claude import ClaudeProvider


def create_tool_use_block(input_dict: dict) -> Mock:
    """Create an emit_output tool_use block mock."""
    block = Mock()
    block.type = "tool_use"
    block.id = "tool_123"
    block.name = "emit_output"
    block.input = input_dict
    return block


def create_mcp_tool_use_block(name: str = "search") -> Mock:
    """Create a non-emit_output tool_use block (an MCP tool call)."""
    block = Mock()
    block.type = "tool_use"
    block.id = "tool_mcp"
    block.name = name
    block.input = {"query": "x"}
    return block


def create_text_block(text: str) -> Mock:
    """Create a text block mock."""
    block = Mock()
    block.type = "text"
    block.text = text
    return block


def create_response(content_blocks: list, msg_id: str = "msg_123") -> Mock:
    """Create a Claude API response mock."""
    response = Mock()
    response.id = msg_id
    response.content = content_blocks
    response.model = "claude-3-5-sonnet-latest"
    response.stop_reason = "end_turn"
    response.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
    response.type = "message"
    response.role = "assistant"
    return response


def _mock_client(responses: list[Mock]) -> Mock:
    client = Mock()
    client.messages = Mock()
    client.messages.create = AsyncMock(side_effect=responses)
    client.models = Mock()
    client.models.list = AsyncMock(return_value=Mock(data=[]))
    client.close = AsyncMock()
    return client


def _agent() -> AgentDef:
    return AgentDef(
        name="reviewer",
        model="claude-3-5-sonnet-latest",
        prompt="review it",
        output={"decision": OutputField(type="string")},
    )


class TestClaudeSchemaShapeRecovery:
    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_wrong_shape_via_tool_use_is_recovered(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """A wrong-typed field arriving via emit_output triggers recovery."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = create_response(
            [create_tool_use_block({"decision": {"verdict": "APPROVE", "why": "ok"}})],
            "msg_bad",
        )
        good = create_response([create_tool_use_block({"decision": "APPROVE"})], "msg_good")

        client = _mock_client([bad, good])
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        result = await provider.execute(_agent(), {"workflow": {"input": {}}}, "review it")

        assert result.content["decision"] == "APPROVE"
        assert client.messages.create.call_count == 2
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_wrong_shape_via_json_fallback_is_recovered(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """The text JSON-fallback path is validated too."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = create_response(
            [create_text_block('{"decision": {"verdict": "APPROVE", "why": "ok"}}')],
            "msg_bad",
        )
        good = create_response([create_tool_use_block({"decision": "APPROVE"})], "msg_good")

        client = _mock_client([bad, good])
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        result = await provider.execute(_agent(), {"workflow": {"input": {}}}, "review it")

        assert result.content["decision"] == "APPROVE"
        assert client.messages.create.call_count == 2
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_recovery_message_uses_schema_wording(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = create_response([create_tool_use_block({"decision": {"a": 1, "b": 2}})], "msg_bad")
        good = create_response([create_tool_use_block({"decision": "APPROVE"})], "msg_good")

        client = _mock_client([bad, good])
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        await provider.execute(_agent(), {"workflow": {"input": {}}}, "review it")

        recovery_call = client.messages.create.call_args_list[1]
        messages = recovery_call.kwargs["messages"]
        user_turn = messages[-1]["content"]
        assert "did not match the required output schema" in user_turn
        assert "did not contain valid JSON" not in user_turn
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_tool_use_failure_replays_as_text_not_tool_use(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """A bare tool_use block without a tool_result would break the API contract."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = create_response([create_tool_use_block({"decision": {"a": 1, "b": 2}})], "msg_bad")
        good = create_response([create_tool_use_block({"decision": "APPROVE"})], "msg_good")

        client = _mock_client([bad, good])
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        await provider.execute(_agent(), {"workflow": {"input": {}}}, "review it")

        recovery_call = client.messages.create.call_args_list[1]
        messages = recovery_call.kwargs["messages"]
        assistant_turn = messages[-2]
        assert assistant_turn["role"] == "assistant"
        # Plain string content, and it carries the rejected answer.
        assert isinstance(assistant_turn["content"], str)
        assert "decision" in assistant_turn["content"]
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_mcp_tool_calls_are_not_validated(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """An MCP tool call is not a final answer and must skip validation.

        Asserted at the decision point rather than end-to-end: driving a real
        MCP round-trip would need a configured MCP manager, which is unrelated
        to the classification being tested here.
        """
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_class.return_value = _mock_client([])
        provider = ClaudeProvider()

        mcp_response = create_response([create_mcp_tool_use_block()], "msg_mcp")
        schema = {"decision": OutputField(type="string")}

        outcome, assistant_text, error = provider._evaluate_structured_response(
            mcp_response, schema
        )

        assert outcome == "mcp_tools"
        assert error is None
        assert assistant_text is None
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_emit_output_wins_over_concurrent_mcp_call(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """A response carrying both is treated as a final answer, as before."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_class.return_value = _mock_client([])
        provider = ClaudeProvider()

        both = create_response(
            [create_tool_use_block({"decision": "APPROVE"}), create_mcp_tool_use_block()],
            "msg_both",
        )
        schema = {"decision": OutputField(type="string")}

        outcome, _, error = provider._evaluate_structured_response(both, schema)

        assert outcome == "success"
        assert error is None
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_exhausted_budget_reraises_validation_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = [
            create_response([create_tool_use_block({"decision": {"a": 1, "b": 2}})], f"m{i}")
            for i in range(6)
        ]

        client = _mock_client(bad)
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        with pytest.raises(ValidationError) as exc_info:
            await provider.execute(_agent(), {"workflow": {"input": {}}}, "review it")

        message = str(exc_info.value)
        assert "decision" in message
        assert "expected string, got dict" in message
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_wrapper_shape_resolves_without_a_round_trip(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        wrapped = create_response(
            [create_tool_use_block({"decision": {"decision": "APPROVE", "why": "ok"}})],
            "msg_wrapped",
        )

        client = _mock_client([wrapped])
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        result = await provider.execute(_agent(), {"workflow": {"input": {}}}, "review it")

        assert result.content["decision"] == "APPROVE"
        assert client.messages.create.call_count == 1
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_bare_scalar_json_fallback_is_recovered(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """A bare JSON scalar used to raise TypeError out of the provider,
        surfacing as 'check API key' with zero recovery attempts."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = create_response([create_text_block("42")], "msg_bad")
        good = create_response([create_tool_use_block({"decision": "APPROVE"})], "msg_good")

        client = _mock_client([bad, good])
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        result = await provider.execute(_agent(), {"workflow": {"input": {}}}, "review it")

        assert result.content["decision"] == "APPROVE"
        assert client.messages.create.call_count == 2
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_emits_parse_recovery_event(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = create_response([create_tool_use_block({"decision": {"a": 1, "b": 2}})], "msg_bad")
        good = create_response([create_tool_use_block({"decision": "APPROVE"})], "msg_good")

        client = _mock_client([bad, good])
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        events: list[tuple[str, dict[str, Any]]] = []
        await provider.execute(
            _agent(),
            {"workflow": {"input": {}}},
            "review it",
            event_callback=lambda name, data: events.append((name, data)),
        )

        recovery = [d for name, d in events if name == "agent_parse_recovery"]
        assert len(recovery) == 1
        assert recovery[0]["reason"] == "schema"
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_syntax_failure_event_is_labelled_syntax(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = create_response([create_text_block("not json at all {{{")], "msg_bad")
        good = create_response([create_tool_use_block({"decision": "APPROVE"})], "msg_good")

        client = _mock_client([bad, good])
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        events: list[tuple[str, dict[str, Any]]] = []
        await provider.execute(
            _agent(),
            {"workflow": {"input": {}}},
            "review it",
            event_callback=lambda name, data: events.append((name, data)),
        )

        recovery = [d for name, d in events if name == "agent_parse_recovery"]
        assert len(recovery) == 1
        assert recovery[0]["reason"] == "syntax"
        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_syntax_exhaustion_raises_provider_error(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Syntax failures keep the ProviderError contract."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = [create_response([create_text_block("not json {{{")], f"m{i}") for i in range(6)]

        client = _mock_client(bad)
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        with pytest.raises(ProviderError, match="Failed to extract valid JSON"):
            await provider.execute(_agent(), {"workflow": {"input": {}}}, "review it")

        await provider.close()

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_zero_budget_makes_exactly_one_attempt(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """0 is a legal configured value meaning "fail fast", not "unset"."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_anthropic_module.APIStatusError = None

        bad = [
            create_response([create_tool_use_block({"decision": {"a": 1, "b": 2}})], f"m{i}")
            for i in range(4)
        ]

        client = _mock_client(bad)
        mock_anthropic_class.return_value = client
        provider = ClaudeProvider()

        agent = AgentDef(
            name="reviewer",
            model="claude-3-5-sonnet-latest",
            prompt="review it",
            output={"decision": OutputField(type="string")},
            retry=RetryPolicy(max_parse_recovery_attempts=0),
        )

        with pytest.raises(ValidationError):
            await provider.execute(agent, {"workflow": {"input": {}}}, "review it")

        assert client.messages.create.call_count == 1
        await provider.close()
