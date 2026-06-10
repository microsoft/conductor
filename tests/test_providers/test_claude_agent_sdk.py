"""Unit tests for the ClaudeAgentSdkProvider implementation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import Mock, patch

import pytest

pytest.importorskip(
    "claude_agent_sdk",
    reason="claude-agent-sdk extra not installed (pip install conductor[claude-agent-sdk])",
)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from conductor.config.schema import AgentDef, OutputField  # noqa: E402
from conductor.exceptions import ProviderError  # noqa: E402
from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider  # noqa: E402


def _assistant(
    content: list,
    model: str = "claude-sonnet-4-5",
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
        assert provider._default_model == "claude-sonnet-4-5"
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
    async def test_validate_connection_returns_true_when_bundled_cli_present(self) -> None:
        """The SDK ships a bundled CLI under _bundled/claude — detection should succeed."""
        provider = ClaudeAgentSdkProvider()
        # The installed claude-agent-sdk extra includes the bundled binary,
        # so this should return True in any env where the test runs.
        assert await provider.validate_connection() is True

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", False)
    async def test_validate_connection_returns_false_when_sdk_missing(self) -> None:
        """If the SDK isn't importable, validate_connection short-circuits to False."""
        # __init__ would normally raise on missing SDK, so construct a stub
        # via __new__ and call validate_connection directly.
        provider = object.__new__(ClaudeAgentSdkProvider)
        assert await provider.validate_connection() is False

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.query", lambda **kwargs: None)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_validate_connection_falls_back_to_path_lookup(self) -> None:
        """When no bundled binary exists, shutil.which('claude') is consulted."""
        import pathlib

        provider = ClaudeAgentSdkProvider()
        with (
            patch.object(pathlib.Path, "exists", return_value=False),
            patch("shutil.which", return_value="/usr/local/bin/claude") as which_mock,
        ):
            assert await provider.validate_connection() is True
        which_mock.assert_called_with("claude")

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.query", lambda **kwargs: None)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_validate_connection_returns_false_when_cli_missing(self) -> None:
        """Bundled missing + not on PATH + no fallback location → False."""
        import pathlib

        provider = ClaudeAgentSdkProvider()
        with (
            patch.object(pathlib.Path, "exists", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            assert await provider.validate_connection() is False


class TestExecute:
    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_execute_text_only(self) -> None:
        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="The answer is 42")],
                usage={"input_tokens": 100, "output_tokens": 50},
            )
            # ResultMessage.usage is the CUMULATIVE session total per SDK docs.
            yield _result(
                result="The answer is 42",
                usage={"input_tokens": 100, "output_tokens": 50},
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
            yield  # unreachable; the bare `yield` makes this an async generator

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
        """ResultMessage.usage is cumulative — provider must report it as-is (no double-count)."""

        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="part1")],
                usage={"input_tokens": 100, "output_tokens": 50},
            )
            yield _assistant(
                content=[TextBlock(text="part2")],
                usage={"input_tokens": 180, "output_tokens": 90},
            )
            # Cumulative session total — NOT the delta from the last message.
            yield _result(usage={"input_tokens": 180, "output_tokens": 90})

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            output = await provider.execute(
                agent=agent,
                context={},
                rendered_prompt="hi",
            )

        assert output.input_tokens == 180
        assert output.output_tokens == 90
        assert output.tokens_used == 270


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
    async def test_output_schema_with_non_json_text_raises(self) -> None:
        """When `output:` schema is declared and content doesn't parse, raise ValidationError.

        Regression test for #241 (A11): previously the provider silently
        wrapped non-JSON text as ``{"response": text}``, violating the
        declared schema contract and causing downstream routes/templates
        to see undefined fields.
        """
        from conductor.exceptions import ValidationError

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
            with pytest.raises(ValidationError, match="declared an output schema"):
                await provider.execute(agent=agent, context={}, rendered_prompt="hi")


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


