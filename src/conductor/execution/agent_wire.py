"""Serialize an agent invocation for the remote runner wire contract.

This module remains stdlib-only; version agreement with the runner protocol
is asserted by tests rather than importing its Pydantic models here.
"""

from __future__ import annotations

from typing import Any

from conductor.execution.types import AgentSpec

_DEFAULT_PROTOCOL_VERSION = 2


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

    body: dict[str, Any] = {
        "agent": {
            "name": spec.name,
            "model": spec.model,
            "system_prompt": spec.system_prompt,
            "output": dict(spec.output_schema) if spec.output_schema is not None else None,
            "max_agent_iterations": spec.max_agent_iterations,
            "max_session_seconds": spec.max_session_seconds,
            "reasoning_effort": spec.reasoning_effort,
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
    if protocol_version >= 2:
        body.update(
            execution_id=spec.execution_id,
            env_overlay=dict(spec.env_overlay) if spec.env_overlay is not None else None,
            skill_directories=list(spec.skill_directories) or None,
            custom_agents=[dict(agent) for agent in spec.custom_agents] or None,
        )
    return body
