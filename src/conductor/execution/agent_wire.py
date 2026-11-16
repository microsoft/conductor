"""Serialize an agent invocation for the remote runner wire contract.

This module remains stdlib-only; version agreement with the runner protocol
is asserted by tests rather than importing its Pydantic models here.
"""

from __future__ import annotations

from typing import Any

from conductor.execution.types import AgentSpec

_DEFAULT_PROTOCOL_VERSION = 2

RUNNER_ROUTING_FEATURE = "copilot_model_routing"
"""Runner ``/health`` feature: the runner accepts ``agent.fusion_tier``/``auto_tier``."""


def _plain_credential(value: Any) -> Any:
    """Unwrap a secret wrapper at the transport boundary without importing it."""
    reveal = getattr(value, "get_secret_value", None)
    return reveal() if callable(reveal) else value


def agent_spec_to_wire_body(
    spec: AgentSpec, *, protocol_version: int = _DEFAULT_PROTOCOL_VERSION
) -> dict[str, Any]:
    """Build the request body, omitting all v2 keys for a v1 runner.

    This is the only place a transport adapter unwraps credential-bearing
    AgentSpec fields into a wire body. Callers must never log the result.
    """
    if protocol_version < 1:
        raise ValueError("runner protocol version must be positive")
    if protocol_version == 1 and (spec.temperature is not None or spec.max_tokens is not None):
        raise ValueError("runner protocol v1 cannot honor temperature or max_tokens")
    if protocol_version == 1 and spec.declared_tools == ():
        raise ValueError("runner protocol v1 cannot honor tools: []")
    if protocol_version == 1 and (spec.fusion_tier is not None or spec.auto_tier is not None):
        raise ValueError("runner protocol v1 cannot honor fusion_tier or auto_tier")

    body: dict[str, Any] = {
        "agent": {
            "name": spec.name,
            "model": spec.model,
            "system_prompt": spec.system_prompt,
            "output": dict(spec.output_schema) if spec.output_schema is not None else None,
            "tools": list(spec.declared_tools) if spec.declared_tools is not None else None,
            "max_agent_iterations": spec.max_agent_iterations,
            "max_session_seconds": spec.max_session_seconds,
            "reasoning_effort": spec.reasoning_effort,
            "temperature": spec.temperature,
            "max_tokens": spec.max_tokens,
            "working_dir": spec.working_dir,
            "retry": dict(spec.retry) if spec.retry is not None else None,
            "context_tier": spec.context_tier,
        },
        "rendered_prompt": spec.rendered_prompt,
        "tools": list(spec.tools) if spec.tools is not None else None,
        "mcp_servers": dict(spec.mcp_servers) if spec.mcp_servers is not None else None,
        "context": dict(spec.context),
        "inner_provider": spec.model_provider,
        "inner_provider_settings": (
            {key: _plain_credential(value) for key, value in spec.provider_credentials.items()}
            if spec.provider_credentials is not None
            else None
        ),
        "tool_output": dict(spec.tool_output) if spec.tool_output is not None else None,
    }
    # Routing preferences are sent only when configured, so a request without
    # them stays acceptable to strict runners that predate the fields.
    for key, value in (("fusion_tier", spec.fusion_tier), ("auto_tier", spec.auto_tier)):
        if value is not None:
            body["agent"][key] = value
    if protocol_version >= 2:
        body.update(
            execution_id=spec.execution_id,
            env_overlay=dict(spec.env_overlay) if spec.env_overlay is not None else None,
            skill_directories=list(spec.skill_directories) or None,
            custom_agents=[dict(agent) for agent in spec.custom_agents] or None,
        )
    else:
        body["agent"].pop("temperature")
        body["agent"].pop("max_tokens")
        body["agent"].pop("tools")
    return body
