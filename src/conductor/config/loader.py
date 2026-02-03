"""YAML configuration loader with environment variable resolution.

This module handles loading YAML workflow configuration files,
resolving environment variables, and parsing them into typed
Pydantic models.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from conductor.config.schema import WorkflowConfig
from conductor.exceptions import ConfigurationError

# Pattern to match ${VAR} or ${VAR:-default}
ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def resolve_env_vars(value: str, max_depth: int = 10) -> str:
    """Resolve ${ENV:-default} patterns in strings.

    Supports recursive resolution where environment variable values
    may themselves contain environment variable references.

    Args:
        value: The string potentially containing env var references.
        max_depth: Maximum recursion depth to prevent infinite loops.

    Returns:
        The string with all environment variables resolved.

    Raises:
        ConfigurationError: If a required environment variable is missing
            (no default provided) or recursion limit is exceeded.
    """
    if max_depth <= 0:
        raise ConfigurationError(
            f"Maximum recursion depth exceeded while resolving environment variables in: {value}",
            suggestion="Check for circular references in your environment variables.",
        )

    def replace_env_var(match: re.Match) -> str:
        var_name = match.group(1)
        default_value = match.group(2)

        env_value = os.environ.get(var_name)

        if env_value is not None:
            return env_value
        elif default_value is not None:
            return default_value
        else:
            raise ConfigurationError(
                f"Required environment variable '{var_name}' is not set",
                suggestion=f"Set the environment variable '{var_name}' or provide a default "
                f"value using the syntax: ${{{{{{var_name}}}}:-default_value}}",
            )

    # Perform substitution
    result = ENV_VAR_PATTERN.sub(replace_env_var, value)

    # Check if there are still env vars to resolve (recursive resolution)
    if ENV_VAR_PATTERN.search(result):
        return resolve_env_vars(result, max_depth - 1)

    return result


def _resolve_env_vars_recursive(data: Any) -> Any:
    """Recursively resolve environment variables in a data structure.

    Args:
        data: The data structure (dict, list, or scalar) to process.

    Returns:
        The data structure with all string values having env vars resolved.
    """
    if isinstance(data, dict):
        return {k: _resolve_env_vars_recursive(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_env_vars_recursive(item) for item in data]
    elif isinstance(data, str):
        return resolve_env_vars(data)
    else:
        return data


class ConfigLoader:
    """Loads and validates workflow configuration from YAML files.

    This class handles:
    - YAML parsing with line number tracking for error messages
    - Environment variable resolution
    - Pydantic schema validation
    """

    def __init__(self) -> None:
        """Initialize the config loader with a ruamel.yaml parser."""
        self._yaml = YAML()
        self._yaml.preserve_quotes = True

    def load(self, path: str | Path) -> WorkflowConfig:
        """Load a workflow configuration from a YAML file.

        Args:
            path: Path to the YAML configuration file.

        Returns:
            A validated WorkflowConfig object.

        Raises:
            ConfigurationError: If the file cannot be read, contains invalid
                YAML syntax, or fails schema validation.
        """
        path = Path(path)

        if not path.exists():
            raise ConfigurationError(
                f"Workflow file not found: {path}",
                suggestion="Check that the file path is correct and the file exists.",
            )

        if not path.is_file():
            raise ConfigurationError(
                f"Path is not a file: {path}",
                suggestion="Provide a path to a YAML file, not a directory.",
            )

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigurationError(
                f"Failed to read workflow file '{path}': {e}",
                suggestion="Check file permissions and ensure the file is readable.",
            ) from e

        return self.load_string(content, source_path=path)

    def load_string(self, content: str, source_path: Path | None = None) -> WorkflowConfig:
        """Load a workflow configuration from a YAML string.

        Args:
            content: The YAML content as a string.
            source_path: Optional path for error messages.

        Returns:
            A validated WorkflowConfig object.

        Raises:
            ConfigurationError: If the YAML is invalid or fails validation.
        """
        source = str(source_path) if source_path else "<string>"

        try:
            data = self._yaml.load(content)
        except YAMLError as e:
            # Extract line number from the YAML error if available
            line_info = ""
            if hasattr(e, "problem_mark") and e.problem_mark is not None:
                mark = e.problem_mark
                # Access YAML marker attributes (dynamic type from ruamel.yaml)
                line_info = f" at line {mark.line + 1}, column {mark.column + 1}"  # type: ignore[union-attr]

            raise ConfigurationError(
                f"Invalid YAML syntax in '{source}'{line_info}: {e}",
                suggestion="Check the YAML syntax. Common issues include incorrect "
                "indentation, missing colons, or unquoted special characters.",
            ) from e

        if data is None:
            raise ConfigurationError(
                f"Empty configuration file: {source}",
                suggestion="Add workflow configuration to the YAML file.",
            )

        if not isinstance(data, dict):
            raise ConfigurationError(
                f"Invalid configuration format in '{source}': "
                f"expected a mapping, got {type(data).__name__}",
                suggestion="Ensure the YAML file contains a valid workflow configuration.",
            )

        # Resolve environment variables
        try:
            data = _resolve_env_vars_recursive(data)
        except ConfigurationError:
            raise
        except Exception as e:
            raise ConfigurationError(
                f"Failed to resolve environment variables in '{source}': {e}",
                suggestion="Check the environment variable syntax. "
                "Use ${VAR_NAME} or ${VAR_NAME:-default_value}.",
            ) from e

        # Validate against Pydantic schema
        return self._validate(data, source)

    def _validate(self, data: dict[str, Any], source: str) -> WorkflowConfig:
        """Validate configuration data against the Pydantic schema.

        Args:
            data: The parsed and env-var-resolved configuration data.
            source: The source file path for error messages.

        Returns:
            A validated WorkflowConfig object.

        Raises:
            ConfigurationError: If the data fails schema validation.
        """
        try:
            return WorkflowConfig.model_validate(data)
        except Exception as e:
            # Format Pydantic validation errors nicely
            error_msg = str(e)

            # Try to extract field path from Pydantic error
            if hasattr(e, "errors") and callable(e.errors):
                errors_result = e.errors()  # type: ignore[operator]
                if errors_result and isinstance(errors_result, list):
                    formatted_errors: list[str] = []
                    for err in errors_result:
                        if isinstance(err, dict):
                            loc_parts = err.get("loc", [])  # type: ignore[call-overload]
                            loc = ".".join(str(x) for x in loc_parts)
                            msg = err.get("msg", "Unknown error")  # type: ignore[call-overload]
                            formatted_errors.append(f"  - {loc}: {msg}")
                    if formatted_errors:
                        error_msg = "\n".join(formatted_errors)

            raise ConfigurationError(
                f"Configuration validation failed in '{source}':\n{error_msg}",
                suggestion="Check the workflow configuration against the schema. "
                "Ensure all required fields are present and have valid values.",
            ) from e


def load_config(path: str | Path) -> WorkflowConfig:
    """Convenience function to load a workflow configuration.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A validated WorkflowConfig object.

    Raises:
        ConfigurationError: If loading or validation fails.
    """
    loader = ConfigLoader()
    return loader.load(path)


def load_config_string(content: str, source_path: Path | None = None) -> WorkflowConfig:
    """Convenience function to load a workflow configuration from a string.

    Args:
        content: The YAML content as a string.
        source_path: Optional path for error messages.

    Returns:
        A validated WorkflowConfig object.

    Raises:
        ConfigurationError: If loading or validation fails.
    """
    loader = ConfigLoader()
    return loader.load_string(content, source_path)


# Alias for backward compatibility
load_workflow = load_config
