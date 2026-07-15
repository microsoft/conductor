"""End-to-end integration tests for the validator block (issue #220).

Exercises the real ``WorkflowEngine`` path across the main loop, parallel
groups, and for-each loops. The primary agent and the validator both go
through ``provider.execute`` (the validator runs as a synthetic agent whose
output schema contains ``passed``/``issues``), so a single ``mock_handler``
can serve both by inspecting ``agent.output``. No SDK or network needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    RetryPolicy,
    RouteDef,
    RuntimeConfig,
    ValidatorConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.executor.agent import AgentExecutor
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider


def _is_validator_agent(agent: AgentDef) -> bool:
    """The synthetic validator agent carries a ``passed`` output field."""
    return agent.output is not None and "passed" in agent.output


def _collect() -> tuple[WorkflowEventEmitter, list[WorkflowEvent]]:
    emitter = WorkflowEventEmitter()
    events: list[WorkflowEvent] = []
    emitter.subscribe(events.append)
    return emitter, events


def _single_agent_config(
    validator: ValidatorConfig,
    retry: RetryPolicy | None = None,
) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="validator-it",
            entry_point="reviewer",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="reviewer",
                model="gpt-4",
                prompt="Review {{ workflow.input.diff }}",
                output={"summary": OutputField(type="string")},
                validator=validator,
                retry=retry,
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"summary": "{{ reviewer.output.summary }}"},
    )


def _types(events: list[WorkflowEvent]) -> list[str]:
    return [e.type for e in events]


def _validator_rows(engine: WorkflowEngine) -> list[str]:
    return [
        a.agent_name
        for a in engine.usage_tracker.get_summary().agents
        if a.agent_name.endswith("(validator)")
    ]


class TestValidatorMainLoop:
    @pytest.mark.asyncio
    async def test_pass_no_rerun(self) -> None:
        """Validator passes → primary runs once, output flows unchanged."""
        primary_calls = 0
        validator_calls = 0

        def handler(agent: AgentDef, prompt: str, ctx: dict[str, Any]) -> dict[str, Any]:
            nonlocal primary_calls, validator_calls
            if _is_validator_agent(agent):
                validator_calls += 1
                return {"passed": True, "issues": []}
            primary_calls += 1
            return {"summary": "looks good"}

        emitter, events = _collect()
        config = _single_agent_config(ValidatorConfig(criteria="Be correct"))
        engine = WorkflowEngine(
            config, CopilotProvider(mock_handler=handler), event_emitter=emitter
        )

        result = await engine.run({"diff": "x"})

        assert primary_calls == 1
        assert validator_calls == 1
        assert result["summary"] == "looks good"
        assert "agent_validator_start" in _types(events)
        complete = next(e for e in events if e.type == "agent_validator_complete")
        assert complete.data["passed"] is True
        assert "agent_validation_failed" not in _types(events)
        assert _validator_rows(engine) == ["reviewer (validator)"]

    @pytest.mark.asyncio
    async def test_fail_then_rerun_succeeds(self) -> None:
        """Validator fails → primary re-runs once with feedback appended."""
        primary_prompts: list[str] = []
        primary_calls = 0

        def handler(agent: AgentDef, prompt: str, ctx: dict[str, Any]) -> dict[str, Any]:
            nonlocal primary_calls
            if _is_validator_agent(agent):
                return {"passed": False, "issues": ["missing null-safety check"]}
            primary_calls += 1
            primary_prompts.append(prompt)
            return {"summary": f"answer {primary_calls}"}

        emitter, events = _collect()
        config = _single_agent_config(ValidatorConfig(criteria="Check null safety"))
        engine = WorkflowEngine(
            config, CopilotProvider(mock_handler=handler), event_emitter=emitter
        )

        result = await engine.run({"diff": "x"})

        # Primary ran twice (initial + re-run); validator graded once.
        assert primary_calls == 2
        assert result["summary"] == "answer 2"
        # Re-run prompt carries the validation feedback section + the issue.
        assert "## Validation feedback" in primary_prompts[1]
        assert "missing null-safety check" in primary_prompts[1]
        failed = next(e for e in events if e.type == "agent_validation_failed")
        assert failed.data["will_retry"] is True
        assert failed.data["issues"] == ["missing null-safety check"]

    @pytest.mark.asyncio
    async def test_fail_rerun_still_bad_commits_second(self) -> None:
        """Second output is committed even if it would still fail (no loop)."""
        validator_calls = 0
        primary_calls = 0

        def handler(agent: AgentDef, prompt: str, ctx: dict[str, Any]) -> dict[str, Any]:
            nonlocal validator_calls, primary_calls
            if _is_validator_agent(agent):
                validator_calls += 1
                return {"passed": False, "issues": ["still wrong"]}
            primary_calls += 1
            return {"summary": f"attempt {primary_calls}"}

        config = _single_agent_config(ValidatorConfig(criteria="Strict"))
        engine = WorkflowEngine(config, CopilotProvider(mock_handler=handler))

        result = await engine.run({"diff": "x"})

        # Exactly one re-run; validator is NOT called a second time.
        assert primary_calls == 2
        assert validator_calls == 1
        assert result["summary"] == "attempt 2"

    @pytest.mark.asyncio
    async def test_api_error_treated_as_pass(self) -> None:
        """Validator call raising → fail-open, no re-run, primary committed."""
        primary_calls = 0

        def handler(agent: AgentDef, prompt: str, ctx: dict[str, Any]) -> dict[str, Any]:
            nonlocal primary_calls
            if _is_validator_agent(agent):
                raise RuntimeError("validator API exploded")
            primary_calls += 1
            return {"summary": "original"}

        emitter, events = _collect()
        config = _single_agent_config(ValidatorConfig(criteria="Check"))
        engine = WorkflowEngine(
            config, CopilotProvider(mock_handler=handler), event_emitter=emitter
        )

        result = await engine.run({"diff": "x"})

        assert primary_calls == 1  # no re-run
        assert result["summary"] == "original"
        complete = next(e for e in events if e.type == "agent_validator_complete")
        assert complete.data["passed"] is True
        assert complete.data["errored"] is True
        assert "agent_validation_failed" not in _types(events)

    @pytest.mark.asyncio
    async def test_max_retries_zero_reports_without_rerun(self) -> None:
        """max_retries=0 → report failure (event) but never re-run."""
        primary_calls = 0

        def handler(agent: AgentDef, prompt: str, ctx: dict[str, Any]) -> dict[str, Any]:
            nonlocal primary_calls
            if _is_validator_agent(agent):
                return {"passed": False, "issues": ["bad"]}
            primary_calls += 1
            return {"summary": "only run"}

        emitter, events = _collect()
        config = _single_agent_config(ValidatorConfig(criteria="Check", max_retries=0))
        engine = WorkflowEngine(
            config, CopilotProvider(mock_handler=handler), event_emitter=emitter
        )

        result = await engine.run({"diff": "x"})

        assert primary_calls == 1  # no re-run
        assert result["summary"] == "only run"
        failed = next(e for e in events if e.type == "agent_validation_failed")
        assert failed.data["will_retry"] is False

    @pytest.mark.asyncio
    async def test_interaction_with_retry_policy(self) -> None:
        """A validator-triggered re-run composes with an agent retry policy."""
        primary_calls = 0

        def handler(agent: AgentDef, prompt: str, ctx: dict[str, Any]) -> dict[str, Any]:
            nonlocal primary_calls
            if _is_validator_agent(agent):
                # Fail only the first validation, pass would-be subsequent ones.
                return {"passed": primary_calls >= 2, "issues": ["fix it"]}
            primary_calls += 1
            return {"summary": f"v{primary_calls}"}

        config = _single_agent_config(
            ValidatorConfig(criteria="Check"),
            retry=RetryPolicy(max_attempts=3, delay_seconds=0.0),
        )
        engine = WorkflowEngine(config, CopilotProvider(mock_handler=handler))

        result = await engine.run({"diff": "x"})

        assert primary_calls == 2  # initial + one validator-driven re-run
        assert result["summary"] == "v2"


class TestValidatorParallel:
    @pytest.mark.asyncio
    async def test_validator_runs_in_parallel_group(self) -> None:
        """Validator on a parallel-group member grades and re-runs once."""
        worker_calls = 0
        validator_calls = 0

        def handler(agent: AgentDef, prompt: str, ctx: dict[str, Any]) -> dict[str, Any]:
            nonlocal worker_calls, validator_calls
            if _is_validator_agent(agent):
                validator_calls += 1
                return {"passed": False, "issues": ["redo"]}
            if agent.name == "worker":
                worker_calls += 1
                return {"result": f"r{worker_calls}"}
            return {"result": "sidekick"}

        emitter, events = _collect()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="par-validator",
                entry_point="team",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="worker",
                    model="gpt-4",
                    prompt="work",
                    output={"result": OutputField(type="string")},
                    validator=ValidatorConfig(criteria="Be thorough"),
                ),
                AgentDef(
                    name="sidekick",
                    model="gpt-4",
                    prompt="assist",
                    output={"result": OutputField(type="string")},
                ),
            ],
            parallel=[
                ParallelGroup(
                    name="team", agents=["worker", "sidekick"], routes=[RouteDef(to="$end")]
                ),
            ],
            output={"done": "true"},
        )
        engine = WorkflowEngine(
            config, CopilotProvider(mock_handler=handler), event_emitter=emitter
        )

        await engine.run({})

        assert validator_calls == 1  # only the worker has a validator
        assert worker_calls == 2  # initial + re-run
        assert "worker (validator)" in _validator_rows(engine)
        assert "agent_validator_complete" in _types(events)


class TestValidatorForEach:
    @pytest.mark.asyncio
    async def test_validator_runs_per_item(self) -> None:
        """Validator runs for each for-each item, recording a row per item."""
        validator_calls = 0

        def handler(agent: AgentDef, prompt: str, ctx: dict[str, Any]) -> dict[str, Any]:
            nonlocal validator_calls
            if _is_validator_agent(agent):
                validator_calls += 1
                return {"passed": True, "issues": []}
            if agent.name == "finder":
                return {"items": ["a", "b"]}
            return {"result": "processed"}

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="fe-validator",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                AgentDef(
                    name="finder",
                    model="gpt-4",
                    prompt="find",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="process")],
                ),
            ],
            for_each=[
                ForEachDef(
                    name="process",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="processor",
                        model="gpt-4",
                        prompt="process {{ item }}",
                        output={"result": OutputField(type="string")},
                        validator=ValidatorConfig(criteria="Check each"),
                    ),
                    max_concurrent=1,
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"done": "true"},
        )
        engine = WorkflowEngine(config, CopilotProvider(mock_handler=handler))

        await engine.run({})

        assert validator_calls == 2  # one per item
        rows = _validator_rows(engine)
        assert "process[0] (validator)" in rows
        assert "process[1] (validator)" in rows


class TestValidatorCostAndFailurePaths:
    """Direct ``_apply_validator`` tests for cost attribution and re-run failure.

    These call the helper directly with a provider whose ``execute`` returns
    explicit token counts, because the shared ``mock_handler`` path yields
    ``usage=None`` (all-zero tokens) and so cannot verify cost attribution.
    """

    def _engine_and_executor(
        self, exec_fn: Any, *, timeout_seconds: float | None = None
    ) -> tuple[WorkflowEngine, AgentExecutor, AgentDef]:
        agent = AgentDef(
            name="reviewer",
            model="gpt-4",
            prompt="Review the diff.",  # no template vars → renders against {}
            output={"summary": OutputField(type="string")},
            validator=ValidatorConfig(criteria="check"),
            timeout_seconds=timeout_seconds,
            routes=[RouteDef(to="$end")],
        )
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="v",
                entry_point="reviewer",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[agent],
            output={"summary": "{{ reviewer.output.summary }}"},
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        provider.execute = exec_fn  # type: ignore[method-assign]
        engine = WorkflowEngine(config, provider)
        executor = AgentExecutor(provider, workflow_tools=[])
        return engine, executor, agent

    @pytest.mark.asyncio
    async def test_rerun_success_records_two_validator_rows(self) -> None:
        primary_n = {"n": 0}

        async def exec_fn(*, agent: AgentDef, rendered_prompt: str, **kw: Any) -> AgentOutput:
            if agent.output and "passed" in agent.output:
                return AgentOutput(
                    content={"passed": False, "issues": ["fix"]},
                    raw_response="",
                    model="judge",
                    input_tokens=10,
                    output_tokens=5,
                )
            primary_n["n"] += 1
            return AgentOutput(
                content={"summary": f"v{primary_n['n']}"},
                raw_response="",
                model="gpt-4",
                input_tokens=1000,
                output_tokens=500,
            )

        engine, executor, agent = self._engine_and_executor(exec_fn)
        original = AgentOutput(
            content={"summary": "v0"},
            raw_response="",
            model="gpt-4",
            input_tokens=900,
            output_tokens=400,
        )

        result = await engine._apply_validator(agent, original, 0.5, {}, executor, None, None)

        assert result.content == {"summary": "v1"}  # the re-run output
        vrows = [
            r
            for r in engine.usage_tracker.get_summary().agents
            if r.agent_name == "reviewer (validator)"
        ]
        assert len(vrows) == 2  # validator call + discarded first attempt
        assert any(r.input_tokens == 10 and r.output_tokens == 5 for r in vrows)
        assert any(r.input_tokens == 900 and r.output_tokens == 400 for r in vrows)

    @pytest.mark.asyncio
    async def test_rerun_failure_keeps_original_no_double_count_and_emits_event(self) -> None:
        async def exec_fn(*, agent: AgentDef, rendered_prompt: str, **kw: Any) -> AgentOutput:
            if agent.output and "passed" in agent.output:
                return AgentOutput(
                    content={"passed": False, "issues": ["fix"]},
                    raw_response="",
                    model="judge",
                    input_tokens=10,
                    output_tokens=5,
                )
            raise RuntimeError("rerun boom")

        engine, executor, agent = self._engine_and_executor(exec_fn)
        original = AgentOutput(
            content={"summary": "orig"}, raw_response="", model="gpt-4", input_tokens=900
        )
        events: list[tuple[str, dict[str, Any]]] = []

        result = await engine._apply_validator(
            agent, original, 0.5, {}, executor, None, lambda e, d: events.append((e, d))
        )

        assert result is original  # original kept on re-run failure
        # Discarded run is NOT recorded when the re-run fails (no double count):
        vrows = [
            r
            for r in engine.usage_tracker.get_summary().agents
            if r.agent_name == "reviewer (validator)"
        ]
        assert len(vrows) == 1  # only the grading call
        failed = [d for (e, d) in events if e == "agent_validation_failed"]
        assert any(d.get("rerun_errored") for d in failed)

    @pytest.mark.asyncio
    async def test_partial_rerun_keeps_original(self) -> None:
        async def exec_fn(*, agent: AgentDef, rendered_prompt: str, **kw: Any) -> AgentOutput:
            if agent.output and "passed" in agent.output:
                return AgentOutput(
                    content={"passed": False, "issues": ["fix"]}, raw_response="", model="judge"
                )
            return AgentOutput(
                content={"summary": "partial"}, raw_response="", model="gpt-4", partial=True
            )

        engine, executor, agent = self._engine_and_executor(exec_fn)
        original = AgentOutput(content={"summary": "orig"}, raw_response="", model="gpt-4")

        result = await engine._apply_validator(agent, original, 0.5, {}, executor, None, None)

        assert result is original  # partial re-run is discarded
        vrows = [
            r
            for r in engine.usage_tracker.get_summary().agents
            if r.agent_name == "reviewer (validator)"
        ]
        assert len(vrows) == 1  # discarded run not recorded on the partial path

    @pytest.mark.asyncio
    async def test_validator_timeout_fails_open(self) -> None:
        async def exec_fn(*, agent: AgentDef, rendered_prompt: str, **kw: Any) -> AgentOutput:
            if agent.output and "passed" in agent.output:
                await asyncio.sleep(5)  # hang the grader past the agent timeout
            return AgentOutput(content={"summary": "x"}, raw_response="", model="gpt-4")

        engine, executor, agent = self._engine_and_executor(exec_fn, timeout_seconds=1.0)
        original = AgentOutput(content={"summary": "orig"}, raw_response="", model="gpt-4")

        result = await engine._apply_validator(agent, original, 0.5, {}, executor, None, None)

        # Grader timed out → fail open (treated as pass) → original returned, no re-run.
        assert result is original


class TestValidatorWorkingDirectoryInheritance:
    """Verify that the synthetic validator agent inherits the primary agent's working directory."""

    @pytest.mark.asyncio
    async def test_validator_working_dir_inheritance(self) -> None:
        # Requirement: The synthetic validator agent must inherit the working directory of
        # the primary agent.
        captured_validator_agent: AgentDef | None = None

        async def exec_fn(*, agent: AgentDef, rendered_prompt: str, **kw: Any) -> AgentOutput:
            nonlocal captured_validator_agent
            captured_validator_agent = agent
            return AgentOutput(
                content={"passed": True, "issues": []},
                raw_response="",
                model="judge",
            )

        primary_agent = AgentDef(
            name="reviewer",
            model="gpt-4",
            prompt="Review",
            output={"summary": OutputField(type="string")},
            validator=ValidatorConfig(criteria="check"),
            routes=[RouteDef(to="$end")],
            working_dir="/path/to/resolved/working_dir",
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        provider.execute = exec_fn  # type: ignore[method-assign]

        from conductor.engine.validator import OutputValidator

        validator = OutputValidator()

        await validator.validate(
            agent=primary_agent,
            primary_prompt="Review",
            primary_output={"summary": "orig"},
            provider=provider,
        )

        assert captured_validator_agent is not None
        assert captured_validator_agent.working_dir == "/path/to/resolved/working_dir"

    @pytest.mark.asyncio
    async def test_validator_working_dir_none(self) -> None:
        # Requirement: If the primary agent has no working directory set, the validator's
        # working directory is None.
        captured_validator_agent: AgentDef | None = None

        async def exec_fn(*, agent: AgentDef, rendered_prompt: str, **kw: Any) -> AgentOutput:
            nonlocal captured_validator_agent
            captured_validator_agent = agent
            return AgentOutput(
                content={"passed": True, "issues": []},
                raw_response="",
                model="judge",
            )

        primary_agent = AgentDef(
            name="reviewer",
            model="gpt-4",
            prompt="Review",
            output={"summary": OutputField(type="string")},
            validator=ValidatorConfig(criteria="check"),
            routes=[RouteDef(to="$end")],
            working_dir=None,
        )
        provider = CopilotProvider(mock_handler=lambda a, p, c: {})
        provider.execute = exec_fn  # type: ignore[method-assign]

        from conductor.engine.validator import OutputValidator

        validator = OutputValidator()

        await validator.validate(
            agent=primary_agent,
            primary_prompt="Review",
            primary_output={"summary": "orig"},
            provider=provider,
        )

        assert captured_validator_agent is not None
        assert captured_validator_agent.working_dir is None
