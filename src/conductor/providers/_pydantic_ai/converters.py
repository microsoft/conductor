"""Convert Conductor ``OutputField`` schemas into Pydantic models.

This module will hold the helpers for recursively translating workflow
``output`` blocks into dynamic Pydantic models suitable for Pydantic AI
``output_type``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

    from conductor.config.schema import OutputField


def _map_output_field_type(field: OutputField) -> Any:
    """Return a Python type for a single Conductor output field.

    Recursively handles scalar types, arrays, and object properties.
    """
    raise NotImplementedError("Phase 2 will implement _map_output_field_type")


def output_schema_to_pydantic_model(
    name: str,
    fields: dict[str, OutputField],
) -> type[BaseModel]:
    """Create a dynamic Pydantic model from a Conductor output schema.

    Args:
        name: The generated model name (e.g., the agent name + "Output").
        fields: Mapping from output key to its Conductor ``OutputField``.

    Returns:
        A new Pydantic ``BaseModel`` subclass matching the schema.
    """
    raise NotImplementedError("Phase 2 will implement output_schema_to_pydantic_model")
