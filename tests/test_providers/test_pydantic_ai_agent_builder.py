"""Unit tests for building a Pydantic AI Agent from a Conductor AgentDef.

Tests verify that build_agent() maps Conductor agent configuration to Pydantic
AI constructs with parity to the existing Claude provider: model resolution,
system prompt wiring, structured output via ToolOutput, sampling settings,
extended-thinking budgets, and Anthropic API constraint coercion.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.output import ToolOutput

from conductor.config.schema import AgentDef, OutputField, ReasoningConfig
from conductor.exceptions import ValidationError
from conductor.providers._pydantic_ai.agent_builder import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OPENAI_MODEL,
    build_agent,
)


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide dummy API keys so model construction succeeds for both backends."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _extract_output_model(agent: Agent[Any, Any]) -> type[BaseModel] | None:
    """Return the wrapped Pydantic model when structured output is configured."""
    if isinstance(agent.output_type, ToolOutput):
        return agent.output_type.output  # type: ignore[return-value]
    return None


def _extract_output_tool_schema(agent: Agent[Any, Any]) -> dict[str, Any] | None:
    """Return the sanitized parameters_json_schema for the output tool."""
    if not isinstance(agent.output_type, ToolOutput):
        return None
    toolset = agent._output_schema.toolset
    if toolset is None or not toolset._tool_defs:
        return None
    return toolset._tool_defs[0].parameters_json_schema


def _assert_no_keys(node: Any, *keys: str) -> None:
    """Recursively assert that none of the given JSON Schema keys appear."""
    if isinstance(node, dict):
        for key in keys:
            assert key not in node, f"forbidden key {key!r} found in schema: {node}"
        for value in node.values():
            _assert_no_keys(value, *keys)
    elif isinstance(node, list):
        for item in node:
            _assert_no_keys(item, *keys)


def _extract_model_name(agent: Agent[Any, Any]) -> str:
    """Return the underlying model name from a built agent."""
    assert isinstance(agent.model, AnthropicModel | OpenAIChatModel)
    return agent.model.model_name


class TestModelMapping:
    """Tests for resolving the Anthropic model identifier."""

    def test_agent_model_is_used_when_present(self) -> None:
        """agent.model must be forwarded to AnthropicModel.model_name."""
        agent_def = AgentDef(name="mapper", model="claude-3-7-sonnet-latest")

        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")

        assert _extract_model_name(pydantic_agent) == "claude-3-7-sonnet-latest"

    def test_default_model_falls_back_when_agent_model_missing(self) -> None:
        """The default_model parameter must be used when agent.model is None."""
        agent_def = AgentDef(name="mapper")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            default_model="claude-opus-4-20250514",
        )

        assert _extract_model_name(pydantic_agent) == "claude-opus-4-20250514"

    def test_default_constant_used_when_no_model_anywhere(self) -> None:
        """The module-level default must be used when no model is supplied."""
        agent_def = AgentDef(name="mapper")

        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")

        assert _extract_model_name(pydantic_agent) == DEFAULT_ANTHROPIC_MODEL


class TestOpenAIModelMapping:
    """Tests for resolving the OpenAI model identifier."""

    def test_openai_agent_model_is_used_when_present(self) -> None:
        """agent.model must be forwarded to OpenAIChatModel.model_name."""
        agent_def = AgentDef(name="mapper", model="gpt-5")

        pydantic_agent = build_agent(
            agent_def, system_prompt="", rendered_prompt="", backend="openai"
        )

        assert _extract_model_name(pydantic_agent) == "gpt-5"
        assert isinstance(pydantic_agent.model, OpenAIChatModel)

    def test_openai_default_model_falls_back_when_agent_model_missing(self) -> None:
        """The default_model parameter must be used when agent.model is None."""
        agent_def = AgentDef(name="mapper")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            backend="openai",
            default_model="gpt-5",
        )

        assert _extract_model_name(pydantic_agent) == "gpt-5"

    def test_openai_default_constant_used_when_no_model_anywhere(self) -> None:
        """The module-level default must be used when no model is supplied."""
        agent_def = AgentDef(name="mapper")

        pydantic_agent = build_agent(
            agent_def, system_prompt="", rendered_prompt="", backend="openai"
        )

        assert _extract_model_name(pydantic_agent) == DEFAULT_OPENAI_MODEL
        assert isinstance(pydantic_agent.model, OpenAIChatModel)


class TestOpenAIBackend:
    """Tests specific to the openai backend branch."""

    def test_openai_output_schema_becomes_tool_output(self) -> None:
        """OpenAI backend must wrap a non-empty output schema in ToolOutput."""
        agent_def = AgentDef(
            name="formatter",
            output={"answer": OutputField(type="string")},
        )

        pydantic_agent = build_agent(
            agent_def, system_prompt="", rendered_prompt="", backend="openai"
        )

        assert isinstance(pydantic_agent.model, OpenAIChatModel)
        assert isinstance(pydantic_agent.output_type, ToolOutput)

    def test_openai_sampling_settings(self) -> None:
        """OpenAI model settings must carry temperature, max_tokens and timeout."""
        agent_def = AgentDef(name="sampler")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            backend="openai",
            default_temperature=0.7,
            default_max_tokens=4096,
            timeout=120.0,
        )

        assert isinstance(pydantic_agent.model, OpenAIChatModel)
        assert pydantic_agent.model_settings.get("temperature") == 0.7
        assert pydantic_agent.model_settings.get("max_tokens") == 4096
        assert pydantic_agent.model_settings.get("timeout") == 120.0

    @pytest.mark.parametrize(
        ("effort",),
        [("low",), ("medium",), ("high",)],
    )
    def test_openai_reasoning_effort_maps_to_openai_reasoning_effort(self, effort: str) -> None:
        """Each supported reasoning effort level must be forwarded as openai_reasoning_effort."""
        agent_def = AgentDef(
            name="reasoner",
            model="gpt-5-mini",
            reasoning=ReasoningConfig(effort=effort),  # type: ignore[arg-type]
        )

        with patch(
            "conductor.providers._pydantic_ai.agent_builder._openai_model_supports_reasoning",
            return_value=True,
        ):
            pydantic_agent = build_agent(
                agent_def, system_prompt="", rendered_prompt="", backend="openai"
            )

        assert pydantic_agent.model_settings.get("openai_reasoning_effort") == effort

    def test_openai_reasoning_effort_rejected_on_non_reasoning_model(self) -> None:
        """Requirement: reasoning effort on a non-reasoning model raises ValidationError.

        The shared helper reports False for the model, so build_agent must fail fast.
        """
        agent_def = AgentDef(
            name="reasoner",
            model="gpt-4o",
            reasoning=ReasoningConfig(effort="low"),
        )

        with patch(
            "conductor.providers._pydantic_ai.agent_builder._openai_model_supports_reasoning",
            return_value=False,
        ), pytest.raises(ValidationError, match="does not support reasoning.effort"):
            build_agent(agent_def, system_prompt="", rendered_prompt="", backend="openai")

    def test_openai_reasoning_effort_accepted_on_reasoning_model(self) -> None:
        """Requirement: reasoning effort on a reasoning-capable model succeeds."""
        agent_def = AgentDef(
            name="reasoner",
            model="o3-mini",
            reasoning=ReasoningConfig(effort="high"),
        )

        with patch(
            "conductor.providers._pydantic_ai.agent_builder._openai_model_supports_reasoning",
            return_value=True,
        ):
            pydantic_agent = build_agent(
                agent_def, system_prompt="", rendered_prompt="", backend="openai"
            )

        assert pydantic_agent.model_settings.get("openai_reasoning_effort") == "high"

    def test_openai_reasoning_validated_against_default_model_when_agent_model_unset(self) -> None:
        """Requirement: the reasoning-support check must use the workflow's default_model
        when agent.model is unset, not the hardcoded library default."""
        agent_def = AgentDef(
            name="reasoner",
            reasoning=ReasoningConfig(effort="low"),
        )

        with patch(
            "conductor.providers._pydantic_ai.agent_builder._openai_model_supports_reasoning",
            return_value=True,
        ):
            pydantic_agent = build_agent(
                agent_def,
                system_prompt="",
                rendered_prompt="",
                backend="openai",
                default_model="o3-mini",
            )

        assert pydantic_agent.model_settings.get("openai_reasoning_effort") == "low"

    def test_openai_custom_base_url_without_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom base_url without an explicit api_key must raise ValidationError."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        agent_def = AgentDef(name="custom_endpoint")

        with pytest.raises(ValidationError):
            build_agent(
                agent_def,
                system_prompt="",
                rendered_prompt="",
                backend="openai",
                base_url="http://localhost:11434/v1",
            )

    def test_openai_explicit_api_key_allows_custom_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom base_url is allowed when api_key is passed explicitly."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        agent_def = AgentDef(name="custom_endpoint")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            backend="openai",
            api_key="explicit-key",
            base_url="http://localhost:11434/v1",
        )

        assert isinstance(pydantic_agent.model, OpenAIChatModel)
        assert str(pydantic_agent.model.client.base_url).rstrip("/") == "http://localhost:11434/v1"

    def test_openai_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Building an openai agent without OPENAI_API_KEY or api_key must raise."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        agent_def = AgentDef(name="unauthenticated")

        with pytest.raises(ValidationError):
            build_agent(agent_def, system_prompt="", rendered_prompt="", backend="openai")

    def test_openai_http_client_is_forwarded(self) -> None:
        """A provided httpx.AsyncClient must be forwarded to the OpenAI client."""
        agent_def = AgentDef(name="shared_client")
        shared = httpx.AsyncClient()

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            backend="openai",
            api_key="explicit-key",
            http_client=shared,
        )

        assert isinstance(pydantic_agent.model, OpenAIChatModel)
        assert pydantic_agent.model.client._client._transport is shared._transport


class TestSystemPromptMapping:
    """Tests for mapping the rendered system prompt."""

    def test_system_prompt_is_set(self) -> None:
        """The rendered Conductor system_prompt must be passed as the Pydantic AI
        system_prompt parameter (Anthropic system role)."""
        agent_def = AgentDef(name="speaker", system_prompt="Original template")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="Rendered instructions",
            rendered_prompt="User task",
        )

        assert pydantic_agent._system_prompts == ("Rendered instructions",)


class TestOutputMapping:
    """Tests for mapping the agent output schema to structured output."""

    def test_output_schema_becomes_tool_output(self) -> None:
        """A non-empty output schema must be wrapped in ToolOutput."""
        agent_def = AgentDef(
            name="formatter",
            output={"answer": OutputField(type="string")},
        )

        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")

        assert isinstance(pydantic_agent.output_type, ToolOutput)
        output_model = _extract_output_model(pydantic_agent)
        assert output_model is not None
        instance = output_model(answer="42")
        assert instance.answer == "42"

    def test_empty_output_schema_falls_back_to_text(self) -> None:
        """An empty or missing output schema must produce text output (str)."""
        agent_def = AgentDef(name="chatter")

        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")

        assert pydantic_agent.output_type is str


class TestSamplingSettings:
    """Tests for temperature and max_tokens mapping."""

    def test_temperature_and_max_tokens_in_model_settings(self) -> None:
        """Runtime defaults for temperature and max_tokens must appear in the
        agent model_settings."""
        agent_def = AgentDef(name="sampler")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            default_temperature=0.7,
            default_max_tokens=4096,
        )

        assert pydantic_agent.model_settings["temperature"] == 0.7
        assert pydantic_agent.model_settings["max_tokens"] == 4096


class TestReasoningMapping:
    """Tests for mapping reasoning effort to Anthropic extended thinking."""

    @pytest.mark.parametrize(
        ("effort", "expected_budget"),
        [
            ("low", 2048),
            ("medium", 8192),
            ("high", 16384),
            ("xhigh", 32768),
            ("max", 59904),
        ],
    )
    def test_reasoning_effort_maps_to_budget(self, effort: str, expected_budget: int) -> None:
        """Each reasoning effort level must map to the correct Anthropic
        budget_tokens value in anthropic_thinking."""
        agent_def = AgentDef(
            name="thinker",
            model="claude-3-7-sonnet-latest",
            reasoning=ReasoningConfig(effort=effort),  # type: ignore[arg-type]
        )

        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")

        thinking = pydantic_agent.model_settings["anthropic_thinking"]
        assert thinking == {"type": "enabled", "budget_tokens": expected_budget}

    def test_reasoning_coerces_temperature_and_bumps_max_tokens(self) -> None:
        """When reasoning is enabled on a thinking model, temperature must be
        coerced to 1.0 and max_tokens must be bumped above the budget."""
        agent_def = AgentDef(
            name="thinker",
            model="claude-3-7-sonnet-latest",
            reasoning=ReasoningConfig(effort="low"),
        )

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            default_temperature=0.5,
            default_max_tokens=1024,
        )

        assert pydantic_agent.model_settings["temperature"] == 1.0
        assert pydantic_agent.model_settings["max_tokens"] == 6144
        thinking = pydantic_agent.model_settings["anthropic_thinking"]
        assert thinking == {"type": "enabled", "budget_tokens": 2048}

    def test_reasoning_on_non_thinking_model_raises(self) -> None:
        """Requesting reasoning on a non-thinking model must raise a clear
        ValidationError matching the current Claude provider behavior."""
        agent_def = AgentDef(
            name="thinker",
            model="claude-3-5-sonnet-latest",
            reasoning=ReasoningConfig(effort="low"),
        )

        with pytest.raises(ValidationError):
            build_agent(agent_def, system_prompt="", rendered_prompt="")

    def test_reasoning_validated_against_default_model_when_agent_model_unset(self) -> None:
        """Requirement: the thinking-support check must use the workflow's
        default_model when agent.model is unset, not the hardcoded library
        default, so a thinking-capable default model is accepted."""
        agent_def = AgentDef(
            name="thinker",
            reasoning=ReasoningConfig(effort="low"),
        )

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            default_model="claude-3-7-sonnet-latest",
        )

        thinking = pydantic_agent.model_settings["anthropic_thinking"]
        assert thinking == {"type": "enabled", "budget_tokens": 2048}

    def test_reasoning_rejected_against_non_thinking_default_model(self) -> None:
        """Requirement: a non-thinking default_model must also be honored, so a
        workflow whose default model lacks thinking support gets a
        ValidationError instead of silently validating against the library
        default."""
        agent_def = AgentDef(
            name="thinker",
            reasoning=ReasoningConfig(effort="low"),
        )

        with pytest.raises(ValidationError):
            build_agent(
                agent_def,
                system_prompt="",
                rendered_prompt="",
                default_model="claude-3-haiku-20240307",
            )


class TestRetries:
    """Tests for Pydantic AI retry budget configuration."""

    def test_tool_retries_zero_output_retries_enabled(self) -> None:
        """Pydantic AI tool retries must be disabled so Conductor-level retry is
        the only retry mechanism, but output retries must be enabled so
        structured-output agents can recover from plain-text responses."""
        agent_def = AgentDef(name="single-shot")

        pydantic_agent = build_agent(agent_def, system_prompt="", rendered_prompt="")

        # pydantic-ai exposes retries via _max_output_retries / _max_tool_retries
        assert pydantic_agent._max_tool_retries == 0
        assert pydantic_agent._max_output_retries == 2

    def test_custom_output_retries_preserve_zero_tool_retries(self) -> None:
        """A custom recovery budget must not enable Pydantic AI tool retries."""
        agent_def = AgentDef(name="custom-recovery")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            max_parse_recovery_attempts=4,
        )

        assert pydantic_agent._max_tool_retries == 0
        assert pydantic_agent._max_output_retries == 4


class TestArrayItemConstraintOutputRetry:
    """Requirement: array-item constraint violations trigger pydantic-ai output retries."""

    @pytest.mark.asyncio
    async def test_array_item_constraint_violation_triggers_output_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the model first returns a tool call with an array item that
        violates an enum/pattern constraint, pydantic-ai's output retry must
        recover and return valid structured output after exactly one retry."""
        calls: list[int] = []

        async def _fake_model(
            messages: list[Any],
            info: Any,
        ) -> ModelResponse:
            calls.append(len(calls))
            output_tool_name = info.output_tools[0].name
            if len(calls) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name=output_tool_name,
                            args={"values": ["bad"]},
                        )
                    ]
                )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=output_tool_name,
                        args={"values": ["ok"]},
                    )
                ]
            )

        monkeypatch.setattr(
            "conductor.providers._pydantic_ai.agent_builder._resolve_anthropic_model",
            lambda *_args, **_kwargs: FunctionModel(_fake_model),
        )

        agent_def = AgentDef(
            name="formatter",
            output={
                "values": OutputField(
                    type="array",
                    items=OutputField(
                        type="string",
                        enum=["ok"],
                        pattern="^o.*$",
                        minLength=2,
                        maxLength=2,
                    ),
                )
            },
        )
        pydantic_agent = build_agent(agent_def, system_prompt="sys", rendered_prompt="go")

        result = await pydantic_agent.run("go")

        assert len(calls) == 2
        assert result.output.values == ["ok"]


