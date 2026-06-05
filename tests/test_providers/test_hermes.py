"""Unit tests for the HermesProvider implementation."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError, ValidationError
from conductor.providers.hermes import HermesProvider


def _make_agent(
    name: str = "test_agent",
    model: str | None = None,
    output: dict[str, OutputField] | None = None,
    max_agent_iterations: int | None = None,
    max_session_seconds: float | None = None,
    tools: list[str] | None = None,
    system_prompt: str | None = None,
) -> AgentDef:
    return AgentDef(
        name=name,
        model=model,
        output=output,
        max_agent_iterations=max_agent_iterations,
        max_session_seconds=max_session_seconds,
        tools=tools,
        system_prompt=system_prompt,
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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

    def test_uses_provider_default_model_when_agent_has_none(self, provider: HermesProvider) -> None:
        agent = _make_agent(model=None)

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            self._run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["model"] == "anthropic/claude-sonnet-4"

    def test_omits_model_when_neither_set(self) -> None:
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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

    def test_isolation_flags_always_set(self, provider: HermesProvider) -> None:
        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            self._run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["quiet_mode"] is True
        assert kwargs["skip_context_files"] is True
        assert kwargs["skip_memory"] is True

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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
            provider = HermesProvider()

        agent = _make_agent(tools=["web_search", "read_file"])

        with pytest.raises(ProviderError, match="does not support per-agent workflow tool"):
            asyncio.run(
                provider.execute(agent, {}, "hello", tools=["web_search", "read_file"])
            )

    def test_hermes_toolsets_forwarded_as_enabled_toolsets(self) -> None:
        """Provider-level hermes_toolsets is forwarded when tools=None."""
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
            provider = HermesProvider(temperature=0.5)

        agent = _make_agent()

        with patch("conductor.providers.hermes.AIAgent") as mock_cls:
            mock_instance = Mock()
            mock_instance.run_conversation.return_value = _make_result()
            mock_cls.return_value = mock_instance

            asyncio.run(provider.execute(agent, {}, "hello"))

        _, kwargs = mock_cls.call_args
        assert kwargs["temperature"] == 0.5

    def test_base_url_forwarded(self) -> None:
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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
        with patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True), \
             patch("conductor.providers.hermes.AIAgent"):
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


class TestHermesClose:
    @patch("conductor.providers.hermes.HERMES_SDK_AVAILABLE", True)
    @patch("conductor.providers.hermes.AIAgent", MagicMock())
    def test_close_is_noop(self) -> None:
        p = HermesProvider()
        asyncio.run(p.close())  # should not raise
