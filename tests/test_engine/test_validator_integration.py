"""End-to-end integration tests for the validator block (issue #220).

Exercises the real ``WorkflowEngine`` path across the main loop, parallel
groups, and for-each loops. The primary agent and the validator both go
through ``provider.execute`` (the validator runs as a synthetic agent whose
output schema contains ``passed``/``issues``), so a single ``mock_handler``
can serve both by inspecting ``agent.output``. No SDK or network needed.
"""

from __future__ import annotations

import asyncio
import logging
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
    async def test_fail_then_continues_provider_state(self) -> None:
        # Requirement: a validator retry continues provider state with feedback only.
        continuation = ["original request", "original response"]
        calls: list[dict[str, Any]] = []

        async def exec_fn(
            *,
            agent: AgentDef,
            rendered_prompt: str,
            continuation_state: Any = None,
            **kwargs: Any,
        ) -> AgentOutput:
            calls.append(
                {
                    "agent": agent,
                    "prompt": rendered_prompt,
                    "continuation_state": continuation_state,
                }
            )
            if _is_validator_agent(agent):
                return AgentOutput(
                    content={"passed": False, "issues": ["fix null safety"]},
                    raw_response="",
                    model="judge",
                )
            return AgentOutput(
                content={"summary": "corrected"},
                raw_response="",
                model="gpt-4",
            )

        engine, executor, agent = TestValidatorCostAndFailurePaths()._engine_and_executor(
            exec_fn, continuation_capable=True
        )
        original = AgentOutput(
            content={"summary": "draft"},
            raw_response="",
            model="gpt-4",
            continuation_state=continuation,
        )

        result = await engine._apply_validator(agent, original, 0.5, {}, executor, None, None)

        assert result.content == {"summary": "corrected"}
        primary_rerun = calls[-1]
        assert primary_rerun["continuation_state"] is continuation
        assert primary_rerun["prompt"].startswith("## Validation feedback")
        assert "Review the diff." not in primary_rerun["prompt"]

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
        # The stateless re-run rebuilds the full prompt: the second prompt is
        # the first prompt plus the feedback, never a feedback-only stub.
        assert primary_prompts[1].startswith(primary_prompts[0])
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
        self,
        exec_fn: Any,
        *,
        timeout_seconds: float | None = None,
        continuation_capable: bool = False,
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

        class _CapableCopilotProvider(CopilotProvider, abstract=True):
            # Test double for continuation tests: declares the support the
            # executor's guard requires so the state is let through.
            @property
            def supports_continuation(self) -> bool:
                return True

        provider_cls = _CapableCopilotProvider if continuation_capable else CopilotProvider
        provider = provider_cls(mock_handler=lambda a, p, c: {})
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
    async def test_rerun_failure_keeps_original_no_double_count_and_emits_event(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
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

        with caplog.at_level(logging.DEBUG, logger="conductor.engine.workflow"):
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
        # The rerun-errored emission carries the failure cause, so the one
        # user-visible surface says *why* the re-run failed, not just *that*.
        rerun_failed = next(d for d in failed if d.get("rerun_errored"))
        assert rerun_failed["error"] == "RuntimeError: rerun boom"
        assert rerun_failed["continued"] is False

        # Requirement (issue #357): the handled fail-open path must not print a
        # traceback at WARNING level — a concise warning names the exception, and
        # the full traceback is only available at DEBUG level.
        warnings = [
            r
            for r in caplog.records
            if r.name == "conductor.engine.workflow"
            and r.levelno == logging.WARNING
            and "Validator re-run failed" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert warnings[0].exc_info is None
        assert "RuntimeError" in warnings[0].getMessage()
        assert "rerun boom" in warnings[0].getMessage()
        debugs = [
            r
            for r in caplog.records
            if r.name == "conductor.engine.workflow" and r.levelno == logging.DEBUG
        ]
        assert any(r.exc_info is not None for r in debugs)

    @pytest.mark.asyncio
    async def test_continuation_rerun_failure_fails_open_to_original(self) -> None:
        # Requirement: a validator re-run that fails while continuing the
        # provider conversation must fail open to the original output — the
        # same contract as the stateless re-run — with the continuation state
        # having actually reached the failed re-run.
        continuation = ["original request", "original response"]
        seen_states: list[Any] = []

        async def exec_fn(
            *,
            agent: AgentDef,
            rendered_prompt: str,
            continuation_state: Any = None,
            **kw: Any,
        ) -> AgentOutput:
            if _is_validator_agent(agent):
                return AgentOutput(
                    content={"passed": False, "issues": ["fix"]},
                    raw_response="",
                    model="judge",
                )
            seen_states.append(continuation_state)
            raise RuntimeError("continuation rerun boom")

        engine, executor, agent = self._engine_and_executor(exec_fn, continuation_capable=True)
        original = AgentOutput(
            content={"summary": "orig"},
            raw_response="",
            model="gpt-4",
            continuation_state=continuation,
        )
        events: list[tuple[str, dict[str, Any]]] = []

        result = await engine._apply_validator(
            agent, original, 0.5, {}, executor, None, lambda e, d: events.append((e, d))
        )

        assert result is original
        assert seen_states == [continuation]
        rerun_failed = next(
            d for (e, d) in events if e == "agent_validation_failed" and d.get("rerun_errored")
        )
        assert rerun_failed["error"] == "RuntimeError: continuation rerun boom"
        assert rerun_failed["continued"] is True

    @pytest.mark.asyncio
    async def test_continuation_state_never_reaches_events_or_checkpoint(
        self, tmp_path: Any
    ) -> None:
        # Requirement: continuation_state is provider-opaque and in-memory
        # only — an arbitrary object must survive neither into emitted event
        # payloads nor into a checkpoint's JSON.
        import json
        from pathlib import Path
        from unittest.mock import patch

        from conductor.engine.checkpoint import CheckpointManager

        sentinel = object()

        async def exec_fn(*, agent: AgentDef, rendered_prompt: str, **kw: Any) -> AgentOutput:
            if _is_validator_agent(agent):
                return AgentOutput(
                    content={"passed": False, "issues": ["fix"]},
                    raw_response="",
                    model="judge",
                )
            return AgentOutput(content={"summary": "v2"}, raw_response="", model="gpt-4")

        engine, executor, agent = self._engine_and_executor(exec_fn, continuation_capable=True)
        original = AgentOutput(
            content={"summary": "orig"},
            raw_response="",
            model="gpt-4",
            continuation_state=sentinel,
        )
        events: list[tuple[str, dict[str, Any]]] = []

        result = await engine._apply_validator(
            agent, original, 0.5, {}, executor, None, lambda e, d: events.append((e, d))
        )
        assert result.content == {"summary": "v2"}

        def _contains(obj: Any) -> bool:
            if obj is sentinel:
                return True
            if isinstance(obj, dict):
                return any(_contains(k) or _contains(v) for k, v in obj.items())
            if isinstance(obj, list | tuple):
                return any(_contains(i) for i in obj)
            return False

        assert events  # the retry emitted validator events
        assert not any(_contains(d) for _, d in events)
        for _, data in events:
            json.dumps(data)  # payloads stay JSON-clean without coercion

        # The engine stores output.content (a plain dict), so a checkpoint
        # taken after the retry round-trips through JSON with no trace of the
        # sentinel.
        engine.context.store(agent.name, result.content)
        workflow_file = Path(tmp_path) / "w.yaml"
        workflow_file.write_text("workflow: {}")
        with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=Path(tmp_path)):
            checkpoint_path = CheckpointManager.save_checkpoint(
                workflow_path=workflow_file,
                context=engine.context,
                limits=engine.limits,
                current_agent=agent.name,
                error=RuntimeError("boom"),
                inputs={},
            )
        assert checkpoint_path is not None
        saved = json.loads(checkpoint_path.read_text())
        assert not _contains(saved)

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


def _sdk_result(result: str):
    """A minimal claude-agent-sdk ``ResultMessage`` carrying the grader's JSON."""
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="result",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="validator-session",
        usage=None,
        result=result,
        structured_output=None,
    )