class TestOutputRecovery:
    """Regression tests for structured-output recovery from plain-text responses."""

    @pytest.mark.asyncio
    async def test_plain_text_response_recovered_via_output_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the model first answers with plain text instead of calling the
        structured-output tool, pydantic-ai's output retry must recover and
        return parsed structured output."""
        calls: list[int] = []

        async def _fake_model(
            messages: list[Any],
            info: Any,
        ) -> ModelResponse:
            calls.append(len(calls))
            output_tool_name = info.output_tools[0].name
            if len(calls) == 1:
                return ModelResponse(parts=[TextPart(content='{"passed": true}')])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=output_tool_name,
                        args={"passed": True, "issues": "no issues"},
                    )
                ]
            )

        monkeypatch.setattr(
            "conductor.providers._pydantic_ai.agent_builder._resolve_anthropic_model",
            lambda *_args, **_kwargs: FunctionModel(_fake_model),
        )

        agent_def = AgentDef(
            name="validator",
            output={
                "passed": OutputField(type="boolean"),
                "issues": OutputField(type="string"),
            },
        )
        pydantic_agent = build_agent(agent_def, system_prompt="sys", rendered_prompt="go")

        result = await pydantic_agent.run("go")

        assert len(calls) == 2
        assert result.output.passed is True
        assert result.output.issues == "no issues"

    @pytest.mark.asyncio
    async def test_zero_output_retries_raises_on_plain_text(self) -> None:
        """With output retries disabled, a plain-text answer to a tool-output
        schema must raise ``UnexpectedModelBehavior`` immediately."""

        async def _fake_model(
            messages: list[Any],
            info: Any,
        ) -> ModelResponse:
            return ModelResponse(parts=[TextPart(content='{"passed": true}')])

        agent_def = AgentDef(
            name="validator",
            output={
                "passed": OutputField(type="boolean"),
                "issues": OutputField(type="string"),
            },
        )
        output_type = build_agent(agent_def, system_prompt="", rendered_prompt="").output_type

        agent = Agent(
            model=FunctionModel(_fake_model),
            output_type=output_type,
            retries={"tools": 0, "output": 0},
        )

        with pytest.raises(UnexpectedModelBehavior):
            await agent.run("go")


class TestConstraintOutputRetry:
    """Tests that structured-output constraint violations trigger pydantic-ai output retries."""

    @pytest.mark.asyncio
    async def test_constraint_violation_triggers_output_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the model first returns a tool call that violates an output
        constraint, pydantic-ai's output retry must recover and return valid
        structured output after exactly one retry."""
        calls: list[int] = []

        async def _fake_model(
            messages: list[Any],
            info: Any,
        ) -> ModelResponse:
            calls.append(len(calls))
            output_tool_name = info.output_tools[0].name
            if len(calls) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name=output_tool_name,
                            args={"value": "bad"},
                        )
                    ]
                )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=output_tool_name,
                        args={"value": "abc"},
                    )
                ]
            )

        monkeypatch.setattr(
            "conductor.providers._pydantic_ai.agent_builder._resolve_anthropic_model",
            lambda *_args, **_kwargs: FunctionModel(_fake_model),
        )

        agent_def = AgentDef(
            name="formatter",
            output={
                "value": OutputField(
                    type="string",
                    enum=["abc"],
                    pattern=r"^a",
                    minLength=3,
                    maxLength=3,
                )
            },
        )
        pydantic_agent = build_agent(agent_def, system_prompt="sys", rendered_prompt="go")

        result = await pydantic_agent.run("go")

        assert len(calls) == 2
        assert result.output.value == "abc"
        # Inspect the message history to confirm one output retry took place.
        messages = result.all_messages()
        model_responses = [m for m in messages if isinstance(m, ModelResponse)]
        assert len(model_responses) == 2


