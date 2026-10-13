"""Tests for execution resolver sessions, engine views, and compatibility."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from conductor.config.environment import (
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    builtin_local_environment,
)
from conductor.config.schema import (
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    StepExecutionConfig,
    WorkflowConfig,
    WorkflowDef,
    WorkflowStepDef,
)
from conductor.engine.execution_resolution import ExecutionResolver, ExecutionResolverSession
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ConfigurationError
from conductor.execution import (
    CommandResult,
    CommandSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    WorkspaceLease,
)


class RecordingBackend:
    def __init__(self) -> None:
        self.prepare_calls: list[RunSpec] = []
        self.run_calls: list[tuple[CommandSpec, WorkspaceLease | None]] = []
        self.finalize_calls: list[tuple[WorkspaceLease, RunOutcome]] = []
        self.lease = WorkspaceLease("lease", "local", "one")

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=True,
            sessions=False,
            shared_workspace=True,
            snapshots=False,
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        self.prepare_calls.append(run)
        return self.lease

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Any = None,
        on_dispatch: Any = None,
    ) -> CommandResult:
        del diagnostics
        if on_dispatch is not None:
            on_dispatch()
        self.run_calls.append((spec, lease))
        return CommandResult(
            outcome="completed",
            stdout=f"{spec.command}\n",
            stderr="",
            exit_code=0,
            resolved_command=spec.command,
            duration_seconds=0.0,
        )

    async def finalize_run(
        self, lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
    ) -> None:
        self.finalize_calls.append((lease, outcome))


def _script_config(
    *,
    name: str = "resolver",
    step_name: str = "run",
    profile: str | None = None,
) -> WorkflowConfig:
    execution = StepExecutionConfig(profile=profile) if profile is not None else None
    return WorkflowConfig(
        workflow=WorkflowDef(
            name=name,
            entry_point=step_name,
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            ScriptStepDef(
                name=step_name,
                command=step_name,
                execution=execution,
                routes=[RouteDef(to="$end")],
            )
        ],
        output={"result": f"{{{{ {step_name}.output.stdout }}}}"},
    )


def _custom_environment() -> ResolvedEnvironment:
    document = EnvironmentDocument(
        default="default",
        profiles={
            "default": ProfileDefinition(backend="local"),
            "inline": ProfileDefinition(backend="local"),
        },
    )
    return ResolvedEnvironment(
        document=document,
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )


@pytest.mark.asyncio
async def test_session_prepare_is_idempotent_and_finalize_detaches() -> None:
    # Requirement: one run prepares each distinct backend once and finalization allows reuse.
    backend = RecordingBackend()
    session = ExecutionResolverSession(builtin_local_environment(), backend)
    run_spec = RunSpec(run_id="run", workflow_name="workflow")

    await session.prepare_leases(run_spec)
    await session.prepare_leases(run_spec)
    assert backend.prepare_calls == [run_spec]
    assert session.lease_for_backend("local") is backend.lease

    await session.finalize_leases("succeeded")
    assert backend.finalize_calls == [(backend.lease, "succeeded")]
    assert session.lease_for_backend("local") is None


@pytest.mark.asyncio
async def test_session_prepares_distinct_backend_instance_once() -> None:
    # Requirement: two backend names sharing one instance acquire and finalize one lease.
    backend = RecordingBackend()
    session = ExecutionResolverSession(builtin_local_environment(), backend)
    session.backends["alias"] = backend

    await session.prepare_leases(RunSpec(run_id="run", workflow_name="workflow"))
    assert len(backend.prepare_calls) == 1
    assert session.lease_for_backend("alias") is backend.lease

    await session.finalize_leases("succeeded")
    assert backend.finalize_calls == [(backend.lease, "succeeded")]


def test_for_each_inline_identity_lookup() -> None:
    # Requirement: inline steps resolve by for_each.<group>.agent, not their repeated bare name.
    config = WorkflowConfig(
        workflow=WorkflowDef(name="inline", entry_point="batch"),
        agents=[],
        for_each=[
            ForEachDef(
                name="batch",
                type="for_each",
                source="workflow.input.items",
                **{"as": "item"},
                agent=ScriptStepDef(
                    name="worker",
                    command="worker",
                    execution=StepExecutionConfig(profile="inline"),
                ),
                routes=[RouteDef(to="$end")],
            )
        ],
    )
    backend = RecordingBackend()
    session = ExecutionResolverSession(_custom_environment(), backend)
    resolver = ExecutionResolver(config, session, workflow_path=None, publish_manifest=True)

    assert resolver.backend_for_step("worker", for_each_group="batch") is backend
    assert resolver.manifest.profiles["for_each.batch.agent"].profile == "inline"


def test_backend_and_environment_together_raise() -> None:
    # Requirement: compatibility backend injection cannot conflict with environment resolution.
    with pytest.raises(ValueError, match="mutually exclusive"):
        WorkflowEngine(
            _script_config(),
            MagicMock(),
            execution_backend=RecordingBackend(),
            execution_environment=builtin_local_environment(),
        )


def test_workspace_lease_without_backend_raises() -> None:
    # Requirement: a legacy lease is valid only when its issuing backend is supplied.
    with pytest.raises(ValueError, match="requires execution_backend"):
        WorkflowEngine(
            _script_config(),
            MagicMock(),
            _workspace_lease=WorkspaceLease("lease", "local", "one"),
        )


def test_unknown_profile_fails_at_engine_construction() -> None:
    # Requirement: root manifest compilation fails before run-side effects begin.
    with pytest.raises(ConfigurationError, match="missing"):
        WorkflowEngine(
            _script_config(profile="missing"),
            MagicMock(),
            execution_environment=_custom_environment(),
        )


@pytest.mark.asyncio
async def test_child_compile_error_surfaces_at_reach_time(tmp_path: Path) -> None:
    # Requirement: child configs remain lazy but fail when the reached child engine is built.
    child = tmp_path / "child.yaml"
    child.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: child
              entry_point: inner
            agents:
              - name: inner
                type: script
                command: inner
                execution:
                  profile: missing
                routes:
                  - to: "$end"
            """
        ),
        encoding="utf-8",
    )
    parent_path = tmp_path / "parent.yaml"
    parent_path.write_text("parent", encoding="utf-8")
    config = WorkflowConfig(
        workflow=WorkflowDef(name="parent", entry_point="child"),
        agents=[WorkflowStepDef(name="child", workflow="child.yaml", routes=[RouteDef(to="$end")])],
    )

    engine = WorkflowEngine(
        config,
        MagicMock(),
        workflow_path=parent_path,
        execution_environment=_custom_environment(),
    )
    with pytest.raises(ConfigurationError, match="missing"):
        await engine.run({})


