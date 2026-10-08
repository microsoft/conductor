"""The stdlib agent serializer and the Pydantic runner boundary agree."""

from __future__ import annotations

from typing import Any

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
    body = agent_spec_to_wire_body(_spec())
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