class TestToolResolution:
    """Coverage for the per-agent ``tools:`` allowlist security boundary (#241 / A1)."""

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    async def test_tools_none_grants_full_preset(self) -> None:
        """``tools is None`` (no allowlist declared) keeps the claude_code preset."""
        options_mock = Mock()

        async def fake_query(**kwargs):
            yield _result(result="done")

        with (
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi")
            await provider.execute(agent=agent, context={}, rendered_prompt="hi", tools=None)

        call_kwargs = options_mock.call_args[1]
        assert call_kwargs["tools"] == {"type": "preset", "preset": "claude_code"}
        assert call_kwargs["permission_mode"] == "bypassPermissions"

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    async def test_empty_tools_list_disables_tools(self) -> None:
        """Explicit ``tools: []`` disables ALL tools and drops the permission bypass.

        Regression test for #241 (A1): previously the empty list was silently
        ignored and the agent got the full ``claude_code`` preset
        (filesystem/bash/web) — a security regression.

        The agent here declares ``tools: []`` explicitly (``agent.tools == []``),
        which is what distinguishes it from an omitted ``tools:`` (the latter
        gets the preset — see :class:`TestOmittedToolsDefaultPreset`).
        """
        options_mock = Mock()

        async def fake_query(**kwargs):
            yield _result(result="done")

        with (
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="test", prompt="hi", tools=[])
            await provider.execute(agent=agent, context={}, rendered_prompt="hi", tools=[])

        call_kwargs = options_mock.call_args[1]
        assert call_kwargs["tools"] == []
        assert call_kwargs["permission_mode"] is None

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_non_empty_tools_list_raises(self) -> None:
        """Non-empty workflow tool allowlists are refused loudly.

        Conductor workflow tool names do not translate to Claude CLI tool
        IDs. Forwarding them would either silently grant the wrong native
        tools or silently drop the allowlist — both unsafe. Refuse loudly
        until proper name translation is implemented.
        """

        async def fake_query(**kwargs):
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="my_agent", prompt="hi")
            with pytest.raises(ProviderError, match="does not support workflow tool allowlists"):
                await provider.execute(
                    agent=agent,
                    context={},
                    rendered_prompt="hi",
                    tools=["search", "read_file"],
                )


