"""In-memory workflow state management for the designer.

Converts between the Pydantic ``WorkflowConfig`` models used by the
conductor engine and the JSON-serialisable dictionaries expected by
the React frontend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from conductor.config.loader import load_config
from conductor.config.schema import (
    AgentDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config
from conductor.exceptions import ConfigurationError


def new_workflow() -> dict[str, Any]:
    """Return a minimal blank workflow as a JSON-serialisable dict."""
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="new-workflow",
            entry_point="agent_1",
            runtime=RuntimeConfig(),
        ),
        agents=[
            AgentDef(
                name="agent_1",
                prompt="Describe what this agent should do.",
            ),
        ],
    )
    return config_to_json(config)


def config_to_json(config: WorkflowConfig) -> dict[str, Any]:
    """Serialise a ``WorkflowConfig`` to a JSON-friendly dict.

    Uses Pydantic's ``model_dump`` with ``by_alias=True`` so that
    fields like ``ForEachDef.as_`` are exported as ``as``.
    """
    return config.model_dump(mode="json", by_alias=True, exclude_none=True)


def json_to_config(data: dict[str, Any]) -> WorkflowConfig:
    """Parse a JSON dict into a validated ``WorkflowConfig``.

    Raises ``ConfigurationError`` if Pydantic validation fails.
    """
    try:
        return WorkflowConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(
            f"Invalid workflow configuration: {exc}",
            suggestion="Check the workflow JSON for missing or invalid fields.",
        ) from exc


def validate_json(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a workflow JSON and return errors/warnings.

    Returns a dict with ``errors`` (list[str]) and ``warnings`` (list[str]).
    Pydantic validation errors are reported first, then semantic validation.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Step 1: Pydantic structural validation
    try:
        config = WorkflowConfig.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = " → ".join(str(p) for p in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        return {"errors": errors, "warnings": warnings}

    # Step 2: Semantic cross-reference validation
    try:
        semantic_warnings = validate_workflow_config(config)
        warnings.extend(semantic_warnings)
    except ConfigurationError as exc:
        errors.append(str(exc))

    return {"errors": errors, "warnings": warnings}


def load_workflow_file(path: Path) -> dict[str, Any]:
    """Load a YAML workflow file and return it as JSON.

    Raises ``ConfigurationError`` if the file cannot be loaded.
    """
    config = load_config(path)
    return config_to_json(config)
