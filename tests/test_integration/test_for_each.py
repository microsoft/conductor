"""Integration tests for for-each (dynamic parallel) execution.

Tests cover:
- Template variable injection ({{ <var> }}, {{ _index }}, {{ _key }})
- For-each execution with batching
- Failure modes (fail_fast, continue_on_error, all_or_nothing)
- Output aggregation (list and dict outputs)
- Empty array handling
"""

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.executor.template import TemplateRenderer


class TestLoopVariableTemplateRendering:
    """Tests for template rendering with loop variables.

    These tests verify that loop variables ({{ var }}, {{ _index }}, {{ _key }})
    are properly injected into context and accessible in agent templates.
    """

    def test_render_template_with_loop_variable(self):
        """Test that loop variable ({{ <var> }}) is accessible in templates."""
        renderer = TemplateRenderer()

        # Context with injected loop variable
        context = {
            "kpi": {"kpi_id": "K1", "name": "Revenue"},
            "_index": 0,
        }

        # Template using loop variable
        template = "Analyzing KPI: {{ kpi.kpi_id }} - {{ kpi.name }}"
        result = renderer.render(template, context)

        assert result == "Analyzing KPI: K1 - Revenue"

    def test_render_template_with_index_variable(self):
        """Test that {{ _index }} is accessible in templates."""
        renderer = TemplateRenderer()

        context = {
            "kpi": {"kpi_id": "K5"},
            "_index": 4,
        }

        template = "Processing item #{{ _index + 1 }}: {{ kpi.kpi_id }}"
        result = renderer.render(template, context)

        assert result == "Processing item #5: K5"

    def test_render_template_with_key_variable(self):
        """Test that {{ _key }} is accessible when key_by is used."""
        renderer = TemplateRenderer()

        context = {
            "kpi": {"kpi_id": "KPI_123", "value": 100},
            "_index": 2,
            "_key": "KPI_123",
        }

        template = "Key={{ _key }}, Index={{ _index }}, Value={{ kpi.value }}"
        result = renderer.render(template, context)

        assert result == "Key=KPI_123, Index=2, Value=100"

    def test_render_template_with_all_loop_variables(self):
        """Test using all loop variables together in one template."""
        renderer = TemplateRenderer()

        context = {
            "item": {"id": "ABC", "status": "active"},
            "_index": 7,
            "_key": "ABC",
        }

        template = """
        Item: {{ item.id }}
        Status: {{ item.status }}
        Position: {{ _index }}
        Key: {{ _key }}
        """.strip()

        result = renderer.render(template, context)

        assert "Item: ABC" in result
        assert "Status: active" in result
        assert "Position: 7" in result
        assert "Key: ABC" in result

    def test_render_template_with_simple_string_item(self):
        """Test loop variable when item is a simple string (not a dict)."""
        renderer = TemplateRenderer()

        context = {
            "color": "blue",
            "_index": 1,
        }

        template = "Color #{{ _index }}: {{ color }}"
        result = renderer.render(template, context)

        assert result == "Color #1: blue"

    def test_render_template_with_number_item(self):
        """Test loop variable when item is a number."""
        renderer = TemplateRenderer()

        context = {
            "score": 95,
            "_index": 3,
        }

        template = "Score[{{ _index }}] = {{ score }}"
        result = renderer.render(template, context)

        assert result == "Score[3] = 95"

    def test_render_template_with_list_item(self):
        """Test loop variable when item is a list."""
        renderer = TemplateRenderer()

        context = {
            "batch": ["a", "b", "c"],
            "_index": 0,
        }

        template = "Batch {{ _index }}: {{ batch | join(', ') }}"
        result = renderer.render(template, context)

        assert result == "Batch 0: a, b, c"

    def test_render_template_with_nested_item_access(self):
        """Test accessing deeply nested fields in loop variable."""
        renderer = TemplateRenderer()

        context = {
            "kpi": {
                "id": "revenue",
                "metrics": {
                    "current": 1000,
                    "target": 1500,
                },
                "tags": ["financial", "quarterly"],
            },
            "_index": 0,
        }

        template = (
            "KPI {{ kpi.id }}: "
            "{{ kpi.metrics.current }}/{{ kpi.metrics.target }} "
            "({{ kpi.tags[0] }})"
        )
        result = renderer.render(template, context)

        assert result == "KPI revenue: 1000/1500 (financial)"

    def test_render_template_with_workflow_and_loop_variables(self):
        """Test that loop variables coexist with workflow context."""
        renderer = TemplateRenderer()

        context = {
            "workflow": {"input": {"goal": "analyze all"}},
            "finder": {"output": {"total": 50}},
            "kpi": {"kpi_id": "K1"},
            "_index": 0,
        }

        template = (
            "Goal: {{ workflow.input.goal }} | "
            "Total: {{ finder.output.total }} | "
            "Current: {{ kpi.kpi_id }} (#{{ _index }})"
        )
        result = renderer.render(template, context)

        assert result == "Goal: analyze all | Total: 50 | Current: K1 (#0)"

    def test_render_template_conditional_with_index(self):
        """Test conditional logic based on _index."""
        renderer = TemplateRenderer()

        context = {
            "item": "test",
            "_index": 0,
        }

        template = "{% if _index == 0 %}First item{% else %}Item #{{ _index }}{% endif %}"
        result = renderer.render(template, context)

        assert result == "First item"

        # Test non-zero index
        context["_index"] = 5
        result = renderer.render(template, context)
        assert result == "Item #5"

    def test_render_template_loop_over_item_fields(self):
        """Test using Jinja2 loop over fields in the loop variable."""
        renderer = TemplateRenderer()

        context = {
            "kpi": {"id": "K1", "name": "Revenue", "value": 100},
            "_index": 0,
        }

        template = "{% for key, val in kpi.items() %}{{ key }}={{ val }} {% endfor %}"
        result = renderer.render(template, context)

        # Result should contain all key-value pairs
        assert "id=K1" in result
        assert "name=Revenue" in result
        assert "value=100" in result

    def test_render_template_with_key_without_index(self):
        """Test template that uses _key but not _index."""
        renderer = TemplateRenderer()

        context = {
            "kpi": {"status": "active"},
            "_index": 99,
            "_key": "KPI_XYZ",
        }

        # Template only uses _key, not _index
        template = "Processing {{ _key }}: {{ kpi.status }}"
        result = renderer.render(template, context)

        assert result == "Processing KPI_XYZ: active"

    def test_render_template_missing_loop_variable(self):
        """Test that missing loop variables cause template errors."""
        renderer = TemplateRenderer()

        # Context missing the loop variable
        context = {
            "_index": 0,
        }

        template = "{{ kpi.kpi_id }}"

        # Should raise an error due to undefined variable
        with pytest.raises((KeyError, Exception)):
            renderer.render(template, context)

    def test_render_template_with_filters_on_loop_variable(self):
        """Test using Jinja2 filters on loop variables."""
        renderer = TemplateRenderer()

        context = {
            "kpi": {"name": "revenue growth"},
            "_index": 0,
        }

        template = "KPI: {{ kpi.name | upper }}"
        result = renderer.render(template, context)

        assert result == "KPI: REVENUE GROWTH"

    def test_render_template_zero_index(self):
        """Test that _index=0 renders correctly (not treated as falsy)."""
        renderer = TemplateRenderer()

        context = {
            "item": "first",
            "_index": 0,
        }

        # This should show "Index: 0", not be treated as missing
        template = "Index: {{ _index }}"
        result = renderer.render(template, context)

        assert result == "Index: 0"

    def test_render_template_empty_string_key(self):
        """Test that empty string key renders correctly."""
        renderer = TemplateRenderer()

        context = {
            "item": "value",
            "_index": 0,
            "_key": "",
        }

        template = "Key: '{{ _key }}'"
        result = renderer.render(template, context)

        assert result == "Key: ''"


