"""Configuration module for Conductor.

This module handles YAML parsing, Pydantic schema validation,
and environment variable resolution.
"""

from conductor.config.loader import (
    ConfigLoader,
    load_config,
    load_config_string,
    resolve_env_vars,
)
from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    GateOption,
    HooksConfig,
    InputDef,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.config.validator import validate_workflow_config

__all__ = [
    # Loader
    "ConfigLoader",
    "load_config",
    "load_config_string",
    "resolve_env_vars",
    # Schema models
    "AgentDef",
    "ContextConfig",
    "GateOption",
    "HooksConfig",
    "InputDef",
    "LimitsConfig",
    "OutputField",
    "RouteDef",
    "RuntimeConfig",
    "WorkflowConfig",
    "WorkflowDef",
    # Validator
    "validate_workflow_config",
]