class TestApiKey:
    """Tests for API key and auth token resolution."""

    def test_missing_api_key_and_auth_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Building an agent without an API key or auth token must raise ValidationError."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        agent_def = AgentDef(name="unauthenticated")

        with pytest.raises(ValidationError):
            build_agent(agent_def, system_prompt="", rendered_prompt="")

    def test_explicit_api_key_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit api_key argument must be used even when the env var is absent."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        agent_def = AgentDef(name="authenticated")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            api_key="explicit-key",
        )

        assert isinstance(pydantic_agent.model, AnthropicModel)

    def test_auth_token_only_no_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An auth_token alone must satisfy authentication with no API key set."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        agent_def = AgentDef(name="bearer_authenticated")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            auth_token="bearer-token",
        )

        assert isinstance(pydantic_agent.model, AnthropicModel)


class TestClientConstruction:
    """Tests that Anthropic SDK client construction matches Conductor semantics."""

    def test_transport_retries_are_disabled(self) -> None:
        """The Anthropic client is built with max_retries=0; only Conductor's
        retry layer must retry."""
        agent_def = AgentDef(name="no_double_retry")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            api_key="explicit-key",
        )

        assert isinstance(pydantic_agent.model, AnthropicModel)
        # The AnthropicProvider was created from an explicit AsyncAnthropic client
        # built with max_retries=0.
        assert pydantic_agent.model.client.max_retries == 0

    def test_timeout_is_forwarded(self) -> None:
        """The provided timeout must reach the Anthropic SDK client."""
        agent_def = AgentDef(name="timeout_agent")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            api_key="explicit-key",
            timeout=120.0,
        )

        assert isinstance(pydantic_agent.model, AnthropicModel)
        assert pydantic_agent.model.client.timeout == 120.0

    def test_auth_token_reaches_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The provided auth_token must reach the Anthropic SDK client as the sole credential."""
        # Requirement: an explicit auth_token is sent as Authorization: Bearer,
        # and the ambient ANTHROPIC_API_KEY must not ride along (the Anthropic
        # SDK sends both headers when both credentials are set).
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        agent_def = AgentDef(name="token_agent")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            auth_token="bearer-token",
        )

        assert isinstance(pydantic_agent.model, AnthropicModel)
        assert pydantic_agent.model.client.auth_token == "bearer-token"
        assert pydantic_agent.model.client.auth_headers == {"Authorization": "Bearer bearer-token"}

    def test_api_key_reaches_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The provided api_key must reach the Anthropic SDK client as the sole credential."""
        # Requirement: an explicit api_key is sent as X-Api-Key, and an ambient
        # ANTHROPIC_AUTH_TOKEN must not ride along (SDK unit semantics).
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-token")
        agent_def = AgentDef(name="key_agent")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            api_key="sk-explicit",
        )

        assert isinstance(pydantic_agent.model, AnthropicModel)
        assert pydantic_agent.model.client.auth_headers == {"X-Api-Key": "sk-explicit"}

    def test_explicit_auth_token_suppresses_ambient_env_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit auth_token must not mix with an ambient ANTHROPIC_API_KEY."""
        # Requirement: credentials resolve as a unit (SDK semantics) — an
        # explicit credential disables env credential resolution, otherwise the
        # ambient key would leak to whatever base_url points at via X-Api-Key.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
        agent_def = AgentDef(name="unit_agent")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            auth_token="bearer-token",
        )

        assert isinstance(pydantic_agent.model, AnthropicModel)
        assert pydantic_agent.model.client.auth_headers == {"Authorization": "Bearer bearer-token"}

    def test_both_credentials_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Setting both credentials must log a both-headers-sent warning."""
        # Requirement: when both credentials are effectively set, Conductor
        # warns instead of silently shipping both auth headers (parity with
        # the Copilot provider's api_key/bearer_token warning).
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        agent_def = AgentDef(name="both_agent")

        with caplog.at_level("WARNING"):
            build_agent(
                agent_def,
                system_prompt="",
                rendered_prompt="",
                api_key="sk-explicit",
                auth_token="bearer-token",
            )

        assert any("Both api_key and auth_token" in r.message for r in caplog.records)


