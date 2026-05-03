"""Integration tests for sub-workflow (type: workflow) steps in WorkflowEngine.

Tests cover:
- Linear workflow with sub-workflow step
- Sub-workflow output accessible in subsequent agent context
- Sub-workflow with routes
- Sub-workflow depth limit enforcement
- Self-referencing circular detection
- Sub-workflow file not found error
- Mixed agent + sub-workflow workflows
- Dry-run plan includes workflow steps
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import MAX_SUBWORKFLOW_DEPTH, WorkflowEngine
from conductor.exceptions import ExecutionError
from conductor.providers.copilot import CopilotProvider


@pytest.fixture
def tmp_workflow_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with sub-workflow files."""
    return tmp_path


def _write_yaml(path: Path, content: str) -> Path:
    """Write YAML content to a file and return the path."""
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


class TestSubWorkflowLinear:
    """Tests for linear workflows with sub-workflow steps."""

    @pytest.mark.asyncio
    async def test_subworkflow_runs_to_end(self, tmp_workflow_dir: Path) -> None:
        """Test linear workflow with a sub-workflow step that completes."""
        # Create the sub-workflow file
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-workflow
              entry_point: inner_agent
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: inner_agent
                prompt: "Do inner work on {{ workflow.input.topic }}"
                routes:
                  - to: "$end"
            output:
              result: "{{ inner_agent.output.result }}"
            """,
        )

        # Create the parent workflow config
        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent-workflow",
                entry_point="research",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="research",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "result": "{{ research.output.result }}",
            },
        )

        def mock_handler(agent, prompt, context):
            return {"result": "inner work done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        result = await engine.run({"topic": "Python"})

        assert result["result"] == "inner work done"

    @pytest.mark.asyncio
    async def test_subworkflow_output_in_context(self, tmp_workflow_dir: Path) -> None:
        """Test sub-workflow output accessible in subsequent agent's context."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: finder
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: finder
                prompt: "Find data"
                routes:
                  - to: "$end"
            output:
              findings: "{{ finder.output.findings }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="research",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="research",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="synthesizer")],
                ),
                AgentDef(
                    name="synthesizer",
                    prompt="Synthesize: {{ research.output.findings }}",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "synthesis": "{{ synthesizer.output.result }}",
            },
        )

        received_prompts = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            if agent.name == "finder":
                return {"findings": "important data"}
            return {"result": "synthesis complete"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        result = await engine.run({})

        # The synthesizer should have received the sub-workflow's output in its prompt
        assert any("important data" in p for p in received_prompts)
        assert result["synthesis"] == "synthesis complete"


class TestSubWorkflowDepthLimit:
    """Tests for sub-workflow depth limit enforcement."""

    @pytest.mark.asyncio
    async def test_depth_limit_exceeded(self, tmp_workflow_dir: Path) -> None:
        """Test that exceeding max sub-workflow depth raises ExecutionError."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Inner"
                routes:
                  - to: "$end"
            output:
              result: "{{ inner.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        provider = CopilotProvider(mock_handler=lambda agent, prompt, context: {"result": "ok"})
        engine = WorkflowEngine(
            config,
            provider,
            workflow_path=parent_path,
            _subworkflow_depth=MAX_SUBWORKFLOW_DEPTH,
        )

        with pytest.raises(ExecutionError, match="depth limit exceeded"):
            await engine.run({})


class TestSubWorkflowErrors:
    """Tests for sub-workflow error conditions."""

    @pytest.mark.asyncio
    async def test_subworkflow_file_not_found(self, tmp_workflow_dir: Path) -> None:
        """Test that missing sub-workflow file raises ExecutionError."""
        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="nonexistent.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider, workflow_path=parent_path)

        with pytest.raises(ExecutionError, match="Sub-workflow file not found"):
            await engine.run({})

    @pytest.mark.asyncio
    async def test_self_referencing_workflow(self, tmp_workflow_dir: Path) -> None:
        """Test that a workflow referencing itself raises ExecutionError."""
        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="parent.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider, workflow_path=parent_path)

        with pytest.raises(ExecutionError, match="Circular sub-workflow reference"):
            await engine.run({})


class TestSubWorkflowRouting:
    """Tests for routing from sub-workflow steps."""

    @pytest.mark.asyncio
    async def test_subworkflow_route_to_agent(self, tmp_workflow_dir: Path) -> None:
        """Test routing from sub-workflow to another agent."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: analyzer
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: analyzer
                prompt: "Analyze"
                routes:
                  - to: "$end"
            output:
              analysis: "{{ analyzer.output.analysis }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[
                        RouteDef(
                            to="summarizer",
                            when="{{ output.analysis == 'needs summary' }}",
                        ),
                        RouteDef(to="$end"),
                    ],
                ),
                AgentDef(
                    name="summarizer",
                    prompt="Summarize",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "result": "{{ sub_wf.output.analysis }}",
            },
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "analyzer":
                return {"analysis": "needs summary"}
            return {"result": "summarized"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        result = await engine.run({})

        assert result["result"] == "needs summary"


class TestSubWorkflowMixed:
    """Tests for mixed agent + sub-workflow workflows."""

    @pytest.mark.asyncio
    async def test_mixed_agent_and_subworkflow(self, tmp_workflow_dir: Path) -> None:
        """Test workflow with both agent and sub-workflow steps."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Inner work"
                routes:
                  - to: "$end"
            output:
              data: "{{ inner.output.data }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mixed",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="setup",
                    prompt="Setup the work",
                    routes=[RouteDef(to="sub_wf")],
                ),
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "data": "{{ sub_wf.output.data }}",
            },
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "setup":
                return {"result": "setup done"}
            return {"data": "inner data"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        result = await engine.run({})

        assert result["data"] == "inner data"


class TestSubWorkflowDryRun:
    """Tests for dry-run plan generation with sub-workflow steps."""

    def test_dry_run_includes_workflow_type(self) -> None:
        """Test that dry-run plan includes sub-workflow steps with correct agent_type."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="sub-wf-dryrun",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="./sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider)
        plan = engine.build_execution_plan()

        assert len(plan.steps) == 1
        assert plan.steps[0].agent_name == "sub_wf"
        assert plan.steps[0].agent_type == "workflow"