class TestOmittedToolsDefaultPreset:
    """An agent that omits ``tools:`` must receive the ``claude_code`` preset.

    Regression test for the executor↔provider contract bug: the executor's
    ``resolve_agent_tools(agent.tools, workflow_tools)`` returns
    ``workflow_tools.copy()`` (``[]`` when the workflow declares no
    ``runtime`` MCP tools) for an omitted ``tools:``, so the provider is
    ALWAYS handed a concrete list and never ``None``. Before the fix, the
    provider could not tell "omitted (defaults to all)" from explicit
    ``tools: []`` (both arrive as ``[]``) and granted ZERO tools to an agent
    that simply forgot to declare ``tools:`` — e.g. a "read a file and
    answer" agent came up with no filesystem tools and failed.

    The provider distinguishes the two cases by inspecting the raw
    ``agent.tools`` field, which preserves the omitted (``None``) vs.
    explicit-empty (``[]``) distinction the executor erases.
    """

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    async def test_omitted_tools_with_empty_executor_list_grants_preset(self) -> None:
        """The real bug: executor passes ``tools=[]`` for an omitted ``tools:``.

        ``AgentDef`` defaults ``tools`` to ``None`` (omitted), and the
        executor turns that into ``[]`` before calling the provider. The
        provider must still grant the ``claude_code`` preset, not no tools.
        """
        options_mock = Mock()

        async def fake_query(**kwargs):
            yield _result(result="done")

        with (
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider()
            # agent.tools is None (omitted), but the executor erases that to []
            # before calling the provider — exactly what AgentExecutor does.
            agent = AgentDef(name="reader", prompt="read a file and answer")
            assert agent.tools is None
            await provider.execute(agent=agent, context={}, rendered_prompt="hi", tools=[])

        call_kwargs = options_mock.call_args[1]
        assert call_kwargs["tools"] == {"type": "preset", "preset": "claude_code"}, (
            "An agent that omits `tools:` must receive the claude_code preset "
            "even though the executor hands the provider an empty list."
        )
        assert call_kwargs["permission_mode"] == "bypassPermissions"

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    async def test_explicit_empty_tools_still_disables_tools(self) -> None:
        """An agent that explicitly declares ``tools: []`` still gets no tools.

        The executor passes ``[]`` here too, but ``agent.tools == []`` (not
        ``None``) records the explicit opt-out, so the provider disables all
        tools and drops the permission bypass.
        """
        options_mock = Mock()

        async def fake_query(**kwargs):
            yield _result(result="done")

        with (
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="no_tools", prompt="hi", tools=[])
            assert agent.tools == []
            await provider.execute(agent=agent, context={}, rendered_prompt="hi", tools=[])

        call_kwargs = options_mock.call_args[1]
        assert call_kwargs["tools"] == []
        assert call_kwargs["permission_mode"] is None

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_explicit_non_empty_tools_still_raises(self) -> None:
        """An explicit non-empty per-agent allowlist is still refused loudly."""

        async def fake_query(**kwargs):
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(name="my_agent", prompt="hi", tools=["search", "read_file"])
            with pytest.raises(ProviderError, match="does not support workflow tool allowlists"):
                await provider.execute(
                    agent=agent,
                    context={},
                    rendered_prompt="hi",
                    tools=["search", "read_file"],
                )

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    async def test_executor_to_provider_end_to_end_grants_preset(self) -> None:
        """End-to-end through AgentExecutor: an omitted ``tools:`` reaches the
        provider as the ``claude_code`` preset, with NO workflow tools declared.

        This pins the full call chain that the original bug broke:
        ``AgentExecutor.execute`` → ``resolve_agent_tools(None, [])`` → ``[]``
        → ``provider.execute(tools=[])`` → preset.
        """
        from conductor.executor.agent import AgentExecutor

        options_mock = Mock()
        captured: dict = {}

        async def fake_query(**kwargs):
            yield _result(structured_output={"answer": "from the file"})

        with (
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider()
            # No workflow-level tools — resolve_agent_tools returns [].
            executor = AgentExecutor(provider, workflow_tools=[])
            agent = AgentDef(
                name="reader",
                prompt="Read README.md and answer.",
                output={"answer": OutputField(type="string")},
            )
            assert agent.tools is None
            await executor.execute(agent=agent, context={})
            captured.update(options_mock.call_args[1])

        assert captured["tools"] == {"type": "preset", "preset": "claude_code"}
        assert captured["permission_mode"] == "bypassPermissions"


class TestAgentTurnStartOrdering:
    """Provider parity: agent_turn_start event ordering (#241 / A3).

    The dashboard reads ``agent_turn_start`` events to drive the per-iteration
    spinner and JSONL iteration boundaries. The contract is:

    * ``{"turn": "awaiting_model"}`` — fires IMMEDIATELY BEFORE each API call.
    * ``{"turn": N}`` — fires at the START of iteration N (before its content).

    Previously this provider fired ``awaiting_model`` AFTER the response arrived
    and ``{"turn": N}`` at iteration END, breaking both the spinner and the
    JSONL boundary contract.
    """

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_initial_awaiting_model_fires_before_first_response(self) -> None:
        events: list[tuple[str, object]] = []

        async def fake_query(**kwargs):
            yield _assistant(content=[TextBlock(text="hi")])
            yield _result(result="hi")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            await provider.execute(
                agent=AgentDef(name="test", prompt="hi"),
                context={},
                rendered_prompt="hi",
                event_callback=lambda t, d: events.append(
                    (t, d.get("turn") if isinstance(d, dict) else None)
                ),
            )

        turn_events = [(t, v) for (t, v) in events if t == "agent_turn_start"]
        assert turn_events[0] == ("agent_turn_start", "awaiting_model"), (
            "First agent_turn_start must be 'awaiting_model' (fires before the "
            "SDK's first API call), but got: " + str(turn_events[0])
        )

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_turn_marker_fires_before_message_content(self) -> None:
        """{"turn": N} must precede the iteration's agent_message events."""
        events: list[tuple[str, object]] = []

        async def fake_query(**kwargs):
            yield _assistant(content=[TextBlock(text="content for turn 1")])
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            await provider.execute(
                agent=AgentDef(name="test", prompt="hi"),
                context={},
                rendered_prompt="hi",
                event_callback=lambda t, d: events.append(
                    (t, d.get("turn") if t == "agent_turn_start" else d.get("content"))
                ),
            )

        types = [e[0] for e in events]
        turn_1_idx = next(i for i, e in enumerate(events) if e == ("agent_turn_start", 1))
        message_idx = types.index("agent_message")
        assert turn_1_idx < message_idx, (
            f"{{'turn': 1}} (idx={turn_1_idx}) must fire BEFORE agent_message "
            f"(idx={message_idx}). Event sequence: {events}"
        )

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_awaiting_model_fires_between_tool_use_and_next_turn(self) -> None:
        """After a tool_use response, awaiting_model must fire before the next turn marker."""
        events: list[tuple[str, object]] = []

        async def fake_query(**kwargs):
            yield _assistant(
                content=[
                    TextBlock(text="calling tool"),
                    ToolUseBlock(id="t1", name="search", input={"q": "hi"}),
                ],
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="result")])
            yield _assistant(content=[TextBlock(text="final answer")])
            yield _result(result="final answer")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            await provider.execute(
                agent=AgentDef(name="test", prompt="hi"),
                context={},
                rendered_prompt="hi",
                event_callback=lambda t, d: events.append(
                    (t, d.get("turn") if t == "agent_turn_start" else None)
                ),
            )

        turn_events = [(t, v) for (t, v) in events if t == "agent_turn_start"]
        # Expected sequence:
        #   awaiting_model (initial), 1 (first asst), awaiting_model (after tool_use),
        #   2 (second asst)
        values = [v for (_, v) in turn_events]
        assert values == [
            "awaiting_model",
            1,
            "awaiting_model",
            2,
        ], f"unexpected agent_turn_start sequence: {values}"


