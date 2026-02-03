"""Integration tests for example workflows.

These tests verify that the example workflows in the examples/ directory
are valid and can execute successfully with mock providers.

Note: Tests use mock handlers to verify workflow structure and execution logic.
Example workflows have been validated to work correctly with mock providers.
For testing with real LLM providers, run examples manually with the conductor CLI.
"""

import asyncio
from pathlib import Path

import pytest

from conductor.config.loader import load_config
from conductor.engine.workflow import WorkflowEngine


class TestParallelExamples:
    """Integration tests for parallel execution examples."""

    def test_parallel_research_workflow_loads(self) -> None:
        """Test that parallel-research.yaml loads and validates successfully."""
        examples_dir = Path(__file__).parent.parent.parent / "examples"
        workflow_file = examples_dir / "parallel-research.yaml"

        assert workflow_file.exists(), f"Example file not found: {workflow_file}"

        # Load and validate the workflow
        config = load_config(workflow_file)

        assert config.workflow.name == "parallel-research"
        assert config.workflow.entry_point == "planner"

        # Verify parallel group exists in the parallel list
        assert len(config.parallel) >= 1, "Should have at least one parallel group"

        # Verify the parallel_researchers group
        parallel_researchers = next(
            (g for g in config.parallel if g.name == "parallel_researchers"), None
        )
        assert parallel_researchers is not None
        assert len(parallel_researchers.agents) == 3
        assert parallel_researchers.failure_mode == "continue_on_error"

    def test_parallel_validation_workflow_loads(self) -> None:
        """Test that parallel-validation.yaml loads and validates successfully."""
        examples_dir = Path(__file__).parent.parent.parent / "examples"
        workflow_file = examples_dir / "parallel-validation.yaml"

        assert workflow_file.exists(), f"Example file not found: {workflow_file}"

        # Load and validate the workflow
        config = load_config(workflow_file)

        assert config.workflow.name == "parallel-validation"
        assert config.workflow.entry_point == "code_analyzer"

        # Verify parallel group exists in the parallel list
        assert len(config.parallel) >= 1, "Should have at least one parallel group"

        # Verify the parallel_validators group
        parallel_validators = next(
            (g for g in config.parallel if g.name == "parallel_validators"), None
        )
        assert parallel_validators is not None
        assert len(parallel_validators.agents) == 4
        assert parallel_validators.failure_mode == "all_or_nothing"

    def test_parallel_research_workflow_executes(self) -> None:
        """Test that parallel-research.yaml executes with mock provider."""
        examples_dir = Path(__file__).parent.parent.parent / "examples"
        workflow_file = examples_dir / "parallel-research.yaml"

        config = load_config(workflow_file)

        # Create mock handler that returns appropriate outputs
        def mock_handler(agent, prompt, context):
            """Mock handler for testing parallel research workflow."""
            agent_outputs = {
                "planner": {
                    "plan": {
                        "questions": ["Q1", "Q2", "Q3"],
                        "areas": ["A1", "A2"],
                        "sources": ["academic", "web", "technical"],
                    },
                    "summary": "Research AI in healthcare",
                },
                "academic_researcher": {
                    "findings": ["Finding 1", "Finding 2"],
                    "sources": ["Source 1", "Source 2"],
                    "confidence": "high",
                },
                "web_researcher": {
                    "findings": ["Web finding 1", "Web finding 2"],
                    "sources": ["Web source 1", "Web source 2"],
                    "confidence": "medium",
                },
                "technical_researcher": {
                    "findings": ["Tech finding 1"],
                    "sources": ["Tech source 1"],
                    "confidence": "high",
                },
                "synthesizer": {
                    "executive_summary": "AI is transforming healthcare...",
                    "key_insights": ["Insight 1", "Insight 2", "Insight 3"],
                    "synthesis": "Detailed synthesis of findings...",
                    "sources_analyzed": 5,
                    "research_quality": "high",
                },
                "quality_checker": {
                    "quality_score": 8,
                    "questions_answered": 3,
                    "coverage_complete": True,
                    "gaps": [],
                    "recommendation": "Accept",
                },
            }
            return agent_outputs.get(agent.name, {"result": "default"})

        # Use a mock provider
        from conductor.providers.copilot import CopilotProvider

        provider = CopilotProvider(mock_handler=mock_handler)

        engine = WorkflowEngine(config, provider)

        # Run with test inputs
        result = asyncio.run(engine.run({"topic": "AI in healthcare", "depth": "moderate"}))

        # Verify output structure
        assert "topic" in result
        assert result["topic"] == "AI in healthcare"
        assert "executive_summary" in result
        assert "quality_score" in result
        assert "researchers_succeeded" in result

    def test_parallel_validation_workflow_executes(self) -> None:
        """Test that parallel-validation.yaml executes with mock provider."""
        examples_dir = Path(__file__).parent.parent.parent / "examples"
        workflow_file = examples_dir / "parallel-validation.yaml"

        config = load_config(workflow_file)

        # Create mock handler that returns appropriate outputs
        def mock_handler(agent, prompt, context):
            """Mock handler for testing parallel validation workflow."""
            agent_outputs = {
                "code_analyzer": {
                    "description": "A simple hello world function",
                    "complexity": "simple",
                    "components": ["function", "print statement"],
                    "checks_needed": ["syntax", "security", "style", "logic"],
                },
                "syntax_validator": {
                    "passed": True,
                    "issues": [],
                    "severity": "none",
                    "details": "No syntax issues found",
                },
                "security_validator": {
                    "passed": True,
                    "vulnerabilities": [],
                    "risk_level": "none",
                    "details": "No security vulnerabilities found",
                },
                "style_validator": {
                    "passed": True,
                    "violations": [],
                    "score": 95,
                    "details": "Code follows style guidelines",
                },
                "logic_validator": {
                    "passed": True,
                    "issues": [],
                    "bug_count": 0,
                    "confidence": "high",
                    "details": "No logic issues found",
                },
                "validation_summary": {
                    "overall_status": "passed",
                    "total_issues": 0,
                    "critical_issues": 0,
                    "recommendations": [],
                    "approval": "approved",
                    "summary": "All validation checks passed",
                },
                "auto_approve": {
                    "approval_message": "Code approved",
                    "approved_at": "2024-01-01T00:00:00Z",
                },
            }
            return agent_outputs.get(agent.name, {"result": "default"})

        # Use a mock provider
        from conductor.providers.copilot import CopilotProvider

        provider = CopilotProvider(mock_handler=mock_handler)

        engine = WorkflowEngine(config, provider)

        # Run with test inputs
        result = asyncio.run(
            engine.run(
                {"code": "def hello(): print('world')", "language": "python", "strict_mode": False}
            )
        )

        # Verify output structure
        assert "validation_status" in result
        assert result["validation_status"] == "passed"
        assert "approval_decision" in result
        assert result["approval_decision"] == "approved"
        assert "syntax_passed" in result
        assert result["syntax_passed"] == "True"  # Templates return strings


class TestExampleWorkflowsValidity:
    """Tests to ensure all example workflows are valid."""

    @pytest.mark.parametrize(
        "example_file",
        [
            "simple-qa.yaml",
            "parallel-research.yaml",
            "parallel-validation.yaml",
            "research-assistant.yaml",
            "design-review.yaml",
        ],
    )
    def test_example_workflow_is_valid(self, example_file: str) -> None:
        """Test that each example workflow is valid and loads successfully."""
        examples_dir = Path(__file__).parent.parent.parent / "examples"
        workflow_file = examples_dir / example_file

        # Skip if file doesn't exist (some may not be created yet)
        if not workflow_file.exists():
            pytest.skip(f"Example file not found: {example_file}")

        # Load and validate - this will raise if invalid
        config = load_config(workflow_file)

        # Basic assertions
        assert config.workflow.name is not None
        assert config.workflow.entry_point is not None
        assert len(config.agents) > 0
