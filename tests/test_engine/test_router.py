"""Unit tests for Router.

Tests cover:
- Unconditional routes
- Conditional routes with Jinja2 templates
- Conditional routes with arithmetic expressions (simpleeval)
- Fallthrough behavior (first match wins)
- $end termination
- Output transformation
- No matching routes error
"""

import pytest

from conductor.config.schema import RouteDef
from conductor.engine.router import Router, RouteResult


class TestRouteResult:
    """Tests for RouteResult dataclass."""

    def test_route_result_minimal(self) -> None:
        """Test RouteResult with only target."""
        result = RouteResult(target="$end")
        assert result.target == "$end"
        assert result.output_transform is None
        assert result.matched_rule is None

    def test_route_result_full(self) -> None:
        """Test RouteResult with all fields."""
        route = RouteDef(to="next_agent", when="{{ output.approved }}")
        result = RouteResult(
            target="next_agent",
            output_transform={"summary": "Done"},
            matched_rule=route,
        )
        assert result.target == "next_agent"
        assert result.output_transform == {"summary": "Done"}
        assert result.matched_rule == route


class TestRouterUnconditional:
    """Tests for unconditional routing."""

    def test_unconditional_route_matches(self) -> None:
        """Test that a route without 'when' always matches."""
        router = Router()
        routes = [RouteDef(to="next_agent")]

        result = router.evaluate(routes, {"value": 1}, {})

        assert result.target == "next_agent"
        assert result.matched_rule is not None
        assert result.matched_rule.to == "next_agent"

    def test_unconditional_route_to_end(self) -> None:
        """Test unconditional route to $end."""
        router = Router()
        routes = [RouteDef(to="$end")]

        result = router.evaluate(routes, {}, {})

        assert result.target == "$end"

    def test_first_unconditional_wins(self) -> None:
        """Test that first unconditional route wins."""
        router = Router()
        routes = [
            RouteDef(to="first"),
            RouteDef(to="second"),
            RouteDef(to="third"),
        ]

        result = router.evaluate(routes, {}, {})

        assert result.target == "first"


class TestRouterJinjaConditions:
    """Tests for Jinja2 template conditions."""

    def test_jinja_condition_true(self) -> None:
        """Test Jinja2 condition that evaluates to true."""
        router = Router()
        routes = [
            RouteDef(to="approved_handler", when="{{ output.approved }}"),
            RouteDef(to="fallback"),
        ]

        result = router.evaluate(routes, {"approved": True}, {})

        assert result.target == "approved_handler"

    def test_jinja_condition_false(self) -> None:
        """Test Jinja2 condition that evaluates to false, falls through."""
        router = Router()
        routes = [
            RouteDef(to="approved_handler", when="{{ output.approved }}"),
            RouteDef(to="fallback"),
        ]

        result = router.evaluate(routes, {"approved": False}, {})

        assert result.target == "fallback"

    def test_jinja_nested_access(self) -> None:
        """Test Jinja2 condition with nested context access."""
        router = Router()
        routes = [
            RouteDef(to="handler", when="{{ output.result.success }}"),
            RouteDef(to="$end"),
        ]

        result = router.evaluate(routes, {"result": {"success": True}}, {})

        assert result.target == "handler"

    def test_jinja_comparison(self) -> None:
        """Test Jinja2 condition with comparison operator."""
        router = Router()
        routes = [
            RouteDef(to="high", when="{{ output.score >= 8 }}"),
            RouteDef(to="low"),
        ]

        result = router.evaluate(routes, {"score": 9}, {})
        assert result.target == "high"

        result = router.evaluate(routes, {"score": 7}, {})
        assert result.target == "low"

    def test_jinja_string_comparison(self) -> None:
        """Test Jinja2 condition with string comparison."""
        router = Router()
        routes = [
            RouteDef(to="success", when="{{ output.status == 'ok' }}"),
            RouteDef(to="error"),
        ]

        result = router.evaluate(routes, {"status": "ok"}, {})
        assert result.target == "success"

        result = router.evaluate(routes, {"status": "error"}, {})
        assert result.target == "error"