class TestTokenAccounting:
    """Provider parity: tokens are reported once, not double-counted (#241 / A4)."""

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_tokens_not_double_counted(self) -> None:
        """ResultMessage.usage is the cumulative total; per-message must NOT be added on top.

        Regression test for #241 (A4): previously the provider summed every
        AssistantMessage.usage AND added ResultMessage.usage on top, reporting
        roughly 2x the actual token count and corrupting cost/budget math.
        """

        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="t1")],
                usage={"input_tokens": 1000, "output_tokens": 500},
            )
            yield _assistant(
                content=[TextBlock(text="t2")],
                usage={"input_tokens": 1500, "output_tokens": 750},
            )
            # SDK reports the cumulative session total here, NOT a delta.
            yield _result(
                result="done",
                usage={"input_tokens": 1500, "output_tokens": 750},
            )

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            output = await provider.execute(
                agent=AgentDef(name="test", prompt="hi"),
                context={},
                rendered_prompt="hi",
            )

        # The right answer is the cumulative session total (1500 in, 750 out).
        # The buggy behavior would have reported 1000+1500+1500=4000 in,
        # 500+750+750=2000 out (a 2.67x overcount).
        assert output.input_tokens == 1500
        assert output.output_tokens == 750
        assert output.tokens_used == 2250

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_partial_output_falls_back_to_per_message_sum(self) -> None:
        """Interrupt mid-stream uses the per-message running sum (no ResultMessage yet)."""
        interrupt = asyncio.Event()
        messages_seen = 0

        async def fake_query(**kwargs):
            nonlocal messages_seen
            yield _assistant(
                content=[TextBlock(text="part1")],
                usage={"input_tokens": 200, "output_tokens": 100},
            )
            messages_seen += 1
            # Fire interrupt AFTER first AssistantMessage processed.
            interrupt.set()
            yield _assistant(
                content=[TextBlock(text="part2 (should not be processed)")],
                usage={"input_tokens": 999, "output_tokens": 999},
            )

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            output = await provider.execute(
                agent=AgentDef(name="test", prompt="hi"),
                context={},
                rendered_prompt="hi",
                interrupt_signal=interrupt,
            )

        # Interrupt fires at top of loop on the second iteration, after the
        # first AssistantMessage processed. Per-message sum reflects only the
        # first message; ResultMessage never arrived to overwrite it.
        assert output.partial is True
        assert output.input_tokens == 200
        assert output.output_tokens == 100


class TestErrorClassification:
    """Differentiated error suggestions per failure mode (#241 / A5).

    Previously every non-ProviderError exception got
    ``"Check that the claude CLI is installed and accessible"``, which was
    misleading for auth, network, parse, and rate-limit failures.
    """

    def test_classify_cli_not_found(self) -> None:
        from claude_agent_sdk import CLINotFoundError

        from conductor.providers.claude_agent_sdk import _classify_error_suggestion

        suggestion = _classify_error_suggestion(CLINotFoundError())
        assert "not installed" in suggestion or "not on PATH" in suggestion

    def test_classify_cli_json_decode(self) -> None:
        from claude_agent_sdk import CLIJSONDecodeError

        from conductor.providers.claude_agent_sdk import _classify_error_suggestion

        suggestion = _classify_error_suggestion(CLIJSONDecodeError("bad json", ValueError("x")))
        assert "malformed response" in suggestion or "version mismatch" in suggestion

    def test_classify_process_error_auth(self) -> None:
        from claude_agent_sdk import ProcessError

        from conductor.providers.claude_agent_sdk import _classify_error_suggestion

        suggestion = _classify_error_suggestion(
            ProcessError("authentication failed", exit_code=1, stderr="401 Unauthorized")
        )
        assert "ANTHROPIC_API_KEY" in suggestion or "claude login" in suggestion

    def test_classify_process_error_rate_limit(self) -> None:
        from claude_agent_sdk import ProcessError

        from conductor.providers.claude_agent_sdk import _classify_error_suggestion

        suggestion = _classify_error_suggestion(
            ProcessError("rate limited", exit_code=1, stderr="429 Too Many Requests")
        )
        assert "Rate-limited" in suggestion or "quota" in suggestion.lower()

    def test_classify_process_error_network(self) -> None:
        from claude_agent_sdk import ProcessError

        from conductor.providers.claude_agent_sdk import _classify_error_suggestion

        suggestion = _classify_error_suggestion(
            ProcessError("network failure", exit_code=1, stderr="connection refused")
        )
        assert "Network" in suggestion or "internet" in suggestion.lower()

    def test_classify_generic_fallback(self) -> None:
        from conductor.providers.claude_agent_sdk import _classify_error_suggestion

        suggestion = _classify_error_suggestion(RuntimeError("something else"))
        assert "claude" in suggestion.lower()  # generic CLI advice

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_execute_uses_classified_suggestion(self) -> None:
        """End-to-end: ProviderError raised by execute carries a tailored suggestion."""
        from claude_agent_sdk import CLINotFoundError

        async def failing_query(**kwargs):
            raise CLINotFoundError()
            yield  # make this an async generator

        with patch("conductor.providers.claude_agent_sdk.query", failing_query):
            provider = ClaudeAgentSdkProvider()
            with pytest.raises(ProviderError) as exc_info:
                await provider.execute(
                    agent=AgentDef(name="test", prompt="hi"),
                    context={},
                    rendered_prompt="hi",
                )

        # Suggestion is appended to the message via ProviderError.__str__.
        assert "not installed" in str(exc_info.value) or "PATH" in str(exc_info.value)


