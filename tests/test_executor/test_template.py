"""Unit tests for the TemplateRenderer class.

Tests cover:
- Simple variable substitution
- JSON filter
- Default filter
- Conditional expressions
- Loops
- Missing variables (StrictUndefined)
- Nested access
- Condition evaluation
"""

import pytest

from conductor.exceptions import TemplateError
from conductor.executor.template import TemplateRenderer


class TestTemplateRendererBasics:
    """Tests for basic template rendering functionality."""

    def test_render_simple_variable(self) -> None:
        """Test rendering a simple variable substitution."""
        renderer = TemplateRenderer()
        result = renderer.render("Hello {{ name }}!", {"name": "World"})
        assert result == "Hello World!"

    def test_render_multiple_variables(self) -> None:
        """Test rendering multiple variables in one template."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{{ greeting }}, {{ name }}!",
            {"greeting": "Hello", "name": "World"},
        )
        assert result == "Hello, World!"

    def test_render_preserves_trailing_newline(self) -> None:
        """Test that trailing newlines are preserved."""
        renderer = TemplateRenderer()
        result = renderer.render("Hello {{ name }}!\n", {"name": "World"})
        assert result == "Hello World!\n"

    def test_render_empty_context(self) -> None:
        """Test rendering a template with no variables."""
        renderer = TemplateRenderer()
        result = renderer.render("Hello, World!", {})
        assert result == "Hello, World!"


class TestTemplateRendererJsonFilter:
    """Tests for the json filter."""

    def test_json_filter_list(self) -> None:
        """Test serializing a list to JSON."""
        renderer = TemplateRenderer()
        result = renderer.render("{{ items | json }}", {"items": ["a", "b", "c"]})
        assert '"a"' in result
        assert '"b"' in result
        assert '"c"' in result

    def test_json_filter_dict(self) -> None:
        """Test serializing a dict to JSON."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{{ data | json }}",
            {"data": {"key": "value", "number": 42}},
        )
        assert '"key": "value"' in result
        assert '"number": 42' in result

    def test_json_filter_nested(self) -> None:
        """Test serializing nested objects to JSON."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{{ data | json }}",
            {"data": {"nested": {"deep": "value"}}},
        )
        assert '"nested"' in result
        assert '"deep": "value"' in result

    def test_json_filter_handles_non_serializable(self) -> None:
        """Test that json filter uses default=str for non-serializable values."""
        renderer = TemplateRenderer()
        # This should not raise, it should convert to string
        result = renderer.render(
            "{{ data | json }}",
            {"data": {"date": "2024-01-01"}},
        )
        assert "2024-01-01" in result


class TestTemplateRendererDefaultFilter:
    """Tests for the default filter."""

    def test_default_filter_with_none(self) -> None:
        """Test default filter returns default when value is None."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "Value: {{ value | default('fallback') }}",
            {"value": None},
        )
        assert result == "Value: fallback"

    def test_default_filter_with_value(self) -> None:
        """Test default filter returns value when not None."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "Value: {{ value | default('fallback') }}",
            {"value": "actual"},
        )
        assert result == "Value: actual"

    def test_default_filter_empty_string(self) -> None:
        """Test default filter keeps empty string (only None triggers default)."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "Value: {{ value | default('fallback') }}",
            {"value": ""},
        )
        assert result == "Value: "


class TestTemplateRendererConditionals:
    """Tests for conditional expressions in templates."""

    def test_if_condition_true(self) -> None:
        """Test if block when condition is true."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{% if approved %}Approved{% else %}Rejected{% endif %}",
            {"approved": True},
        )
        assert result == "Approved"

    def test_if_condition_false(self) -> None:
        """Test if block when condition is false."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{% if approved %}Approved{% else %}Rejected{% endif %}",
            {"approved": False},
        )
        assert result == "Rejected"

    def test_if_with_comparison(self) -> None:
        """Test if block with comparison operators."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{% if score > 5 %}Pass{% else %}Fail{% endif %}",
            {"score": 7},
        )
        assert result == "Pass"

    def test_if_with_nested_access(self) -> None:
        """Test if block accessing nested attributes."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{% if output.status == 'ok' %}Success{% endif %}",
            {"output": {"status": "ok"}},
        )
        assert result == "Success"


