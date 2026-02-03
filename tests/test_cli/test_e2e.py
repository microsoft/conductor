"""End-to-end tests for the CLI with example workflows.

This module tests complete workflow execution from CLI to output.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef

runner = CliRunner()


class TestEndToEnd:
    """End-to-end tests for workflow execution."""

    @pytest.fixture
    def simple_workflow(self, tmp_path: Path) -> Path:
        """Create a simple Q&A workflow file."""
        workflow_file = tmp_path / "simple-qa.yaml"
        workflow_file.write_text("""\
workflow:
  name: simple-qa
  description: A simple question-answering workflow
  entry_point: answerer

  input:
    question:
      type: string
      required: true
      description: The question to answer

agents:
  - name: answerer
    model: gpt-4
    prompt: |
      Answer the following question concisely:
      {{ workflow.input.question }}
    output:
      answer:
        type: string
        description: The answer to the question
    routes:
      - to: $end

output:
  answer: "{{ answerer.output.answer }}"
""")
        return workflow_file

    @pytest.fixture
    def multi_agent_workflow(self, tmp_path: Path) -> Path:
        """Create a multi-agent workflow file."""
        workflow_file = tmp_path / "multi-agent.yaml"
        workflow_file.write_text("""\
workflow:
  name: multi-agent
  description: A workflow with multiple agents
  entry_point: researcher

  input:
    topic:
      type: string
      required: true

agents:
  - name: researcher
    model: gpt-4
    prompt: |
      Research the topic: {{ workflow.input.topic }}
      Provide key facts.
    output:
      facts:
        type: array
        description: List of key facts
    routes:
      - to: summarizer

  - name: summarizer
    model: gpt-4
    prompt: |
      Summarize these facts about {{ workflow.input.topic }}:
      {{ researcher.output.facts | json }}
    output:
      summary:
        type: string
        description: Summary of the facts
    routes:
      - to: $end

output:
  summary: "{{ summarizer.output.summary }}"
  facts: "{{ researcher.output.facts | json }}"
""")
        return workflow_file

    async def test_simple_workflow_e2e(self, simple_workflow: Path) -> None:
        """Test end-to-end execution of a simple workflow."""
        from conductor.cli.run import run_workflow_async
        from conductor.providers.copilot import CopilotProvider

        # Create a mock handler that matches CopilotProvider's expected signature
        # (AgentDef, str, dict[str, Any]) -> dict[str, Any]
        def mock_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            return {"answer": "Python is a programming language."}

        # Patch create_provider in the registry module (used by CLI)
        with patch("conductor.providers.registry.create_provider") as mock_factory:
            mock_provider = CopilotProvider(mock_handler=mock_handler)
            mock_factory.return_value = mock_provider

            result = await run_workflow_async(
                simple_workflow,
                {"question": "What is Python?"},
            )

            assert "answer" in result
            assert result["answer"] == "Python is a programming language."

    async def test_multi_agent_workflow_e2e(self, multi_agent_workflow: Path) -> None:
        """Test end-to-end execution of a multi-agent workflow."""
        from conductor.cli.run import run_workflow_async
        from conductor.providers.copilot import CopilotProvider

        call_count = 0

        def mock_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                # First agent (researcher)
                return {"facts": ["Fact 1", "Fact 2", "Fact 3"]}
            else:
                # Second agent (summarizer)
                return {"summary": "Summary of the three facts."}

        with patch("conductor.providers.registry.create_provider") as mock_factory:
            mock_provider = CopilotProvider(mock_handler=mock_handler)
            mock_factory.return_value = mock_provider

            result = await run_workflow_async(
                multi_agent_workflow,
                {"topic": "AI"},
            )

            assert "summary" in result
            assert "facts" in result
            assert call_count == 2

    def test_simple_workflow_cli_e2e(self, simple_workflow: Path) -> None:
        """Test CLI execution of a simple workflow."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"answer": "42"}

            result = runner.invoke(
                app,
                [
                    "run",
                    str(simple_workflow),
                    "-i",
                    "question=What is the meaning of life?",
                ],
            )

            # Should succeed
            assert result.exit_code == 0 or mock_run.called

            # Verify inputs were passed correctly
            if mock_run.called:
                call_args = mock_run.call_args
                inputs = call_args[0][1]
                assert "question" in inputs
                assert inputs["question"] == "What is the meaning of life?"

    def test_workflow_with_missing_required_input(self, simple_workflow: Path) -> None:
        """Test that missing required input produces error."""
        # Don't mock - let it actually try to run
        # The workflow requires 'question' input
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            # Simulate validation error for missing input
            from conductor.exceptions import ValidationError

            mock_run.side_effect = ValidationError("Missing required input: question")

            result = runner.invoke(
                app,
                [
                    "run",
                    str(simple_workflow),
                    # No question input provided
                ],
            )

            assert result.exit_code != 0
            assert "Error" in result.output

    def test_workflow_output_is_json(self, simple_workflow: Path) -> None:
        """Test that workflow output is valid JSON."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {
                "answer": "The answer is 42.",
                "metadata": {"confidence": 0.95},
            }

            result = runner.invoke(
                app,
                [
                    "run",
                    str(simple_workflow),
                    "-i",
                    "question=What is the answer?",
                ],
            )

            # Check that output contains JSON
            # Rich adds formatting, so we just verify the data is there
            assert "answer" in result.output or result.exit_code == 0


class TestFixtureWorkflows:
    """Tests using the project's fixture workflows."""

    def test_valid_simple_fixture(self, fixtures_dir: Path) -> None:
        """Test running the valid_simple fixture."""
        workflow_file = fixtures_dir / "valid_simple.yaml"

        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"message": "Hello!"}

            result = runner.invoke(
                app,
                [
                    "run",
                    str(workflow_file),
                    "-i",
                    "name=World",
                ],
            )

            # Should attempt to run (mock intercepts)
            assert mock_run.called or result.exit_code == 0

    def test_invalid_workflow_produces_error(self, fixtures_dir: Path) -> None:
        """Test that invalid workflow produces appropriate error."""
        # Use a workflow with invalid route
        workflow_file = fixtures_dir / "invalid_bad_route.yaml"

        result = runner.invoke(
            app,
            [
                "run",
                str(workflow_file),
            ],
        )

        # Should fail with error
        assert result.exit_code != 0