class TestCancelledErrorPropagation:
    """asyncio.CancelledError must propagate, not be wrapped in ProviderError (#241 / A8)."""

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_cancelled_error_propagates(self) -> None:
        """A bare ``except Exception`` previously swallowed CancelledError, breaking interrupts."""

        async def cancelling_query(**kwargs):
            raise asyncio.CancelledError
            yield  # make this an async generator

        with patch("conductor.providers.claude_agent_sdk.query", cancelling_query):
            provider = ClaudeAgentSdkProvider()
            with pytest.raises(asyncio.CancelledError):
                await provider.execute(
                    agent=AgentDef(name="test", prompt="hi"),
                    context={},
                    rendered_prompt="hi",
                )


class TestMaxSessionSeconds:
    """Wall-clock session timeout enforcement (#241 / A7)."""

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_max_session_seconds_enforced(self) -> None:
        """A slow stream exceeding max_session_seconds raises ProviderError."""

        async def slow_query(**kwargs):
            yield _assistant(content=[TextBlock(text="part1")])
            # Sleep longer than the session limit so the next iteration's
            # boundary check trips. ``asyncio.sleep`` keeps the test fast.
            await asyncio.sleep(0.05)
            yield _assistant(content=[TextBlock(text="part2")])
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", slow_query):
            provider = ClaudeAgentSdkProvider(max_session_seconds=0.01)
            with pytest.raises(ProviderError, match="exceeded maximum session duration"):
                await provider.execute(
                    agent=AgentDef(name="slow_agent", prompt="hi"),
                    context={},
                    rendered_prompt="hi",
                )

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_max_session_seconds_none_disables_timeout(self) -> None:
        """When max_session_seconds is None, no timeout check fires."""

        async def query(**kwargs):
            yield _assistant(content=[TextBlock(text="done")])
            await asyncio.sleep(0.01)
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", query):
            provider = ClaudeAgentSdkProvider(max_session_seconds=None)
            output = await provider.execute(
                agent=AgentDef(name="test", prompt="hi"),
                context={},
                rendered_prompt="hi",
            )

        assert output.content == {"response": "done"}


