"""Integration tests for WorkflowEngine ownership of the execution backend.

Tests cover:
- Default engine construction builds one LocalRunnerBackend shared with the
  script executor (no CLI wiring needed)
- Lease lifecycle on run() and resume(): prepare -> run_command -> finalize
- Honest outcome reporting: failing scripts finalize "failed", cancelled runs
  finalize "cancelled" exactly once
- prepare_run failure leaves nothing to finalize
- Sub-workflow inheritance through BOTH child constructors (direct and
  child_engine_kwargs), with root-only prepare/finalize
- Backward compatibility of a bare ScriptExecutor()

Requirement: the engine owns backend + lease at run scope.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import conductor.engine.execution_resolution as execution_resolution_module
from conductor import redaction
from conductor.config.environment import (
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
)
from conductor.config.schema import (
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    OutputField,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    StepExecutionConfig,
    WorkflowConfig,
    WorkflowDef,
    WorkflowStepDef,
)
from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ExecutionError
from conductor.execution import (
    CommandResult,
    CommandSpec,
    LocalRunnerBackend,
    ResolvedExecutionSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    StartError,
    WorkspaceLease,
)
from conductor.executor.script import ScriptExecutor, ScriptOutput
from conductor.redaction import REDACTED_MARKER, RunRedactor


class RecordingBackend:
    """RunnerBackend test double recording the full lifecycle.

    run_command returns a completed result by default; individual tests can
    override ``command_result`` or ``run_command_impl`` to script failures or
    blocking behavior.
    """

    def __init__(self) -> None:
        self.prepare_calls: list[RunSpec] = []
        self.run_calls: list[tuple[CommandSpec, WorkspaceLease | None]] = []
        self.finalize_calls: list[tuple[WorkspaceLease, RunOutcome]] = []
        self.lease = WorkspaceLease(lease_id="test-lease", backend="recording", incarnation="0001")
        self.command_result = CommandResult(
            outcome="completed",
            stdout="recorded",
            stderr="",
            exit_code=0,
            resolved_command="recorded-cmd",
            duration_seconds=0.0,
        )
        self.run_command_impl: Any = None
        self.prepare_error: Exception | None = None

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=True, sessions=False, shared_workspace=True, snapshots=False
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        self.prepare_calls.append(run)
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.lease

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Any = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> CommandResult:
        if on_dispatch is not None:
            on_dispatch()
        self.run_calls.append((spec, lease))
        if self.run_command_impl is not None:
            return await self.run_command_impl(spec, lease)
        return self.command_result

    async def finalize_run(
        self, lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
    ) -> None:
        self.finalize_calls.append((lease, outcome))


def _script_config(name: str = "engine-backend") -> WorkflowConfig:
    """Minimal one-script workflow (no provider interaction)."""
    return WorkflowConfig(
        workflow=WorkflowDef(
            name=name,
            entry_point="runner",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            ScriptStepDef(
                name="runner",
                command="does-not-matter-recorded",
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"result": "{{ runner.output.stdout }}"},
    )


class TestDefaultConstruction:
    """Engine construction without the new kwargs keeps working everywhere."""

    @pytest.mark.asyncio
    async def test_default_engine_builds_one_local_backend(self) -> None:
        """Requirement: an engine built without execution_backend constructs a
        LocalRunnerBackend itself, and the SAME instance sits in
        script_executor._backend -- a single ownership point. This pins that
        cli/run.py construction sites (run/resume/mock) need no edits: the
        default backend covers them."""
        engine = WorkflowEngine(_script_config(), MagicMock())

        assert isinstance(engine._execution_backend, LocalRunnerBackend)
        assert engine.script_executor._backend is engine._execution_backend
        assert engine._workspace_lease is None


class TestRunLeaseLifecycle:
    """Root engine prepares before the loop and finalizes in finally."""

    @pytest.mark.asyncio
    async def test_run_lifecycles_lease_with_succeeded_outcome(self) -> None:
        """Requirement: engine.run() on a one-script workflow calls
        prepare_run exactly once with RunSpec(run_id=<engine run id>),
        run_command receives the spec plus that lease, and finalize_run is
        called exactly once with "succeeded" -- the lease lifecycle belongs
        to the run."""
        backend = RecordingBackend()
        engine = WorkflowEngine(_script_config(), MagicMock(), execution_backend=backend)

        result = await engine.run({})

        assert result["result"] == "recorded"
        assert len(backend.prepare_calls) == 1
        assert backend.prepare_calls[0].run_id == engine._run_id
        assert backend.prepare_calls[0].workflow_name == "engine-backend"
        assert len(backend.run_calls) == 1
        spec, lease = backend.run_calls[0]
        assert isinstance(spec, CommandSpec)
        assert lease is backend.lease
        assert backend.finalize_calls == [(backend.lease, "succeeded")]

    @pytest.mark.asyncio
    async def test_failing_script_finalizes_failed(self) -> None:
        """Requirement: a script step that fails (command not found) surfaces
        as ExecutionError from engine.run() and the backend still hears an
        honest "failed" outcome -- finalize distinguishes failure from
        success."""
        backend = RecordingBackend()
        backend.command_result = CommandResult(
            outcome="command_not_found",
            resolved_command="missing",
            start_error=StartError(kind="file_not_found", message="No such file or directory"),
        )
        engine = WorkflowEngine(_script_config(), MagicMock(), execution_backend=backend)

        with pytest.raises(ExecutionError):
            await engine.run({})

        assert [outcome for _, outcome in backend.finalize_calls] == ["failed"]
        assert len(backend.finalize_calls) == 1

    @pytest.mark.asyncio
    async def test_prepare_failure_never_finalizes(self) -> None:
        """Requirement: a prepare_run that raises leaves NO lease to finalize;
        finalize_run must never be called (and never with None) -- the
        lease-guard in _finalize_execution_backend, not just the happy path."""
        backend = RecordingBackend()
        backend.prepare_error = RuntimeError("realm unavailable")
        engine = WorkflowEngine(_script_config(), MagicMock(), execution_backend=backend)

        with pytest.raises(RuntimeError, match="realm unavailable"):
            await engine.run({})

        assert backend.finalize_calls == []

    @pytest.mark.asyncio
    async def test_cancelled_run_finalizes_cancelled_exactly_once(self) -> None:
        """Requirement: cancelling the run task surfaces CancelledError AND
        finalize_run is called exactly once with "cancelled" -- cancellation
        never finalizes as "failed" and never double-finalizes."""

        async def block_forever(spec: CommandSpec, lease: WorkspaceLease | None) -> CommandResult:
            await asyncio.Event().wait()  # noqa: ASYNC110 - never set by design
            raise AssertionError("unreachable")

        backend = RecordingBackend()
        backend.run_command_impl = block_forever
        engine = WorkflowEngine(_script_config(), MagicMock(), execution_backend=backend)

        task = asyncio.create_task(engine.run({}))
        await asyncio.sleep(0.1)  # let the run reach the script step
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert [outcome for _, outcome in backend.finalize_calls] == ["cancelled"]
        assert len(backend.finalize_calls) == 1


class TestResumeLeaseLifecycle:
    """resume() must lifecycle the backend identically to run() (parity)."""

    @pytest.mark.asyncio
    async def test_resume_lifecycles_lease_with_succeeded_outcome(self) -> None:
        """Requirement: engine.resume() on a restored engine calls
        prepare_run -> run_command -> finalize_run("succeeded") exactly like
        run() -- the resume wiring cannot silently differ from the run
        wiring (Oracle blocker)."""
        backend = RecordingBackend()
        engine = WorkflowEngine(_script_config(), MagicMock(), execution_backend=backend)

        restored_ctx = WorkflowContext()
        restored_ctx.set_workflow_inputs({})
        engine.set_context(restored_ctx)
        engine.set_limits(
            LimitEnforcer.from_dict(
                {"current_iteration": 0, "max_iterations": 10, "execution_history": []},
                timeout_seconds=None,
                budget_usd=None,
                budget_mode="audit",
            )
        )

        result = await engine.resume("runner")

        assert result["result"] == "recorded"
        assert len(backend.prepare_calls) == 1
        assert backend.prepare_calls[0].run_id == engine._run_id
        assert len(backend.run_calls) == 1
        assert backend.run_calls[0][1] is backend.lease
        assert backend.finalize_calls == [(backend.lease, "succeeded")]


class TestSubworkflowInheritance:
    """Sub-workflow engines inherit backend + lease; root-only prepare/finalize."""

    def _write_sub_with_script(self, directory: Path) -> Path:
        """Sub-workflow whose only step is a script step."""
        sub = directory / "sub.yaml"
        sub.write_text(
            textwrap.dedent(
                """\
                workflow:
                  name: sub-with-script
                  entry_point: inner_script
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 5
                agents:
                  - name: inner_script
                    type: script
                    command: recorded-sub-command
                    routes:
                      - to: "$end"
                output:
                  result: "{{ inner_script.output.stdout }}"
                """
            ),
            encoding="utf-8",
        )
        return sub

    @pytest.mark.asyncio
    async def test_direct_child_constructor_shares_one_backend_and_lease(
        self, tmp_path: Path
    ) -> None:
        """Requirement: a type:workflow child built by _execute_subworkflow
        runs its script step through the SAME backend instance with the
        SAME lease, and prepare_run is called exactly once for the whole run
        -- no second backend/lease per sub-workflow (Oracle blocker)."""
        self._write_sub_with_script(tmp_path)
        parent_path = tmp_path / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="outer_script",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                ScriptStepDef(
                    name="outer_script",
                    command="recorded-parent-command",
                    routes=[RouteDef(to="child")],
                ),
                WorkflowStepDef(
                    name="child",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ child.output.result }}"},
        )

        backend = RecordingBackend()
        engine = WorkflowEngine(
            config, MagicMock(), workflow_path=parent_path, execution_backend=backend
        )
        result = await engine.run({})

        assert result["result"] == "recorded"
        # Parent script + child script: both through the one injected backend.
        assert len(backend.run_calls) == 2
        assert all(lease is backend.lease for _, lease in backend.run_calls)
        # Root-only prepare/finalize: exactly one pair for the whole run.
        assert len(backend.prepare_calls) == 1
        assert backend.finalize_calls == [(backend.lease, "succeeded")]

    @pytest.mark.asyncio
    async def test_for_each_child_kwargs_path_inherits_backend_and_lease(
        self, tmp_path: Path
    ) -> None:
        """Requirement: the child_engine_kwargs dict path (for_each inline
        sub-workflows) threads backend + lease exactly like the direct
        constructor -- both construction sites are covered (the dict path
        historically missed threaded kwargs, see _dashboard_context_path)."""
        self._write_sub_with_script(tmp_path)
        parent_path = tmp_path / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="finder",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=20),
            ),
            agents=[
                # Requirement: the finder agent only feeds the for_each source;
                # the recorded backend answers every script step, so no real
                # LLM call is needed anywhere in this workflow.
                ScriptStepDef(
                    name="finder",
                    command="recorded-finder-command",
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
                    agent=WorkflowStepDef(
                        name="runner",
                        workflow="sub.yaml",
                        input_mapping={"item": "{{ item }}"},
                    ),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"done": "1"},
        )

        # finder is a script step: its stdout is parsed as JSON for items.
        backend = RecordingBackend()
        backend.command_result = CommandResult(
            outcome="completed",
            stdout='{"items": ["a", "b"]}',
            stderr="",
            exit_code=0,
            resolved_command="recorded-finder-command",
            duration_seconds=0.0,
        )
        engine = WorkflowEngine(
            config, MagicMock(), workflow_path=parent_path, execution_backend=backend
        )
        await engine.run({})

        # finder + 2 for_each iterations of the sub-workflow's script step.
        assert len(backend.run_calls) == 3
        assert all(lease is backend.lease for _, lease in backend.run_calls)
        assert len(backend.prepare_calls) == 1
        assert backend.finalize_calls == [(backend.lease, "succeeded")]


class TestScriptExecutorBackwardCompat:
    """A bare ScriptExecutor outside any engine keeps working."""

    @pytest.mark.asyncio
    async def test_bare_script_executor_still_executes(self) -> None:
        """Requirement: ScriptExecutor() with no arguments still runs a script
        step through a default LocalRunnerBackend -- the constructor contract
        is backward compatible for embedders that never touch the engine."""
        executor = ScriptExecutor()
        agent = ScriptStepDef(
            name="standalone",
            command=sys.executable,
            args=["-c", "print('standalone ok')"],
        )
        output: ScriptOutput = await executor.execute(agent, {})
        assert "standalone ok" in output.stdout
        assert output.exit_code == 0


class GuardedLeaseBackend(RecordingBackend):
    """RecordingBackend that mints a fresh lease per prepare and rejects
    already-finalized handles, so a reused lease fails loudly (PR #541
    review: a finalized lease must never be handed to another command)."""

    def __init__(self) -> None:
        super().__init__()
        self.leases: list[WorkspaceLease] = []
        self._finalized: set[WorkspaceLease] = set()

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        self.prepare_calls.append(run)
        lease = WorkspaceLease(run.run_id, "guarded", f"incarnation-{len(self.leases)}")
        self.leases.append(lease)
        return lease

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Any = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> CommandResult:
        assert lease not in self._finalized, "run_command received a finalized lease"
        return await super().run_command(
            spec, lease, diagnostics=diagnostics, on_dispatch=on_dispatch
        )

    async def finalize_run(
        self, lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
    ) -> None:
        assert lease not in self._finalized, "lease finalized twice"
        self._finalized.add(lease)
        self.finalize_calls.append((lease, outcome))


class TestRepeatedRunLeaseLifecycle:
    """A reused engine instance must prepare a fresh lease per run."""

    @pytest.mark.asyncio
    async def test_second_run_prepares_a_fresh_lease(self) -> None:
        """Requirement: finalization detaches the lease from engine state, so
        a repeated run() on the SAME engine instance calls prepare_run again
        and neither reuses nor re-finalizes the first run's handle (PR #541
        review)."""
        backend = GuardedLeaseBackend()
        engine = WorkflowEngine(_script_config(), MagicMock(), execution_backend=backend)

        await engine.run({})
        assert engine._workspace_lease is None
        await engine.run({})
        assert engine._workspace_lease is None

        assert len(backend.prepare_calls) == 2
        assert backend.leases[0] is not backend.leases[1]
        assert backend.finalize_calls == [
            (backend.leases[0], "succeeded"),
            (backend.leases[1], "succeeded"),
        ]


class TestFinalizationUnderCancellation:
    """Backend cleanup completes even when cancellation races it."""

    @pytest.mark.asyncio
    async def test_cancellation_during_finalize_completes_cleanup(self) -> None:
        """Requirement: a cancellation arriving while finalize_run is
        mid-cleanup is held back by the shield until the backend's teardown
        completes, then re-raised -- an interrupted async finalize would leak
        the backend's realm resources (PR #541 review). The outcome was
        already determined by the finished loop, so it stays "succeeded"."""
        backend = RecordingBackend()
        finalize_started = asyncio.Event()
        finalize_release = asyncio.Event()
        completed: list[tuple[WorkspaceLease, RunOutcome]] = []

        async def blocking_finalize(
            lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
        ) -> None:
            finalize_started.set()
            await finalize_release.wait()
            completed.append((lease, outcome))

        backend.finalize_run = blocking_finalize  # type: ignore[method-assign]
        engine = WorkflowEngine(_script_config(), MagicMock(), execution_backend=backend)

        task = asyncio.create_task(engine.run({}))
        await finalize_started.wait()
        task.cancel()
        await asyncio.sleep(0.05)  # let the cancellation land in the shield
        assert not task.done(), "cancellation must not interrupt finalization"
        finalize_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed == [(backend.lease, "succeeded")]

    @pytest.mark.asyncio
    async def test_cancellation_during_mcp_close_still_finalizes(self) -> None:
        """Requirement: a cancellation landing in the finally's MCP-manager
        close must not skip backend finalization -- the finalize runs from an
        inner finally so realm cleanup is unconditional (PR #541 review)."""
        backend = RecordingBackend()
        engine = WorkflowEngine(_script_config(), MagicMock(), execution_backend=backend)

        step_started = asyncio.Event()
        step_release = asyncio.Event()
        close_started = asyncio.Event()

        async def blocking_command(
            spec: CommandSpec, lease: WorkspaceLease | None
        ) -> CommandResult:
            step_started.set()
            await step_release.wait()
            return backend.command_result

        backend.run_command_impl = blocking_command

        class BlockingCloseManager:
            async def close(self) -> None:
                close_started.set()
                await asyncio.Event().wait()  # never released by design

        task = asyncio.create_task(engine.run({}))
        await step_started.wait()
        engine._mcp_step_managers[("server", "cwd")] = cast("Any", BlockingCloseManager())
        step_release.set()
        await close_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.finalize_calls == [(backend.lease, "succeeded")]


class TestExecutionSpecThreading:
    """The manifest's container payload threads into the backend's CommandSpec.

    Requirement: ``ExecutionResolver.execution_spec_for_step`` adapts the
    manifest audit model to the stdlib-only contract, and ``_execute_script``
    carries the payload (plus the step name) into ``ScriptExecutor.execute``.
    """

    def _docker_environment(self) -> ResolvedEnvironment:
        document = EnvironmentDocument(
            default="local",
            profiles={
                "local": ProfileDefinition(backend="local"),
                "container": ProfileDefinition(
                    backend="docker",
                    docker=DockerProfileOptions(
                        image="python:3.12",
                        platform="linux/amd64",
                        user="1000",
                    ),
                ),
            },
        )
        return ResolvedEnvironment(
            document=document,
            name="test",
            source="path",
            path=None,
            digest="sha256:test",
        )

    def _docker_script_config(self) -> WorkflowConfig:
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="docker-threading",
                entry_point="runner",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                ScriptStepDef(
                    name="runner",
                    command="recorded-docker-command",
                    execution=StepExecutionConfig(profile="container"),
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ runner.output.stdout }}"},
        )

    @pytest.mark.asyncio
    async def test_docker_step_payload_reaches_command_spec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: a docker-profiled script step executes with
        CommandSpec.execution carrying the manifest-resolved values and
        name=step, so the container backend receives everything the profile
        promised without re-reading the manifest."""

        # Requirement: stay off the filesystem bundle path -- this test pins
        # the kwargs threading, not bundle collection (covered in test_engine).
        async def no_bundle(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr("conductor.engine.workflow.prepare_run_bundle", no_bundle)
        backend = RecordingBackend()
        monkeypatch.setitem(
            execution_resolution_module.BACKEND_FACTORIES, "docker", lambda: backend
        )
        workflow_path = tmp_path / "workflow.yaml"
        workflow_path.write_text("dummy", encoding="utf-8")
        engine = WorkflowEngine(
            self._docker_script_config(),
            MagicMock(),
            workflow_path=workflow_path,
            execution_environment=self._docker_environment(),
        )

        result = await engine.run({})

        assert result["result"] == "recorded"
        spec, lease = backend.run_calls[0]
        assert lease is backend.lease
        assert spec.execution == ResolvedExecutionSpec(
            image="python:3.12",
            platform="linux/amd64",
            user="1000",
        )
        assert spec.name == "runner"

    @pytest.mark.asyncio
    async def test_local_step_keeps_legacy_command_spec(self) -> None:
        """Requirement: local-path byte parity -- a step without a container
        payload produces the exact legacy CommandSpec (execution=None and
        name=None), so existing backends and parity tests observe no change."""
        backend = RecordingBackend()
        engine = WorkflowEngine(_script_config(), MagicMock(), execution_backend=backend)

        await engine.run({})

        spec = backend.run_calls[0][0]
        assert spec.execution is None
        assert spec.name is None
        assert spec == CommandSpec(
            command="does-not-matter-recorded",
            args=(),
            working_dir=None,
            env={},
            inherit_control_environment=True,
            stdin=None,
            timeout=None,
        )

    def test_for_each_key_path_resolves_inline_agent_payload(self, tmp_path: Path) -> None:
        """Requirement: the qualified for-each manifest key
        (``for_each.<group>.agent``) resolves the inline agent's payload, and a
        local-profiled inline agent resolves to None -- the same identity
        helper drives both compile and runtime lookup."""
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="for-each-threading",
                entry_point="batch",
                runtime=RuntimeConfig(provider="copilot"),
                limits=LimitsConfig(max_iterations=10),
            ),
            for_each=[
                ForEachDef(
                    name="batch",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    max_concurrent=1,
                    agent=ScriptStepDef(
                        name="item",
                        command="recorded-item-command",
                        execution=StepExecutionConfig(profile="container"),
                    ),
                ),
                ForEachDef(
                    name="plain",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    max_concurrent=1,
                    agent=ScriptStepDef(
                        name="plain_item",
                        command="recorded-plain-command",
                        execution=StepExecutionConfig(profile="local"),
                    ),
                ),
            ],
            agents=[],
            output={"done": "1"},
        )
        workflow_path = tmp_path / "workflow.yaml"
        workflow_path.write_text("dummy", encoding="utf-8")
        engine = WorkflowEngine(
            config,
            MagicMock(),
            workflow_path=workflow_path,
            execution_environment=self._docker_environment(),
        )
        resolver = engine._execution_resolver

        assert resolver.execution_spec_for_step("item", for_each_group="batch") == (
            ResolvedExecutionSpec(image="python:3.12", platform="linux/amd64", user="1000")
        )
        assert resolver.execution_spec_for_step("plain_item", for_each_group="plain") is None

    @pytest.mark.asyncio
    async def test_subworkflow_child_resolves_payload_lazily(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: a sub-workflow child engine compiles its own manifest
        view at reach time and resolves the docker payload through it -- the
        root manifest never claims authority over configuration it has not
        read, yet the child step still receives the resolved payload."""

        # Requirement: the child workflow declares the docker profile; the
        # child manifest (and its payload) exists only once the child engine
        # is constructed at reach time.
        sub_path = tmp_path / "sub.yaml"
        sub_path.write_text(
            textwrap.dedent(
                """\
                workflow:
                  name: sub-with-docker-script
                  entry_point: inner_script
                  runtime:
                    provider: copilot
                  limits:
                    max_iterations: 5
                agents:
                  - name: inner_script
                    type: script
                    command: recorded-sub-command
                    execution:
                      profile: container
                    routes:
                      - to: "$end"
                output:
                  result: "{{ inner_script.output.stdout }}"
                """
            ),
            encoding="utf-8",
        )
        parent_path = tmp_path / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="outer_script",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                ScriptStepDef(
                    name="outer_script",
                    command="recorded-parent-command",
                    routes=[RouteDef(to="child")],
                ),
                WorkflowStepDef(
                    name="child",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ child.output.result }}"},
        )

        async def no_bundle(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr("conductor.engine.workflow.prepare_run_bundle", no_bundle)
        monkeypatch.setattr(execution_resolution_module, "materialize_run_bundle", no_bundle)
        backend = RecordingBackend()
        monkeypatch.setitem(
            execution_resolution_module.BACKEND_FACTORIES, "docker", lambda: backend
        )
        engine = WorkflowEngine(
            config,
            MagicMock(),
            workflow_path=parent_path,
            execution_environment=self._docker_environment(),
        )
        # Requirement: the parent's local step must not hit the real host
        # backend either -- both steps are recorded by the one double.
        engine._execution_session.backends["local"] = backend

        result = await engine.run({})

        assert result["result"] == "recorded"
        specs_by_command = {spec.command: spec for spec, _ in backend.run_calls}
        child_spec = specs_by_command["recorded-sub-command"]
        # Requirement: the child's own manifest view supplied the payload.
        assert child_spec.execution == ResolvedExecutionSpec(
            image="python:3.12",
            platform="linux/amd64",
            user="1000",
        )
        assert child_spec.name == "inner_script"
        # Requirement: the local parent step stays payload-free (byte parity).
        parent_spec = specs_by_command["recorded-parent-command"]
        assert parent_spec.execution is None
        assert parent_spec.name is None
        # Requirement: the child discovers the docker backend lazily at reach
        # time and leases it against the ROOT run spec (late discovery) --
        # one prepare per backend name, both naming the parent workflow.
        assert len(backend.prepare_calls) == 2
        assert all(call.workflow_name == "parent" for call in backend.prepare_calls)
        assert [outcome for _, outcome in backend.finalize_calls] == ["succeeded"]
        assert len(backend.finalize_calls) == 1


class TestDiagnosticsScrub:
    """Backend diagnostics pass through the run redactor when one is active."""

    @pytest.mark.asyncio
    async def test_diagnostics_scrubbed_when_redactor_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: with an active run redactor, a secret value appearing
        in backend diagnostics (e.g. pull/registry output echoing an env
        credential) never reaches the verbose sink unredacted."""
        messages: list[str] = []
        monkeypatch.setattr(
            "conductor.executor.script._verbose_log",
            lambda message, style="dim": messages.append(message),
        )
        secret = "registry-credential-value"
        redactor = RunRedactor()
        redactor.register([secret])
        token = redaction.set_current(redactor)
        try:
            sink = ScriptExecutor._make_diagnostics()
            sink(f"pull failed while using {secret}")
        finally:
            redaction.reset_current(token)

        assert messages == [f"pull failed while using {REDACTED_MARKER}"]

    @pytest.mark.asyncio
    async def test_diagnostics_pass_through_without_redactor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement: with no active redactor the sink output is
        byte-identical to the backend's diagnostic text (zero-noise parity)."""
        messages: list[str] = []
        monkeypatch.setattr(
            "conductor.executor.script._verbose_log",
            lambda message, style="dim": messages.append(message),
        )

        sink = ScriptExecutor._make_diagnostics()
        sink("pull failed while using registry-credential-value")

        assert messages == ["pull failed while using registry-credential-value"]
