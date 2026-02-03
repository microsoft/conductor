"""Route evaluation for workflow conditional branching.

This module provides the Router class for evaluating routing rules to
determine the next agent in a workflow, including support for Jinja2
template conditions and simpleeval arithmetic expressions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from conductor.executor.template import TemplateRenderer

if TYPE_CHECKING:
    from conductor.config.schema import RouteDef


@dataclass
class RouteResult:
    """Result of route evaluation.

    Attributes:
        target: Next agent name or '$end'.
        output_transform: Optional output transformation from the route.
        matched_rule: The route definition that matched.
    """

    target: str
    """Next agent name or '$end'."""

    output_transform: dict[str, Any] | None = None
    """Optional output transformation from the route."""

    matched_rule: RouteDef | None = None
    """The route definition that matched."""


class Router:
    """Evaluates routing rules to determine next agent.

    The Router supports two types of conditions:
    1. Jinja2 template expressions: {{ output.approved }}
    2. Arithmetic expressions via simpleeval: score > 7, iteration < 5

    Example:
        >>> from conductor.config.schema import RouteDef
        >>> router = Router()
        >>> routes = [
        ...     RouteDef(to="handler", when="{{ output.success }}"),
        ...     RouteDef(to="$end"),
        ... ]
        >>> result = router.evaluate(routes, {"success": True}, {})
        >>> result.target
        'handler'
    """

    def __init__(self) -> None:
        """Initialize the Router with a template renderer."""
        self.renderer = TemplateRenderer()

    def evaluate(
        self,
        routes: list[RouteDef],
        current_output: dict[str, Any],
        context: dict[str, Any],
    ) -> RouteResult:
        """Evaluate routes and return the first matching target.

        Routes are evaluated in order. First matching 'when' condition wins.
        A route with no 'when' clause always matches.

        Args:
            routes: Ordered list of route definitions.
            current_output: Output from the just-executed agent.
            context: Full workflow context.

        Returns:
            RouteResult with target and optional output transform.

        Raises:
            ValueError: If no routes match (shouldn't happen with proper config).
        """
        # Add current output to context for condition evaluation
        eval_context = {
            **context,
            "output": current_output,
        }

        for route in routes:
            if route.when is None:
                # No condition = always matches
                return RouteResult(
                    target=route.to,
                    output_transform=self._render_output(route.output, eval_context),
                    matched_rule=route,
                )

            # Evaluate the condition
            if self._evaluate_condition(route.when, eval_context):
                return RouteResult(
                    target=route.to,
                    output_transform=self._render_output(route.output, eval_context),
                    matched_rule=route,
                )

        # No routes matched - this is a configuration error
        raise ValueError(
            "No matching route found. Ensure at least one route has no 'when' clause "
            "or add a catch-all route at the end."
        )

    def _evaluate_condition(self, when: str, context: dict[str, Any]) -> bool:
        """Evaluate a 'when' condition.

        First tries template-style conditions ({{ expr }}).
        If that fails or returns non-boolean, tries simpleeval for arithmetic.

        Args:
            when: The condition expression to evaluate.
            context: Variables available for evaluation.

        Returns:
            Boolean result of the condition.
        """
        # Check if it's a Jinja2 template expression
        if "{{" in when and "}}" in when:
            return self.renderer.evaluate_condition(when, context)

        # Otherwise, use simpleeval for arithmetic expressions
        return self._evaluate_arithmetic(when, context)

    def _evaluate_arithmetic(self, expr: str, context: dict[str, Any]) -> bool:
        """Evaluate arithmetic expression using simpleeval.

        Supports: score > 7, iteration < 5, count == 10, etc.

        Args:
            expr: The arithmetic expression to evaluate.
            context: Variables available for evaluation.

        Returns:
            Boolean result of the expression.

        Raises:
            ValueError: If the expression references unknown variables or is invalid.
        """
        from simpleeval import NameNotDefined, simple_eval

        # Flatten context for simpleeval
        flat_context = self._flatten_context(context)

        try:
            result = simple_eval(expr, names=flat_context)
            return bool(result)
        except NameNotDefined as e:
            raise ValueError(f"Unknown variable in expression: {e}") from e
        except Exception as e:
            raise ValueError(f"Failed to evaluate expression '{expr}': {e}") from e

    def _flatten_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """Flatten nested context for simpleeval.

        Converts {'output': {'score': 7}} to {'output_score': 7, 'score': 7}
        This allows expressions like 'score > 7' to work directly.

        Args:
            context: Nested context dictionary.

        Returns:
            Flattened context with both nested and top-level access patterns.
        """
        flat: dict[str, Any] = {}
        for key, value in context.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    flat[f"{key}_{sub_key}"] = sub_value
                    # Also add top-level access for common patterns
                    if key == "output":
                        flat[sub_key] = sub_value
            else:
                flat[key] = value
        return flat

    def _render_output(
        self,
        output: dict[str, str] | None,
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Render output transformation templates.

        Args:
            output: Optional mapping of output keys to template expressions.
            context: Variables available for template rendering.

        Returns:
            Rendered output dictionary, or None if no output specified.
        """
        if output is None:
            return None

        result: dict[str, Any] = {}
        for key, template in output.items():
            result[key] = self.renderer.render(template, context)
        return result