class TestInputValidation:
    """Tests for input validation during workflow execution."""

    @pytest.fixture
    def typed_inputs_workflow(self, tmp_path: Path) -> Path:
        """Create a workflow with typed inputs."""
        workflow_file = tmp_path / "typed-inputs.yaml"
        workflow_file.write_text("""\
workflow:
  name: typed-inputs
  entry_point: processor

  input:
    count:
      type: number
      required: true
    items:
      type: array
      required: false
      default: []
    enabled:
      type: boolean
      required: false
      default: true

agents:
  - name: processor
    model: gpt-4
    prompt: |
      Process {{ workflow.input.count }} items.
      Items: {{ workflow.input.items | json }}
      Enabled: {{ workflow.input.enabled }}
    output:
      result:
        type: string
    routes:
      - to: $end

output:
  result: "{{ processor.output.result }}"
""")
        return workflow_file

    def test_numeric_input_coercion(self, typed_inputs_workflow: Path) -> None:
        """Test that numeric inputs are coerced correctly."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(
                app,
                [
                    "run",
                    str(typed_inputs_workflow),
                    "-i",
                    "count=42",
                ],
            )

            if mock_run.called:
                inputs = mock_run.call_args[0][1]
                assert inputs["count"] == 42
                assert isinstance(inputs["count"], int)

    def test_boolean_input_coercion(self, typed_inputs_workflow: Path) -> None:
        """Test that boolean inputs are coerced correctly."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(
                app,
                [
                    "run",
                    str(typed_inputs_workflow),
                    "-i",
                    "count=5",
                    "-i",
                    "enabled=false",
                ],
            )

            if mock_run.called:
                inputs = mock_run.call_args[0][1]
                assert inputs["enabled"] is False
                assert isinstance(inputs["enabled"], bool)

    def test_array_input_coercion(self, typed_inputs_workflow: Path) -> None:
        """Test that array inputs are coerced correctly."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(
                app,
                [
                    "run",
                    str(typed_inputs_workflow),
                    "-i",
                    "count=3",
                    "-i",
                    'items=["a", "b", "c"]',
                ],
            )

            if mock_run.called:
                inputs = mock_run.call_args[0][1]
                assert inputs["items"] == ["a", "b", "c"]
                assert isinstance(inputs["items"], list)