class TestValidatorRunsWithoutMcpServers:
    """The grading call must reach no tool, on a provider that attaches MCP.

    Regression for the interaction between the validator and
    ``claude-agent-sdk``'s ``tools: []`` + MCP refusal: the grader legitimately
    asks for ``tools: []``, the provider still carried the workflow's MCP
    servers, and the refusal fired on the *synthetic* agent. ``OutputValidator``
    is fail-open, so the run then treated every graded output as valid with no
    error surfaced -- the validator silently stopped validating.

    These go through the real ``OutputValidator`` rather than the helper,
    because the fail-open swallow is exactly what a helper-level test would
    miss.
    """

    @staticmethod
    def _agent() -> AgentDef:
        return AgentDef(
            name="writer",
            prompt="Write it.",
            output={"text": OutputField(type="string")},
            validator=ValidatorConfig(criteria="Must be polite."),
        )

    @staticmethod
    def _provider(**kwargs: Any):
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        return ClaudeAgentSdkProvider(
            mcp_servers={"docs": {"type": "stdio", "command": "docs-server"}}, **kwargs
        )

    async def _grade(self, monkeypatch: pytest.MonkeyPatch, **provider_kwargs: Any):
        """Run one real grading call and capture the session it produced."""
        pytest.importorskip("claude_agent_sdk")
        import conductor.providers.claude_agent_sdk as sdk
        from conductor.engine.validator import OutputValidator

        captured: dict[str, Any] = {}
        wrote_config: list[str] = []

        async def fake_query(**kwargs: Any):
            captured["options"] = kwargs["options"]
            yield _sdk_result('{"passed": false, "issues": ["too curt"]}')

        def spy_write(servers: dict[str, Any]) -> str:
            wrote_config.append("called")
            raise AssertionError("no MCP config file may be written for the grader")

        monkeypatch.setattr(sdk, "query", fake_query)
        monkeypatch.setattr(sdk, "_write_mcp_config", spy_write)
        monkeypatch.setattr(sdk, "CLAUDE_AGENT_SDK_AVAILABLE", True)

        provider = self._provider(**provider_kwargs)
        outcome = await OutputValidator().validate(
            agent=self._agent(),
            primary_prompt="Write it.",
            primary_output={"text": "no."},
            provider=provider,
        )
        return outcome, captured, wrote_config

    async def test_grading_call_happens_and_is_parsed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not fail-open: the SDK really ran and the verdict was read."""
        outcome, captured, _ = await self._grade(monkeypatch)

        assert "options" in captured, "the grading SDK call never happened"
        assert outcome.errored is False, "fail-open swallowed the grading call"
        assert outcome.passed is False
        assert outcome.issues == ["too curt"]

    async def test_grading_session_has_no_mcp_servers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, captured, wrote_config = await self._grade(monkeypatch)
        options = captured["options"]

        assert options.mcp_servers in (None, {}, ""), options.mcp_servers
        assert not wrote_config, "no MCP config file should have been written"
        # No servers means no server-scoped grants either.
        assert list(options.allowed_tools) == []

    async def test_grading_session_keeps_strict_mcp_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Suppression must not become a door to ambient MCP discovery."""
        _, captured, _ = await self._grade(monkeypatch)

        assert captured["options"].strict_mcp_config is True

    async def test_grading_session_has_no_builtin_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The grader keeps ``tools: []`` -- it is not handed native tools."""
        _, captured, _ = await self._grade(monkeypatch)
        options = captured["options"]

        assert options.tools == []
        assert options.permission_mode == "dontAsk"

    async def test_holds_under_the_claude_code_opt_in_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A workflow-level ``native_tools: claude_code`` must not widen the
        grader: its own ``tools: []`` still wins, and MCP stays suppressed."""
        _, captured, wrote_config = await self._grade(monkeypatch, native_tools="claude_code")
        options = captured["options"]

        assert options.tools == []
        assert options.permission_mode == "dontAsk"
        assert not wrote_config

    async def test_authored_empty_tools_agent_with_mcp_is_still_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: the refusal stays load-bearing for real agents.

        An ordinary authored agent declaring ``tools: []`` alongside MCP
        servers must still be rejected before the config file is written and
        before the SDK is invoked -- suppression is reachable only through the
        execution-level flag, never from YAML.
        """
        pytest.importorskip("claude_agent_sdk")
        import conductor.providers.claude_agent_sdk as sdk
        from conductor.exceptions import ProviderError

        queried = False

        async def fake_query(**kwargs: Any):
            nonlocal queried
            queried = True
            yield _sdk_result("{}")

        def spy_write(servers: dict[str, Any]) -> str:
            raise AssertionError("no MCP config file may be written on the refusal path")

        monkeypatch.setattr(sdk, "query", fake_query)
        monkeypatch.setattr(sdk, "_write_mcp_config", spy_write)
        monkeypatch.setattr(sdk, "CLAUDE_AGENT_SDK_AVAILABLE", True)

        provider = self._provider()
        with pytest.raises(ProviderError, match=r"sets 'tools: \[\]'"):
            await provider.execute(
                agent=AgentDef(name="worker", prompt="hi", tools=[]),
                context={},
                rendered_prompt="hi",
                tools=[],
            )

        assert queried is False