class TestTemplateRendererLoops:
    """Tests for loop expressions in templates."""

    def test_for_loop_list(self) -> None:
        """Test for loop over a list."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{% for item in items %}{{ item }} {% endfor %}",
            {"items": ["a", "b", "c"]},
        )
        assert result == "a b c "

    def test_for_loop_dict(self) -> None:
        """Test for loop over a dict."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{% for key, value in data.items() %}{{ key }}={{ value }} {% endfor %}",
            {"data": {"x": 1, "y": 2}},
        )
        assert "x=1" in result
        assert "y=2" in result

    def test_for_loop_with_index(self) -> None:
        """Test for loop with loop index."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{% for item in items %}{{ loop.index }}.{{ item }} {% endfor %}",
            {"items": ["a", "b"]},
        )
        assert result == "1.a 2.b "


class TestTemplateRendererMissingVariables:
    """Tests for StrictUndefined behavior with missing variables."""

    def test_missing_variable_raises_template_error(self) -> None:
        """Test that missing variables raise TemplateError."""
        renderer = TemplateRenderer()
        with pytest.raises(TemplateError) as exc_info:
            renderer.render("{{ missing }}", {})
        assert "missing" in str(exc_info.value).lower()

    def test_missing_nested_variable_raises_template_error(self) -> None:
        """Test that missing nested variables raise TemplateError."""
        renderer = TemplateRenderer()
        with pytest.raises(TemplateError) as exc_info:
            renderer.render("{{ data.missing }}", {"data": {}})
        assert "missing" in str(exc_info.value).lower()

    def test_template_error_has_suggestion(self) -> None:
        """Test that TemplateError includes a suggestion."""
        renderer = TemplateRenderer()
        with pytest.raises(TemplateError) as exc_info:
            renderer.render("{{ undefined_var }}", {})
        assert exc_info.value.suggestion is not None


class TestTemplateRendererNestedAccess:
    """Tests for accessing nested data in templates."""

    def test_nested_dict_access(self) -> None:
        """Test accessing nested dict values."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{{ workflow.input.goal }}",
            {"workflow": {"input": {"goal": "test goal"}}},
        )
        assert result == "test goal"

    def test_deeply_nested_access(self) -> None:
        """Test accessing deeply nested values."""
        renderer = TemplateRenderer()
        result = renderer.render(
            "{{ a.b.c.d.e }}",
            {"a": {"b": {"c": {"d": {"e": "found"}}}}},
        )
        assert result == "found"

    def test_agent_output_access(self) -> None:
        """Test accessing agent output in typical workflow context."""
        renderer = TemplateRenderer()
        context = {
            "workflow": {"input": {"query": "test"}},
            "researcher": {"output": {"findings": "result data"}},
        }
        result = renderer.render(
            "Query: {{ workflow.input.query }}\nFindings: {{ researcher.output.findings }}",
            context,
        )
        assert "Query: test" in result
        assert "Findings: result data" in result