class TestRouterArithmeticExpressions:
    """Tests for simpleeval arithmetic expressions."""

    def test_arithmetic_greater_than(self) -> None:
        """Test arithmetic greater than comparison."""
        router = Router()
        routes = [
            RouteDef(to="high", when="score > 7"),
            RouteDef(to="low"),
        ]

        result = router.evaluate(routes, {"score": 8}, {})
        assert result.target == "high"

        result = router.evaluate(routes, {"score": 6}, {})
        assert result.target == "low"

    def test_arithmetic_less_than(self) -> None:
        """Test arithmetic less than comparison."""
        router = Router()
        routes = [
            RouteDef(to="continue", when="iteration < 5"),
            RouteDef(to="$end"),
        ]

        # Iteration tracked in context
        result = router.evaluate(routes, {"iteration": 3}, {})
        assert result.target == "continue"

        result = router.evaluate(routes, {"iteration": 5}, {})
        assert result.target == "$end"

    def test_arithmetic_equals(self) -> None:
        """Test arithmetic equality comparison."""
        router = Router()
        routes = [
            RouteDef(to="exact", when="count == 10"),
            RouteDef(to="other"),
        ]

        result = router.evaluate(routes, {"count": 10}, {})
        assert result.target == "exact"

        result = router.evaluate(routes, {"count": 9}, {})
        assert result.target == "other"

    def test_arithmetic_with_context_flattening(self) -> None:
        """Test that output values are flattened for simpleeval."""
        router = Router()
        routes = [
            RouteDef(to="high", when="score > 7"),  # 'score' directly from output
            RouteDef(to="low"),
        ]

        # Router flattens output.score to just 'score' for simpleeval
        result = router.evaluate(routes, {"score": 8}, {})
        assert result.target == "high"

    def test_arithmetic_with_output_prefix(self) -> None:
        """Test arithmetic with output_ prefix."""
        router = Router()
        routes = [
            RouteDef(to="high", when="output_score > 7"),
            RouteDef(to="low"),
        ]

        result = router.evaluate(routes, {"score": 8}, {})
        assert result.target == "high"

    def test_arithmetic_not_equals(self) -> None:
        """Test arithmetic not equals comparison."""
        router = Router()
        routes = [
            RouteDef(to="proceed", when="status != 0"),
            RouteDef(to="skip"),
        ]

        result = router.evaluate(routes, {"status": 1}, {})
        assert result.target == "proceed"

        result = router.evaluate(routes, {"status": 0}, {})
        assert result.target == "skip"

    def test_arithmetic_compound_expression(self) -> None:
        """Test compound arithmetic expression."""
        router = Router()
        routes = [
            RouteDef(to="acceptable", when="score >= 5 and score <= 10"),
            RouteDef(to="reject"),
        ]

        result = router.evaluate(routes, {"score": 7}, {})
        assert result.target == "acceptable"

        result = router.evaluate(routes, {"score": 15}, {})
        assert result.target == "reject"


class TestRouterFallthrough:
    """Tests for fallthrough behavior (first match wins)."""

    def test_first_matching_condition_wins(self) -> None:
        """Test that first matching condition wins."""
        router = Router()
        routes = [
            RouteDef(to="first", when="{{ output.score >= 9 }}"),
            RouteDef(to="second", when="{{ output.score >= 7 }}"),
            RouteDef(to="third"),
        ]

        # Score 9 matches first condition
        result = router.evaluate(routes, {"score": 9}, {})
        assert result.target == "first"

        # Score 7 should match second (not first)
        result = router.evaluate(routes, {"score": 7}, {})
        assert result.target == "second"

        # Score 5 should fall through to third
        result = router.evaluate(routes, {"score": 5}, {})
        assert result.target == "third"

    def test_fallthrough_to_unconditional(self) -> None:
        """Test that unconditional route catches all."""
        router = Router()
        routes = [
            RouteDef(to="special", when="{{ output.special }}"),
            RouteDef(to="default"),  # Catch-all
        ]

        result = router.evaluate(routes, {"special": False, "other": True}, {})
        assert result.target == "default"


class TestRouterOutputTransform:
    """Tests for output transformation."""

    def test_output_transform_rendered(self) -> None:
        """Test that output transform templates are rendered."""
        router = Router()
        routes = [
            RouteDef(
                to="$end",
                output={
                    "message": "Processed: {{ output.result }}",
                    "status": "{{ output.status }}",
                },
            ),
        ]

        result = router.evaluate(routes, {"result": "success", "status": "complete"}, {})

        assert result.target == "$end"
        assert result.output_transform is not None
        assert result.output_transform["message"] == "Processed: success"
        assert result.output_transform["status"] == "complete"

    def test_no_output_transform(self) -> None:
        """Test route without output transform."""
        router = Router()
        routes = [RouteDef(to="next")]

        result = router.evaluate(routes, {}, {})

        assert result.output_transform is None


