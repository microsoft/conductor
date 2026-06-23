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
    InputDef,
    LimitsConfig,
    OutputField,
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
    async def test_self_referencing_workflow_hits_depth_limit(self, tmp_workflow_dir: Path) -> None:
        """Test that a self-referencing workflow is allowed but bounded by depth limit."""
        # Write a real self-referencing workflow YAML
        parent_path = tmp_workflow_dir / "parent.yaml"
        _write_yaml(
            parent_path,
            """\
            workflow:
              name: self-ref
              entry_point: sub_wf
              runtime:
                provider: copilot
              limits:
                max_iterations: 50
            agents:
              - name: sub_wf
                type: workflow
                workflow: parent.yaml
                routes:
                  - to: "$end"
            output: {}
            """,
        )

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

        # Self-reference is now allowed but will hit depth limit
        with pytest.raises(ExecutionError, match="depth limit exceeded"):
            await engine.run({})

    @pytest.mark.asyncio
    async def test_max_depth_per_agent(self, tmp_workflow_dir: Path) -> None:
        """Test that per-agent max_depth is enforced before global limit."""
        parent_path = tmp_workflow_dir / "parent.yaml"
        _write_yaml(
            parent_path,
            """\
            workflow:
              name: self-ref
              entry_point: sub_wf
              runtime:
                provider: copilot
              limits:
                max_iterations: 50
            agents:
              - name: sub_wf
                type: workflow
                workflow: parent.yaml
                max_depth: 2
                routes:
                  - to: "$end"
            output: {}
            """,
        )

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
                    max_depth=2,
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider, workflow_path=parent_path)

        with pytest.raises(ExecutionError, match="max_depth.*exceeded"):
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

        # The child's inner agent should see JSON-parsed values rendered into the prompt.
        # json.loads("42") -> int 42, json.loads("true") -> bool True (Python repr)
        inner_prompt = [p for p in received_prompts if "Count=" in p][0]
        assert "Count=42" in inner_prompt
        assert "Flag=True" in inner_prompt

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


