"""Route evaluation for workflow conditional branching.

This module provides the Router class for evaluating routing rules to
determine the next agent in a workflow, including support for Jinja2
template conditions and simpleeval arithmetic expressions.

Routes split into two buckets at evaluation time:

- **Success routes** (``on_error`` is ``None``, the default): matched
  only when the producing node completed successfully.
- **Error routes** (``on_error`` is set): matched only when the
  producing node raised a typed error envelope.

Within each bucket, the first route whose ``when:`` evaluates truthy
wins; a route with no ``when:`` always matches its bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from conductor.executor.template import TemplateRenderer

if TYPE_CHECKING:
    from conductor.config.schema import RouteDef
    from conductor.engine.errors import ErrorEnvelope


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

    Success-path example::

        >>> from conductor.config.schema import RouteDef
        >>> router = Router()
        >>> routes = [
        ...     RouteDef(to="handler", when="{{ output.success }}"),
        ...     RouteDef(to="$end"),
        ... ]
        >>> result = router.evaluate(routes, {"success": True}, {})
        >>> result.target
        'handler'

    Error-path example::

        >>> routes = [
        ...     RouteDef(to="recover", on_error="external.git.fetch_failed"),
        ...     RouteDef(to="$end"),  # success fallback, not taken on error
        ... ]
        >>> envelope = {"kind": "external.git.fetch_failed", "message": "boom",
        ...             "details": {}}
        >>> result = router.evaluate(routes, {}, {}, error=envelope)
        >>> result.target
        'recover'
    """

    def __init__(self) -> None:
        """Initialize the Router with a template renderer."""
        self.renderer = TemplateRenderer()

    def evaluate(
        self,
        routes: list[RouteDef],
        current_output: dict[str, Any],
        context: dict[str, Any],
        error: ErrorEnvelope | None = None,
    ) -> RouteResult:
        """Evaluate routes and return the first matching target.

        When ``error`` is None, only success routes (``on_error is None``)
        are considered. When ``error`` is provided, only error routes
        (``on_error`` set) are considered, AND the route's ``on_error``
        kind matcher must match the envelope's kind.

        Args:
            routes: Ordered list of route definitions.
            current_output: Output from the just-executed agent. Pass an
                empty dict if the agent raised (output is not meaningful
                in that case).
            context: Full workflow context.
            error: Error envelope from a node-level raise, or None for
                the success path.

        Returns:
            RouteResult with target and optional output transform.

        Raises:
            ValueError: On the success path, if no route matches —
                indicates a configuration error (no success catch-all).
            UnhandledNodeError: On the error path, if no error route
                matches the envelope's kind — engine catches this and
                re-raises as :class:`conductor.exceptions.UnhandledWorkflowError`.
        """
        if error is None:
            return self._evaluate_success(routes, current_output, context)
        return self._evaluate_error(routes, context, error)

    def _evaluate_success(
        self,
        routes: list[RouteDef],
        current_output: dict[str, Any],
        context: dict[str, Any],
    ) -> RouteResult:
        """Evaluate success routes (``on_error is None``)."""
        eval_context = {**context, "output": current_output}

        for route in routes:
            if route.on_error is not None:
                continue  # error routes don't compete on the success path

            if route.when is None or self._evaluate_condition(route.when, eval_context):
                return RouteResult(
                    target=route.to,
                    output_transform=self._render_output(route.output, eval_context),
                    matched_rule=route,
                )

        raise ValueError(
            "No matching route found. Ensure at least one route has no 'when' clause "
            "or add a catch-all route at the end."
        )

    def _evaluate_error(
        self,
        routes: list[RouteDef],
        context: dict[str, Any],
        error: ErrorEnvelope,
    ) -> RouteResult:
        """Evaluate error routes against an envelope.

        Raises :class:`conductor.exceptions.UnhandledNodeError` when no
        error route matches. The exception is imported lazily to avoid
        a hard import cycle through ``conductor.exceptions`` for the
        success path (which doesn't need it).
        """
        # `error` exposed to Jinja and simpleeval; templates use
        # `{{ error.kind }}`, simpleeval sees flattened `error_kind`.
        eval_context = {**context, "error": error}

        for route in routes:
            if route.on_error is None:
                continue  # success routes don't compete on the error path
            if not _on_error_matches(route.on_error, error["kind"]):
                continue
            if route.when is None or self._evaluate_condition(route.when, eval_context):
                return RouteResult(
                    target=route.to,
                    output_transform=self._render_output(route.output, eval_context),
                    matched_rule=route,
                )

        # Deferred import: success path must not depend on this.
        from conductor.exceptions import UnhandledNodeError

        # Best-effort node name from the matched-against frame; engine
        # call sites pass the failing node's name via the envelope or
        # in their own UnhandledWorkflowError wrap. Here we use a
        # placeholder since the router doesn't track node identity.
        raise UnhandledNodeError(dict(error), node_name=str(context.get("_current_node", "?")))

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
                    if key in ("output", "error"):
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


def _on_error_matches(on_error: bool | str | list[str], kind: str) -> bool:
    """Return True if a route's ``on_error`` matcher accepts ``kind``.

    - ``True`` matches any kind (catch-all).
    - ``str`` matches by exact equality.
    - ``list[str]`` matches if the kind appears in the list.

    ``False`` and ``None`` are filtered out at the bucket level and
    should never reach this function.
    """
    if on_error is True:
        return True
    if isinstance(on_error, str):
        return on_error == kind
    if isinstance(on_error, list):
        return kind in on_error
    return False
