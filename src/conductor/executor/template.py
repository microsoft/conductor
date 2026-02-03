"""Jinja2-based template renderer for prompts and expressions.

This module provides the TemplateRenderer class for rendering Jinja2 templates
with workflow context, including custom filters for JSON serialization.
"""

from __future__ import annotations

import json
from typing import Any

from jinja2 import BaseLoader, Environment, StrictUndefined, TemplateSyntaxError
from jinja2 import UndefinedError as Jinja2UndefinedError

from conductor.exceptions import TemplateError


class TemplateRenderer:
    """Jinja2-based template renderer for prompts and expressions.

    Uses StrictUndefined to fail fast on missing variables and provides
    custom filters for common operations like JSON serialization.

    Example:
        >>> renderer = TemplateRenderer()
        >>> renderer.render("Hello {{ name }}!", {"name": "World"})
        'Hello World!'
        >>> renderer.render("Data: {{ items | json }}", {"items": ["a", "b"]})
        'Data: [\\n  "a",\\n  "b"\\n]'
    """

    def __init__(self) -> None:
        """Initialize the template renderer with Jinja2 environment."""
        self.env = Environment(
            loader=BaseLoader(),
            undefined=StrictUndefined,  # Fail fast on missing variables
            autoescape=False,  # No HTML escaping for prompts
            keep_trailing_newline=True,
        )

        # Register custom filters
        self.env.filters["json"] = self._json_filter
        self.env.filters["default"] = self._default_filter

    @staticmethod
    def _json_filter(value: Any, indent: int = 2) -> str:
        """Serialize value to formatted JSON string.

        Args:
            value: Any JSON-serializable value.
            indent: Number of spaces for indentation.

        Returns:
            Formatted JSON string.
        """
        return json.dumps(value, indent=indent, default=str)

    @staticmethod
    def _default_filter(value: Any, default: Any = "") -> Any:
        """Return default if value is None or undefined.

        Args:
            value: The value to check.
            default: Default value to return if value is None.

        Returns:
            The value if not None, otherwise the default.
        """
        if value is None:
            return default
        return value

    def render(self, template: str, context: dict[str, Any]) -> str:
        """Render a template string with the given context.

        Args:
            template: Jinja2 template string.
            context: Variables available in the template.

        Returns:
            Rendered string.

        Raises:
            TemplateError: If rendering fails due to missing variables or syntax errors.
        """
        try:
            tmpl = self.env.from_string(template)
            return tmpl.render(**context)
        except Jinja2UndefinedError as e:
            # Extract the variable name from the error message
            error_msg = str(e)
            # Jinja2 error messages are like "'name' is undefined"
            variable_name = self._extract_variable_name(error_msg)
            raise TemplateError(
                f"Undefined variable in template: {e}",
                suggestion=f"Ensure variable '{variable_name}' is defined in the context",
            ) from e
        except TemplateSyntaxError as e:
            raise TemplateError(
                f"Template syntax error: {e}",
                suggestion="Check template syntax for Jinja2 compatibility",
            ) from e
        except Exception as e:
            raise TemplateError(
                f"Template rendering failed: {e}",
                suggestion="Check template and context for errors",
            ) from e

    def evaluate_condition(self, expression: str, context: dict[str, Any]) -> bool:
        """Evaluate a template expression as a boolean condition.

        Args:
            expression: Jinja2 expression (e.g., "{{ output.approved }}").
            context: Variables available for evaluation.

        Returns:
            Boolean result of the expression.

        Raises:
            TemplateError: If expression evaluation fails.
        """
        result = self.render(expression, context)
        # Handle string representations of booleans
        result_lower = result.lower().strip()
        if result_lower in ("true", "1", "yes"):
            return True
        if result_lower in ("false", "0", "no", ""):
            return False
        return bool(result)

    @staticmethod
    def _extract_variable_name(error_msg: str) -> str:
        """Extract variable name from Jinja2 undefined error message.

        Args:
            error_msg: The error message from Jinja2.

        Returns:
            The variable name, or "unknown" if extraction fails.
        """
        # Jinja2 error messages are like "'name' is undefined"
        if "'" in error_msg:
            parts = error_msg.split("'")
            if len(parts) >= 2:
                return parts[1]
        return "unknown"