class TestSubWorkflowIterationCounting:
    """Tests for sub-workflow iteration limit counting."""

    @pytest.mark.asyncio
    async def test_subworkflow_counts_toward_iteration_limit(self, tmp_workflow_dir: Path) -> None:
        """Test that sub-workflow step counts as one iteration in parent."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Inner"
                routes:
                  - to: "$end"
            output:
              result: "{{ inner.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        def mock_handler(agent, prompt, context):
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        await engine.run({})

        assert engine.limits.current_iteration == 1


class TestSubWorkflowInputMapping:
    """Tests for input_mapping on sub-workflow agents."""

    @pytest.mark.asyncio
    async def test_input_mapping_renders_expressions(self, tmp_workflow_dir: Path) -> None:
        """Test that input_mapping Jinja2 expressions are rendered and passed as strings."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: inner
              runtime:
                provider: copilot
              input:
                item_id:
                  type: string
                  required: true
                title:
                  type: string
                  required: true
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Work on {{ workflow.input.item_id }}: {{ workflow.input.title }}"
                routes:
                  - to: "$end"
            output:
              result: "{{ inner.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="setup",
                    prompt="Setup",
                    routes=[RouteDef(to="sub_wf")],
                ),
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    input_mapping={
                        "item_id": "{{ setup.output.id }}",
                        "title": "{{ setup.output.name }}",
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ sub_wf.output.result }}"},
        )

        received_prompts: list[str] = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            if agent.name == "setup":
                return {"id": "42", "name": "Fix the bug"}
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        result = await engine.run({})

        assert result["result"] == "done"
        # The inner agent should have received the mapped values
        assert any("42" in p and "Fix the bug" in p for p in received_prompts)

    @pytest.mark.asyncio
    async def test_input_mapping_values_are_strings(self, tmp_workflow_dir: Path) -> None:
        """Test that input_mapping passes values as strings (no json.loads coercion).

        The rendered template values are always strings when entering the child
        workflow. Output template rendering may coerce them further, so we verify
        via the prompt the child agent actually receives.
        """
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: inner
              runtime:
                provider: copilot
              input:
                count:
                  type: string
                  required: true
                flag:
                  type: string
                  required: true
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Count={{ workflow.input.count }} Flag={{ workflow.input.flag }}"
                routes:
                  - to: "$end"
            output:
              result: "{{ inner.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="setup",
                    prompt="Setup",
                    routes=[RouteDef(to="sub_wf")],
                ),
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    input_mapping={
                        "count": "{{ setup.output.num }}",
                        "flag": "{{ setup.output.active }}",
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ sub_wf.output.result }}"},
        )

        received_prompts: list[str] = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            if agent.name == "setup":
                return {"num": "42", "active": "true"}
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        await engine.run({})

        # The child's inner agent should see the string values rendered into the prompt
        inner_prompt = [p for p in received_prompts if "Count=" in p][0]
        assert "Count=42" in inner_prompt
        assert "Flag=true" in inner_prompt

    @pytest.mark.asyncio
    async def test_no_input_mapping_forwards_parent_inputs(self, tmp_workflow_dir: Path) -> None:
        """Test backward compat: no input_mapping forwards parent workflow.input.*."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: inner
              runtime:
                provider: copilot
              input:
                topic:
                  type: string
                  required: false
                  default: "default"
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Work on {{ workflow.input.topic }}"
                routes:
                  - to: "$end"
            output:
              topic: "{{ workflow.input.topic }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    # No input_mapping — should forward parent's workflow.input.*
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"topic": "{{ sub_wf.output.topic }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        result = await engine.run({"topic": "Python"})

        # Parent's workflow.input.topic should be forwarded to child
        assert result["topic"] == "Python"

    @pytest.mark.asyncio
    async def test_empty_input_mapping_passes_nothing(self, tmp_workflow_dir: Path) -> None:
        """Test that input_mapping: {} means 'pass no inputs' (not default forwarding)."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: inner
              runtime:
                provider: copilot
              input:
                topic:
                  type: string
                  required: false
                  default: "fallback"
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Work on {{ workflow.input.topic }}"
                routes:
                  - to: "$end"
            output:
              topic: "{{ workflow.input.topic }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    input_mapping={},  # Explicitly empty — pass nothing
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"topic": "{{ sub_wf.output.topic }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        result = await engine.run({"topic": "Python"})

        # Empty input_mapping = no inputs passed, child should use its default
        assert result["topic"] == "fallback"

    @pytest.mark.asyncio
    async def test_input_mapping_error_includes_key_name(self, tmp_workflow_dir: Path) -> None:
        """Test that template errors include the failing key name."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: inner
              runtime:
                provider: copilot
              input:
                value:
                  type: string
                  required: true
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Use {{ workflow.input.value }}"
                routes:
                  - to: "$end"
            output:
              result: "done"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="sub_wf",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    input_mapping={
                        "value": "{{ nonexistent_agent.output.missing }}",
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider, workflow_path=parent_path)

        with pytest.raises(ExecutionError, match="input_mapping key 'value'"):
            await engine.run({})

    @pytest.mark.asyncio
    async def test_no_parent_context_leaks_to_child(self, tmp_workflow_dir: Path) -> None:
        """Test that parent agent outputs are NOT injected into child context."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-wf
              entry_point: inner
              runtime:
                provider: copilot
              input:
                data:
                  type: string
                  required: true
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Use {{ workflow.input.data }}"
                routes:
                  - to: "$end"
            output:
              result: "{{ inner.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="setup",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="setup",
                    prompt="Setup",
                    routes=[RouteDef(to="sub_wf")],
                ),
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="sub.yaml",
                    input_mapping={"data": "{{ setup.output.value }}"},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ sub_wf.output.result }}"},
        )

        child_contexts: list[dict] = []

        def mock_handler(agent, prompt, context):
            if agent.name == "setup":
                return {"value": "hello"}
            # Capture what the child's inner agent can see
            child_contexts.append(dict(context))
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)
        await engine.run({})

        # Parent's "setup" agent should NOT appear in child's context
        assert len(child_contexts) == 1
        assert "setup" not in child_contexts[0]
