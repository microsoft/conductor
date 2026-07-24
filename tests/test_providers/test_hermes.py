"""Unit tests for the HermesProvider implementation."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField, ReasoningConfig
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers.hermes import _MAX_SCHEMA_DEPTH, HermesProvider, _build_prompt_schema


def _make_agent(
    name: str = "test_agent",
    model: str | None = None,
    output: dict[str, OutputField] | None = None,
    max_agent_iterations: int | None = None,
    max_session_seconds: float | None = None,
    tools: list[str] | None = None,
    system_prompt: str | None = None,
    reasoning: ReasoningConfig | None = None,
) -> AgentDef:
    return AgentDef(
        name=name,
        model=model,
        output=output,
        max_agent_iterations=max_agent_iterations,
        max_session_seconds=max_session_seconds,
        tools=tools,
        system_prompt=system_prompt,
        reasoning=reasoning,
    )


def _make_result(
    final_response: str = "hello",
    completed: bool = True,
    failed: bool = False,
    partial: bool = False,
    error: str | None = None,
    model: str | None = "anthropic/claude-sonnet-4",
    input_tokens: int | None = 10,
    output_tokens: int | None = 20,
    total_tokens: int | None = 30,
) -> dict[str, Any]:
    return {
        "final_response": final_response,
        "completed": completed,
        "failed": failed,
        "partial": partial,
        "error": error,
        "messages": [],
        "api_calls": 1,
        "model": model,
        "provider": "anthropic",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


class TestHermesProviderInit:
    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", False)
    def test_raises_when_sdk_not_installed(self) -> None:
        with pytest.raises(ProviderError, match="hermes-agent"):
            HermesProvider()

    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.AIAgent", MagicMock())
    def test_init_defaults(self) -> None:
        p = HermesProvider()
        assert p._default_model is None
        assert p._default_max_agent_iterations is None
        assert p._default_max_session_seconds is None

    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.AIAgent", MagicMock())
    def test_init_custom(self) -> None:
        p = HermesProvider(model="openai/gpt-4o", max_agent_iterations=25)
        assert p._default_model == "openai/gpt-4o"
        assert p._default_max_agent_iterations == 25


class TestHermesValidateConnection:
    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", False)
    def test_returns_false_when_sdk_missing(self) -> None:
        p = object.__new__(HermesProvider)
        result = asyncio.run(p.validate_connection())
        assert result is False

    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.AIAgent", MagicMock())
    def test_returns_true_when_sdk_available(self) -> None:
        p = object.__new__(HermesProvider)
        result = asyncio.run(p.validate_connection())
        assert result is True


class TestHermesExecute:
    @pytest.fixture()
    def provider(self) -> HermesProvider:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            return HermesProvider(model="anthropic/claude-sonnet-4", max_agent_iterations=10)

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_plain_text_no_schema(self, provider: HermesProvider) -> None:
        agent = _make_agent()
        result_dict = _make_result(final_response="world")

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = result_dict
            mock_cls.return_value = mock_instance

            output = self._run(provider.execute(agent, {}, "say hello"))

        assert output.content == {"text": "world"}
        assert output.raw_response["final_response"] == "world"

    def test_json_schema_appends_instruction(self, provider: HermesProvider) -> None:
        schema = {"answer": OutputField(type="string")}
        agent = _make_agent(output=schema)
        result_dict = _make_result(final_response='{"answer": "pong"}')

        captured_prompts: list[str] = []

        def fake_run_conv(prompt: str, **kwargs: Any) -> dict[str, Any]:
            captured_prompts.append(prompt)
            return result_dict

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.side_effect = fake_run_conv
            mock_cls.return_value = mock_instance

            output = self._run(provider.execute(agent, {}, "answer this"))

        assert "MUST respond with a JSON object matching this schema" in captured_prompts[0]
        assert output.content == {"answer": "pong"}

    def test_json_schema_validation_error(self, provider: HermesProvider) -> None:
        schema = {"answer": OutputField(type="string")}
        agent = _make_agent(output=schema)
        # Missing required field 'answer' — recovery loop exhausted
        result_dict = _make_result(final_response='{"wrong": "field"}')

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = result_dict
            mock_cls.return_value = mock_instance

            with pytest.raises(ProviderError, match="Failed to parse structured output"):
                self._run(provider.execute(agent, {}, "answer this"))

    def test_passes_model_to_aiagent(self, provider: HermesProvider) -> None:
        agent = _make_agent(model="openai/gpt-4o")

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            self._run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["model"] == "openai/gpt-4o"

    def test_uses_provider_default_model_when_agent_has_none(
        self, provider: HermesProvider
    ) -> None:
        agent = _make_agent(model=None)

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            self._run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["model"] == "anthropic/claude-sonnet-4"

    def test_omits_model_when_neither_set(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider_no_model = HermesProvider()

        agent = _make_agent(model=None)

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            self._run(provider_no_model.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert "model" not in kwargs

    def test_passes_max_iterations(self, provider: HermesProvider) -> None:
        agent = _make_agent(max_agent_iterations=42)

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            self._run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["max_iterations"] == 42

    def test_quiet_mode_always_set(self, provider: HermesProvider) -> None:
        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            self._run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["quiet_mode"] is True

    def test_skip_flags_omitted_by_default(self, provider: HermesProvider) -> None:
        """When skip_memory/skip_context_files are not configured, they are omitted
        from agent_kwargs so the hermes-agent library defaults apply (False)."""
        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            self._run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert "skip_context_files" not in kwargs
        assert "skip_memory" not in kwargs

    def test_skip_memory_true_forwarded(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(skip_memory=True)

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["skip_memory"] is True

    def test_skip_context_files_true_forwarded(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(skip_context_files=True)

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["skip_context_files"] is True

    def test_skip_flags_false_forwarded(self) -> None:
        """Explicit False is forwarded (though it matches library default)."""
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(skip_memory=False, skip_context_files=False)

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["skip_memory"] is False
        assert kwargs["skip_context_files"] is False

    def test_token_counts_populated(self, provider: HermesProvider) -> None:
        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result(
                input_tokens=100, output_tokens=50, total_tokens=150
            )
            mock_cls.return_value = mock_instance

            output = self._run(provider.execute(agent, {}, "hello"))

        assert output.input_tokens == 100
        assert output.output_tokens == 50
        assert output.tokens_used == 150

    def test_session_metadata_in_raw_response(self, provider: HermesProvider) -> None:
        agent = _make_agent()
        result_dict = _make_result(final_response="hi", model="openai/gpt-4o")

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = result_dict
            mock_cls.return_value = mock_instance

            output = self._run(provider.execute(agent, {}, "hello"))

        assert output.raw_response["model"] == "openai/gpt-4o"
        assert "messages" in output.raw_response
        assert "api_calls" in output.raw_response

    def test_raises_provider_error_on_failed_result(self, provider: HermesProvider) -> None:
        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result(
                failed=True, final_response=None, error="quota exhausted"
            )
            mock_cls.return_value = mock_instance

            with pytest.raises(ProviderError, match="quota exhausted"):
                self._run(provider.execute(agent, {}, "hello"))

    def test_raises_provider_error_on_none_final_response(self, provider: HermesProvider) -> None:
        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result(
                final_response=None, completed=False, error="truncated"
            )
            mock_cls.return_value = mock_instance

            with pytest.raises(ProviderError, match="no final response"):
                self._run(provider.execute(agent, {}, "hello"))

    def test_raises_provider_error_on_sdk_exception(self, provider: HermesProvider) -> None:
        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.side_effect = RuntimeError("network error")
            mock_cls.return_value = mock_instance

            with pytest.raises(ProviderError, match="network error"):
                self._run(provider.execute(agent, {}, "hello"))

    def test_event_callback_fires(self, provider: HermesProvider) -> None:
        agent = _make_agent()
        events: list[tuple[str, dict]] = []

        def cb(event: str, data: dict) -> None:
            events.append((event, data))

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result(final_response="hi")
            mock_cls.return_value = mock_instance

            self._run(provider.execute(agent, {}, "hello", event_callback=cb))

        # agent_turn_start fires before the executor call
        event_types = [e[0] for e in events]
        assert "agent_turn_start" in event_types
        turn_start = next(d for t, d in events if t == "agent_turn_start")
        assert turn_start == {"turn": "awaiting_model"}

        # Streaming callbacks are wired into AIAgent constructor
        _, kwargs = mock_cls.call_args
        assert "stream_delta_callback" in kwargs
        assert "reasoning_callback" in kwargs

    def test_streaming_callback_emits_events(self, provider: HermesProvider) -> None:
        """Verify that stream_delta_callback and reasoning_callback emit events."""
        agent = _make_agent()
        events: list[tuple[str, dict]] = []

        def cb(event: str, data: dict) -> None:
            events.append((event, data))

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result(final_response="hi")
            mock_cls.return_value = mock_instance

            self._run(provider.execute(agent, {}, "hello", event_callback=cb))

            # Simulate what hermes does: invoke the callbacks
            _, kwargs = mock_cls.call_args
            kwargs["stream_delta_callback"]("hello ")
            kwargs["stream_delta_callback"]("world")
            kwargs["reasoning_callback"]("thinking...")

        msg_events = [(t, d) for t, d in events if t == "agent_message"]
        assert len(msg_events) == 2
        assert msg_events[0][1] == {"content": "hello "}
        assert msg_events[1][1] == {"content": "world"}

        reason_events = [(t, d) for t, d in events if t == "agent_reasoning"]
        assert len(reason_events) == 1
        assert reason_events[0][1] == {"content": "thinking..."}

    def test_interrupt_signal_raises_provider_error(self, provider: HermesProvider) -> None:
        agent = _make_agent()

        async def run_with_pre_set_interrupt() -> None:
            interrupt = asyncio.Event()
            interrupt.set()  # already set before execute — wins the race immediately

            with patch("conductor.providers.hermes.AIAgent") as mock_cls:
                import time

                mock_instance = Mock()
                mock_instance.run_conversation.side_effect = lambda *a, **kw: time.sleep(5)
                mock_cls.return_value = mock_instance

                with pytest.raises(ProviderError, match="interrupted"):
                    await provider.execute(agent, {}, "hello", interrupt_signal=interrupt)

        asyncio.run(run_with_pre_set_interrupt())


class TestHermesSystemPrompt:
    def test_system_prompt_forwarded(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider()

        agent = _make_agent(system_prompt="You are a helpful assistant.")
        captured: list[dict] = []

        def fake_run_conv(prompt: str, **kwargs: Any) -> dict[str, Any]:
            captured.append(kwargs)
            return _make_result()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.side_effect = fake_run_conv
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        assert captured[0].get("system_message") == "You are a helpful assistant."

    def test_system_prompt_none_when_not_set(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider()

        agent = _make_agent(system_prompt=None)
        captured: list[dict] = []

        def fake_run_conv(prompt: str, **kwargs: Any) -> dict[str, Any]:
            captured.append(kwargs)
            return _make_result()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.side_effect = fake_run_conv
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        assert captured[0].get("system_message") is None


class TestHermesToolsMapping:
    def test_tools_none_uses_hermes_defaults(self) -> None:
        """tools=None (omitted) does not set enabled_toolsets — hermes uses its defaults."""
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider()

        agent = _make_agent(tools=None)

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert "enabled_toolsets" not in kwargs

    def test_tools_empty_disables_all(self) -> None:
        """tools=[] explicitly disables all hermes toolsets."""
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider()

        agent = _make_agent(tools=[])

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello", tools=[]))

        _, kwargs = mock_cls.call_args
        assert kwargs["enabled_toolsets"] == []

    def test_tools_nonempty_raises_provider_error(self) -> None:
        """Non-empty tools: list raises ProviderError (vocabulary mismatch)."""
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider()

        agent = _make_agent(tools=["web_search", "read_file"])

        with pytest.raises(ProviderError, match="does not support per-agent workflow tool"):
            asyncio.run(provider.execute(agent, {}, "hello", tools=["web_search", "read_file"]))

    def test_hermes_toolsets_forwarded_as_enabled_toolsets(self) -> None:
        """Provider-level hermes_toolsets is forwarded when tools=None."""
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(hermes_toolsets=["filesystem", "web"])

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["enabled_toolsets"] == ["filesystem", "web"]

    def test_hermes_toolsets_empty_disables_all(self) -> None:
        """Provider-level hermes_toolsets=[] disables all toolsets."""
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(hermes_toolsets=[])

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["enabled_toolsets"] == []


class TestHermesProviderParams:
    def test_max_tokens_forwarded(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(max_tokens=1024)

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["max_tokens"] == 1024

    def test_temperature_forwarded(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(temperature=0.5)

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["request_overrides"] == {"temperature": 0.5}

    def test_base_url_forwarded(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(base_url="https://openrouter.ai/api/v1")

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["base_url"] == "https://openrouter.ai/api/v1"

    def test_api_key_forwarded(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(api_key="sk-test-key")

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["api_key"] == "sk-test-key"

    def test_error_message_includes_model(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(model="anthropic/claude-sonnet-4")

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result(
                failed=True, final_response=None, error="quota exhausted"
            )
            mock_cls.return_value = mock_instance

            with pytest.raises(ProviderError, match="anthropic/claude-sonnet-4"):
                asyncio.run(provider.execute(agent, {}, "hello"))

    def test_missing_params_not_forwarded(self) -> None:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider()

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert "max_tokens" not in kwargs
        assert "temperature" not in kwargs
        assert "base_url" not in kwargs
        assert "api_key" not in kwargs
        assert "skip_memory" not in kwargs
        assert "skip_context_files" not in kwargs


class TestHermesHome:
    def test_tilde_expanded_before_sdk_call(self) -> None:
        """hermes_home with ~ is expanded to an absolute path."""
        import sys

        mock_hermes_constants = MagicMock()
        mock_hermes_constants.set_hermes_home_override.return_value = "token"

        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(hermes_home="~/.hermes/profiles/chloe")

        agent = _make_agent()

        with (
            patch("conductor.providers.hermes.AIAgent") as mock_cls,
            patch.dict(sys.modules, {"hermes_constants": mock_hermes_constants}),
        ):
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        called_path = mock_hermes_constants.set_hermes_home_override.call_args[0][0]
        assert "~" not in called_path
        assert called_path.endswith(".hermes/profiles/chloe")


class TestHermesClose:
    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.AIAgent", MagicMock())
    def test_close_is_noop(self) -> None:
        p = HermesProvider()
        asyncio.run(p.close())  # should not raise


class TestHermesReasoningEffort:
    """#299: HermesProvider.execute() re-checks the resolved reasoning.effort
    against CAPABILITIES.reasoning_effort at runtime, closing the gap where
    only the opt-in `conductor validate` static cross-check guarded against
    an unsupported level (and that check is skipped for templated effort)."""

    @pytest.fixture()
    def provider(self) -> HermesProvider:
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            return HermesProvider(model="anthropic/claude-sonnet-4")

    def test_supported_effort_forwarded(self, provider: HermesProvider) -> None:
        agent = _make_agent(reasoning=ReasoningConfig(effort="high"))

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["reasoning_config"] == {"effort": "high"}

    def test_max_effort_rejected_at_execute_time(self, provider: HermesProvider) -> None:
        """A literal `effort: max` is rejected by execute() itself, not just
        by the opt-in `conductor validate` command."""
        agent = _make_agent(reasoning=ReasoningConfig(effort="max"))

        with pytest.raises(ValidationError, match="supports only"):
            asyncio.run(provider.execute(agent, {}, "hello"))

    def test_max_effort_via_runtime_default_rejected(self) -> None:
        """`runtime.default_reasoning_effort: max` is rejected the same way
        as a per-agent override."""
        with (
            patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True),
            patch("conductor.providers.hermes.AIAgent"),
        ):
            provider = HermesProvider(
                model="anthropic/claude-sonnet-4", default_reasoning_effort="max"
            )
        agent = _make_agent()

        with pytest.raises(ValidationError, match="supports only"):
            asyncio.run(provider.execute(agent, {}, "hello"))

    def test_max_effort_from_rendered_template_rejected(self, provider: HermesProvider) -> None:
        """A Jinja-templated `reasoning.effort` that only resolves to `max`
        after rendering is still caught here — the static validator's
        membership check is skipped for templates, so this runtime re-check
        is the only guard for this case."""
        # By the time `execute()` runs, AgentExecutor has already rendered
        # the template to a concrete literal (mirrors resolve_reasoning_effort's
        # own documented invariant in providers/reasoning.py).
        agent = _make_agent(reasoning=ReasoningConfig(effort="max"))

        with pytest.raises(ValidationError, match="supports only"):
            asyncio.run(provider.execute(agent, {}, "hello"))

    def test_no_effort_set_omits_reasoning_config(self, provider: HermesProvider) -> None:
        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert "reasoning_config" not in kwargs

    def test_real_capabilities_tuple_excludes_max(self) -> None:
        """Guard against an accidental future widening of the real
        CAPABILITIES declaration (as opposed to a test mock)."""
        assert "max" not in HermesProvider.CAPABILITIES.reasoning_effort


class TestHermesBuildPromptSchema:
    """Tests for the Hermes prompt-schema builder wrapper.

    The wrapper delegates to the shared recursive builder in
    conductor.providers._schema and converts SchemaDepthError into ValidationError
    with the exact message and suggestion. These tests pin the shared
    semantics (no description fallback, required inside array-item objects,
    recursive array-of-array items) and the depth boundary.
    """

    def _chain_schema(self, levels: int) -> dict[str, OutputField]:
        """Build a chain of nested objects `levels` deep."""
        schema: dict[str, OutputField] = {"leaf": OutputField(type="string")}
        for _ in range(levels):
            schema = {"nested": OutputField(type="object", properties=schema)}
        return schema

    def test_build_prompt_schema_omits_missing_description(self) -> None:
        """Top-level fields without explicit descriptions must not synthesize a description."""
        schema = {"answer": OutputField(type="string")}
        result = _build_prompt_schema(schema)
        assert result == {"answer": {"type": "string"}}

    def test_build_prompt_schema_array_item_object_has_required(self) -> None:
        """Array<object> item schemas must include the required property names."""
        schema = {
            "items": OutputField(
                type="array",
                items=OutputField(
                    type="object",
                    properties={
                        "key": OutputField(type="string"),
                        "value": OutputField(type="number"),
                    },
                ),
            )
        }
        result = _build_prompt_schema(schema)
        assert result["items"]["items"]["required"] == ["key", "value"]
        assert "properties" in result["items"]["items"]

    def test_build_prompt_schema_array_of_arrays_recurses(self) -> None:
        """Array<array> item schemas must recurse and expose the inner item type."""
        schema = {
            "matrix": OutputField(
                type="array",
                items=OutputField(type="array", items=OutputField(type="number")),
            )
        }
        result = _build_prompt_schema(schema)
        assert result["matrix"]["items"]["type"] == "array"
        assert result["matrix"]["items"]["items"]["type"] == "number"

    def test_build_prompt_schema_exceeds_max_depth(self) -> None:
        """Depths above _MAX_SCHEMA_DEPTH raise ValidationError with the exact
        shared message and suggestion."""
        # A 12-level object chain reaches depth 11, which exceeds the default max depth of 10.
        overly_nested = self._chain_schema(_MAX_SCHEMA_DEPTH + 2)
        with pytest.raises(ValidationError) as exc_info:
            _build_prompt_schema(overly_nested)

        error = exc_info.value
        expected = f"Schema nesting depth exceeds maximum of {_MAX_SCHEMA_DEPTH} levels"
        assert error.args[0] == expected
        assert error.suggestion == "Simplify your output schema to reduce nesting depth"

    def test_nested_array_object_descriptions_preserved(self) -> None:
        """Explicit descriptions must survive at every level of nested array<object> schemas."""
        schema = {
            "outer": OutputField(
                type="array",
                description="Outer array",
                items=OutputField(
                    type="object",
                    description="Object item",
                    properties={
                        "inner": OutputField(
                            type="array",
                            description="Inner array",
                            items=OutputField(
                                type="object",
                                description="Inner object",
                                properties={
                                    "name": OutputField(type="string", description="Name field"),
                                },
                            ),
                        ),
                    },
                ),
            )
        }
        result = _build_prompt_schema(schema)
        assert result["outer"]["description"] == "Outer array"
        assert result["outer"]["items"]["description"] == "Object item"
        assert result["outer"]["items"]["properties"]["inner"]["description"] == "Inner array"
        assert (
            result["outer"]["items"]["properties"]["inner"]["items"]["description"]
            == "Inner object"
        )
        assert (
            result["outer"]["items"]["properties"]["inner"]["items"]["properties"]["name"][
                "description"
            ]
            == "Name field"
        )

    def test_build_prompt_schema_array_item_depth_parity(self) -> None:
        """Shared depth counting: for array<object>, properties inside the item start at depth 2.

        A chain of 8 object levels inside an array<object> item (leaf at depth 10) is
        accepted; a chain of 9 (leaf at depth 11) raises ValidationError.
        """

        def chain_in_array_item(levels: int) -> dict[str, OutputField]:
            return {
                "arr": OutputField(
                    type="array",
                    items=OutputField(
                        type="object",
                        properties=self._chain_schema(levels),
                    ),
                )
            }

        # _chain_schema(8) reaches depth 10 inside the array item: accepted.
        _build_prompt_schema(chain_in_array_item(8))

        # _chain_schema(9) reaches depth 11: one level too deep.
        with pytest.raises(ValidationError, match="exceeds maximum"):
            _build_prompt_schema(chain_in_array_item(9))

    def test_build_prompt_schema_non_object_array_item_at_boundary_accepted(self) -> None:
        """Shared depth counting: every array item, including scalar items, consumes one level.

        An object chain of 9 nested objects ending in scalar array fields has array items
        at depth 10 (accepted); 10 nested objects pushes them to depth 11 (raises). A
        pure chain of 10 nested arrays has its scalar leaf at depth 10 (accepted); 11
        nested arrays raises.
        """
        # 9 nested objects ending in scalar array fields: array items sit at depth 10.
        inner: dict[str, OutputField] = {
            "tags": OutputField(type="array", items=OutputField(type="string")),
        }
        for _ in range(_MAX_SCHEMA_DEPTH - 1):
            inner = {"nested": OutputField(type="object", properties=inner)}
        _build_prompt_schema(inner)

        # 10 nested objects push scalar array items to depth 11: raises.
        too_deep = {"nested": OutputField(type="object", properties=inner)}
        with pytest.raises(ValidationError, match="exceeds maximum"):
            _build_prompt_schema(too_deep)

        # Pure array chain: 10 nested arrays accepted, 11 raises.
        def nested_array_chain(levels: int) -> OutputField:
            leaf: OutputField = OutputField(type="string")
            for _ in range(levels):
                leaf = OutputField(type="array", items=leaf)
            return leaf

        _build_prompt_schema({"matrix": nested_array_chain(_MAX_SCHEMA_DEPTH)})
        with pytest.raises(ValidationError, match="exceeds maximum"):
            _build_prompt_schema({"matrix": nested_array_chain(_MAX_SCHEMA_DEPTH + 1)})