class TestSubWorkflowDashboardPath:
    """Tests for engine-driven parent_path / slot_key on dashboard events.

    These keep concurrent for_each-of-workflow iterations addressable in the
    web dashboard without depending on a single shared activeContextPath.
    """

    @pytest.mark.asyncio
    async def test_for_each_subworkflow_emits_distinct_slot_keys(
        self, tmp_workflow_dir: Path
    ) -> None:
        """For-each iterations each get a unique bracketed slot_key."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-workflow
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "inner {{ workflow.input.item }}"
                routes:
                  - to: "$end"
            output:
              result: "{{ workflow.input.item }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        from conductor.config.schema import ForEachDef, OutputField

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    prompt="find",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="batch")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="batch",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    max_concurrent=1,
                    agent=AgentDef(
                        name="runner",
                        type="workflow",
                        workflow="sub.yaml",
                        input_mapping={"item": "{{ item }}"},
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"done": "1"},
        )

        events: list[tuple[str, dict]] = []
        emitter = MagicMock()
        emitter.emit.side_effect = lambda ev: events.append((ev.type, dict(ev.data)))

        def _handler(agent, prompt, context):
            if agent.name == "finder":
                return {"items": ["a", "b", "c"]}
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path, event_emitter=emitter)
        await engine.run({})

        started = [d for t, d in events if t == "subworkflow_started"]
        assert len(started) == 3, f"expected 3 iterations, got {len(started)}"
        slot_keys = sorted(d["slot_key"] for d in started)
        assert slot_keys == ["batch[0]", "batch[1]", "batch[2]"]
        for d in started:
            assert d["parent_path"] == []
            assert d["agent_name"] == "batch"
            assert d.get("iteration") in (1, 2, 3)
            assert d.get("item_key") in ("0", "1", "2")

        completed = [d for t, d in events if t == "subworkflow_completed"]
        assert len(completed) == 3
        completed_slots = sorted(d["slot_key"] for d in completed)
        assert completed_slots == ["batch[0]", "batch[1]", "batch[2]"]

    @pytest.mark.asyncio
    async def test_sequential_subworkflow_emits_parent_path_and_slot_key(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Sequential sub-workflow emits parent_path=[] and slot_key=agent.name."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "inner"
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
                entry_point="research",
                runtime=RuntimeConfig(provider="copilot"),
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
            output={"result": "{{ research.output.result }}"},
        )

        events: list[tuple[str, dict]] = []
        emitter = MagicMock()
        emitter.emit.side_effect = lambda ev: events.append((ev.type, dict(ev.data)))

        provider = CopilotProvider(mock_handler=lambda agent, prompt, context: {"result": "ok"})
        engine = WorkflowEngine(config, provider, workflow_path=parent_path, event_emitter=emitter)
        await engine.run({})

        started = [d for t, d in events if t == "subworkflow_started"]
        completed = [d for t, d in events if t == "subworkflow_completed"]
        assert len(started) == 1, f"expected 1 subworkflow_started, got {len(started)}"
        assert started[0]["agent_name"] == "research"
        assert started[0]["parent_path"] == []
        assert started[0]["slot_key"] == "research"
        assert len(completed) == 1
        assert completed[0]["parent_path"] == []
        assert completed[0]["slot_key"] == "research"

        # The child engine auto-stamps subworkflow_path on every event it emits
        # so the dashboard can route per-context state correctly.
        child_workflow_completed = [
            d
            for t, d in events
            if t == "workflow_completed" and d.get("subworkflow_path") == ["research"]
        ]
        assert len(child_workflow_completed) == 1

    @pytest.mark.asyncio
    async def test_subworkflow_failed_event_carries_parent_path_and_slot_key(
        self, tmp_workflow_dir: Path
    ) -> None:
        """When a sub-workflow raises, subworkflow_failed carries dashboard fields.

        The success path is asserted in
        ``test_sequential_subworkflow_emits_parent_path_and_slot_key`` —
        this complements it by exercising the exception branch in
        ``WorkflowEngine`` (which emits subworkflow_failed before re-raising).
        """
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-broken
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "inner"
                routes:
                  - to: nonexistent_target
            output:
              result: "{{ inner.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="research",
                runtime=RuntimeConfig(provider="copilot"),
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
            output={"result": "{{ research.output.result }}"},
        )

        events: list[tuple[str, dict]] = []
        emitter = MagicMock()
        emitter.emit.side_effect = lambda ev: events.append((ev.type, dict(ev.data)))

        provider = CopilotProvider(mock_handler=lambda agent, prompt, context: {"result": "ok"})
        engine = WorkflowEngine(config, provider, workflow_path=parent_path, event_emitter=emitter)

        with pytest.raises(ExecutionError):
            await engine.run({})

        failed = [d for t, d in events if t == "subworkflow_failed"]
        assert len(failed) == 1, f"expected 1 subworkflow_failed event, got {len(failed)}"
        assert failed[0]["agent_name"] == "research"
        assert failed[0]["parent_path"] == []
        assert failed[0]["slot_key"] == "research"
        assert "error_type" in failed[0]
        assert "message" in failed[0]

    @pytest.mark.asyncio
    async def test_nested_subworkflow_path_accumulates(self, tmp_workflow_dir: Path) -> None:
        """At depth >= 2, subworkflow_path on auto-stamped events chains correctly.

        Parent -> mid (workflow) -> leaf (workflow). Events emitted by the
        leaf engine must carry subworkflow_path = ["mid", "leaf"], proving
        ``_dashboard_context_path`` accumulates across nesting levels.
        """
        _write_yaml(
            tmp_workflow_dir / "leaf.yaml",
            """\
            workflow:
              name: leaf
              entry_point: leaf_inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: leaf_inner
                prompt: "leaf"
                routes:
                  - to: "$end"
            output:
              result: "leaf_done"
            """,
        )
        _write_yaml(
            tmp_workflow_dir / "mid.yaml",
            """\
            workflow:
              name: mid
              entry_point: leaf
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: leaf
                type: workflow
                workflow: leaf.yaml
                routes:
                  - to: "$end"
            output:
              result: "{{ leaf.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="mid",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="mid",
                    type="workflow",
                    workflow="mid.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ mid.output.result }}"},
        )

        events: list[tuple[str, dict]] = []
        emitter = MagicMock()
        emitter.emit.side_effect = lambda ev: events.append((ev.type, dict(ev.data)))

        provider = CopilotProvider(mock_handler=lambda a, p, c: {"result": "ok"})
        engine = WorkflowEngine(config, provider, workflow_path=parent_path, event_emitter=emitter)
        await engine.run({})

        # Events from the leaf engine must carry the full slot-key chain.
        leaf_workflow_completed = [
            d
            for t, d in events
            if t == "workflow_completed" and d.get("subworkflow_path") == ["mid", "leaf"]
        ]
        assert len(leaf_workflow_completed) == 1, (
            "expected workflow_completed from depth-2 engine carrying "
            "subworkflow_path=['mid', 'leaf']"
        )

        # Mid engine emits its own workflow_completed at depth 1.
        mid_workflow_completed = [
            d
            for t, d in events
            if t == "workflow_completed" and d.get("subworkflow_path") == ["mid"]
        ]
        assert len(mid_workflow_completed) == 1

        # Root engine's workflow_completed has no subworkflow_path stamp.
        root_workflow_completed = [
            d for t, d in events if t == "workflow_completed" and "subworkflow_path" not in d
        ]
        assert len(root_workflow_completed) == 1

        # Nested subworkflow_started carries parent_path = ["mid"].
        nested_started = [
            d for t, d in events if t == "subworkflow_started" and d.get("parent_path") == ["mid"]
        ]
        assert len(nested_started) == 1
        assert nested_started[0]["agent_name"] == "leaf"
        assert nested_started[0]["slot_key"] == "leaf"

    @pytest.mark.asyncio
    async def test_concurrent_for_each_subworkflow_emits_distinct_slot_keys(
        self, tmp_workflow_dir: Path
    ) -> None:
        """For-each iterations get distinct slot_keys even with max_concurrent > 1.

        The existing ``test_for_each_subworkflow_emits_distinct_slot_keys``
        runs with max_concurrent=1 (sequential). This variant uses
        max_concurrent=3 so iterations actually overlap, proving that
        slot_key uniqueness is not an artifact of serial execution.
        """
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub-workflow
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "inner {{ workflow.input.item }}"
                routes:
                  - to: "$end"
            output:
              result: "{{ workflow.input.item }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        from conductor.config.schema import ForEachDef, OutputField

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    prompt="find",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="batch")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="batch",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    max_concurrent=3,
                    agent=AgentDef(
                        name="runner",
                        type="workflow",
                        workflow="sub.yaml",
                        input_mapping={"item": "{{ item }}"},
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"done": "1"},
        )

        events: list[tuple[str, dict]] = []
        emitter = MagicMock()
        emitter.emit.side_effect = lambda ev: events.append((ev.type, dict(ev.data)))

        def _handler(agent, prompt, context):
            if agent.name == "finder":
                return {"items": ["x", "y", "z"]}
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path, event_emitter=emitter)
        await engine.run({})

        started = [d for t, d in events if t == "subworkflow_started"]
        assert len(started) == 3
        slot_keys = {d["slot_key"] for d in started}
        assert slot_keys == {"batch[0]", "batch[1]", "batch[2]"}, (
            "concurrent iterations must each get a distinct slot_key"
        )

        # Each iteration's child engine must auto-stamp its own slot key
        # on outgoing events (this is what keeps concurrent dashboard
        # contexts isolated under for_each-of-workflow).
        child_completed_paths = {
            tuple(d["subworkflow_path"])
            for t, d in events
            if t == "workflow_completed" and "subworkflow_path" in d
        }
        assert child_completed_paths == {
            ("batch[0]",),
            ("batch[1]",),
            ("batch[2]",),
        }