class TestRetryableClassification:
    """is_retryable is derived from error context, not hardcoded False (#241 / A9).

    Anthropic 429s and 5xx are transient — they MUST be marked retryable so
    workflow-level retry: blocks can recover from them. Auth and parse errors
    must NOT be retryable.
    """

    @pytest.mark.parametrize(
        "stderr,expected_retryable",
        [
            ("429 Too Many Requests", True),
            ("rate limit exceeded", True),
            ("503 Service Unavailable", True),
            ("502 Bad Gateway", True),
            ("overloaded_error", True),
            ("network connection refused", True),
            ("connection timeout", True),
            ("401 Unauthorized", False),
            ("authentication failed", False),
            ("invalid api key", False),
            ("400 Bad Request", False),
        ],
    )
    def test_process_error_classification(self, stderr: str, expected_retryable: bool) -> None:
        from claude_agent_sdk import ProcessError

        from conductor.providers.claude_agent_sdk import _is_retryable_exception

        exc = ProcessError("CLI failed", exit_code=1, stderr=stderr)
        assert _is_retryable_exception(exc) is expected_retryable, (
            f"stderr={stderr!r} expected retryable={expected_retryable}"
        )

    def test_parse_errors_not_retryable(self) -> None:
        from claude_agent_sdk import CLIJSONDecodeError
        from claude_agent_sdk._errors import MessageParseError

        from conductor.providers.claude_agent_sdk import _is_retryable_exception

        assert _is_retryable_exception(CLIJSONDecodeError("bad", ValueError("x"))) is False
        assert _is_retryable_exception(MessageParseError("bad")) is False

    def test_cli_not_found_not_retryable(self) -> None:
        from claude_agent_sdk import CLINotFoundError

        from conductor.providers.claude_agent_sdk import _is_retryable_exception

        assert _is_retryable_exception(CLINotFoundError()) is False

    def test_cli_connection_error_is_retryable(self) -> None:
        from claude_agent_sdk import CLIConnectionError

        from conductor.providers.claude_agent_sdk import _is_retryable_exception

        assert _is_retryable_exception(CLIConnectionError("subprocess died")) is True

    @pytest.mark.parametrize(
        "api_status,expected_retryable",
        [
            (429, True),
            (500, True),
            (502, True),
            (503, True),
            (504, True),
            (599, True),
            (401, False),
            (403, False),
            (400, False),
        ],
    )
    def test_result_message_api_status(self, api_status: int, expected_retryable: bool) -> None:
        from conductor.providers.claude_agent_sdk import _is_retryable_result

        msg = Mock(api_error_status=api_status, stop_reason=None, errors=None, result=None)
        assert _is_retryable_result(msg) is expected_retryable

    @pytest.mark.parametrize(
        "stop_reason,expected_retryable",
        [
            ("rate_limit", True),
            ("overloaded", True),
            ("server_error", True),
            ("max_tokens", False),
            ("max_turns", False),
            ("stop_sequence", False),
            ("tool_use", False),
            ("end_turn", False),
        ],
    )
    def test_result_message_stop_reason(self, stop_reason: str, expected_retryable: bool) -> None:
        from conductor.providers.claude_agent_sdk import _is_retryable_result

        msg = Mock(api_error_status=None, stop_reason=stop_reason, errors=None, result=None)
        assert _is_retryable_result(msg) is expected_retryable

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_provider_error_carries_is_retryable_from_result(self) -> None:
        """End-to-end: rate-limit ResultMessage raises ProviderError(is_retryable=True)."""

        async def fake_query(**kwargs):
            yield _result(is_error=True, result="429 rate_limit hit")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            with pytest.raises(ProviderError) as exc_info:
                await provider.execute(
                    agent=AgentDef(name="test", prompt="hi"),
                    context={},
                    rendered_prompt="hi",
                )

        assert exc_info.value.is_retryable is True

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_provider_error_carries_is_retryable_false_for_auth(self) -> None:
        """ResultMessage(is_error, auth failure) raises ProviderError(is_retryable=False)."""

        async def fake_query(**kwargs):
            yield _result(is_error=True, result="401 unauthorized invalid api key")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            with pytest.raises(ProviderError) as exc_info:
                await provider.execute(
                    agent=AgentDef(name="test", prompt="hi"),
                    context={},
                    rendered_prompt="hi",
                )

        assert exc_info.value.is_retryable is False


class TestSchemaContractEnforcement:
    """When `agent.output` is declared, parse failures must raise (#241 / A11)."""

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_structured_output_non_json_string_raises_when_schema_declared(self) -> None:
        from conductor.exceptions import ValidationError

        async def fake_query(**kwargs):
            yield _result(structured_output="this is not json")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            agent = AgentDef(
                name="test",
                prompt="hi",
                output={"answer": OutputField(type="string")},
            )
            with pytest.raises(ValidationError, match="non-JSON structured_output"):
                await provider.execute(agent=agent, context={}, rendered_prompt="hi")

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_no_schema_still_tolerates_non_json(self) -> None:
        """Without a declared schema, the response wrapper fallback is fine."""

        async def fake_query(**kwargs):
            yield _assistant(content=[TextBlock(text="just some prose")])
            yield _result()

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            output = await provider.execute(
                agent=AgentDef(name="test", prompt="hi"),
                context={},
                rendered_prompt="hi",
            )

        assert output.content == {"response": "just some prose"}

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_partial_output_tolerates_schema_mismatch(self) -> None:
        """Mid-interrupt partial output must NOT raise even if it doesn't parse.

        Partial output is best-effort by definition — surfacing what we
        have is more useful than failing the whole workflow on top of an
        already-aborted run.
        """
        interrupt = asyncio.Event()

        async def fake_query(**kwargs):
            yield _assistant(content=[TextBlock(text='incomplete partial json {"key":')])
            interrupt.set()
            yield _assistant(content=[TextBlock(text="should not be reached")])

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            output = await provider.execute(
                agent=AgentDef(
                    name="test",
                    prompt="hi",
                    output={"key": OutputField(type="string")},
                ),
                context={},
                rendered_prompt="hi",
                interrupt_signal=interrupt,
            )

        assert output.partial is True
        # Fallback to {"response": ...} is acceptable for partial output.
        assert output.content == {"response": 'incomplete partial json {"key":'}