@pytest.mark.asyncio
async def test_subworkflow_child_view_resolves_child_script_step(tmp_path: Path) -> None:
    # Requirement: a child-only script resolves in its own view over the root session.
    child = tmp_path / "child.yaml"
    child.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: child
              entry_point: inner_script
            agents:
              - name: inner_script
                type: script
                command: inner_script
                routes:
                  - to: "$end"
            output:
              result: "{{ inner_script.output.stdout }}"
            """
        ),
        encoding="utf-8",
    )
    parent_path = tmp_path / "parent.yaml"
    parent_path.write_text("parent", encoding="utf-8")
    config = WorkflowConfig(
        workflow=WorkflowDef(name="parent", entry_point="outer_script"),
        agents=[
            ScriptStepDef(
                name="outer_script",
                command="outer_script",
                routes=[RouteDef(to="child")],
            ),
            WorkflowStepDef(name="child", workflow="child.yaml", routes=[RouteDef(to="$end")]),
        ],
        output={"result": "{{ child.output.result }}"},
    )
    backend = RecordingBackend()
    engine = WorkflowEngine(
        config,
        MagicMock(),
        workflow_path=parent_path,
        execution_backend=backend,
    )

    result = await engine.run({})

    assert result == {"result": "inner_script\n"}
    assert [call.command for call, _ in backend.run_calls] == ["outer_script", "inner_script"]
    assert all(lease is backend.lease for _, lease in backend.run_calls)
    assert len(backend.prepare_calls) == 1
    assert backend.finalize_calls == [(backend.lease, "succeeded")]


@pytest.mark.asyncio
async def test_child_view_shares_session_backend_and_lease(tmp_path: Path) -> None:
    # Requirement: child views share backend and lease identities while remaining distinct views.
    backend = RecordingBackend()
    session = ExecutionResolverSession(builtin_local_environment(), backend)
    await session.prepare_leases(RunSpec(run_id="run", workflow_name="root"))
    root = WorkflowEngine(_script_config(step_name="root"), MagicMock(), _execution_session=session)
    child = WorkflowEngine(
        _script_config(step_name="child"),
        MagicMock(),
        _subworkflow_depth=1,
        _execution_session=session,
    )

    assert child._execution_session is root._execution_session
    assert child._execution_backend is root._execution_backend is backend
    assert child._execution_resolver is not root._execution_resolver
    assert session.lease_for_backend("local") is backend.lease
    assert child._workspace_lease is None


@pytest.mark.asyncio
async def test_manifest_in_workflow_started_root_only() -> None:
    # Requirement: only the root workflow_started system payload publishes a manifest.
    root_events: list[WorkflowEvent] = []
    root_emitter = WorkflowEventEmitter()
    root_emitter.subscribe(root_events.append)
    backend = RecordingBackend()
    root = WorkflowEngine(
        _script_config(),
        MagicMock(),
        event_emitter=root_emitter,
        execution_backend=backend,
    )
    await root.run({})

    child_events: list[WorkflowEvent] = []
    child_emitter = WorkflowEventEmitter()
    child_emitter.subscribe(child_events.append)
    child = WorkflowEngine(
        _script_config(name="child"),
        MagicMock(),
        event_emitter=child_emitter,
        _subworkflow_depth=1,
        _execution_session=root._execution_session,
    )
    await child._execute_loop("run")

    root_started = next(event for event in root_events if event.type == "workflow_started")
    child_started = next(event for event in child_events if event.type == "workflow_started")
    assert "execution_manifest" in root_started.data["system"]
    assert "execution_manifest" not in child_started.data["system"]
