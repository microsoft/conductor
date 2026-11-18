"""The stdlib agent serializer and the Pydantic runner boundary agree."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from conductor.execution.agent_wire import _DEFAULT_PROTOCOL_VERSION, agent_spec_to_wire_body
from conductor.execution.types import AgentSpec
from conductor.runner.protocol import (
    RUNNER_PROTOCOL_VERSION,
    RunnerAgentPayload,
    RunnerAgentRequest,
)


class _V1Request(BaseModel):
    """The eight fields a deployed v1 runner accepts on /execute."""

    model_config = ConfigDict(extra="forbid")

    agent: RunnerAgentPayload
    rendered_prompt: str
    tools: list[str] | None = None
    mcp_servers: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    inner_provider: str = "copilot"
    inner_provider_settings: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None


class _PreRoutingPayload(BaseModel):
    """The strict v2 agent payload accepted by runners that predate routing tiers."""

    model_config = ConfigDict(extra="forbid")

    name: str
    model: str | None = None
    system_prompt: str | None = None
    output: dict[str, Any] | None = None
    tools: list[str] | None = None
    max_agent_iterations: int | None = None
    max_session_seconds: float | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    working_dir: str | None = None
    retry: dict[str, Any] | None = None
    context_tier: str | None = None


def _spec() -> AgentSpec:
    return AgentSpec(
        name="review",
        execution_id="execution-42",
        model_provider="openai",
        model="gpt-4.1",
        rendered_prompt="Summarize.",
        output_schema={"answer": {"type": "string"}},
        tools=("read",),
        context={"count": 2},
        mcp_servers={"git": {"command": "echo"}},
        provider_credentials={"api_key": SecretStr("model-secret")},
        env_overlay={"MCP_TOKEN": "spawn-secret"},
        skill_directories=("/workspace/main/skills/review",),
        custom_agents=({"name": "reviewer", "prompt": "Review."},),
    )


# Requirement: every v2 serializer field is accepted by the current strict request model.
def test_v2_body_covers_request_and_payload_fields() -> None:
    assert _DEFAULT_PROTOCOL_VERSION == RUNNER_PROTOCOL_VERSION
    # Routing fields are sent only when set, so set both to cover every payload key.
    body = agent_spec_to_wire_body(replace(_spec(), fusion_tier="high", auto_tier="efficiency"))
    assert set(body) == set(RunnerAgentRequest.model_fields)
    parsed = RunnerAgentRequest.model_validate(body)
    assert set(body["agent"]) == set(RunnerAgentPayload.model_fields)
    assert parsed.env_overlay is not None
    secret = parsed.env_overlay["MCP_TOKEN"]
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == "spawn-secret"
    assert parsed.custom_agents == [{"name": "reviewer", "prompt": "Review."}]


# Requirement: a v1 runner forbids new keys even when they would be null.
def test_v1_body_is_accepted_by_the_pinned_v1_request_shape() -> None:
    v1_fields = (
        "agent",
        "rendered_prompt",
        "tools",
        "mcp_servers",
        "context",
        "inner_provider",
        "inner_provider_settings",
        "tool_output",
    )
    body = agent_spec_to_wire_body(_spec(), protocol_version=1)
    assert set(body) == set(v1_fields)
    assert set(_V1Request.model_fields) == set(v1_fields)
    assert _V1Request.model_validate(body).rendered_prompt == "Summarize."
    assert body["inner_provider_settings"] == {"api_key": "model-secret"}
    assert "temperature" not in body["agent"]
    assert "max_tokens" not in body["agent"]
    assert "tools" not in body["agent"]


def test_sampling_values_survive_spec_to_wire_parse() -> None:
    # Requirement: the strict runner request parses both effective sampling values.
    body = agent_spec_to_wire_body(replace(_spec(), temperature=0.27, max_tokens=4096))
    parsed = RunnerAgentRequest.model_validate(body)
    assert parsed.agent.temperature == 0.27
    assert parsed.agent.max_tokens == 4096


def test_declared_empty_tools_survive_spec_to_wire_parse() -> None:
    # Requirement: resolved and declared empty lists both survive the remote boundary.
    body = agent_spec_to_wire_body(replace(_spec(), tools=(), declared_tools=()))

    parsed = RunnerAgentRequest.model_validate(body)

    assert parsed.tools == []
    assert parsed.agent.tools == []


def test_v1_sampling_configuration_is_rejected() -> None:
    # Requirement: a v1 runner must not silently ignore configured sampling settings.
    with pytest.raises(ValueError, match="v1 cannot honor"):
        agent_spec_to_wire_body(replace(_spec(), temperature=0.27), protocol_version=1)


def test_v1_declared_empty_tools_are_rejected() -> None:
    # Requirement: an older runner must not widen explicit tools: [] to its default catalog.
    with pytest.raises(ValueError, match="v1 cannot honor tools"):
        agent_spec_to_wire_body(replace(_spec(), declared_tools=()), protocol_version=1)


def test_unset_routing_fields_are_omitted_for_older_runners() -> None:
    # Requirement: a request without routing tiers stays acceptable to a strict
    # runner that predates the fields.
    body = agent_spec_to_wire_body(_spec())

    assert "fusion_tier" not in body["agent"]
    assert "auto_tier" not in body["agent"]
    assert set(body["agent"]) == set(_PreRoutingPayload.model_fields)
    _PreRoutingPayload.model_validate(body["agent"])


@pytest.mark.parametrize(("field", "value"), [("fusion_tier", "max"), ("auto_tier", "efficiency")])
def test_routing_fields_survive_spec_to_wire_parse(field: str, value: str) -> None:
    # Requirement: a configured routing preference reaches the strict runner model intact.
    body = agent_spec_to_wire_body(replace(_spec(), **{field: value}))

    parsed = RunnerAgentRequest.model_validate(body)

    assert getattr(parsed.agent, field) == value
    other = "auto_tier" if field == "fusion_tier" else "fusion_tier"
    assert other not in body["agent"]
    with pytest.raises(ValueError, match="extra"):
        _PreRoutingPayload.model_validate(body["agent"])


@pytest.mark.parametrize("field", ["fusion_tier", "auto_tier"])
def test_v1_routing_configuration_is_rejected(field: str) -> None:
    # Requirement: a v1 runner must not silently drop a requested routing preference.
    with pytest.raises(ValueError, match="v1 cannot honor fusion_tier or auto_tier"):
        agent_spec_to_wire_body(replace(_spec(), **{field: "high"}), protocol_version=1)
