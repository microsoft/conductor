"""Unit tests for the ClaudeAgentSdkProvider implementation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import Mock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError
from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider


def _assistant(
    content: list,
    model: str = "claude-sonnet-4-6",
    usage: dict | None = None,
) -> AssistantMessage:
    return AssistantMessage(content=content, model=model, usage=usage)


def _result(
    result: str | None = None,
    structured_output: object | None = None,
    usage: dict | None = None,
    is_error: bool = False,
) -> ResultMessage:
    return ResultMessage(
        subtype="result",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=is_error,
        num_turns=1,
        session_id="test-session",
        usage=usage,
        result=result,
        structured_output=structured_output,
    )


class TestClaudeAgentSdkProviderInitialization:
    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", False)
    def test_init_raises_when_sdk_not_installed(self) -> None:
        with pytest.raises(ProviderError, match="Claude Agent SDK not installed"):
            ClaudeAgentSdkProvider()

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.query", lambda **kwargs: None)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    def test_init_with_defaults(self) -> None:
        provider = ClaudeAgentSdkProvider()
        assert provider._default_model == "claude-sonnet-4-6"
        assert provider._default_max_turns == 50

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.query", lambda **kwargs: None)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    def test_init_with_custom_params(self) -> None:
        provider = ClaudeAgentSdkProvider(
            model="claude-opus-4-20250514",
            max_turns=10,
        )
        assert provider._default_model == "claude-opus-4-20250514"
        assert provider._default_max_turns == 10


class TestValidateConnection:
    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.query", lambda **kwargs: None)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_validate_connection_returns_true(self) -> None:
        provider = ClaudeAgentSdkProvider()
        assert await provider.validate_connection() is True


class TestExecute:
    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_execute_text_only(self) -> None:
        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="The answer is 42")],
                usage={"input_tokens": 100, "output_tokens": 50},
            )
            yield _result(
                result="The answer is 42",
                usage={"input_tokens": 0, "output_tokens": 0},
            )

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test_agent", prompt="What is the answer?")
            output = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="What is the answer?",
            )

        assert output.content == {"response": "The answer is 42"}
        assert output.input_tokens == 100
        assert output.output_tokens == 50
        assert output.partial is False

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_execute_structured_output(self) -> None:
        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="thinking...")],
                usage={"input_tokens": 100, "output_tokens": 50},
            )
            yield _result(
                structured_output={"answer": "42", "confidence": 0.95},
                usage={"input_tokens": 0, "output_tokens": 0},
            )

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(
                name="test_agent",
                prompt="What is the answer?",
                output={
                    "answer": OutputField(type="string"),
                    "confidence": OutputField(type="number"),
                },
            )
            output = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="What is the answer?",
            )

        assert output.content == {"answer": "42", "confidence": 0.95}

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_execute_emits_event_callbacks(self) -> None:
        async def fake_query(**kwargs):
            yield _assistant(
                content=[
                    TextBlock(text="Hello"),
                    ToolUseBlock(id="t1", name="search", input={"q": "test"}),
                    ThinkingBlock(thinking="Hmm", signature="sig"),
                ],
                usage={"input_tokens": 50, "output_tokens": 25},
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="search results")])
            yield _result()

        events: list[tuple[str, dict]] = []

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="hi",
                event_callback=lambda t, d: events.append((t, d)),
            )

        event_types = [e[0] for e in events]
        assert "agent_turn_start" in event_types
        assert "agent_message" in event_types
        assert "agent_tool_start" in event_types
        assert "agent_tool_complete" in event_types
        assert "agent_reasoning" in event_types

        tool_complete = next(e for e in events if e[0] == "agent_tool_complete")
        assert tool_complete[1]["tool_name"] == "search"
        assert "search results" in tool_complete[1]["result"]

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_execute_interrupt_signal(self) -> None:
        interrupt = asyncio.Event()
        interrupt.set()

        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="partial")],
                usage={"input_tokens": 10, "output_tokens": 5},
            )

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            output = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="hi",
                interrupt_signal=interrupt,
            )

        assert output.partial is True

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_execute_error_result(self) -> None:
        async def fake_query(**kwargs):
            yield _result(is_error=True, result="API key invalid")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            with pytest.raises(ProviderError, match="API key invalid"):
                await provider.execute(
                    agent=agent,
                    context={},
                    rendered_prompt="hi",
                )

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_execute_wraps_unexpected_errors(self) -> None:
        async def failing_query(**kwargs):
            raise RuntimeError("connection refused")
            yield  # noqa: F401 - make it an async generator

        with patch("conductor.providers.claude_agent_sdk.query", failing_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            with pytest.raises(ProviderError, match="connection refused"):
                await provider.execute(
                    agent=agent,
                    context={},
                    rendered_prompt="hi",
                )

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_execute_token_accumulation(self) -> None:
        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="part1")],
                usage={"input_tokens": 100, "output_tokens": 50},
            )
            yield _assistant(
                content=[TextBlock(text="part2")],
                usage={"input_tokens": 80, "output_tokens": 40},
            )
            yield _result(usage={"input_tokens": 10, "output_tokens": 5})

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            output = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="hi",
            )

        assert output.input_tokens == 190
        assert output.output_tokens == 95
        assert output.tokens_used == 285


class TestOutputFormatConstruction:
    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_output_format_built_from_output_fields(self) -> None:
        original_options = Mock()

        async def capture_query(**kwargs):
            yield _result(structured_output={"name": "test", "score": 5})

        with (
            patch("conductor.providers.claude_agent_sdk.query", capture_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", original_options),
        ):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(
                name="test",
                prompt="hi",
                output={
                    "name": OutputField(type="string", description="The name"),
                    "score": OutputField(type="number"),
                },
            )
            await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        call_kwargs = original_options.call_args[1]
        output_format = call_kwargs["output_format"]
        assert output_format["type"] == "json_schema"
        schema = output_format["schema"]
        assert schema["type"] == "object"
        assert "name" in schema["properties"]
        assert "score" in schema["properties"]
        assert schema["properties"]["name"]["description"] == "The name"
        assert set(schema["required"]) == {"name", "score"}


class TestSchemaBuilding:
    def test_nested_object_schema(self) -> None:
        from conductor.providers.claude_agent_sdk import _build_output_format

        output = {
            "person": OutputField(
                type="object",
                properties={
                    "name": OutputField(type="string", description="Full name"),
                    "age": OutputField(type="number"),
                },
            ),
        }
        result = _build_output_format(output)
        person = result["schema"]["properties"]["person"]
        assert person["type"] == "object"
        assert "name" in person["properties"]
        assert person["properties"]["name"]["description"] == "Full name"
        assert person["required"] == ["name", "age"]

    def test_array_schema(self) -> None:
        from conductor.providers.claude_agent_sdk import _build_output_format

        output = {
            "tags": OutputField(
                type="array",
                items=OutputField(type="string"),
            ),
        }
        result = _build_output_format(output)
        tags = result["schema"]["properties"]["tags"]
        assert tags["type"] == "array"
        assert tags["items"]["type"] == "string"

    def test_array_of_objects_schema(self) -> None:
        from conductor.providers.claude_agent_sdk import _build_output_format

        output = {
            "items": OutputField(
                type="array",
                items=OutputField(
                    type="object",
                    properties={
                        "id": OutputField(type="number"),
                        "label": OutputField(type="string"),
                    },
                ),
            ),
        }
        result = _build_output_format(output)
        items_schema = result["schema"]["properties"]["items"]["items"]
        assert items_schema["type"] == "object"
        assert set(items_schema["required"]) == {"id", "label"}

    def test_depth_limit_raises(self) -> None:
        from conductor.providers.claude_agent_sdk import _build_field_schema

        field_def = OutputField(type="string")
        for _ in range(12):
            field_def = OutputField(type="object", properties={"nested": field_def})

        with pytest.raises(ProviderError, match="nesting exceeds 10"):
            _build_field_schema(field_def)


class TestBuildOutput:
    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_string_structured_output_parsed_as_json(self) -> None:
        async def fake_query(**kwargs):
            yield _result(structured_output='{"answer": "yes"}')

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(
                name="test",
                prompt="hi",
                output={"answer": OutputField(type="string")},
            )
            output = await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        assert output.content == {"answer": "yes"}

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_non_dict_structured_output_wrapped(self) -> None:
        async def fake_query(**kwargs):
            yield _result(structured_output=42)

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            output = await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        assert output.content == {"response": "42"}

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_output_schema_with_non_json_text_falls_back(self) -> None:
        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="not valid json")],
                usage={"input_tokens": 10, "output_tokens": 5},
            )
            yield _result()

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(
                name="test",
                prompt="hi",
                output={"answer": OutputField(type="string")},
            )
            output = await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        assert output.content == {"response": "not valid json"}


class TestMessageDispatch:
    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_unknown_message_types_ignored(self) -> None:
        @dataclass
        class FakeSystemMessage:
            subtype: str = "init"
            data: dict = field(default_factory=dict)

        @dataclass
        class FakeStreamEvent:
            event: str = "keepalive"

        async def fake_query(**kwargs):
            yield FakeSystemMessage()
            yield FakeStreamEvent()
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            output = await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        assert output.content == {"response": "done"}

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_tool_result_with_no_matching_pending_tool(self) -> None:
        events: list[tuple[str, dict]] = []

        async def fake_query(**kwargs):
            yield UserMessage(
                content=[ToolResultBlock(tool_use_id="orphan_id", content="orphan result")]
            )
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="hi",
                event_callback=lambda t, d: events.append((t, d)),
            )

        tool_complete = [e for e in events if e[0] == "agent_tool_complete"]
        assert len(tool_complete) == 1
        assert tool_complete[0][1]["tool_name"] == "unknown"

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_system_prompt_passed_to_options(self) -> None:
        options_mock = Mock()

        async def fake_query(**kwargs):
            yield _result(result="done")

        with (
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(
                name="test",
                prompt="hi",
                system_prompt="You are a helpful assistant",
            )
            await provider.execute(agent=agent, context={}, rendered_prompt="hi")

        call_kwargs = options_mock.call_args[1]
        assert call_kwargs["system_prompt"] == "You are a helpful assistant"