class TestTemplateRendererEvaluateCondition:
    """Tests for the evaluate_condition method."""

    def test_evaluate_condition_true(self) -> None:
        """Test evaluating a true condition."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ output.approved }}",
            {"output": {"approved": True}},
        )
        assert result is True

    def test_evaluate_condition_false(self) -> None:
        """Test evaluating a false condition."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ output.approved }}",
            {"output": {"approved": False}},
        )
        assert result is False

    def test_evaluate_condition_comparison(self) -> None:
        """Test evaluating a comparison condition."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ output.score > 5 }}",
            {"output": {"score": 7}},
        )
        assert result is True

    def test_evaluate_condition_comparison_false(self) -> None:
        """Test evaluating a comparison that returns false."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ output.score > 5 }}",
            {"output": {"score": 3}},
        )
        assert result is False

    def test_evaluate_condition_string_true(self) -> None:
        """Test that string 'true' evaluates to True."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ value }}",
            {"value": "true"},
        )
        assert result is True

    def test_evaluate_condition_string_yes(self) -> None:
        """Test that string 'yes' evaluates to True."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ value }}",
            {"value": "yes"},
        )
        assert result is True

    def test_evaluate_condition_string_1(self) -> None:
        """Test that string '1' evaluates to True."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ value }}",
            {"value": "1"},
        )
        assert result is True

    def test_evaluate_condition_string_false(self) -> None:
        """Test that string 'false' evaluates to False."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ value }}",
            {"value": "false"},
        )
        assert result is False

    def test_evaluate_condition_string_no(self) -> None:
        """Test that string 'no' evaluates to False."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ value }}",
            {"value": "no"},
        )
        assert result is False

    def test_evaluate_condition_empty_string(self) -> None:
        """Test that empty string evaluates to False."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ value }}",
            {"value": ""},
        )
        assert result is False

    def test_evaluate_condition_truthy_string(self) -> None:
        """Test that non-empty, non-boolean string evaluates to True."""
        renderer = TemplateRenderer()
        result = renderer.evaluate_condition(
            "{{ value }}",
            {"value": "some text"},
        )
        assert result is True

    def test_evaluate_condition_case_insensitive(self) -> None:
        """Test that boolean string evaluation is case insensitive."""
        renderer = TemplateRenderer()
        assert renderer.evaluate_condition("{{ v }}", {"v": "TRUE"}) is True
        assert renderer.evaluate_condition("{{ v }}", {"v": "False"}) is False
        assert renderer.evaluate_condition("{{ v }}", {"v": "YES"}) is True


class TestTemplateRendererSyntaxErrors:
    """Tests for handling syntax errors in templates."""

    def test_invalid_syntax_raises_template_error(self) -> None:
        """Test that invalid template syntax raises TemplateError."""
        renderer = TemplateRenderer()
        with pytest.raises(TemplateError) as exc_info:
            renderer.render("{{ invalid syntax }}", {})
        assert "syntax" in str(exc_info.value).lower()

    def test_unclosed_block_raises_template_error(self) -> None:
        """Test that unclosed blocks raise TemplateError."""
        renderer = TemplateRenderer()
        with pytest.raises(TemplateError) as exc_info:
            renderer.render("{% if true %}no endif", {})
        assert exc_info.value.suggestion is not None

    def test_unclosed_variable_raises_template_error(self) -> None:
        """Test that unclosed variable tags raise TemplateError."""
        renderer = TemplateRenderer()
        with pytest.raises(TemplateError) as exc_info:
            renderer.render("{{ name", {"name": "test"})
        assert exc_info.value.suggestion is not None


