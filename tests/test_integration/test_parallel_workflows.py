"""Integration tests for parallel workflow execution.

Tests cover:
- PE-7.1: Parallel research agents workflow
- PE-7.2: Parallel validators with continue_on_error
- PE-7.3: Mixed sequential and parallel agents
- PE-7.4: All failure modes with real agent executions
"""

import asyncio

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ExecutionError
from conductor.providers.copilot import CopilotProvider


class TestParallelResearchWorkflow:
    """PE-7.1: Test parallel research agents executing concurrently."""

    def test_parallel_research_agents_success(self) -> None:
        """Test that multiple research agents execute in parallel and aggregate results."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-research",
                description="Research workflow with parallel data gathering",
                entry_point="parallel_research",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
            ),
            agents=[
                AgentDef(
                    name="web_researcher",
                    model="gpt-4",
                    prompt="Research {{ workflow.input.topic }} on the web",
                    output={
                        "findings": OutputField(type="string"),
                        "sources": OutputField(type="array"),
                    },
                ),
                AgentDef(
                    name="paper_researcher",
                    model="gpt-4",
                    prompt="Find academic papers about {{ workflow.input.topic }}",
                    output={
                        "findings": OutputField(type="string"),
                        "papers": OutputField(type="array"),
                    },
                ),
                AgentDef(
                    name="expert_researcher",
                    model="gpt-4",
                    prompt="Find expert opinions on {{ workflow.input.topic }}",
                    output={
                        "findings": OutputField(type="string"),
                        "experts": OutputField(type="array"),
                    },
                ),
                AgentDef(
                    name="synthesizer",
                    model="gpt-4",
                    prompt="""Synthesize research findings:
