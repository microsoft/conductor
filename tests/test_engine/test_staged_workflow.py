"""Tests for staged re-invocation feature in the workflow engine.

Verifies end-to-end staged workflow execution, context dual-key storage,
the ``stages`` dict injection for Jinja2 templates, and backward compatibility
with classic loop-back patterns.
"""

from __future__ import annotations

import pytest

from conductor.config.expander import expand_stages
from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    StageDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.context import WorkflowContext
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.copilot import CopilotProvider


class TestStagedWorkflowExecution:
    """Integration tests for staged agent execution through the workflow engine."""

    @pytest.mark.asyncio
    async def test_vp_ic_vp_review_workflow(self) -> None:
        """Test VP → IC → VP:review end-to-end staged workflow.

        Constructs a three-step workflow where the VP agent plans, IC implements,
        and VP is re-invoked via its ``review`` stage to approve. Verifies that
        both VP outputs are independently accessible and the final output is
        correctly rendered from stage-qualified template references.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="staged-test",
                entry_point="vp",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Plan: {{ workflow.input.project }}",
                    output={"plan": OutputField(type="string")},
                    routes=[RouteDef(to="ic")],
                    stages={
                        "review": StageDef(
                            prompt="Review: {{ ic.output.impl }}",
                            input=["ic.output"],
                            output={
                                "approved": OutputField(type="boolean"),
                                "feedback": OutputField(type="string"),
                            },
                            routes=[RouteDef(to="$end")],
                        ),
                    },
                ),
                AgentDef(
                    name="ic",
                    model="gpt-4",
                    prompt="Implement: {{ vp.output.plan }}",
                    output={"impl": OutputField(type="string")},
                    routes=[RouteDef(to="vp:review")],
                ),
            ],
            output={
                "plan": "{{ stages.vp.default.output.plan }}",
                "approved": "{{ stages.vp.review.output.approved }}",
            },
        )
        config = expand_stages(config)

        responses = {
            "vp:default": {"plan": "Build microservice"},
            "ic": {"impl": "Built auth module"},
            "vp:review": {"approved": True, "feedback": "Looks good"},
        }

        def mock_handler(agent, prompt, context):
            return responses[agent.name]

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({"project": "auth-service"})

        assert result["plan"] == "Build microservice"
        assert result["approved"] == "True"

        summary = engine.get_execution_summary()
        assert summary["agents_executed"] == ["vp:default", "ic", "vp:review"]

    @pytest.mark.asyncio
    async def test_staged_workflow_with_loop_back(self) -> None:
        """Test VP → IC → VP:review → IC (loop) → VP:review → $end.

        The VP:review stage can loop back to IC for revisions when it does not
        approve. On the first review the mock rejects; on the second it
        approves.  Verifies the loop-back pattern executes correctly and the
        final result reflects approval.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="staged-loop-test",
                entry_point="vp",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="vp",
                    model="gpt-4",
                    prompt="Plan: {{ workflow.input.project }}",
                    output={"plan": OutputField(type="string")},
                    routes=[RouteDef(to="ic")],
                    stages={
                        "review": StageDef(
                            prompt="Review: {{ ic.output.impl }}",
                            input=["ic.output"],
                            output={
                                "approved": OutputField(type="boolean"),
                                "feedback": OutputField(type="string"),
                            },
                            routes=[
                                RouteDef(to="$end", when="{{ output.approved }}"),
                                RouteDef(to="ic"),
                            ],
                        ),
                    },
                ),
                AgentDef(
                    name="ic",
                    model="gpt-4",
                    # Use stage-qualified reference because the vp base key
                    # gets overwritten by the review stage via dual-key storage.
                    prompt="Implement: {{ stages.vp.default.output.plan }}",
                    output={"impl": OutputField(type="string")},
                    routes=[RouteDef(to="vp:review")],
                ),
            ],
            output={
                "approved": "{{ stages.vp.review.output.approved }}",
                "feedback": "{{ stages.vp.review.output.feedback }}",
            },
        )
        config = expand_stages(config)

        review_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal review_count
            if agent.name == "vp:default":
                return {"plan": "Build microservice"}
            if agent.name == "ic":
                revision = review_count + 1
                return {"impl": f"Implementation v{revision}"}
            if agent.name == "vp:review":
                review_count += 1
                if review_count == 1:
                    return {"approved": False, "feedback": "Needs revision"}
                return {"approved": True, "feedback": "Approved"}
            raise ValueError(f"Unexpected agent: {agent.name}")

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({"project": "auth-service"})

        assert result["approved"] == "True"
        assert result["feedback"] == "Approved"

        summary = engine.get_execution_summary()
        assert summary["agents_executed"] == [
            "vp:default",
            "ic",
            "vp:review",
            "ic",
            "vp:review",
        ]

    @pytest.mark.asyncio
    async def test_backward_compat_loop_back_without_stages(self) -> None:
        """Test classic drafter → reviewer → drafter loop without stages.

        Ensures that the traditional loop-back pattern (no stages) continues to
        work correctly after the staged-agent feature was introduced.  The
        reviewer rejects on the first pass and approves on the second.
        """
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="loop-test",
                entry_point="drafter",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="drafter",
                    model="gpt-4",
                    prompt="Draft",
                    output={"draft": OutputField(type="string")},
                    routes=[RouteDef(to="reviewer")],
                ),
                AgentDef(
                    name="reviewer",
                    model="gpt-4",
                    prompt="Review: {{ drafter.output.draft }}",
                    output={"approved": OutputField(type="boolean")},
                    routes=[
                        RouteDef(to="$end", when="{{ output.approved }}"),
                        RouteDef(to="drafter"),
                    ],
                ),
            ],
            output={"final": "{{ drafter.output.draft }}"},
        )

        draft_count = 0

        def mock_handler(agent, prompt, context):
            nonlocal draft_count
            if agent.name == "drafter":
                draft_count += 1
                return {"draft": f"Draft v{draft_count}"}
            if agent.name == "reviewer":
                approved = draft_count >= 2
                return {"approved": approved}
            raise ValueError(f"Unexpected agent: {agent.name}")

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(config, provider)

        result = await engine.run({})

        assert result["final"] == "Draft v2"

        summary = engine.get_execution_summary()
        assert summary["agents_executed"] == [
            "drafter",
            "reviewer",
            "drafter",
            "reviewer",
        ]