class TestTemplateRendererParallelOutputs:
    """Tests for rendering templates with parallel group outputs."""

    def test_render_parallel_group_output(self) -> None:
        """Test rendering template that accesses parallel group outputs."""
        renderer = TemplateRenderer()
        context = {
            "research_group": {
                "outputs": {
                    "researcher1": {"finding": "Discovery A"},
                    "researcher2": {"finding": "Discovery B"},
                },
                "errors": {},
            }
        }
        result = renderer.render(
            "Results: {{ research_group.outputs.researcher1.finding }} and "
            "{{ research_group.outputs.researcher2.finding }}",
            context,
        )
        assert result == "Results: Discovery A and Discovery B"

    def test_render_parallel_group_with_errors(self) -> None:
        """Test rendering template that checks parallel group errors."""
        renderer = TemplateRenderer()
        context = {
            "validators": {
                "outputs": {
                    "validator1": {"status": "pass"},
                },
                "errors": {
                    "validator2": {
                        "agent_name": "validator2",
                        "message": "Failed to validate",
                    }
                },
            }
        }
        template = (
            "{% if validators.errors %}Found {{ validators.errors | length }} error(s)"
            "{% else %}All passed{% endif %}"
        )
        result = renderer.render(template, context)
        assert "Found 1 error(s)" in result

    def test_render_loop_over_parallel_outputs(self) -> None:
        """Test rendering template that loops over parallel outputs."""
        renderer = TemplateRenderer()
        context = {
            "analyzers": {
                "outputs": {
                    "analyzer_a": {"score": 85},
                    "analyzer_b": {"score": 92},
                    "analyzer_c": {"score": 78},
                },
                "errors": {},
            }
        }
        template = (
            "{% for name, output in analyzers.outputs.items() %}"
            "{{ name }}: {{ output.score }}\n"
            "{% endfor %}"
        )
        result = renderer.render(template, context)
        assert "analyzer_a: 85" in result
        assert "analyzer_b: 92" in result
        assert "analyzer_c: 78" in result

    def test_render_mixed_regular_and_parallel_outputs(self) -> None:
        """Test rendering template with both regular and parallel outputs."""
        renderer = TemplateRenderer()
        context = {
            "workflow": {"input": {"topic": "AI"}},
            "planner": {"output": {"steps": ["research", "analyze"]}},
            "research_team": {
                "outputs": {
                    "researcher1": {"summary": "Finding 1"},
                    "researcher2": {"summary": "Finding 2"},
                },
                "errors": {},
            },
        }
        template = """Topic: {{ workflow.input.topic }}
Plan: {{ planner.output.steps | json }}
Research findings:
- {{ research_team.outputs.researcher1.summary }}
- {{ research_team.outputs.researcher2.summary }}"""
        result = renderer.render(template, context)
        assert "Topic: AI" in result
        assert "Finding 1" in result
        assert "Finding 2" in result

    def test_render_parallel_output_with_json_filter(self) -> None:
        """Test rendering parallel outputs with json filter."""
        renderer = TemplateRenderer()
        context = {
            "checkers": {
                "outputs": {
                    "syntax_check": {"valid": True, "warnings": []},
                    "style_check": {"valid": False, "issues": ["line too long"]},
                },
                "errors": {},
            }
        }
        result = renderer.render(
            "{{ checkers.outputs | json }}",
            context,
        )
        assert "syntax_check" in result
        assert "style_check" in result
        assert "valid" in result

    def test_render_conditional_on_parallel_errors(self) -> None:
        """Test conditional rendering based on parallel errors."""
        renderer = TemplateRenderer()
        context_with_errors = {
            "tasks": {
                "outputs": {},
                "errors": {
                    "task1": {"message": "Failed"},
                },
            }
        }
        context_no_errors = {
            "tasks": {
                "outputs": {
                    "task1": {"result": "Success"},
                },
                "errors": {},
            }
        }

        template = (
            """{% if tasks.errors %}Failures detected{% else %}All tasks succeeded{% endif %}"""
        )

        result_with_errors = renderer.render(template, context_with_errors)
        assert "Failures detected" in result_with_errors

        result_no_errors = renderer.render(template, context_no_errors)
        assert "All tasks succeeded" in result_no_errors

    def test_render_default_filter_with_parallel_output(self) -> None:
        """Test default filter with potentially None parallel output field."""
        renderer = TemplateRenderer()
        context = {
            "group": {
                "outputs": {
                    "agent1": {"result": "data"},
                    "agent2": {"result": None},  # Field exists but is None
                },
                "errors": {},
            }
        }
        # Access field that's None with default filter
        result = renderer.render(
            "{{ group.outputs.agent2.result | default('N/A') }}",
            context,
        )
        assert result == "N/A"