class TestParityCoverage:
    """Test gaps identified in the PR #104 review (#241 / test-gaps).

    Each test pins a specific parity contract or override behavior that
    was previously unverified.
    """

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_output_model_field_populated(self) -> None:
        """AgentOutput.model must reflect the model the SDK actually used."""

        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="hi")],
                model="claude-opus-4-5-20251111",  # SDK-reported model may differ from request
            )
            yield _result(result="hi", usage={"input_tokens": 1, "output_tokens": 1})

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            output = await provider.execute(
                agent=AgentDef(name="test", prompt="hi", model="claude-sonnet-4-5"),
                context={},
                rendered_prompt="hi",
            )

        assert output.model == "claude-opus-4-5-20251111"

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    async def test_per_agent_model_override(self) -> None:
        """agent.model overrides the provider default."""
        options_mock = Mock()

        async def fake_query(**kwargs):
            yield _result(result="done")

        with (
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider(model="claude-sonnet-4-5")  # default
            await provider.execute(
                agent=AgentDef(name="test", prompt="hi", model="claude-opus-4-5"),
                context={},
                rendered_prompt="hi",
            )

        assert options_mock.call_args[1]["model"] == "claude-opus-4-5"

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    async def test_per_agent_max_iterations_override(self) -> None:
        """agent.max_agent_iterations overrides the provider default max_turns."""
        options_mock = Mock()

        async def fake_query(**kwargs):
            yield _result(result="done")

        with (
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider(max_turns=50)  # default
            await provider.execute(
                agent=AgentDef(name="test", prompt="hi", max_agent_iterations=7),
                context={},
                rendered_prompt="hi",
            )

        assert options_mock.call_args[1]["max_turns"] == 7

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_pending_tools_across_message_boundaries(self) -> None:
        """Tool results in a different message than the tool_use still pair correctly."""
        events: list[tuple[str, dict]] = []

        async def fake_query(**kwargs):
            # Three tool_use blocks in one assistant message.
            yield _assistant(
                content=[
                    TextBlock(text="calling tools"),
                    ToolUseBlock(id="tool_a", name="alpha", input={"x": 1}),
                    ToolUseBlock(id="tool_b", name="beta", input={"x": 2}),
                    ToolUseBlock(id="tool_c", name="gamma", input={"x": 3}),
                ]
            )
            # Results arrive across TWO separate UserMessages, out of order.
            yield UserMessage(
                content=[ToolResultBlock(tool_use_id="tool_c", content="gamma_result")]
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="tool_a", content="alpha_result"),
                    ToolResultBlock(tool_use_id="tool_b", content="beta_result"),
                ]
            )
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            await provider.execute(
                agent=AgentDef(name="test", prompt="hi"),
                context={},
                rendered_prompt="hi",
                event_callback=lambda t, d: events.append((t, d)),
            )

        completions = [
            (e[1]["tool_name"], e[1]["result"]) for e in events if e[0] == "agent_tool_complete"
        ]
        # Each tool_use must be paired with its matching result by ID,
        # regardless of message boundaries or arrival order.
        assert ("gamma", "gamma_result") in completions
        assert ("alpha", "alpha_result") in completions
        assert ("beta", "beta_result") in completions
        # No "unknown" entries — every result paired.
        assert all(name in {"gamma", "alpha", "beta"} for name, _ in completions)

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.query", lambda **kwargs: None)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_close_releases_resources(self) -> None:
        """close() is idempotent and safe to call multiple times."""
        provider = ClaudeAgentSdkProvider()
        await provider.close()
        await provider.close()  # second call must not raise

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_mid_stream_interrupt_returns_partial(self) -> None:
        """Interrupt fired between messages (not pre-iteration) returns partial output."""
        interrupt = asyncio.Event()
        processed = []

        async def fake_query(**kwargs):
            yield _assistant(
                content=[TextBlock(text="first chunk")],
                usage={"input_tokens": 100, "output_tokens": 50},
            )
            processed.append(1)
            interrupt.set()  # Mid-stream interrupt AFTER the first message processed.
            yield _assistant(
                content=[TextBlock(text="second chunk (should not append to content)")],
                usage={"input_tokens": 200, "output_tokens": 100},
            )
            processed.append(2)
            yield _result(result="never reached")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            output = await provider.execute(
                agent=AgentDef(name="test", prompt="hi"),
                context={},
                rendered_prompt="hi",
                interrupt_signal=interrupt,
            )

        assert output.partial is True
        # First chunk content is in the partial output; second chunk is NOT
        # (interrupt fires at top of loop before second message processes).
        assert "first chunk" in output.content["response"]
        assert "second chunk" not in output.content["response"]


