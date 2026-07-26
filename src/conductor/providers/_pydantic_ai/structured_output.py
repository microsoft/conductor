"""Structured output post-processing for the Pydantic AI provider.

Pydantic-ai is configured to request structured output via ``ToolOutput`` (the
default for non-text schemas in this provider). After a successful run, the
result's output is a validated Pydantic ``BaseModel`` instance. This module
converts that instance into a plain dict and runs it through Conductor's own
``validate_output()`` so that the final ``AgentOutput.content`` matches the
shape and semantics produced by the current ClaudeProvider.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from conductor.config.schema import OutputField
from conductor.exceptions import ProviderError, ValidationError
from conductor.executor.output import parse_json_output, validate_output

logger = logging.getLogger(__name__)


def extract_content(
    output: Any,
    output_schema: dict[str, OutputField] | None,
    agent_name: str,
) -> dict[str, Any]:
    """Convert a pydantic-ai ``result.output`` into a validated content dict.

    For structured output, the model output is dumped to a dict and passed
    through Conductor's ``validate_output()``. This enforces Conductor-level
    semantics (required fields, scalar type checks, extra keys ignored) on top
    of pydantic-ai's own tool-output validation.

    For plain text output (no schema), the result is wrapped in
    ``{"result": text}`` to match ClaudeProvider's text extraction contract.

    Args:
        output: The value returned as ``result.output``.
        output_schema: Expected Conductor output schema, or ``None`` for text.
        agent_name: Name of the agent, used in error messages.

    Returns:
        Validated content dict ready for ``AgentOutput.content``.

    Raises:
        ValidationError: If the structured output fails Conductor-level validation.
        ProviderError: If structured output is requested but the result is not a
            Pydantic model instance or a string fallback.
    """
    if output_schema is None:
        return _wrap_text_output(output)

    if isinstance(output, BaseModel):
        content = output.model_dump()
        validate_output(content, output_schema)
        return content

    if isinstance(output, str):
        return parse_text_fallback(output, output_schema, agent_name)

    raise ProviderError(
        f"Agent '{agent_name}' produced non-structured output for a structured schema",
        suggestion="Ensure the model calls the output tool or returns valid JSON",
    )


def _wrap_text_output(output: Any) -> dict[str, Any]:
    """Wrap plain text output in the standard Conductor result dict."""
    if isinstance(output, BaseModel):
        return output.model_dump()
    if isinstance(output, str):
        return {"result": output}
    return {"result": output}


def parse_text_fallback(
    text: str,
    output_schema: dict[str, OutputField],
    agent_name: str,
) -> dict[str, Any]:
    """Recover structured output from a plain text response.

    Mirrors ClaudeProvider's JSON fallback: when the model returns prose or a
    JSON blob instead of using the structured output tool, this helper uses the
    same ``parse_json_output()`` function from ``executor/output.py`` to extract
    a dict and then validates it against the schema.

    Args:
        text: Raw text response from the model.
        output_schema: Expected Conductor output schema.
        agent_name: Name of the agent, used in error messages.

    Returns:
        Validated content dict.

    Raises:
        ProviderError: If no valid JSON matching the schema can be extracted.
        ValidationError: If extracted JSON fails schema validation.
    """
    try:
        content = parse_json_output(text)
    except ValidationError as e:
        raise ProviderError(
            f"Agent '{agent_name}' returned text that could not be parsed as JSON",
            suggestion=f"{e.suggestion} Ensure the model uses the structured output tool.",
        ) from e

    validate_output(content, output_schema)
    return content
