"""Integration tests for the implement.yaml workflow flow.

Verifies the epic_selector → coder → epic_reviewer → committer loop
routes correctly across multiple epics, and routes to plan_reviewer
when all epics are complete.
"""

from pathlib import Path
from typing import Any

import pytest

from conductor.config.loader import load_workflow
from conductor.config.schema import AgentDef
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.copilot import CopilotProvider

IMPLEMENT_YAML = Path(__file__).parent.parent.parent / "examples" / "implement.yaml"


def create_implement_mock_handler(
    total_epics: int = 3,
    reject_epic: int | None = None,
) -> tuple[Any, list[str]]:
    """Create a mock handler that simulates the implement workflow agents.

    Args:
        total_epics: Number of epics to simulate.
        reject_epic: If set, the epic_reviewer will REQUEST_CHANGES on this
            epic number (1-indexed) the first time, then APPROVE on retry.

    Returns:
        Tuple of (mock_handler, agent_call_log) where agent_call_log is a
        list of agent names in the order they were called.
    """
    agent_calls: list[str] = []
    epic_counter = {"current": 0, "rejected": set()}

    def mock_handler(
        agent: AgentDef, rendered_prompt: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        agent_calls.append(agent.name)

        if agent.name == "epic_selector":
            epic_counter["current"] += 1
            epic_num = epic_counter["current"]

            if epic_num > total_epics:
                return {
                    "plan_summary": "Test plan with epics",
                    "all_epics": [
                        {"id": f"EPIC-{i:03d}", "status": "DONE"} for i in range(1, total_epics + 1)
                    ],
                    "current_epic": "",
                    "epic_details": "",
                    "prerequisites_met": True,
                    "all_complete": True,
                    "remaining_count": 0,
                }

            return {
                "plan_summary": f"Test plan: {total_epics} epics total",
                "all_epics": [
                    {
                        "id": f"EPIC-{i:03d}",
                        "status": "DONE" if i < epic_num else "NOT STARTED",
                    }
                    for i in range(1, total_epics + 1)
                ],
                "current_epic": f"EPIC-{epic_num:03d}",
                "epic_details": f"Details for EPIC-{epic_num:03d}: implement feature {epic_num}",
                "prerequisites_met": True,
                "all_complete": False,
                "remaining_count": total_epics - epic_num + 1,
            }

        elif agent.name == "coder":
            # Extract epic from context
            _epic = "EPIC-???"
            for line in rendered_prompt.split("\n"):
                if "EPIC-" in line and "Implement" in line:
                    _epic = line.strip().split("**")[-2] if "**" in line else line.strip()
                    break

            return {
                "current_epic": f"EPIC-{epic_counter['current']:03d}",
                "epic_details": f"Implemented EPIC-{epic_counter['current']:03d}",
                "files_modified": [f"src/feature_{epic_counter['current']}.py"],
                "changes_made": [f"Added feature {epic_counter['current']}"],
                "tests_added": [f"tests/test_feature_{epic_counter['current']}.py"],
                "edge_cases_handled": ["null input"],
                "implementation_notes": "Implementation complete",
            }

        elif agent.name == "epic_reviewer":
            current = epic_counter["current"]
            # If this epic should be rejected and hasn't been yet
            if reject_epic and current == reject_epic and current not in epic_counter["rejected"]:
                epic_counter["rejected"].add(current)
                return {
                    "decision": "REQUEST_CHANGES",
                    "feedback": "Need better error handling",
                    "issues": ["Missing null check", "No logging"],
                    "strengths": ["Good structure"],
                    "approved": False,
                }

            return {
                "decision": "APPROVE",
                "feedback": "Implementation looks good",
                "issues": [],
                "strengths": ["Clean code", "Good tests"],
                "approved": True,
            }

        elif agent.name == "committer":
            current = epic_counter["current"]
            remaining = total_epics - current
            return {
                "epic_completed": f"EPIC-{current:03d}",
                "commit_message": f"EPIC-{current:03d}: Implement feature {current}",
                "plan_updated": True,
                "remaining_epics": [f"EPIC-{i:03d}" for i in range(current + 1, total_epics + 1)],
                "all_complete": remaining == 0,
                "next_epic": f"EPIC-{current + 1:03d}" if remaining > 0 else "",
            }

        elif agent.name == "plan_reviewer":
            return {
                "decision": "APPROVE",
                "feedback": "All changes look great",
                "architecture_issues": [],
                "code_issues": [],
                "documentation_issues": [],
                "test_gaps": [],
                "strengths": ["Consistent patterns", "Good coverage"],
                "approved": True,
            }

        elif agent.name == "fixer":
            return {
                "issues_fixed": ["Fixed all issues"],
                "files_modified": ["src/fix.py"],
                "tests_added": ["tests/test_fix.py"],
                "documentation_updated": ["README.md"],
                "commit_message": "fix: Address review feedback",
                "fix_notes": "All issues resolved",
                "all_issues_resolved": True,
            }

        return {}

    return mock_handler, agent_calls


@pytest.fixture
def implement_config():
    """Load the implement.yaml workflow config."""
    if not IMPLEMENT_YAML.exists():
        pytest.skip(f"implement.yaml not found at {IMPLEMENT_YAML}")
    return load_workflow(IMPLEMENT_YAML)


class TestImplementWorkflowFlow:
    """Tests verifying the routing flow of implement.yaml."""

    @pytest.mark.asyncio
    async def test_single_epic_flow(self, implement_config) -> None:
        """Test flow with 1 epic: selector → coder → reviewer → committer."""
        mock_handler, agent_calls = create_implement_mock_handler(total_epics=1)
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(implement_config, provider)

        result = await engine.run({"plan": "test-plan.md"})

        # Verify agent call sequence
        assert agent_calls == [
            "epic_selector",  # Selects EPIC-001
            "coder",  # Implements EPIC-001
            "epic_reviewer",  # Reviews EPIC-001
            "committer",  # Commits, reports all_complete=True
            "plan_reviewer",  # Holistic review, approves
        ]
        assert result is not None
        await provider.close()

    @pytest.mark.asyncio
    async def test_multi_epic_flow(self, implement_config) -> None:
        """Test flow with 3 epics loops through epic_selector for each."""
        mock_handler, agent_calls = create_implement_mock_handler(total_epics=3)
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(implement_config, provider)

        result = await engine.run({"plan": "test-plan.md"})

        # Should loop: selector→coder→reviewer→committer, 3 times,
        # then committer routes to plan_reviewer
        expected = []
        for _i in range(3):
            expected.extend(["epic_selector", "coder", "epic_reviewer", "committer"])
        expected.append("plan_reviewer")

        assert agent_calls == expected
        assert result is not None
        await provider.close()

    @pytest.mark.asyncio
    async def test_epic_reviewer_reject_loops_to_coder(self, implement_config) -> None:
        """Test that REQUEST_CHANGES from epic_reviewer routes back to coder, not epic_selector."""
        mock_handler, agent_calls = create_implement_mock_handler(total_epics=1, reject_epic=1)
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(implement_config, provider)

        result = await engine.run({"plan": "test-plan.md"})

        # Epic 1: selector → coder → reviewer (REJECT) → coder → reviewer (APPROVE) → committer
        # Then: plan_reviewer
        assert agent_calls == [
            "epic_selector",  # Selects EPIC-001
            "coder",  # Implements (first attempt)
            "epic_reviewer",  # Rejects
            "coder",  # Re-implements
            "epic_reviewer",  # Approves
            "committer",  # Commits, all_complete
            "plan_reviewer",  # Holistic review
        ]
        assert result is not None
        await provider.close()

    @pytest.mark.asyncio
    async def test_coder_always_routes_to_reviewer(self, implement_config) -> None:
        """Verify coder ALWAYS routes to epic_reviewer (no conditional skip)."""
        mock_handler, agent_calls = create_implement_mock_handler(total_epics=2)
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(implement_config, provider)

        await engine.run({"plan": "test-plan.md"})

        # Every coder call should be immediately followed by epic_reviewer
        for i, call in enumerate(agent_calls):
            if call == "coder":
                assert i + 1 < len(agent_calls)
                assert agent_calls[i + 1] == "epic_reviewer", (
                    f"coder at index {i} was followed by '{agent_calls[i + 1]}', "
                    f"expected 'epic_reviewer'"
                )
        await provider.close()

    @pytest.mark.asyncio
    async def test_committer_loops_to_epic_selector_not_coder(self, implement_config) -> None:
        """Verify committer routes to epic_selector (not coder) when more epics remain."""
        mock_handler, agent_calls = create_implement_mock_handler(total_epics=2)
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(implement_config, provider)

        await engine.run({"plan": "test-plan.md"})

        # Every committer call (except the last) should be followed by epic_selector
        committer_indices = [i for i, c in enumerate(agent_calls) if c == "committer"]
        for idx in committer_indices[:-1]:  # All except last
            assert agent_calls[idx + 1] == "epic_selector", (
                f"committer at index {idx} was followed by '{agent_calls[idx + 1]}', "
                f"expected 'epic_selector'"
            )
        # Last committer should be followed by plan_reviewer
        last_committer = committer_indices[-1]
        assert agent_calls[last_committer + 1] == "plan_reviewer"
        await provider.close()

    @pytest.mark.asyncio
    async def test_entry_point_is_epic_selector(self, implement_config) -> None:
        """Verify the workflow starts with epic_selector, not coder."""
        mock_handler, agent_calls = create_implement_mock_handler(total_epics=1)
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(implement_config, provider)

        await engine.run({"plan": "test-plan.md"})

        assert agent_calls[0] == "epic_selector"
        await provider.close()

    @pytest.mark.asyncio
    async def test_epic_selector_all_complete_routes_to_plan_reviewer(
        self, implement_config
    ) -> None:
        """If epic_selector reports all_complete, routes directly to plan_reviewer."""
        mock_handler, agent_calls = create_implement_mock_handler(total_epics=0)
        # Override: epic_selector will immediately say all_complete
        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(implement_config, provider)

        await engine.run({"plan": "test-plan.md"})

        assert agent_calls == ["epic_selector", "plan_reviewer"]
        await provider.close()
