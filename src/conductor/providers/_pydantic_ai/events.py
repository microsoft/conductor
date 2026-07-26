"""Map Pydantic AI streaming events into Conductor event payloads.

This module will hold the bridge between Pydantic AI's event stream and
Conductor's ``EventCallback`` payloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic_ai import Agent
    from pydantic_ai.messages import AgentEvent


def map_pydantic_event(
    agent: Agent[Any, Any],
    event: AgentEvent,
) -> tuple[str, dict[str, Any]] | None:
    """Translate a Pydantic AI event into a Conductor event tuple.

    Args:
        agent: The Pydantic AI agent that produced the event.
        event: A Pydantic AI ``AgentEvent``.

    Returns:
        A tuple ``(event_type, data_dict)`` for Conductor, or ``None`` if
the event should be ignored.
    """
    raise NotImplementedError("Phase 5 will implement map_pydantic_event")