class TestRouterErrors:
    """Tests for error handling."""

    def test_no_matching_routes_raises_error(self) -> None:
        """Test that no matching routes raises ValueError."""
        router = Router()
        routes = [
            RouteDef(to="a", when="{{ output.flag1 }}"),
            RouteDef(to="b", when="{{ output.flag2 }}"),
        ]

        with pytest.raises(ValueError, match="No matching route found"):
            router.evaluate(routes, {"flag1": False, "flag2": False}, {})

    def test_unknown_variable_in_arithmetic(self) -> None:
        """Test that unknown variable in arithmetic raises error."""
        router = Router()
        routes = [
            RouteDef(to="a", when="unknown_var > 5"),
        ]

        with pytest.raises(ValueError, match="Unknown variable"):
            router.evaluate(routes, {"score": 10}, {})

    def test_invalid_arithmetic_expression(self) -> None:
        """Test that invalid arithmetic expression raises error."""
        router = Router()
        routes = [
            RouteDef(to="a", when="score >>> 5"),  # Invalid operator
        ]

        with pytest.raises(ValueError, match="Failed to evaluate expression"):
            router.evaluate(routes, {"score": 10}, {})


class TestRouterContextAccess:
    """Tests for context access patterns."""

    def test_workflow_context_in_jinja(self) -> None:
        """Test accessing workflow context in Jinja2 conditions."""
        router = Router()
        routes = [
            RouteDef(to="loop", when="{{ context.iteration < 3 }}"),
            RouteDef(to="$end"),
        ]

        result = router.evaluate(routes, {}, {"context": {"iteration": 1}})
        assert result.target == "loop"

        result = router.evaluate(routes, {}, {"context": {"iteration": 5}})
        assert result.target == "$end"

    def test_previous_agent_output_access(self) -> None:
        """Test accessing previous agent output in conditions."""
        router = Router()
        routes = [
            RouteDef(to="refine", when="{{ reviewer.output.approved == false }}"),
            RouteDef(to="$end"),
        ]

        context = {"reviewer": {"output": {"approved": False}}}
        result = router.evaluate(routes, {}, context)
        assert result.target == "refine"

        context = {"reviewer": {"output": {"approved": True}}}
        result = router.evaluate(routes, {}, context)
        assert result.target == "$end"


class TestRouterEdgeCases:
    """Edge case tests."""

    def test_empty_routes_raises_error(self) -> None:
        """Test that empty routes list raises error."""
        router = Router()

        with pytest.raises(ValueError, match="No matching route found"):
            router.evaluate([], {}, {})

    def test_boolean_output_value(self) -> None:
        """Test condition with boolean output value."""
        router = Router()
        routes = [
            RouteDef(to="yes", when="{{ output.flag }}"),
            RouteDef(to="no"),
        ]

        result = router.evaluate(routes, {"flag": True}, {})
        assert result.target == "yes"

        result = router.evaluate(routes, {"flag": False}, {})
        assert result.target == "no"

    def test_zero_is_falsy_in_jinja(self) -> None:
        """Test that zero is falsy in Jinja2 conditions."""
        router = Router()
        routes = [
            RouteDef(to="nonzero", when="{{ output.count }}"),
            RouteDef(to="zero"),
        ]

        result = router.evaluate(routes, {"count": 1}, {})
        assert result.target == "nonzero"

        result = router.evaluate(routes, {"count": 0}, {})
        assert result.target == "zero"

    def test_empty_string_is_falsy_in_jinja(self) -> None:
        """Test that empty string is falsy in Jinja2 conditions."""
        router = Router()
        routes = [
            RouteDef(to="has_value", when="{{ output.value }}"),
            RouteDef(to="empty"),
        ]

        result = router.evaluate(routes, {"value": "something"}, {})
        assert result.target == "has_value"

        result = router.evaluate(routes, {"value": ""}, {})
        assert result.target == "empty"