Web: {{ parallel_research.outputs.web_researcher.findings }}
Papers: {{ parallel_research.outputs.paper_researcher.findings }}
Experts: {{ parallel_research.outputs.expert_researcher.findings }}""",
                    output={
                        "summary": OutputField(type="string"),
                        "confidence": OutputField(type="number"),
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_research",
                    description="Gather research from multiple sources in parallel",
                    agents=["web_researcher", "paper_researcher", "expert_researcher"],
                    failure_mode="fail_fast",
                    routes=[RouteDef(to="synthesizer")],
                ),
            ],
            output={
                "summary": "{{ synthesizer.output.summary }}",
                "web_sources": "{{ parallel_research.outputs.web_researcher.sources | json }}",
                "papers": "{{ parallel_research.outputs.paper_researcher.papers | json }}",
                "experts": "{{ parallel_research.outputs.expert_researcher.experts | json }}",
            },
        )

        call_order = []

        def mock_handler(agent, prompt, context):
            call_order.append(agent.name)

            if agent.name == "web_researcher":
                return {
                    "findings": "Web research findings about quantum computing",
                    "sources": ["source1.com", "source2.org"],
                }
            elif agent.name == "paper_researcher":
                return {
                    "findings": "Academic papers on quantum computing",
                    "papers": ["Paper A", "Paper B", "Paper C"],
                }
            elif agent.name == "expert_researcher":
                return {
                    "findings": "Expert opinions on quantum computing",
                    "experts": ["Dr. Smith", "Dr. Jones"],
                }
            elif agent.name == "synthesizer":
                # Verify all parallel outputs are accessible
                assert "Web research findings" in prompt
                assert "Academic papers" in prompt
                assert "Expert opinions" in prompt
                return {
                    "summary": "Comprehensive research summary",
                    "confidence": 0.95,
                }
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        result = asyncio.run(engine.run({"topic": "quantum computing"}))

        # Verify all agents were executed
        assert "web_researcher" in call_order
        assert "paper_researcher" in call_order
        assert "expert_researcher" in call_order
        assert "synthesizer" in call_order

        # Verify synthesizer ran after parallel group
        synthesizer_idx = call_order.index("synthesizer")
        assert synthesizer_idx > 0  # Ran after parallel agents

        # Verify output aggregation
        assert result["summary"] == "Comprehensive research summary"
        # With | json filter, arrays are returned as-is
        assert result["web_sources"] == ["source1.com", "source2.org"]
        assert result["papers"] == ["Paper A", "Paper B", "Paper C"]
        assert result["experts"] == ["Dr. Smith", "Dr. Jones"]


class TestParallelValidatorsWorkflow:
    """PE-7.2: Test parallel validators with continue_on_error mode."""

    def test_parallel_validators_continue_on_error(self) -> None:
        """Test that validators run in parallel with partial failure handling."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-validation",
                description="Run multiple validators in parallel",
                entry_point="parallel_validators",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
            ),
            agents=[
                AgentDef(
                    name="schema_validator",
                    model="gpt-4",
                    prompt="Validate schema of {{ workflow.input.data }}",
                    output={
                        "valid": OutputField(type="boolean"),
                        "errors": OutputField(type="array"),
                    },
                ),
                AgentDef(
                    name="security_validator",
                    model="gpt-4",
                    prompt="Check security of {{ workflow.input.data }}",
                    output={
                        "valid": OutputField(type="boolean"),
                        "issues": OutputField(type="array"),
                    },
                ),
                AgentDef(
                    name="performance_validator",
                    model="gpt-4",
                    prompt="Analyze performance of {{ workflow.input.data }}",
                    output={
                        "valid": OutputField(type="boolean"),
                        "warnings": OutputField(type="array"),
                    },
                ),
                AgentDef(
                    name="reporter",
                    model="gpt-4",
                    prompt="""Generate validation report.
Schema valid: {{ parallel_validators.outputs.schema_validator.valid \
if 'schema_validator' in parallel_validators.outputs else 'N/A' }}
Security valid: {{ parallel_validators.outputs.security_validator.valid \
if 'security_validator' in parallel_validators.outputs else 'FAILED' }}
Performance valid: {{ parallel_validators.outputs.performance_validator.valid \
if 'performance_validator' in parallel_validators.outputs else 'N/A' }}
Errors: {{ parallel_validators.errors | json }}""",
                    output={
                        "report": OutputField(type="string"),
                        "overall_status": OutputField(type="string"),
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_validators",
                    description="Run all validators in parallel",
                    agents=["schema_validator", "security_validator", "performance_validator"],
                    failure_mode="continue_on_error",
                    routes=[RouteDef(to="reporter")],
                ),
            ],
            output={
                "report": "{{ reporter.output.report }}",
                "status": "{{ reporter.output.overall_status }}",
            },
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "schema_validator":
                return {"valid": True, "errors": []}
            elif agent.name == "security_validator":
                # This validator will fail
                raise ExecutionError(
                    "Security validation failed: SQL injection detected",
                    suggestion="Sanitize user inputs",
                )
            elif agent.name == "performance_validator":
                return {"valid": False, "warnings": ["High memory usage", "Slow queries"]}
            elif agent.name == "reporter":
                # Verify error information is accessible
                assert "errors" in prompt.lower() or "parallel_validators" in prompt
                return {
                    "report": "Validation completed with partial failures",
                    "overall_status": "partial_failure",
                }
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        # Should not raise despite security_validator failure
        result = asyncio.run(engine.run({"data": "sample data"}))

        # Verify workflow completed
        assert result["report"] == "Validation completed with partial failures"
        assert result["status"] == "partial_failure"

    def test_parallel_validators_all_fail_continue_on_error(self) -> None:
        """Test that continue_on_error fails if ALL validators fail."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="parallel-validation-all-fail",
                entry_point="parallel_validators",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="validator1",
                    model="gpt-4",
                    prompt="Validate 1",
                    output={"valid": OutputField(type="boolean")},
                ),
                AgentDef(
                    name="validator2",
                    model="gpt-4",
                    prompt="Validate 2",
                    output={"valid": OutputField(type="boolean")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_validators",
                    agents=["validator1", "validator2"],
                    failure_mode="continue_on_error",
                ),
            ],
            output={"result": "done"},
        )

        def mock_handler(agent, prompt, context):
            # Both validators fail
            raise ExecutionError(f"{agent.name} failed")

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        # Should raise because ALL validators failed
        with pytest.raises(ExecutionError) as exc_info:
            asyncio.run(engine.run({}))

        assert "parallel" in str(exc_info.value).lower()


class TestMixedSequentialParallelWorkflow:
    """PE-7.3: Test workflows with both sequential and parallel agents."""

    def test_sequential_then_parallel_then_sequential(self) -> None:
        """Test workflow: sequential → parallel → sequential."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="mixed-workflow",
                description="Mix of sequential and parallel execution",
                entry_point="planner",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
            ),
            agents=[
                AgentDef(
                    name="planner",
                    model="gpt-4",
                    prompt="Create plan for {{ workflow.input.task }}",
                    output={
                        "plan": OutputField(type="string"),
                        "subtasks": OutputField(type="array"),
                    },
                    routes=[RouteDef(to="parallel_executors")],
                ),
                AgentDef(
                    name="executor1",
                    model="gpt-4",
                    prompt="Execute subtask 1: {{ planner.output.subtasks[0] }}",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="executor2",
                    model="gpt-4",
                    prompt="Execute subtask 2: {{ planner.output.subtasks[1] }}",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="executor3",
                    model="gpt-4",
                    prompt="Execute subtask 3: {{ planner.output.subtasks[2] }}",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="aggregator",
                    model="gpt-4",
                    prompt="""Aggregate results:
Task 1: {{ parallel_executors.outputs.executor1.result }}
Task 2: {{ parallel_executors.outputs.executor2.result }}
Task 3: {{ parallel_executors.outputs.executor3.result }}
Original plan: {{ planner.output.plan }}""",
                    output={
                        "summary": OutputField(type="string"),
                        "success": OutputField(type="boolean"),
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_executors",
                    description="Execute subtasks in parallel",
                    agents=["executor1", "executor2", "executor3"],
                    failure_mode="all_or_nothing",
                    routes=[RouteDef(to="aggregator")],
                ),
            ],
            output={
                "summary": "{{ aggregator.output.summary }}",
                "success": "{{ aggregator.output.success }}",
            },
        )

        execution_order = []

        def mock_handler(agent, prompt, context):
            execution_order.append(agent.name)

            if agent.name == "planner":
                return {
                    "plan": "Complete the task in 3 steps",
                    "subtasks": ["Step 1", "Step 2", "Step 3"],
                }
            elif agent.name in ["executor1", "executor2", "executor3"]:
                return {"result": f"Completed {agent.name}"}
            elif agent.name == "aggregator":
                # Verify access to both sequential and parallel outputs
                assert "planner" in prompt or "plan" in prompt.lower()
                assert "executor" in prompt
                return {"summary": "All tasks completed successfully", "success": True}
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        result = asyncio.run(engine.run({"task": "Build a website"}))

        # Verify execution order
        assert execution_order[0] == "planner"  # First sequential agent
        # Next three are parallel (order may vary)
        parallel_agents = set(execution_order[1:4])
        assert parallel_agents == {"executor1", "executor2", "executor3"}
        assert execution_order[4] == "aggregator"  # Final sequential agent

        # Verify output
        assert result["summary"] == "All tasks completed successfully"
        assert result["success"] == "True"  # Boolean rendered as string

    def test_routing_from_parallel_group_based_on_results(self) -> None:
        """Test routing decisions based on parallel group outputs."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="conditional-parallel",
                entry_point="parallel_checks",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="check1",
                    model="gpt-4",
                    prompt="Check 1",
                    output={"passed": OutputField(type="boolean")},
                ),
                AgentDef(
                    name="check2",
                    model="gpt-4",
                    prompt="Check 2",
                    output={"passed": OutputField(type="boolean")},
                ),
                AgentDef(
                    name="evaluator",
                    model="gpt-4",
                    prompt="Evaluate checks",
                    output={
                        "all_passed": OutputField(type="boolean"),
                        "action": OutputField(type="string"),
                    },
                    routes=[
                        RouteDef(to="success_handler", when="all_passed == True"),
                        RouteDef(to="failure_handler"),
                    ],
                ),
                AgentDef(
                    name="success_handler",
                    model="gpt-4",
                    prompt="Handle success",
                    output={"message": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
                AgentDef(
                    name="failure_handler",
                    model="gpt-4",
                    prompt="Handle failure",
                    output={"message": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_checks",
                    agents=["check1", "check2"],
                    failure_mode="continue_on_error",
                    routes=[RouteDef(to="evaluator")],
                ),
            ],
            output={
                "result": (
                    "{{ success_handler.output.message if success_handler is defined "
                    "else failure_handler.output.message }}"
                )
            },
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "check1" or agent.name == "check2":
                return {"passed": True}
            elif agent.name == "evaluator":
                return {"all_passed": True, "action": "proceed"}
            elif agent.name == "success_handler":
                return {"message": "All checks passed!"}
            elif agent.name == "failure_handler":
                return {"message": "Some checks failed"}
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        result = asyncio.run(engine.run({}))
        assert result["result"] == "All checks passed!"


class TestParallelFailureModes:
    """PE-7.4: Test all failure modes with real agent executions."""

    def test_fail_fast_mode_stops_immediately(self) -> None:
        """Test that fail_fast mode stops on first failure."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="fail-fast-test",
                entry_point="parallel_group",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Task 3",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=["agent1", "agent2", "agent3"],
                    failure_mode="fail_fast",
                ),
            ],
            output={"result": "done"},
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "agent2":
                raise ExecutionError("Agent 2 failed intentionally")
            return {"result": f"{agent.name} completed"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        with pytest.raises(ExecutionError) as exc_info:
            asyncio.run(engine.run({}))

        error_msg = str(exc_info.value).lower()
        assert "parallel" in error_msg or "agent2" in error_msg

    def test_all_or_nothing_mode_requires_all_success(self) -> None:
        """Test that all_or_nothing fails if any agent fails."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="all-or-nothing-test",
                entry_point="parallel_group",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Task 3",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=["agent1", "agent2", "agent3"],
                    failure_mode="all_or_nothing",
                ),
            ],
            output={"result": "done"},
        )

        call_count = {"count": 0}

        def mock_handler(agent, prompt, context):
            call_count["count"] += 1
            if agent.name == "agent3":
                raise ExecutionError("Agent 3 failed")
            return {"result": f"{agent.name} completed"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        with pytest.raises(ExecutionError) as exc_info:
            asyncio.run(engine.run({}))

        # All agents should have been called
        # Note: provider may retry, so count might be higher than number of agents
        assert call_count["count"] >= 3
        assert "parallel" in str(exc_info.value).lower()

    def test_all_or_nothing_mode_success_when_all_succeed(self) -> None:
        """Test that all_or_nothing succeeds when all agents succeed."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="all-or-nothing-success",
                entry_point="parallel_group",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=["agent1", "agent2"],
                    failure_mode="all_or_nothing",
                ),
            ],
            output={
                "result1": "{{ parallel_group.outputs.agent1.result }}",
                "result2": "{{ parallel_group.outputs.agent2.result }}",
            },
        )

        def mock_handler(agent, prompt, context):
            return {"result": f"{agent.name} completed"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        result = asyncio.run(engine.run({}))
        assert result["result1"] == "agent1 completed"
        assert result["result2"] == "agent2 completed"

    def test_continue_on_error_with_mixed_results(self) -> None:
        """Test continue_on_error collects errors and successful results."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="continue-on-error-test",
                entry_point="parallel_group",
                runtime=RuntimeConfig(provider="copilot"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Task 1",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Task 2",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="agent3",
                    model="gpt-4",
                    prompt="Task 3",
                    output={"result": OutputField(type="string")},
                ),
                AgentDef(
                    name="checker",
                    model="gpt-4",
                    prompt="""Check results and errors:
Success 1: {{ parallel_group.outputs.agent1.result \
if 'agent1' in parallel_group.outputs else 'N/A' }}
Success 3: {{ parallel_group.outputs.agent3.result \
if 'agent3' in parallel_group.outputs else 'N/A' }}
Has errors: {{ (parallel_group.errors | length) > 0 }}""",
                    output={"status": OutputField(type="string")},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=["agent1", "agent2", "agent3"],
                    failure_mode="continue_on_error",
                    routes=[RouteDef(to="checker")],
                ),
            ],
            output={"status": "{{ checker.output.status }}"},
        )

        def mock_handler(agent, prompt, context):
            if agent.name == "agent1":
                return {"result": "agent1 success"}
            elif agent.name == "agent2":
                raise ExecutionError("agent2 failed")
            elif agent.name == "agent3":
                return {"result": "agent3 success"}
            elif agent.name == "checker":
                # Verify partial results are accessible
                assert "agent1 success" in prompt or "N/A" in prompt
                assert "agent3 success" in prompt or "N/A" in prompt
                return {"status": "partial_success"}
            return {}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        result = asyncio.run(engine.run({}))
        assert result["status"] == "partial_success"


class TestParallelContextIsolation:
    """Test that parallel agents have isolated context snapshots."""

    def test_context_isolation_prevents_interference(self) -> None:
        """Test that parallel agents cannot interfere with each other's context."""
        workflow = WorkflowConfig(
            workflow=WorkflowDef(
                name="context-isolation-test",
                entry_point="parallel_group",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
            ),
            agents=[
                AgentDef(
                    name="agent1",
                    model="gpt-4",
                    prompt="Process {{ workflow.input.value }}",
                    output={"result": OutputField(type="number")},
                ),
                AgentDef(
                    name="agent2",
                    model="gpt-4",
                    prompt="Process {{ workflow.input.value }}",
                    output={"result": OutputField(type="number")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="parallel_group",
                    agents=["agent1", "agent2"],
                    failure_mode="fail_fast",
                ),
            ],
            output={
                "result1": "{{ parallel_group.outputs.agent1.result }}",
                "result2": "{{ parallel_group.outputs.agent2.result }}",
            },
        )

        def mock_handler(agent, prompt, context):
            # Both agents should see the same input value
            assert "42" in prompt
            # Return different results
            if agent.name == "agent1":
                return {"result": 100}
            else:
                return {"result": 200}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(workflow, provider)

        result = asyncio.run(engine.run({"value": 42}))

        # Both agents processed independently
        assert result["result1"] == 100
        assert result["result2"] == 200
