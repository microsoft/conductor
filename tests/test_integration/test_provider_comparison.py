"""Integration tests for provider comparison.

EPIC-008-T7: Provider comparison test (same workflow, different provider)
"""

import pytest

from conductor.config.schema import (
    AgentDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.copilot import CopilotProvider


class TestProviderComparison:
    """EPIC-008-T7: Provider comparison tests."""

    @pytest.mark.asyncio
    async def test_copilot_workflow_baseline(self) -> None:
        """Test workflow execution with Copilot provider."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="comparison-test",
                description="Provider comparison workflow",
                entry_point="qa_agent",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="qa_agent",
                    model="gpt-4",
                    prompt="Answer: {{ workflow.input.question }}",
                    output={"answer": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"qa_agent": "{{ qa_agent.output }}"},
        )

        def mock_handler(agent, prompt, context):
            return {"answer": "Copilot response: A high-level programming language"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        result = await engine.run({"question": "What is Python?"})

        # Verify structure - Copilot returns nested output
        assert "qa_agent" in result
        # Result is a dict with nested agent output
        agent_out = result["qa_agent"]
        if isinstance(agent_out, dict) and "answer" in agent_out:
            assert "programming language" in agent_out["answer"]
        else:
            # If agent output is returned directly as string
            assert "programming language" in str(agent_out)
