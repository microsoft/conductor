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
from conductor.exceptions import TemplateError


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

    @pytest.mark.parametrize(
        ("expression", "output", "missing"),
        [
            ("{{ output.ok }}", {}, None),
            ("{{ absent }}", {}, "absent"),
            ("{{ output. }}", {}, None),
        ],
    )
    def test_jinja_condition_reports_route_and_keeps_metadata(
        self, expression: str, output: dict, missing: str | None
    ) -> None:
        router = Router()
        with pytest.raises(TemplateError) as exc_info:
            router.evaluate(
                [RouteDef(to="handler", when=expression), RouteDef(to="fallback")],
                output,
                {},
                source_name="detector",
                diagnostic_context="script exit 2; stdout: empty",
            )
        error = exc_info.value
        assert f"from 'detector' to 'handler' condition {expression!r}" in str(error)
        assert "script exit 2; stdout: empty" in str(error)
        assert error.template_string == expression
        assert error.undefined_variable == missing
        assert isinstance(error.__cause__, TemplateError)
        assert error.__cause__.__cause__ is not None
        assert str(error).count("Suggestion:") == 1
        assert "fallback" not in str(error)
        if expression == "{{ output.ok }}":
            assert "dict object" not in error.suggestion
        if expression == "{{ output. }}":
            assert "syntax error" in str(error)

    @pytest.mark.parametrize("expression", ["unknown_var > 5", "score >>> 5"])
    def test_arithmetic_condition_reports_source_and_cause(self, expression: str) -> None:
        router = Router()
        with pytest.raises(ValueError) as exc_info:
            router.evaluate(
                [RouteDef(to="handler", when=expression)],
                {"score": 10},
                {},
                source_name="source",
                diagnostic_context="script exit 1",
            )
        assert f"from 'source' to 'handler' condition {expression!r}" in str(exc_info.value)
        assert "script exit 1" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, ValueError)

    @pytest.mark.parametrize("when", [None, "{{ output.active }}"])
    def test_output_transform_error_identifies_key_and_template(self, when: str | None) -> None:
        router = Router()
        template = "{{ output.missing }}"
        with pytest.raises(TemplateError) as exc_info:
            router.evaluate(
                [RouteDef(to="$end", when=when, output={"bad": template, "later": "{{ absent }}"})],
                {"active": True},
                {},
                source_name="producer",
            )
        error = exc_info.value
        assert "from 'producer' to '$end' output transformation 'bad'" in str(error)
        assert repr(template) in str(error)
        assert "condition" not in error.args[0]
        assert error.template_string == template
        assert str(error).count("Suggestion:") == 1

    def test_omitted_diagnostics_and_short_circuit(self) -> None:
        router = Router()
        routes = [
            RouteDef(to="skip", when="{{ output.flag }}", output={"bad": "{{ absent }}"}),
            RouteDef(to="go"),
            RouteDef(to="never", when="{{ missing }}"),
        ]
        assert router.evaluate(routes, {"flag": False}, {}).target == "go"
        with pytest.raises(TemplateError) as exc_info:
            router.evaluate([RouteDef(to="bad", when="{{ missing }}")], {}, {})
        assert "Route to 'bad'" in str(exc_info.value)
        assert "script exit" not in str(exc_info.value)

    def test_template_location_metadata_survives_route_wrapping(self) -> None:
        from unittest.mock import patch

        router = Router()
        original = TemplateError(
            "broken",
            suggestion="repair",
            file_path="workflow.yaml",
            line_number=8,
            template_string="{{ broken }}",
            undefined_variable="broken",
        )
        with (
            patch.object(router.renderer, "evaluate_condition", side_effect=original),
            pytest.raises(TemplateError) as exc_info,
        ):
            router.evaluate(
                [RouteDef(to="target", when="{{ broken }}")],
                {},
                {},
                source_name="source",
            )
        error = exc_info.value
        assert error.__cause__ is original
        assert error.file_path == "workflow.yaml"
        assert error.line_number == 8
        assert error.template_string == "{{ broken }}"
        assert error.undefined_variable == "broken"
        assert str(error).count("Suggestion:") == 1


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