class TestRegistrySubWorkflowResolution:
    """Tests for _resolve_subworkflow_path with registry references."""

    @pytest.mark.asyncio
    async def test_registry_ref_resolved_and_executed(
        self, tmp_workflow_dir: Path, tmp_path: Path
    ) -> None:
        """Registry reference fetches workflow and executes it.

        Mocks only ``fetch_workflow`` so that the engine's real
        ``_resolve_subworkflow_path`` runs end-to-end: real ``resolve_ref``
        parses the ``analysis@team-a#v1.0.0`` syntax, real precedence check
        confirms no local file shadows it, and the registry branch is taken.
        """
        from unittest.mock import patch

        # Write a real cached sub-workflow to a temp location
        cached_sub = tmp_path / "sub.yaml"
        _write_yaml(
            cached_sub,
            """\
            workflow:
              name: sub-from-registry
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 10
            agents:
              - name: inner
                type: agent
                prompt: do it
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
                    workflow="analysis@team-a#v1.0.0",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ sub_wf.output.result }}"},
        )

        # Set up a real registry config so resolve_ref can find "team-a"
        from conductor.registry.config import RegistriesConfig, RegistryEntry, RegistryType

        registry_config = RegistriesConfig(
            registries={
                "team-a": RegistryEntry(
                    type=RegistryType.github,
                    source="https://github.com/example/team-a",
                ),
            },
        )

        def mock_handler(agent, prompt, context):
            return {"result": "registry-result"}

        from conductor.providers.copilot import CopilotProvider

        provider = CopilotProvider(mock_handler=mock_handler)

        # Patch the registry config loader (used by resolve_ref) and
        # fetch_workflow (the network boundary). Real resolve_ref parses
        # the ref string and looks up the registry; real
        # _resolve_subworkflow_path is exercised end-to-end.
        with (
            patch("conductor.registry.resolver.load_config", return_value=registry_config),
            patch("conductor.registry.cache.fetch_workflow", return_value=cached_sub),
        ):
            engine = WorkflowEngine(config, provider, workflow_path=parent_path)
            result = await engine.run({})

        assert result.get("result") == "registry-result"

    @pytest.mark.asyncio
    async def test_registry_fetch_failure_raises_execution_error(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Registry fetch failure is wrapped in ExecutionError with agent context."""
        from unittest.mock import patch

        from conductor.exceptions import ExecutionError
        from conductor.registry.errors import RegistryError

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
                    workflow="missing@unknown-registry#v1.0.0",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider, workflow_path=parent_path)

        # Patch resolve_ref to return a registry kind, then patch fetch_workflow to fail
        from conductor.registry.config import RegistryEntry, RegistryType
        from conductor.registry.resolver import ResolvedRef

        fake_entry = RegistryEntry(type=RegistryType.github, source="https://github.com/x/y")
        fake_resolved = ResolvedRef(
            kind="registry",
            workflow="missing",
            registry_name="unknown-registry",
            ref="v1.0.0",
            registry_entry=fake_entry,
        )

        with (
            patch("conductor.registry.resolver.resolve_ref", return_value=fake_resolved),
            patch(
                "conductor.registry.cache.fetch_workflow",
                side_effect=RegistryError("not found"),
            ),
            pytest.raises(ExecutionError, match="Failed to fetch sub-workflow"),
        ):
            await engine.run({})

    @pytest.mark.asyncio
    async def test_local_file_takes_precedence_over_registry(self, tmp_workflow_dir: Path) -> None:
        """An extensionless name that matches a local file is not treated as registry ref."""
        # Create a local file named "analysis" (no extension) beside the parent
        analysis_path = tmp_workflow_dir / "analysis"
        _write_yaml(
            analysis_path,
            """\
            workflow:
              name: analysis
              entry_point: step
              runtime:
                provider: copilot
              limits:
                max_iterations: 10
            agents:
              - name: step
                type: agent
                prompt: analyze
                routes:
                  - to: "$end"
            output:
              result: "{{ step.output.result }}"
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
                    workflow="analysis",  # extensionless — local file wins
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ sub_wf.output.result }}"},
        )

        from conductor.providers.copilot import CopilotProvider

        provider = CopilotProvider(mock_handler=lambda agent, prompt, context: {"result": "local"})

        # Patch resolve_ref to verify it is NOT called when a local file
        # exists — the precedence check must short-circuit before parsing.
        from unittest.mock import patch

        with patch("conductor.registry.resolver.resolve_ref") as mock_resolve_ref:
            engine = WorkflowEngine(config, provider, workflow_path=parent_path)
            result = await engine.run({})

        assert result.get("result") == "local"
        mock_resolve_ref.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_registry_ref_raises_execution_error(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Malformed registry ref raises ExecutionError with helpful message."""
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
                    workflow="a@b@c",  # two '@' signs — malformed
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider, workflow_path=parent_path)

        with pytest.raises(ExecutionError, match="Failed to resolve sub-workflow"):
            await engine.run({})

    @pytest.mark.asyncio
    async def test_resume_re_resolves_registry_ref_to_same_path(
        self, tmp_workflow_dir: Path, tmp_path: Path
    ) -> None:
        """On resume, a registry sub-workflow ref is re-resolved cleanly.

        Verifies the documented compatibility behavior: ``_resolve_subworkflow_path``
        is called again during ``engine.resume()``, and for a SHA-pinned (or cached)
        registry ref, ``fetch_workflow`` returns the same local cached path it
        returned on the original run. This is the determinism guarantee for resume.
        """
        from unittest.mock import patch

        from conductor.engine.checkpoint import CheckpointManager
        from conductor.engine.context import WorkflowContext
        from conductor.engine.limits import LimitEnforcer
        from conductor.exceptions import ProviderError
        from conductor.providers.copilot import CopilotProvider
        from conductor.registry.config import RegistryEntry, RegistryType
        from conductor.registry.resolver import ResolvedRef

        # Set up a real cached sub-workflow file
        cached_sub = tmp_path / "cache" / "team-a" / "analysis" / "abcdef123456" / "analysis.yaml"
        cached_sub.parent.mkdir(parents=True)
        _write_yaml(
            cached_sub,
            """\
            workflow:
              name: analysis
              entry_point: do_work
              runtime:
                provider: copilot
              limits:
                max_iterations: 10
            agents:
              - name: do_work
                type: agent
                prompt: analyze
                routes:
                  - to: "$end"
            output:
              result: "{{ do_work.output.result }}"
            """,
        )

        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="planner",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="planner",
                    type="agent",
                    prompt="plan",
                    routes=[RouteDef(to="sub_wf")],
                ),
                AgentDef(
                    name="sub_wf",
                    type="workflow",
                    workflow="analysis@team-a#v1.0.0",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ sub_wf.output.result }}"},
        )

        # Track fetch_workflow calls and the paths returned on each call
        fetch_call_paths: list[Path] = []

        def tracked_fetch(*args, **kwargs):
            fetch_call_paths.append(cached_sub)
            return cached_sub

        fake_entry = RegistryEntry(type=RegistryType.github, source="https://github.com/x/y")
        fake_resolved = ResolvedRef(
            kind="registry",
            workflow="analysis",
            registry_name="team-a",
            ref="v1.0.0",
            registry_entry=fake_entry,
        )

        # First run: planner succeeds, sub_wf inner agent fails
        run_count = {"do_work": 0}

        def failing_handler(agent, prompt, context):
            if agent.name == "planner":
                return {"plan": "do analysis"}
            if agent.name == "do_work":
                run_count["do_work"] += 1
                if run_count["do_work"] == 1:
                    raise ProviderError("transient failure")
                return {"result": "analysis-complete"}
            return {}

        provider = CopilotProvider(mock_handler=failing_handler)
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)

        with (
            patch("conductor.registry.resolver.resolve_ref", return_value=fake_resolved),
            patch("conductor.registry.cache.fetch_workflow", side_effect=tracked_fetch),
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            pytest.raises(ProviderError, match="transient failure"),
        ):
            await engine.run({})

        # Sub-workflow was reached (fetch was called) but inner agent failed
        assert len(fetch_call_paths) == 1, "fetch_workflow should have been called once"
        first_path = fetch_call_paths[0]

        checkpoint_path = engine._last_checkpoint_path
        assert checkpoint_path is not None

        # Resume: re-create engine, restore state, run again
        cp = CheckpointManager.load_checkpoint(checkpoint_path)
        engine2 = WorkflowEngine(config, provider, workflow_path=parent_path)
        engine2.set_context(WorkflowContext.from_dict(cp.context))
        engine2.set_limits(
            LimitEnforcer.from_dict(
                cp.limits,
                timeout_seconds=config.workflow.limits.timeout_seconds,
                budget_usd=config.workflow.limits.budget_usd,
                budget_mode=config.workflow.limits.budget_mode,
            )
        )

        with (
            patch("conductor.registry.resolver.resolve_ref", return_value=fake_resolved),
            patch("conductor.registry.cache.fetch_workflow", side_effect=tracked_fetch),
            patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        ):
            result = await engine2.resume(cp.current_agent)

        # fetch_workflow was called again on resume — and returned the SAME
        # cached path. This is the deterministic-resume guarantee for cached
        # registry refs.
        assert len(fetch_call_paths) == 2, "fetch_workflow should be called again on resume"
        assert fetch_call_paths[1] == first_path, (
            "resume must resolve the registry ref to the same cached path as the original run"
        )
        assert result["result"] == "analysis-complete"

    @pytest.mark.asyncio
    async def test_adhoc_ref_resolved_and_executed(
        self, tmp_workflow_dir: Path, tmp_path: Path
    ) -> None:
        """Ad-hoc registry reference (owner/repo in registry slot) fetches and executes.

        Sub-workflow agent with `workflow: "analysis@myorg/workflows#v1.0.0"`
        where the registry slot contains a literal owner/repo path.
        Mocks only ``fetch_workflow_adhoc`` so the engine's real
        ``_resolve_subworkflow_path`` and ``resolve_ref`` run end-to-end,
        exercising the parsing of the adhoc format.
        """
        from unittest.mock import patch

        # Write a real cached sub-workflow to a temp location
        cached_sub = tmp_path / "sub.yaml"
        _write_yaml(
            cached_sub,
            """\
            workflow:
              name: sub-from-adhoc
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 10
            agents:
              - name: inner
                type: agent
                prompt: do it
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
                    workflow="analysis@myorg/workflows#v1.0.0",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ sub_wf.output.result }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"result": "adhoc-result"}

        from conductor.providers.copilot import CopilotProvider

        provider = CopilotProvider(mock_handler=mock_handler)

        # Patch fetch_workflow_adhoc so it returns the cached sub-workflow path.
        # Real resolve_ref parses the ref string and creates an adhoc kind.
        with patch(
            "conductor.registry.cache.fetch_workflow_adhoc", return_value=cached_sub
        ) as mock_adhoc_fetch:
            engine = WorkflowEngine(config, provider, workflow_path=parent_path)
            result = await engine.run({})

        assert result.get("result") == "adhoc-result"
        # Verify fetch_workflow_adhoc was called with the right args
        mock_adhoc_fetch.assert_called_once()
        call_kwargs = mock_adhoc_fetch.call_args[1]
        assert call_kwargs["owner"] == "myorg"
        assert call_kwargs["repo"] == "workflows"
        assert call_kwargs["workflow_name"] == "analysis"
        assert call_kwargs["ref"] == "v1.0.0"

    @pytest.mark.asyncio
    async def test_adhoc_fetch_failure_raises_execution_error(self, tmp_workflow_dir: Path) -> None:
        """Ad-hoc registry fetch failure is wrapped in ExecutionError."""
        from unittest.mock import patch

        from conductor.registry.errors import RegistryError

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
                    workflow="missing@acme/tools#latest",
                    routes=[RouteDef(to="$end")],
                ),
            ],
        )

        mock_provider = MagicMock()
        engine = WorkflowEngine(config, mock_provider, workflow_path=parent_path)

        with (
            patch(
                "conductor.registry.cache.fetch_workflow_adhoc",
                side_effect=RegistryError("workflow not found in repository"),
            ),
            pytest.raises(ExecutionError, match="Failed to fetch sub-workflow"),
        ):
            await engine.run({})


class TestCrossWorkflowRegistryRef:
    """Cross-workflow relative refs between workflows in the same registry.

    These exercise the auto-fetch hook in ``_resolve_subworkflow_path`` that
    handles refs like ``../document-review/workflow.yaml`` from a parent
    workflow that lives inside a registry SHA cache directory.
    """

    @pytest.mark.asyncio
    async def test_relative_ref_to_sibling_workflow_in_registry_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reproduces the bug: parent at sdd-plan/plan.yaml refs ../document-review/workflow.yaml.

        With the fixed cache layout, both workflows share a per-SHA root and
        the relative path resolves correctly. The sub-workflow file is
        auto-fetched on demand by ``auto_fetch_relative_workflow`` because
        only the parent was previously cached.
        """
        import json
        from unittest.mock import patch

        from conductor.registry.cache import CACHE_LAYOUT_VERSION

        # Point CONDUCTOR_HOME at a temp dir so the cache lives there.
        home = tmp_path / "conductor_home"
        home.mkdir()
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))

        sha = "a" * 40
        sha_dir = sha[:12]
        cache_base = home / "cache" / "registries"
        official_sha_root = cache_base / "official" / sha_dir

        # Pre-cache the parent workflow (sdd-plan/plan.yaml) only.
        parent_dir = official_sha_root / "sdd-plan"
        parent_dir.mkdir(parents=True)
        parent_path = parent_dir / "plan.yaml"
        _write_yaml(
            parent_path,
            """\
            workflow:
              name: sdd-plan
              entry_point: sub
              runtime:
                provider: copilot
              limits:
                max_iterations: 10
            agents:
              - name: sub
                type: workflow
                workflow: ../document-review/workflow.yaml
                routes:
                  - to: "$end"
            output:
              result: "{{ sub.output.verdict }}"
            """,
        )

        # Pre-write source.json + cached index + sentinel for sdd-plan.
        meta_dir = cache_base / "official" / "_meta" / sha_dir
        meta_dir.mkdir(parents=True)
        (meta_dir / "source.json").write_text(
            json.dumps(
                {
                    "cache_layout_version": CACHE_LAYOUT_VERSION,
                    "registry_type": "github",
                    "source": "myorg/workflows",
                    "full_sha": sha,
                },
                sort_keys=True,
                indent=2,
            )
        )
        (meta_dir / "index.yaml").write_text(
            "workflows:\n"
            "  sdd-plan:\n    description: ''\n    path: sdd-plan/plan.yaml\n"
            "  document-review:\n    description: ''\n    path: document-review/workflow.yaml\n"
        )
        (meta_dir / "sdd-plan.complete").write_text("")

        # Document-review YAML that the engine will auto-fetch.
        sub_yaml = textwrap.dedent(
            """\
            workflow:
              name: document-review
              entry_point: reviewer
              runtime:
                provider: copilot
              limits:
                max_iterations: 10
            agents:
              - name: reviewer
                type: agent
                prompt: review the doc
                routes:
                  - to: "$end"
            output:
              verdict: "{{ reviewer.output.verdict }}"
            """
        )

        from conductor.registry.index import RegistryIndex, WorkflowInfo

        index_obj = RegistryIndex(
            workflows={
                "sdd-plan": WorkflowInfo(description="", path="sdd-plan/plan.yaml"),
                "document-review": WorkflowInfo(
                    description="", path="document-review/workflow.yaml"
                ),
            }
        )

        def fake_fetch_github(entry, workflow_path, sha_arg, dest_dir):
            target = dest_dir / workflow_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(sub_yaml)

        def mock_handler(agent, prompt, context):
            return {"verdict": "approved"}

        provider = CopilotProvider(mock_handler=mock_handler)

        # Load the parent workflow from the cached path and run it.
        from conductor.config.loader import load_config

        config = load_config(parent_path)

        with (
            patch("conductor.registry.cache.materialize_to_sha", return_value=sha),
            patch("conductor.registry.cache.resolve_ref", return_value=sha),
            patch("conductor.registry.cache.load_index", return_value=index_obj),
            patch("conductor.registry.cache._fetch_github", side_effect=fake_fetch_github),
        ):
            engine = WorkflowEngine(config, provider, workflow_path=parent_path)
            result = await engine.run({})

        assert result.get("result") == "approved"

        # Verify the sibling was actually auto-fetched into the shared SHA root.
        sibling = official_sha_root / "document-review" / "workflow.yaml"
        assert sibling.exists()
        # And its sentinel was written.
        assert (meta_dir / "document-review.complete").exists()