class TestStagedContextStorage:
    """Tests for WorkflowContext dual-key storage and stages dict injection."""

    def test_dual_key_storage(self) -> None:
        """Test that stage-qualified names produce dual-key entries.

        Storing output under ``vp:default`` should create entries for both
        ``vp:default`` and the base name ``vp``.  A subsequent store under
        ``vp:review`` should overwrite the ``vp`` base key while preserving
        the original ``vp:default`` entry.
        """
        ctx = WorkflowContext()
        ctx.store("vp:default", {"plan": "Build it"})

        # Both keys exist
        assert "vp:default" in ctx.agent_outputs
        assert "vp" in ctx.agent_outputs
        assert ctx.agent_outputs["vp:default"]["plan"] == "Build it"
        assert ctx.agent_outputs["vp"]["plan"] == "Build it"

        # Store review stage — base name overwrites
        ctx.store("vp:review", {"approved": True})
        assert ctx.agent_outputs["vp:review"]["approved"] is True
        assert ctx.agent_outputs["vp"]["approved"] is True  # overwritten

        # Stage-specific keys preserved
        assert ctx.agent_outputs["vp:default"]["plan"] == "Build it"

    def test_stages_dict_in_context(self) -> None:
        """Test that ``_build_stages_dict()`` produces correct nested structure.

        After storing outputs for ``vp:default``, ``ic``, and ``vp:review``,
        the stages dict should contain nested entries for the ``vp`` base name
        only (``ic`` is not stage-qualified).
        """
        ctx = WorkflowContext()
        ctx.store("vp:default", {"plan": "Build it"})
        ctx.store("ic", {"impl": "Done"})
        ctx.store("vp:review", {"approved": True})

        agent_ctx = ctx.build_for_agent("output", [], mode="accumulate")

        assert "stages" in agent_ctx
        assert "vp" in agent_ctx["stages"]
        assert "default" in agent_ctx["stages"]["vp"]
        assert "review" in agent_ctx["stages"]["vp"]
        assert agent_ctx["stages"]["vp"]["default"]["output"]["plan"] == "Build it"
        assert agent_ctx["stages"]["vp"]["review"]["output"]["approved"] is True

        # Non-staged agents do not appear in stages dict
        assert "ic" not in agent_ctx["stages"]

    def test_stages_dict_empty_when_no_stages(self) -> None:
        """Test that stages dict is empty when no colon-qualified outputs exist."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"data": "value"})

        agent_ctx = ctx.build_for_agent("agent2", [], mode="accumulate")

        assert agent_ctx["stages"] == {}

    def test_non_staged_agent_store_unchanged(self) -> None:
        """Test that storing a regular (non-staged) agent creates exactly one key."""
        ctx = WorkflowContext()
        ctx.store("agent1", {"x": 1})

        assert "agent1" in ctx.agent_outputs
        # No extra base-name key created for a non-colon-qualified name
        assert len(ctx.agent_outputs) == 1

    def test_explicit_mode_with_stage_qualified_input(self) -> None:
        """Test explicit context mode with stage-qualified input references.

        When an agent declares ``vp:default.output`` as an explicit input, the
        context should include the ``vp:default`` entry and the ``stages`` dict
        should still be injected.
        """
        ctx = WorkflowContext()
        ctx.store("vp:default", {"plan": "Build it"})
        ctx.store("vp:review", {"approved": True})

        agent_ctx = ctx.build_for_agent("ic", ["vp:default.output"], mode="explicit")

        # The stages dict is always injected
        assert "stages" in agent_ctx

        # The vp:default output is accessible via the colon-qualified key
        assert "vp:default" in agent_ctx
        assert agent_ctx["vp:default"]["output"]["plan"] == "Build it"