class TestToolSchemaSanitization:
    """Regression tests for the JSON schema attached to the Pydantic AI output tool."""

    def test_nullable_ranged_number_advertises_null_and_strips_internal_keys(self) -> None:
        """A nullable number with minimum/maximum must be advertised to the model
        as a type list that includes ``null``, must keep the ``minimum``/``maximum``
        keywords, and must never expose pydantic-internal ``ge``/``le``/``default``
        keys."""
        agent_def = AgentDef(
            name="ratio",
            output={
                "ratio": OutputField(
                    type="number",
                    minimum=0,
                    maximum=10,
                    nullable=True,
                )
            },
        )

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="sys",
            rendered_prompt="p",
            default_model="claude-sonnet-5",
            api_key="sk-test-dummy",
        )

        schema = _extract_output_tool_schema(pydantic_agent)
        assert schema is not None
        ratio_schema = schema["properties"]["ratio"]
        assert "type" in ratio_schema
        assert isinstance(ratio_schema["type"], list)
        assert "null" in ratio_schema["type"]
        assert ratio_schema["minimum"] == 0
        assert ratio_schema["maximum"] == 10
        _assert_no_keys(schema, "ge", "le", "default")

    def test_ranged_number_never_exposes_ge_le(self) -> None:
        """A non-nullable ranged number must advertise ``minimum``/``maximum``
        while keeping the raw ``ge``/``le`` keys out of the tool schema."""
        agent_def = AgentDef(
            name="score",
            output={
                "score": OutputField(
                    type="number",
                    minimum=0,
                    maximum=10,
                )
            },
        )

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="sys",
            rendered_prompt="p",
            default_model="claude-sonnet-5",
            api_key="sk-test-dummy",
        )

        schema = _extract_output_tool_schema(pydantic_agent)
        assert schema is not None
        score_schema = schema["properties"]["score"]
        # NumberType is ``int | float``, so the non-nullable schema keeps pydantic's
        # ``anyOf: [integer, number]`` shape rather than a single ``type``.
        assert "anyOf" in score_schema
        assert score_schema["minimum"] == 0
        assert score_schema["maximum"] == 10
        _assert_no_keys(schema, "ge", "le", "default")
