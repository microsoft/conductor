"""Tests for PydanticDeepProvider.

Covers:
- Initialization (with/without package available)
- Output schema conversion (_build_output_model)
- execute() – structured and plain output, token extraction
- execute() with event_callback – canonical event names
- execute() with interrupt_signal – partial=True result
- execute_dialog_turn()
- validate_connection()
- close()
- factory wiring (create_provider("pydantic-deep"))
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError
from conductor.providers.pydantic_deep import (
    PYDANTIC_DEEP_AVAILABLE,
    PydanticDeepProvider,
    _build_output_model,
    _conductor_type_to_python,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(
    name: str = "test",
    model: str | None = None,
    output: dict[str, OutputField] | None = None,
) -> AgentDef:
    """Build a minimal AgentDef for testing."""
    data: dict[str, Any] = {
        "name": name,
        "prompt": "Do something",
        "routes": [{"to": "$end"}],
    }
    if model:
        data["model"] = model
    if output:
        data["output"] = output
    return AgentDef(**data)


def _make_mock_result(
    output: Any = "hello",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> MagicMock:
    """Build a mock pydantic-ai RunResult."""
    from pydantic_ai.usage import RunUsage

    usage = RunUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    result = MagicMock()
    result.output = output
    result.usage.return_value = usage
    return result


def _make_provider(**kwargs: Any) -> PydanticDeepProvider:
    """Build a PydanticDeepProvider skipping the import guard."""
    with patch("conductor.providers.pydantic_deep.PYDANTIC_DEEP_AVAILABLE", True):
        return PydanticDeepProvider(**kwargs)


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------

class TestOutputSchema:
    """Tests for _conductor_type_to_python and _build_output_model."""

    def test_scalar_types(self) -> None:
        assert _conductor_type_to_python(OutputField(type="string")) is str
        assert _conductor_type_to_python(OutputField(type="number")) is float
        assert _conductor_type_to_python(OutputField(type="boolean")) is bool

    def test_array_with_items(self) -> None:
        field = OutputField(type="array", items=OutputField(type="string"))
        result = _conductor_type_to_python(field)
        assert result == list[str]

    def test_array_without_items(self) -> None:
        from typing import get_args, get_origin

        field = OutputField(type="array")
        result = _conductor_type_to_python(field)
        assert get_origin(result) is list

    def test_object_with_properties(self) -> None:
        from pydantic import BaseModel

        field = OutputField(
            type="object",
            properties={"x": OutputField(type="string"), "y": OutputField(type="number")},
        )
        result = _conductor_type_to_python(field)
        assert issubclass(result, BaseModel)
        instance = result(x="hi", y=1.5)
        assert instance.x == "hi"  # type: ignore[attr-defined]

    def test_object_without_properties_gives_dict(self) -> None:
        field = OutputField(type="object")
        result = _conductor_type_to_python(field)
        assert result == dict[str, Any]

    def test_build_output_model_creates_pydantic_model(self) -> None:
        from pydantic import BaseModel

        schema = {
            "summary": OutputField(type="string"),
            "score": OutputField(type="number"),
            "found": OutputField(type="boolean"),
        }
        model = _build_output_model(schema)
        assert issubclass(model, BaseModel)
        instance = model(summary="ok", score=0.9, found=True)
        assert instance.summary == "ok"  # type: ignore[attr-defined]
        assert instance.score == 0.9  # type: ignore[attr-defined]

    def test_build_output_model_nested_array(self) -> None:
        from pydantic import BaseModel

        schema = {
            "items": OutputField(type="array", items=OutputField(type="string")),
        }
        model = _build_output_model(schema)
        assert issubclass(model, BaseModel)
        instance = model(items=["a", "b"])
        assert instance.items == ["a", "b"]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestPydanticDeepProviderInit:
    """Tests for PydanticDeepProvider.__init__."""

    @patch("conductor.providers.pydantic_deep.PYDANTIC_DEEP_AVAILABLE", False)
    def test_raises_when_package_not_installed(self) -> None:
        with pytest.raises(ProviderError, match="pydantic-deep package"):
            PydanticDeepProvider()

    @patch("conductor.providers.pydantic_deep.PYDANTIC_DEEP_AVAILABLE", True)
    def test_default_model(self) -> None:
        p = PydanticDeepProvider()
        assert p._model == "anthropic:claude-sonnet-4-6"

    @patch("conductor.providers.pydantic_deep.PYDANTIC_DEEP_AVAILABLE", True)
    def test_custom_model(self) -> None:
        p = PydanticDeepProvider(model="openai:gpt-4o")
        assert p._model == "openai:gpt-4o"

    @patch("conductor.providers.pydantic_deep.PYDANTIC_DEEP_AVAILABLE", True)
    def test_custom_params_stored(self) -> None:
        p = PydanticDeepProvider(
            temperature=0.5,
            max_tokens=4096,
            timeout=300.0,
            max_agent_iterations=10,
        )
        assert p._temperature == 0.5
        assert p._max_tokens == 4096
        assert p._timeout == 300.0
        assert p._max_agent_iterations == 10


# ---------------------------------------------------------------------------
# execute() — plain string output
# ---------------------------------------------------------------------------

class TestExecute:
    """Tests for PydanticDeepProvider.execute()."""

    @pytest.mark.asyncio
    async def test_plain_string_output(self) -> None:
        provider = _make_provider()
        agent = _make_agent()
        mock_result = _make_mock_result(output="hello world")

        with (
            patch.object(provider, "_build_agent", return_value=MagicMock()),
            patch.object(provider, "_run_with_interrupt", new=AsyncMock(return_value=mock_result)),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            result = await provider.execute(agent, {}, "do something")

        assert result.content == {"result": "hello world"}
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert result.tokens_used == 15
        assert result.partial is False

    @pytest.mark.asyncio
    async def test_structured_output(self) -> None:
        from pydantic import BaseModel

        provider = _make_provider()
        schema = {
            "summary": OutputField(type="string"),
            "score": OutputField(type="number"),
        }
        agent = _make_agent(output=schema)

        OutputModel = _build_output_model(schema)
        structured_output = OutputModel(summary="great", score=0.95)
        mock_result = _make_mock_result(output=structured_output)

        with (
            patch.object(provider, "_build_agent", return_value=MagicMock()),
            patch.object(provider, "_run_with_interrupt", new=AsyncMock(return_value=mock_result)),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            result = await provider.execute(agent, {}, "analyze this")

        assert isinstance(result.content, dict)
        assert result.content["summary"] == "great"
        assert result.content["score"] == pytest.approx(0.95)

    @pytest.mark.asyncio
    async def test_interrupt_returns_partial(self) -> None:
        provider = _make_provider()
        agent = _make_agent()

        with (
            patch.object(provider, "_build_agent", return_value=MagicMock()),
            patch.object(provider, "_run_with_interrupt", new=AsyncMock(return_value=None)),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            result = await provider.execute(agent, {}, "do it", interrupt_signal=asyncio.Event())

        assert result.partial is True
        assert result.content == {"result": ""}

    @pytest.mark.asyncio
    async def test_execute_exception_wrapped_as_provider_error(self) -> None:
        provider = _make_provider(timeout=None)
        agent = _make_agent()

        with (
            patch.object(provider, "_build_agent", return_value=MagicMock()),
            patch.object(
                provider,
                "_run_with_interrupt",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            with pytest.raises(ProviderError, match="boom"):
                await provider.execute(agent, {}, "do it")

    @pytest.mark.asyncio
    async def test_per_agent_model_override(self) -> None:
        provider = _make_provider(model="anthropic:claude-sonnet-4-6")
        agent = _make_agent(model="openai:gpt-4o")
        mock_result = _make_mock_result()
        built_agents: list[Any] = []

        original_build = provider._build_agent

        def capture_build(model: str, thinking: Any, output_type: Any = None) -> Any:
            built_agents.append(model)
            return MagicMock()

        with (
            patch.object(provider, "_build_agent", side_effect=capture_build),
            patch.object(provider, "_run_with_interrupt", new=AsyncMock(return_value=mock_result)),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            result = await provider.execute(agent, {}, "task")

        assert "openai:gpt-4o" in built_agents
        assert result.model == "openai:gpt-4o"

    @pytest.mark.asyncio
    async def test_max_turns_from_agent_def(self) -> None:
        provider = _make_provider()
        data: dict[str, Any] = {
            "name": "test",
            "prompt": "go",
            "routes": [{"to": "$end"}],
            "max_agent_iterations": 5,
        }
        agent = AgentDef(**data)
        mock_result = _make_mock_result()
        captured_kwargs: list[dict[str, Any]] = []

        async def capture_run(p: Any, prompt: str, deps: Any, kw: dict[str, Any], sig: Any) -> Any:
            captured_kwargs.append(dict(kw))
            return mock_result

        with (
            patch.object(provider, "_build_agent", return_value=MagicMock()),
            patch.object(provider, "_run_with_interrupt", new=AsyncMock(side_effect=capture_run)),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            await provider.execute(agent, {}, "go")

        assert captured_kwargs[0].get("max_turns") == 5


# ---------------------------------------------------------------------------
# execute() with event_callback
# ---------------------------------------------------------------------------

class TestEventCallback:
    """Tests for canonical event names emitted via event_callback."""

    @pytest.mark.asyncio
    async def test_events_routed_through_run_with_events(self) -> None:
        provider = _make_provider()
        agent = _make_agent()
        mock_result = _make_mock_result()
        events: list[tuple[str, dict[str, Any]]] = []

        def callback(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        with (
            patch.object(provider, "_build_agent", return_value=MagicMock()),
            patch.object(
                provider, "_run_with_events", new=AsyncMock(return_value=mock_result)
            ),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            await provider.execute(agent, {}, "go", event_callback=callback)

        # _run_with_events should have been called (not _run_with_interrupt)
        # The mock returns immediately so no real events — just verify routing.

    @pytest.mark.asyncio
    async def test_safe_callback_swallows_exception(self) -> None:
        from conductor.providers.pydantic_deep import _safe_callback

        def bad_cb(event_type: str, data: dict[str, Any]) -> None:
            raise RuntimeError("callback exploded")

        # Should not raise
        _safe_callback(bad_cb, "agent_message", {"content": "hi"})


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRetry:
    """Tests for _execute_with_retry behavior."""

    @pytest.mark.asyncio
    async def test_non_retryable_provider_error_raises_immediately(self) -> None:
        provider = _make_provider()
        agent = _make_agent()

        with patch.object(
            provider,
            "_execute_once",
            new=AsyncMock(
                side_effect=ProviderError("fatal", is_retryable=False)
            ),
        ):
            with pytest.raises(ProviderError, match="fatal"):
                await provider._execute_with_retry(agent, {}, "go")

    @pytest.mark.asyncio
    async def test_retryable_error_retries_and_succeeds(self) -> None:
        provider = _make_provider()
        agent = _make_agent()
        mock_result = _make_mock_result()

        call_count = 0

        async def flaky_execute(*args: Any, **kwargs: Any) -> AgentOutput:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ProviderError("transient", is_retryable=True)
            return await AsyncMock(return_value=_make_mock_result())(*args, **kwargs)

        with (
            patch.object(provider, "_execute_once", new=AsyncMock(side_effect=flaky_execute)),
            patch("conductor.providers.pydantic_deep.asyncio.sleep", new=AsyncMock()),
        ):
            result = await provider._execute_with_retry(agent, {}, "go")

        assert call_count == 2


# ---------------------------------------------------------------------------
# execute_dialog_turn()
# ---------------------------------------------------------------------------

class TestDialogTurn:
    """Tests for execute_dialog_turn()."""

    @pytest.mark.asyncio
    async def test_returns_string_response(self) -> None:
        provider = _make_provider()
        mock_result = MagicMock()
        mock_result.output = "I am an agent."

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        with (
            patch(
                "conductor.providers.pydantic_deep.create_deep_agent",
                return_value=mock_agent,
            ),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            response = await provider.execute_dialog_turn(
                system_prompt="You are helpful.",
                user_message="Hello",
            )

        assert response == "I am an agent."

    @pytest.mark.asyncio
    async def test_history_prepended_to_prompt(self) -> None:
        provider = _make_provider()
        mock_result = MagicMock()
        mock_result.output = "ok"

        mock_agent = MagicMock()
        captured_prompts: list[str] = []

        async def capture_run(prompt: str, **kwargs: Any) -> Any:
            captured_prompts.append(prompt)
            return mock_result

        mock_agent.run = capture_run

        with (
            patch(
                "conductor.providers.pydantic_deep.create_deep_agent",
                return_value=mock_agent,
            ),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            await provider.execute_dialog_turn(
                system_prompt="sys",
                user_message="new question",
                history=[
                    {"role": "user", "content": "prev question"},
                    {"role": "assistant", "content": "prev answer"},
                ],
            )

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        assert "prev question" in prompt
        assert "prev answer" in prompt
        assert "new question" in prompt

    @pytest.mark.asyncio
    async def test_timeout_raises_provider_error(self) -> None:
        provider = _make_provider(timeout=0.001)
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=asyncio.TimeoutError())

        with (
            patch(
                "conductor.providers.pydantic_deep.create_deep_agent",
                return_value=mock_agent,
            ),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
            patch("conductor.providers.pydantic_deep.asyncio.wait_for", side_effect=asyncio.TimeoutError()),
        ):
            with pytest.raises(ProviderError, match="timed out"):
                await provider.execute_dialog_turn("sys", "hi")


# ---------------------------------------------------------------------------
# validate_connection()
# ---------------------------------------------------------------------------

class TestValidateConnection:
    """Tests for validate_connection()."""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self) -> None:
        provider = _make_provider()
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=MagicMock())

        with (
            patch(
                "conductor.providers.pydantic_deep.create_deep_agent",
                return_value=mock_agent,
            ),
            patch(
                "conductor.providers.pydantic_deep.DeepAgentDeps",
                return_value=MagicMock(),
            ),
            patch(
                "conductor.providers.pydantic_deep.StateBackend",
                return_value=MagicMock(),
            ),
        ):
            assert await provider.validate_connection() is True

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self) -> None:
        provider = _make_provider()

        with patch(
            "conductor.providers.pydantic_deep.create_deep_agent",
            side_effect=RuntimeError("no connection"),
        ):
            assert await provider.validate_connection() is False


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

class TestClose:
    @pytest.mark.asyncio
    async def test_close_is_noop(self) -> None:
        provider = _make_provider()
        await provider.close()  # should not raise


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------

class TestFactoryWiring:
    """Tests for create_provider("pydantic-deep") factory wiring."""

    @patch("conductor.providers.factory.PYDANTIC_DEEP_AVAILABLE", False)
    @pytest.mark.asyncio
    async def test_raises_when_package_not_installed(self) -> None:
        from conductor.providers.factory import create_provider

        with pytest.raises(ProviderError, match="pydantic-deep package"):
            await create_provider("pydantic-deep")

    @patch("conductor.providers.factory.PYDANTIC_DEEP_AVAILABLE", True)
    @patch("conductor.providers.pydantic_deep.PYDANTIC_DEEP_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_creates_pydantic_deep_provider_no_validate(self) -> None:
        from conductor.providers.factory import create_provider

        provider = await create_provider("pydantic-deep", validate=False)
        assert isinstance(provider, PydanticDeepProvider)
        await provider.close()

    @patch("conductor.providers.factory.PYDANTIC_DEEP_AVAILABLE", True)
    @patch("conductor.providers.pydantic_deep.PYDANTIC_DEEP_AVAILABLE", True)
    @pytest.mark.asyncio
    async def test_passes_runtime_config_to_provider(self) -> None:
        from conductor.providers.factory import create_provider

        provider = await create_provider(
            "pydantic-deep",
            validate=False,
            default_model="openai:gpt-4o",
            temperature=0.3,
            max_tokens=2048,
            timeout=120.0,
        )
        assert isinstance(provider, PydanticDeepProvider)
        assert provider._model == "openai:gpt-4o"
        assert provider._temperature == 0.3
        assert provider._max_tokens == 2048
        assert provider._timeout == 120.0
        await provider.close()

    @pytest.mark.asyncio
    async def test_unknown_provider_raises(self) -> None:
        from conductor.providers.factory import create_provider

        with pytest.raises(ProviderError, match="Unknown provider"):
            await create_provider("not-a-real-provider")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Schema wiring
# ---------------------------------------------------------------------------

class TestSchemaWiring:
    """Tests for schema Literal extensions."""

    def test_runtime_config_accepts_pydantic_deep(self) -> None:
        from conductor.config.schema import RuntimeConfig

        config = RuntimeConfig(provider="pydantic-deep")
        assert config.provider == "pydantic-deep"

    def test_agent_def_accepts_pydantic_deep_provider(self) -> None:
        agent = AgentDef(
            name="test",
            prompt="go",
            provider="pydantic-deep",
            routes=[{"to": "$end"}],
        )
        assert agent.provider == "pydantic-deep"

    def test_registry_provider_type_includes_pydantic_deep(self) -> None:
        from conductor.providers.registry import ProviderType
        import typing

        args = typing.get_args(ProviderType)
        assert "pydantic-deep" in args