class TestPerAgentMaxSessionSeconds:
    """Per-agent ``max_session_seconds`` overrides the provider default (#241 rubber-duck)."""

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_per_agent_override_enforced(self) -> None:
        """An agent with max_session_seconds set overrides the provider default.

        ``AgentDef.max_session_seconds`` has ``ge=1.0`` per the schema, so
        the test uses a 1-second timeout + a >1s sleep to provoke the
        timeout deterministically.
        """

        async def slow_query(**kwargs):
            yield _assistant(content=[TextBlock(text="t")])
            await asyncio.sleep(1.2)
            yield _assistant(content=[TextBlock(text="should not reach")])
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", slow_query):
            # Provider default is "no timeout"; agent override trips first.
            provider = ClaudeAgentSdkProvider(max_session_seconds=None)
            with pytest.raises(ProviderError, match="exceeded maximum session duration"):
                await provider.execute(
                    agent=AgentDef(name="a", prompt="hi", max_session_seconds=1.0),
                    context={},
                    rendered_prompt="hi",
                )

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_provider_default_used_when_agent_does_not_override(self) -> None:
        """When agent.max_session_seconds is None, the provider default kicks in."""

        async def slow_query(**kwargs):
            yield _assistant(content=[TextBlock(text="t")])
            await asyncio.sleep(0.05)
            yield _assistant(content=[TextBlock(text="should not reach")])
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", slow_query):
            provider = ClaudeAgentSdkProvider(max_session_seconds=0.01)
            with pytest.raises(ProviderError, match="exceeded maximum session duration"):
                await provider.execute(
                    agent=AgentDef(name="a", prompt="hi"),  # no override
                    context={},
                    rendered_prompt="hi",
                )


class TestErrorClassificationGaps:
    """Coverage for previously-untested suggestion branches (#241 test gap)."""

    def test_classify_cli_connection_error(self) -> None:
        from claude_agent_sdk import CLIConnectionError

        from conductor.providers.claude_agent_sdk import _classify_error_suggestion

        suggestion = _classify_error_suggestion(CLIConnectionError("subprocess died"))
        assert "Could not connect" in suggestion or "firewall" in suggestion

    def test_classify_process_error_generic_fallback(self) -> None:
        """ProcessError whose stderr matches no auth/rate/network pattern."""
        from claude_agent_sdk import ProcessError

        from conductor.providers.claude_agent_sdk import _classify_error_suggestion

        # No auth/rate/network keywords.
        suggestion = _classify_error_suggestion(
            ProcessError("unexpected", exit_code=1, stderr="weird subprocess output")
        )
        assert "subprocess failed" in suggestion.lower()


class TestRetryableResultTextFallback:
    """When stop_reason and api_error_status are both None, fall back to text scan (#241 gap)."""

    @pytest.mark.parametrize(
        "errors,expected",
        [
            (["503 Service Unavailable"], True),
            (["network timeout"], True),
            (["connection refused"], True),
            (["unknown error code"], False),
        ],
    )
    def test_result_message_text_fallback(self, errors, expected) -> None:
        from conductor.providers.claude_agent_sdk import _is_retryable_result

        msg = Mock(api_error_status=None, stop_reason=None, errors=errors, result=None)
        assert _is_retryable_result(msg) is expected


class TestBuildErrorMessage:
    """Direct tests for _build_error_message aggregation (#241 test gap)."""

    def test_includes_all_fields(self) -> None:
        msg = Mock(errors=["e1", "e2"], result="failed", stop_reason="error", num_turns=3)
        text = ClaudeAgentSdkProvider._build_error_message(msg)
        assert "e1; e2" in text
        assert "failed" in text
        assert "stop_reason=error" in text
        assert "after 3 turns" in text

    def test_no_details_fallback(self) -> None:
        msg = Mock(errors=None, result=None, stop_reason=None, num_turns=None)
        text = ClaudeAgentSdkProvider._build_error_message(msg)
        assert "no details" in text


class TestToolResultTruncation:
    """The 500-char preview is load-bearing for dashboard / JSONL (#241 test gap)."""

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_long_tool_result_truncated_to_500_chars(self) -> None:
        events: list[tuple[str, dict]] = []
        long_result = "x" * 5000

        async def fake_query(**kwargs):
            yield _assistant(
                content=[ToolUseBlock(id="t1", name="search", input={})],
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=long_result)])
            yield _result(result="done")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            await provider.execute(
                agent=AgentDef(name="t", prompt="hi"),
                context={},
                rendered_prompt="hi",
                event_callback=lambda t, d: events.append((t, d)),
            )

        completion = next(e for e in events if e[0] == "agent_tool_complete")
        assert len(completion[1]["result"]) == 500


class TestSafeCallbackSwallowing:
    """A buggy event_callback must not abort SDK execution (#241 test gap)."""

    @patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", Mock)
    async def test_failing_event_callback_does_not_abort_execution(self) -> None:
        def boom(t: str, d: dict) -> None:
            raise RuntimeError("subscriber bug")

        async def fake_query(**kwargs):
            yield _assistant(content=[TextBlock(text="hi")])
            yield _result(result="hi")

        with patch("conductor.providers.claude_agent_sdk.query", fake_query):
            provider = ClaudeAgentSdkProvider()
            # Must NOT raise — _safe_callback swallows the subscriber's RuntimeError.
            output = await provider.execute(
                agent=AgentDef(name="t", prompt="hi"),
                context={},
                rendered_prompt="hi",
                event_callback=boom,
            )
        assert output.content == {"response": "hi"}