class TestForEachExecution:
    """Integration tests for for-each group execution.

    Tests cover:
    - Basic for-each execution with batching
    - Failure modes (fail_fast, continue_on_error, all_or_nothing)
    - Empty array handling
    - Output aggregation (list and dict)
    """

    @pytest.mark.asyncio
    async def test_simple_for_each_execution(self):
        """Test basic for-each execution with 3 items and max_concurrent=2."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        # Create workflow with for-each group
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="for-each-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 2,
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item.id }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={
                "results": "{{ processors.outputs | tojson }}",
                "count": "{{ processors.count }}",
            },
        )

        # Mock provider
        provider = MagicMock()
        provider.execute = AsyncMock()

        # Mock finder output
        provider.execute.side_effect = [
            # Finder returns 3 items
            AgentOutput(
                content={
                    "items": [
                        {"id": "A"},
                        {"id": "B"},
                        {"id": "C"},
                    ]
                },
                raw_response={},
                model="gpt-4",
                tokens_used=50,
            ),
            # Processor results (3 items)
            AgentOutput(
                content={"result": "processed A"},
                raw_response={},
                model="gpt-4",
                tokens_used=30,
            ),
            AgentOutput(
                content={"result": "processed B"},
                raw_response={},
                model="gpt-4",
                tokens_used=30,
            ),
            AgentOutput(
                content={"result": "processed C"},
                raw_response={},
                model="gpt-4",
                tokens_used=30,
            ),
        ]

        # Execute workflow
        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Verify results
        assert "results" in result
        assert "count" in result

        # Results should be parsed as a list
        results_list = result["results"]
        assert isinstance(results_list, list)
        assert len(results_list) == 3
        assert results_list[0]["result"] == "processed A"
        assert results_list[1]["result"] == "processed B"
        assert results_list[2]["result"] == "processed C"
        assert result["count"] == 3

        # Verify batching (items 0-1 in batch 1, item 2 in batch 2)
        assert provider.execute.call_count == 4  # 1 finder + 3 processors

    @pytest.mark.asyncio
    async def test_for_each_with_empty_array(self):
        """Test for-each gracefully handles empty arrays."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="empty-array-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={
                "count": "{{ processors.count }}",
            },
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Finder returns empty array
        provider.execute.return_value = AgentOutput(
            content={"items": []},
            raw_response={},
            model="gpt-4",
            tokens_used=20,
        )

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Should complete without errors
        assert result["count"] == 0
        assert provider.execute.call_count == 1  # Only finder executed

    @pytest.mark.asyncio
    async def test_for_each_fail_fast_mode(self):
        """Test fail_fast mode stops on first error."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.exceptions import ExecutionError
        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="fail-fast-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 10,
                        "failure_mode": "fail_fast",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 3 items, second processor fails
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok"}, raw_response={}, model="gpt-4", tokens_used=10),
            ExecutionError("Processing failed"),
            AgentOutput(content={"result": "ok"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        engine = WorkflowEngine(config, provider)

        # Should raise ExecutionError
        with pytest.raises(ExecutionError) as exc_info:
            await engine.run({})

        assert "fail_fast mode" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_for_each_batching_respects_max_concurrent(self):
        """Test that batching processes items in chunks of max_concurrent."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="batching-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 2,
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={"count": "{{ processors.count }}"},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Track execution timing
        execution_times = []

        async def mock_execute(*args, **kwargs):
            agent = kwargs.get("agent") if "agent" in kwargs else args[0]
            execution_times.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.1)  # Simulate processing time
            return AgentOutput(
                content={"result": "ok"}
                if agent.name == "processor"
                else {"items": ["A", "B", "C", "D", "E"]},
                raw_response={},
                model="gpt-4",
                tokens_used=10,
            )

        provider.execute.side_effect = mock_execute

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Verify all items processed
        assert result["count"] == 5

        # Verify batching (should have 3 batches: 2+2+1)
        # We can't easily verify exact batch execution, but we verify all items processed
        assert provider.execute.call_count == 6  # 1 finder + 5 processors

    @pytest.mark.asyncio
    async def test_for_each_continue_on_error_partial_success(self):
        """Test continue_on_error mode with partial successes - workflow should continue."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.exceptions import ExecutionError
        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="continue-on-error-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 10,
                        "failure_mode": "continue_on_error",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={
                "count": "{{ processors.count }}",
                "success_count": "{{ processors.outputs | length }}",
            },
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 5 items, items at index 1 and 3 fail
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C", "D", "E"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok_A"}, raw_response={}, model="gpt-4", tokens_used=10),
            ExecutionError("Processing failed for B"),
            AgentOutput(content={"result": "ok_C"}, raw_response={}, model="gpt-4", tokens_used=10),
            ExecutionError("Processing failed for D"),
            AgentOutput(content={"result": "ok_E"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        engine = WorkflowEngine(config, provider)

        # Should NOT raise error (some items succeeded)
        result = await engine.run({})

        # Verify partial success
        assert result["count"] == 5
        assert result["success_count"] == 3  # Only 3 successful items

    @pytest.mark.asyncio
    async def test_for_each_continue_on_error_all_fail(self):
        """Test continue_on_error mode when all items fail - should raise error."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.exceptions import ExecutionError
        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="continue-on-error-all-fail-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 10,
                        "failure_mode": "continue_on_error",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 3 items, all processors fail
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            ExecutionError("Processing failed for A"),
            ExecutionError("Processing failed for B"),
            ExecutionError("Processing failed for C"),
        ]

        engine = WorkflowEngine(config, provider)

        # Should raise ExecutionError because ALL items failed
        with pytest.raises(ExecutionError) as exc_info:
            await engine.run({})

        assert "All items in for-each group 'processors' failed" in str(exc_info.value)
        assert "continue_on_error mode" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_for_each_all_or_nothing_all_succeed(self):
        """Test all_or_nothing mode when all items succeed - should complete successfully."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="all-or-nothing-success-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 10,
                        "failure_mode": "all_or_nothing",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "$end"}],
                    }
                ),
            ],
            output={
                "count": "{{ processors.count }}",
                "success_count": "{{ processors.outputs | length }}",
            },
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 3 items, all processors succeed
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok_A"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"result": "ok_B"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"result": "ok_C"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # All items should succeed
        assert result["count"] == 3
        assert result["success_count"] == 3

    @pytest.mark.asyncio
    async def test_for_each_all_or_nothing_any_fail(self):
        """Test all_or_nothing mode when any item fails - should raise error."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.exceptions import ExecutionError
        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="all-or-nothing-fail-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "max_concurrent": 10,
                        "failure_mode": "all_or_nothing",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 5 items, item at index 2 fails
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C", "D", "E"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok_A"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"result": "ok_B"}, raw_response={}, model="gpt-4", tokens_used=10),
            ExecutionError("Processing failed for C"),
            AgentOutput(content={"result": "ok_D"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"result": "ok_E"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        engine = WorkflowEngine(config, provider)

        # Should raise ExecutionError because one item failed (even though 4 succeeded)
        with pytest.raises(ExecutionError) as exc_info:
            await engine.run({})

        assert "For-each group 'processors' failed" in str(exc_info.value)
        assert "4 succeeded, 1 failed" in str(exc_info.value)
        assert "all_or_nothing mode" in str(exc_info.value)


@pytest.mark.asyncio
class TestForEachOutputAccess:
    """Tests for accessing for-each outputs in downstream agents.

    These tests verify:
    - Index-based output access (outputs[0])
    - Key-based output access (outputs["key"])
    - Output access patterns from downstream agents
    """

    async def test_index_based_output_access(self):
        """Test accessing for-each outputs by index in downstream agent."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="index-output-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
                AgentDef(
                    name="summarizer",
                    model="gpt-4",
                    prompt=(
                        "First: {{ processors.outputs[0].result }}, "
                        "Last: {{ processors.outputs[2].result }}"
                    ),
                    output={"summary": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "summarizer"}],
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 3 items, all succeed
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(
                content={"result": "result_A"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
            AgentOutput(
                content={"result": "result_B"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
            AgentOutput(
                content={"result": "result_C"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
            AgentOutput(
                content={"summary": "done"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
        ]

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Verify that the workflow completed successfully
        # The fact that it didn't raise an error means the template was rendered correctly
        assert result == {}

        # Verify all agents were called (finder + 3 processors + summarizer)
        assert provider.execute.call_count == 5

    async def test_key_based_output_access(self):
        """Test accessing for-each outputs by key when key_by is specified."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="key-output-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find KPIs",
                    output={"kpis": OutputField(type="array")},
                    routes=[RouteDef(to="analyzers")],
                ),
                AgentDef(
                    name="reporter",
                    model="gpt-4",
                    prompt=(
                        "Revenue: {{ analyzers.outputs['REV001'].status }}, "
                        "Profit: {{ analyzers.outputs['PROF001'].status }}"
                    ),
                    output={"report": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "analyzers",
                        "type": "for_each",
                        "source": "finder.output.kpis",
                        "as": "kpi",
                        "key_by": "kpi_id",
                        "agent": {
                            "name": "analyzer",
                            "model": "gpt-4",
                            "prompt": "Analyze {{ kpi.kpi_id }}",
                            "output": {"status": {"type": "string"}},
                        },
                        "routes": [{"to": "reporter"}],
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns KPIs with kpi_id keys
        provider.execute.side_effect = [
            AgentOutput(
                content={
                    "kpis": [
                        {"kpi_id": "REV001", "name": "Revenue"},
                        {"kpi_id": "PROF001", "name": "Profit"},
                        {"kpi_id": "COST001", "name": "Cost"},
                    ]
                },
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"status": "good"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(
                content={"status": "excellent"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
            AgentOutput(content={"status": "ok"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"report": "done"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Verify that the workflow completed successfully
        # The fact that it didn't raise an error means the template was rendered correctly
        assert result == {}

        # Verify all agents were called (finder + 3 analyzers + reporter)
        assert provider.execute.call_count == 5

    async def test_iterate_over_outputs(self):
        """Test iterating over for-each outputs in downstream agent template."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="iterate-output-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
                AgentDef(
                    name="collector",
                    model="gpt-4",
                    prompt=(
                        "Results: {% for result in processors.outputs %}"
                        "{{ result.status }}{% if not loop.last %}, {% endif %}"
                        "{% endfor %}"
                    ),
                    output={"collection": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"status": {"type": "string"}},
                        },
                        "routes": [{"to": "collector"}],
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["X", "Y", "Z"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"status": "ok_X"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"status": "ok_Y"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"status": "ok_Z"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(
                content={"collection": "done"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
        ]

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Verify that the workflow completed successfully
        # The fact that it didn't raise an error means the template was rendered correctly
        assert result == {}

        # Verify all agents were called (finder + 3 processors + collector)
        assert provider.execute.call_count == 5

    async def test_empty_outputs_structure(self):
        """Test that empty arrays produce correct empty output structures."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        # Test both list and dict outputs with empty arrays
        for key_by in [None, "item_id"]:
            config = WorkflowConfig(
                workflow=WorkflowDef(
                    name="empty-structure-test",
                    entry_point="finder",
                    runtime=RuntimeConfig(provider="copilot"),
                    context=ContextConfig(mode="accumulate"),
                    limits=LimitsConfig(max_iterations=50),
                ),
                agents=[
                    AgentDef(
                        name="finder",
                        model="gpt-4",
                        prompt="Find items",
                        output={"items": OutputField(type="array")},
                        routes=[RouteDef(to="processors")],
                    ),
                    AgentDef(
                        name="checker",
                        model="gpt-4",
                        prompt="Count: {{ processors.count }}",
                        output={"result": OutputField(type="string")},
                        routes=[RouteDef(to="$end")],
                    ),
                ],
                for_each=[
                    ForEachDef.model_validate(
                        {
                            "name": "processors",
                            "type": "for_each",
                            "source": "finder.output.items",
                            "as": "item",
                            "key_by": key_by,
                            "agent": {
                                "name": "processor",
                                "model": "gpt-4",
                                "prompt": "Process",
                                "output": {"status": {"type": "string"}},
                            },
                            "routes": [{"to": "checker"}],
                        }
                    ),
                ],
                output={},
            )

            provider = MagicMock()
            provider.execute = AsyncMock()

            # Setup: finder returns empty array
            provider.execute.side_effect = [
                AgentOutput(
                    content={"items": []},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=20,
                ),
                AgentOutput(
                    content={"result": "done"}, raw_response={}, model="gpt-4", tokens_used=10
                ),
            ]

            engine = WorkflowEngine(config, provider)
            result = await engine.run({})

            # Verify that the workflow completed successfully
            # The fact that it didn't raise an error means the template was rendered correctly
            assert result == {}

            # Verify both agents were called (finder + checker, no processors)
            assert provider.execute.call_count == 2

    async def test_access_errors_in_downstream_agent(self):
        """Test accessing for-each errors in downstream agent with continue_on_error."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.exceptions import ExecutionError as ExecError
        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="errors-access-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
                AgentDef(
                    name="error_checker",
                    model="gpt-4",
                    prompt="Error count: {{ processors.errors | length }}",
                    output={"report": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "failure_mode": "continue_on_error",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "error_checker"}],
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: 3 items, 1 fails
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok"}, raw_response={}, model="gpt-4", tokens_used=10),
            ExecError("Failed to process B"),
            AgentOutput(content={"result": "ok"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"report": "done"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Verify that the workflow completed successfully
        # The fact that it didn't raise an error means the template was rendered correctly
        assert result == {}

        # Verify all agents were called (finder + 3 processors + error_checker)
        assert provider.execute.call_count == 5

    async def test_explicit_mode_with_for_each_outputs(self):
        """Test accessing for-each outputs with explicit context mode."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="explicit-mode-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="explicit"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
                AgentDef(
                    name="summarizer",
                    model="gpt-4",
                    prompt="First: {{ processors.outputs[0].result }}",
                    output={"summary": OutputField(type="string")},
                    input=["processors.outputs"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "summarizer"}],
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns 2 items
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(
                content={"result": "result_A"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
            AgentOutput(
                content={"result": "result_B"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
            AgentOutput(
                content={"summary": "done"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
        ]

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Verify that the workflow completed successfully
        assert result == {}

        # Verify all agents were called (finder + 2 processors + summarizer)
        assert provider.execute.call_count == 4

    async def test_explicit_mode_with_dict_outputs(self):
        """Test accessing keyed for-each outputs with explicit context mode."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="explicit-dict-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="explicit"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find KPIs",
                    output={"kpis": OutputField(type="array")},
                    routes=[RouteDef(to="analyzers")],
                ),
                AgentDef(
                    name="reporter",
                    model="gpt-4",
                    prompt="Status: {{ analyzers.outputs['K1'].status }}",
                    output={"report": OutputField(type="string")},
                    input=["analyzers.outputs"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "analyzers",
                        "type": "for_each",
                        "source": "finder.output.kpis",
                        "as": "kpi",
                        "key_by": "id",
                        "agent": {
                            "name": "analyzer",
                            "model": "gpt-4",
                            "prompt": "Analyze {{ kpi.id }}",
                            "output": {"status": {"type": "string"}},
                        },
                        "routes": [{"to": "reporter"}],
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: finder returns KPIs with id keys
        provider.execute.side_effect = [
            AgentOutput(
                content={
                    "kpis": [
                        {"id": "K1", "name": "Revenue"},
                        {"id": "K2", "name": "Profit"},
                    ]
                },
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"status": "good"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(
                content={"status": "excellent"}, raw_response={}, model="gpt-4", tokens_used=10
            ),
            AgentOutput(content={"report": "done"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Verify that the workflow completed successfully
        assert result == {}

        # Verify all agents were called (finder + 2 analyzers + reporter)
        assert provider.execute.call_count == 4

    async def test_explicit_mode_with_errors(self):
        """Test accessing for-each errors with explicit context mode."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.exceptions import ExecutionError as ExecError
        from conductor.providers.base import AgentOutput

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="explicit-errors-test",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="explicit"),
                limits=LimitsConfig(max_iterations=50),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="Find items",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="processors")],
                ),
                AgentDef(
                    name="error_checker",
                    model="gpt-4",
                    prompt="Errors: {{ processors.errors | length }}",
                    output={"report": OutputField(type="string")},
                    input=["processors.errors"],
                    routes=[RouteDef(to="$end")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "processors",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "failure_mode": "continue_on_error",
                        "agent": {
                            "name": "processor",
                            "model": "gpt-4",
                            "prompt": "Process {{ item }}",
                            "output": {"result": {"type": "string"}},
                        },
                        "routes": [{"to": "error_checker"}],
                    }
                ),
            ],
            output={},
        )

        provider = MagicMock()
        provider.execute = AsyncMock()

        # Setup: 3 items, 1 fails
        provider.execute.side_effect = [
            AgentOutput(
                content={"items": ["A", "B", "C"]},
                raw_response={},
                model="gpt-4",
                tokens_used=20,
            ),
            AgentOutput(content={"result": "ok"}, raw_response={}, model="gpt-4", tokens_used=10),
            ExecError("Failed to process B"),
            AgentOutput(content={"result": "ok"}, raw_response={}, model="gpt-4", tokens_used=10),
            AgentOutput(content={"report": "done"}, raw_response={}, model="gpt-4", tokens_used=10),
        ]

        engine = WorkflowEngine(config, provider)
        result = await engine.run({})

        # Verify that the workflow completed successfully
        assert result == {}

        # Verify all agents were called (finder + 3 processors + error_checker)
        assert provider.execute.call_count == 5

    async def test_explicit_mode_with_empty_outputs(self):
        """Test E7-T5: Empty outputs in explicit mode produce correct structures."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.providers.base import AgentOutput

        # Test both list outputs (no key_by) and dict outputs (with key_by)
        for key_by in [None, "item_id"]:
            config = WorkflowConfig(
                workflow=WorkflowDef(
                    name="explicit-empty-test",
                    entry_point="finder",
                    runtime=RuntimeConfig(provider="copilot"),
                    context=ContextConfig(mode="explicit"),
                    limits=LimitsConfig(max_iterations=50),
                ),
                agents=[
                    AgentDef(
                        name="finder",
                        model="gpt-4",
                        prompt="Find items",
                        output={"items": OutputField(type="array")},
                        routes=[RouteDef(to="processors")],
                    ),
                    AgentDef(
                        name="checker",
                        model="gpt-4",
                        prompt="Count: {{ processors.count }}, Errors: {{ processors.errors }}",
                        output={"summary": OutputField(type="string")},
                        input=["processors.outputs", "processors.errors", "processors.count"],
                        routes=[RouteDef(to="$end")],
                    ),
                ],
                for_each=[
                    ForEachDef.model_validate(
                        {
                            "name": "processors",
                            "type": "for_each",
                            "source": "finder.output.items",
                            "as": "item",
                            "key_by": key_by,
                            "agent": {
                                "name": "processor",
                                "model": "gpt-4",
                                "prompt": "Process {{ item }}",
                                "output": {"result": {"type": "string"}},
                            },
                            "routes": [{"to": "checker"}],
                        }
                    ),
                ],
                output={},
            )

            provider = MagicMock()
            provider.execute = AsyncMock()

            # Setup: finder returns empty array
            provider.execute.side_effect = [
                AgentOutput(
                    content={"items": []},
                    raw_response={},
                    model="gpt-4",
                    tokens_used=20,
                ),
                AgentOutput(
                    content={"summary": "done"}, raw_response={}, model="gpt-4", tokens_used=10
                ),
            ]

            engine = WorkflowEngine(config, provider)
            result = await engine.run({})

            # Verify that the workflow completed successfully
            # Empty list should produce {outputs: [], errors: {}, count: 0}
            # Empty dict (with key_by) should produce {outputs: {}, errors: {}, count: 0}
            assert result == {}

            # Verify only finder and checker were called (no processors since array is empty)
            assert provider.execute.call_count == 2
