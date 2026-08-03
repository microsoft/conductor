"""Unit tests for building a Pydantic AI Agent from a Conductor AgentDef.

Tests verify that build_agent() maps Conductor agent configuration to Pydantic
AI constructs with parity to the existing Claude provider: model resolution,
system prompt wiring, structured output via ToolOutput, sampling settings,
extended-thinking budgets, and Anthropic API constraint coercion.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.output import ToolOutput

from conductor.config.schema import AgentDef, OutputField, ReasoningConfig
from conductor.exceptions import ValidationError
from conductor.providers._pydantic_ai.agent_builder import (
    DEFAULT_ANTHROPIC_MODEL,
    build_agent,
)


@pytest.fixture(autouse=True)
def _ensure_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy API key so AnthropicModel construction succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _extract_model_name(agent: Agent[Any, Any]) -> str:
    """Return the underlying Anthropic model name from a built agent."""
    assert isinstance(agent.model, AnthropicModel)
    return agent.model.model_name


def _extract_output_model(agent: Agent[Any, Any]) -> type[BaseModel] | None:
    """Return the wrapped Pydantic model when structured output is configured."""
    if isinstance(agent.output_type, ToolOutput):
        return agent.output_type.output  # type: ignore[return-value]
    return None


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

    def test_auth_token_reaches_client(self) -> None:
        """The provided auth_token must reach the Anthropic SDK client."""
        agent_def = AgentDef(name="token_agent")

        pydantic_agent = build_agent(
            agent_def,
            system_prompt="",
            rendered_prompt="",
            auth_token="bearer-token",
        )

        assert isinstance(pydantic_agent.model, AnthropicModel)
        assert pydantic_agent.model.client.auth_token == "bearer-token"