class TestRouterErrorBucket:
    """Tests for on_error routing.

    Routes split into a success bucket (``on_error is None``) and an
    error bucket (``on_error`` set). Only the bucket matching the
    presence/absence of an envelope competes.
    """

    @staticmethod
    def _envelope(kind: str, message: str = "boom") -> dict[str, object]:
        """Build a minimal ErrorEnvelope-shaped dict for tests."""
        return {"kind": kind, "message": message, "details": {}}

    def test_error_path_success_routes_skipped(self) -> None:
        """Success routes never match when an envelope is provided."""
        router = Router()
        routes = [
            RouteDef(to="should_not_match"),  # success catch-all
            RouteDef(to="handler", on_error=True),
        ]

        result = router.evaluate(routes, {}, {}, error=self._envelope("any.kind"))
        assert result.target == "handler"

    def test_success_path_error_routes_skipped(self) -> None:
        """Error routes never match on the success path."""
        router = Router()
        routes = [
            RouteDef(to="error_handler", on_error=True),
            RouteDef(to="next"),  # success catch-all
        ]

        result = router.evaluate(routes, {"ok": True}, {})
        assert result.target == "next"

    def test_on_error_true_catches_any_kind(self) -> None:
        """``on_error: true`` matches any envelope kind."""
        router = Router()
        routes = [RouteDef(to="catch_all", on_error=True)]

        result = router.evaluate(routes, {}, {}, error=self._envelope("external.git.drift"))
        assert result.target == "catch_all"

    def test_on_error_string_exact_match(self) -> None:
        """A string ``on_error`` matches only the exact kind."""
        router = Router()
        routes = [
            RouteDef(to="git_handler", on_error="external.git.drift"),
            RouteDef(to="fallback", on_error=True),
        ]

        result = router.evaluate(routes, {}, {}, error=self._envelope("external.git.drift"))
        assert result.target == "git_handler"

        result = router.evaluate(routes, {}, {}, error=self._envelope("external.api.timeout"))
        assert result.target == "fallback"

    def test_on_error_list_membership(self) -> None:
        """A list ``on_error`` matches if the kind appears in the list."""
        router = Router()
        routes = [
            RouteDef(
                to="external_handler",
                on_error=["external.git.drift", "external.api.timeout"],
            ),
            RouteDef(to="fallback", on_error=True),
        ]

        result = router.evaluate(routes, {}, {}, error=self._envelope("external.api.timeout"))
        assert result.target == "external_handler"

        result = router.evaluate(routes, {}, {}, error=self._envelope("policy.violation"))
        assert result.target == "fallback"

    def test_error_route_when_clause_applies(self) -> None:
        """``when:`` still applies in the error bucket."""
        router = Router()
        routes = [
            RouteDef(
                to="retry",
                on_error=True,
                when="{{ error.details.retryable }}",
            ),
            RouteDef(to="give_up", on_error=True),
        ]

        retryable = {"kind": "external.x", "message": "m", "details": {"retryable": True}}
        not_retryable = {"kind": "external.x", "message": "m", "details": {"retryable": False}}

        assert router.evaluate(routes, {}, {}, error=retryable).target == "retry"
        assert router.evaluate(routes, {}, {}, error=not_retryable).target == "give_up"

    def test_error_eval_context_exposes_kind_via_jinja(self) -> None:
        """Templates on error routes can reference ``error.kind`` etc."""
        router = Router()
        routes = [
            RouteDef(
                to="match",
                on_error=True,
                when="{{ error.kind == 'policy.violation' }}",
            ),
            RouteDef(to="other", on_error=True),
        ]

        result = router.evaluate(routes, {}, {}, error=self._envelope("policy.violation"))
        assert result.target == "match"

    def test_error_route_output_transform_sees_error(self) -> None:
        """``output:`` on an error route renders against the envelope."""
        router = Router()
        routes = [
            RouteDef(
                to="reporter",
                on_error=True,
                output={"failed_kind": "{{ error.kind }}"},
            )
        ]

        result = router.evaluate(routes, {}, {}, error=self._envelope("external.git.drift"))
        assert result.output_transform == {"failed_kind": "external.git.drift"}

    def test_no_matching_error_route_raises_unhandled_node_error(self) -> None:
        """An envelope with no matching error route raises UnhandledNodeError."""
        from conductor.exceptions import UnhandledNodeError

        router = Router()
        routes = [
            RouteDef(to="git", on_error="external.git.drift"),
            RouteDef(to="next"),  # success catch-all — must NOT swallow errors
        ]

        envelope = self._envelope("policy.violation")
        with pytest.raises(UnhandledNodeError) as exc_info:
            router.evaluate(routes, {}, {}, error=envelope)

        # The envelope is preserved on the exception so the engine can
        # wrap it in UnhandledWorkflowError.
        assert exc_info.value.envelope["kind"] == "policy.violation"

    def test_first_matching_error_route_wins(self) -> None:
        """Order matters within the error bucket."""
        router = Router()
        routes = [
            RouteDef(to="first", on_error=True),
            RouteDef(to="second", on_error=True),
        ]

        result = router.evaluate(routes, {}, {}, error=self._envelope("any.thing"))
        assert result.target == "first"

    def test_success_no_match_still_raises_value_error(self) -> None:
        """Backwards-compat: success-path exhaustion still raises ValueError."""
        router = Router()
        # Only an error route, no success catch-all.
        routes = [RouteDef(to="handler", on_error=True)]

        with pytest.raises(ValueError, match="No matching route found"):
            router.evaluate(routes, {"x": 1}, {})

    def test_simpleeval_can_reference_flattened_error_fields(self) -> None:
        """simpleeval flattening exposes ``error.kind`` as ``kind``/``error_kind``."""
        router = Router()
        routes = [
            RouteDef(to="git", on_error=True, when="kind == 'external.git.drift'"),
            RouteDef(to="other", on_error=True),
        ]

        result = router.evaluate(routes, {}, {}, error=self._envelope("external.git.drift"))
        assert result.target == "git"