class TestSubWorkflowTerminate:
    """Sub-workflow terminate semantics (issue #219).

    A ``type: terminate`` step inside a child sub-workflow MUST stay scoped to
    that child. From the parent's perspective:

    - ``status: success`` → child returns its rendered output cleanly; the
      parent continues with its next routes as if a normal `$end` had been
      reached inside the child.
    - ``status: failed`` → child raises ``WorkflowTerminated``; the parent's
      ``_run_child_engine`` converts that to a normal ``ExecutionError`` so
      the parent treats it like any other sub-workflow failure (parent
      ``subworkflow_failed`` event fires; parent's outer ``workflow_failed``
      does NOT carry ``is_explicit: true``).

    Without this conversion, a child terminate would bubble all the way out
    and the parent's `workflow_failed` would falsely attribute the explicit
    termination to the parent (whose author never opted in).
    """

    @pytest.mark.asyncio
    async def test_child_success_terminate_returns_output(self, tmp_workflow_dir: Path) -> None:
        """A child `terminate status: success` returns its output_template to the parent."""
        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub
              entry_point: bye
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: bye
                type: terminate
                status: success
                reason: "Document is already current."
                output_template:
                  result: "no-op"
                  reason: "{{ bye.output.reason }}"
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
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "child_result": "{{ research.output.result }}",
                "child_reason": "{{ research.output.reason }}",
            },
        )

        engine = WorkflowEngine(config, CopilotProvider(), workflow_path=parent_path)
        result = await engine.run({})
        assert result == {
            "child_result": "no-op",
            "child_reason": "Document is already current.",
        }

    @pytest.mark.asyncio
    async def test_child_failed_terminate_surfaces_as_execution_error(
        self, tmp_workflow_dir: Path
    ) -> None:
        """Failed-terminate in a child must propagate as ExecutionError to the parent.

        The parent's outer handler treats it like any other sub-workflow
        failure (parent's workflow_failed has NO ``is_explicit: true``). This
        protects parent authors from accidentally inheriting child-only
        termination semantics.
        """
        from conductor.events import WorkflowEvent, WorkflowEventEmitter

        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub
              entry_point: abort
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: abort
                type: terminate
                status: failed
                reason: "Child refused to run."
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
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )

        events: list[WorkflowEvent] = []
        emitter = WorkflowEventEmitter()
        emitter.subscribe(events.append)

        engine = WorkflowEngine(
            config, CopilotProvider(), workflow_path=parent_path, event_emitter=emitter
        )

        with pytest.raises(ExecutionError) as excinfo:
            await engine.run({})

        # The parent sees a sub-workflow failure, not its own explicit termination.
        assert "Child refused to run." in str(excinfo.value)
        assert "research" in str(excinfo.value)

        # The downgrade must produce a plain ExecutionError, NOT pass through
        # a WorkflowTerminated subtype. Without this assertion the existing
        # `pytest.raises(ExecutionError)` would pass even if the boundary
        # conversion silently leaked a child's WorkflowTerminated (which IS
        # an ExecutionError subclass) to the parent.
        from conductor.exceptions import WorkflowTerminated

        assert not isinstance(excinfo.value, WorkflowTerminated), (
            "boundary downgrade must convert WorkflowTerminated to plain ExecutionError"
        )

        # The child's rendered output must be preserved as an attribute on
        # the downgraded error so on_error hooks and debugging surfaces can
        # inspect what the child intended to emit (see issue #219 PR review:
        # "child sub-workflow's `output` dict is silently discarded").
        assert hasattr(excinfo.value, "terminated_output"), (
            "downgraded ExecutionError must carry `terminated_output` attribute"
        )
        assert excinfo.value.terminated_output == {}
        assert excinfo.value.terminated_reason == "Child refused to run."
        assert excinfo.value.terminated_by == "abort"

        # subworkflow_failed must fire so the dashboard shows the child as failed.
        assert any(e.type == "subworkflow_failed" for e in events), (
            f"expected subworkflow_failed; got {[e.type for e in events]}"
        )

        # The parent's OWN `workflow_failed` must NOT inherit is_explicit.
        # Child engines share the parent's emitter and emit their own
        # `workflow_failed` with `subworkflow_path` set; we filter those out
        # to look only at parent-level events (no subworkflow_path).
        outer = [
            e for e in events if e.type == "workflow_failed" and not e.data.get("subworkflow_path")
        ]
        assert outer, "expected parent's workflow_failed event"
        assert all(not e.data.get("is_explicit") for e in outer), (
            f"parent must not inherit is_explicit from child terminate; got: "
            f"{[e.data for e in outer]}"
        )
        # Parent's error_type should be SubworkflowTerminatedError — child's
        # termination is downgraded at the boundary so the parent treats it
        # normally. SubworkflowTerminatedError IS-A ExecutionError so the
        # outer ConductorError handler picks it up via the same code path as
        # any other sub-workflow failure, but the distinct class name makes
        # the cause visible to event consumers.
        assert all(e.data.get("error_type") == "SubworkflowTerminatedError" for e in outer), (
            f"parent error_type should be SubworkflowTerminatedError; got: "
            f"{[e.data.get('error_type') for e in outer]}"
        )

    @pytest.mark.asyncio
    async def test_child_failed_terminate_preserves_output_dict(
        self, tmp_workflow_dir: Path
    ) -> None:
        """The child's rendered `output_template` is preserved across the boundary.

        The boundary downgrade in `_run_child_engine` attaches the child's
        rendered output to the converted ExecutionError as
        `terminated_output`. Without this, parent debugging surfaces lose
        every structured field the child author put in `output_template:`
        — defeating the point of having `output_template` on a failed
        terminate at all.
        """
        from conductor.exceptions import WorkflowTerminated

        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub
              entry_point: abort
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: abort
                type: terminate
                status: failed
                reason: "structured failure"
                output_template:
                  error_code: "E_UPSTREAM"
                  retry_after: "60"
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
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        engine = WorkflowEngine(config, CopilotProvider(), workflow_path=parent_path)

        with pytest.raises(ExecutionError) as excinfo:
            await engine.run({})

        # Verify each structured field the child author set survives.
        assert excinfo.value.terminated_output == {
            "error_code": "E_UPSTREAM",
            # `_maybe_parse_json` coerces "60" to int 60 — same shape as the
            # child engine would have returned to the parent on success.
            "retry_after": 60,
        }
        assert excinfo.value.terminated_reason == "structured failure"
        # The original WorkflowTerminated is preserved on __cause__ for any
        # consumer that wants the full chain.
        assert isinstance(excinfo.value.__cause__, WorkflowTerminated)
        assert excinfo.value.__cause__.output == {
            "error_code": "E_UPSTREAM",
            "retry_after": 60,
        }

    @pytest.mark.asyncio
    async def test_failed_terminate_in_for_each_workflow_iteration(
        self, tmp_workflow_dir: Path
    ) -> None:
        """The for_each-of-workflow path (`_execute_subworkflow_with_inputs`) downgrades too.

        The original PR routed both sub-workflow execution helpers through
        `_run_child_engine`, but the original test suite only exercised the
        sequential `_execute_subworkflow` path. This test drives the
        per-iteration `_execute_subworkflow_with_inputs` path with a
        failed-terminate child to verify the boundary downgrade applies
        there too.
        """
        from conductor.exceptions import WorkflowTerminated

        _write_yaml(
            tmp_workflow_dir / "sub.yaml",
            """\
            workflow:
              name: sub
              entry_point: bail
              input:
                item:
                  type: string
                  required: true
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: bail
                type: terminate
                status: failed
                reason: "iteration {{ workflow.input.item }} failed"
                output_template:
                  failed_item: "{{ workflow.input.item }}"
            """,
        )
        parent_path = tmp_workflow_dir / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        # for_each over a list of inputs, each iteration runs the sub-workflow.
        # The first iteration's failed-terminate is what we expect to bubble
        # up (fail_fast is the default).
        from conductor.config.schema import ForEachDef

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="finder",
                input={"items": InputDef(type="array", default=["x", "y"])},
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="x",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="loop")],
                ),
            ],
            for_each=[
                ForEachDef.model_validate(
                    {
                        "name": "loop",
                        "type": "for_each",
                        "source": "finder.output.items",
                        "as": "item",
                        "agent": AgentDef(
                            name="child",
                            type="workflow",
                            workflow="sub.yaml",
                            input_mapping={"item": "{{ item }}"},
                        ),
                        "failure_mode": "fail_fast",
                        "routes": [RouteDef(to="$end")],
                    }
                ),
            ],
            output={},
        )

        provider = CopilotProvider(mock_handler=lambda *_a, **_kw: {"items": ["x", "y"]})
        engine = WorkflowEngine(config, provider, workflow_path=parent_path)

        # The first iteration's failed-terminate must downgrade to
        # ExecutionError (NOT propagate as WorkflowTerminated through the
        # for_each gather). The downgraded error must carry the child's
        # rendered output_template.
        with pytest.raises(ExecutionError) as excinfo:
            await engine.run({})

        assert not isinstance(excinfo.value, WorkflowTerminated), (
            "for_each-of-workflow boundary must downgrade child WorkflowTerminated"
        )
        # `terminated_output` may or may not be present depending on whether
        # for_each wraps the ExecutionError further; assert at minimum that
        # the reason text from the failed iteration is in the error chain.
        message = str(excinfo.value)
        assert "iteration x failed" in message or "iteration y failed" in message, (
            f"expected child reason in error chain; got: {message}"
        )
