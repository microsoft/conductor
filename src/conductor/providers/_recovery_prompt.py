"""Shared parse-recovery prompt for providers that re-prompt in plain text.

Copilot and Hermes correct an unusable structured response the same way: send
the model a follow-up message carrying the error, a snippet of what it
returned, and the schema it was supposed to match. That wording is covered by
the provider-parity rule in AGENTS.md, so it lives here rather than in two
copies free to drift apart.

Claude is deliberately not a caller. It re-prompts through its ``emit_output``
tool and never echoes the schema back, so its instruction text is genuinely
different and stays in ``claude.py``.
"""

from __future__ import annotations

import json
from typing import Any

_MAX_RESPONSE_CHARS = 500


def build_parse_recovery_prompt(
    parse_error: str,
    original_response: str,
    schema: dict[str, Any],
    *,
    is_schema_failure: bool = False,
) -> str:
    """Build a prompt to recover from a JSON parse or schema-shape failure.

    Args:
        parse_error: The error from the parse or validation attempt.
        original_response: The model's rejected response. Truncated so a long
            answer cannot crowd out the schema.
        schema: Expected output schema, rendered into the prompt.
        is_schema_failure: True when the response parsed cleanly but a field
            had the wrong type. Telling a model its valid JSON "could not be
            parsed" invites it to re-send the same payload.

    Returns:
        A prompt asking the model to correct its response.
    """
    truncated = original_response[:_MAX_RESPONSE_CHARS]
    if len(original_response) > _MAX_RESPONSE_CHARS:
        truncated += "..."

    if is_schema_failure:
        opening = (
            "Your previous response was valid JSON but did not match the "
            "required output schema.\n\n"
            f"**Schema Error:** {parse_error}\n\n"
        )
        closing = (
            "Please respond with ONLY a valid JSON object matching the schema above, "
            "paying close attention to the type of each field. Return scalar values "
            "directly rather than wrapping them in an object. Do NOT include markdown "
            "code blocks, explanatory text, or anything other than the raw JSON object."
        )
    else:
        opening = (
            "Your previous response could not be parsed as valid JSON.\n\n"
            f"**Parse Error:** {parse_error}\n\n"
        )
        closing = (
            "Please respond with ONLY a valid JSON object matching the schema above. "
            "Do NOT include markdown code blocks, explanatory text, or anything other "
            "than the raw JSON object."
        )

    return (
        f"{opening}"
        f"**Your response started with:**\n```\n{truncated}\n```\n\n"
        f"**Expected JSON schema:**\n```json\n{json.dumps(schema, indent=2)}\n```\n\n"
        f"{closing}"
    )
