"""Resume gates and lifecycle decisions at the engine boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, patch

import pytest

from conductor.config.environment import (
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    builtin_local_environment,
)
from conductor.config.schema import (
    AgentDef,
    OutputField,
    RestartConfig,
    RouteDef,
    ScriptStepDef,
    StepExecutionConfig,
    WorkflowConfig,
    WorkflowDef,
    WorkspaceConfig,
)
from conductor.engine.checkpoint import CheckpointData, CheckpointManager
from conductor.engine.execution_resolution import BACKEND_FACTORIES
from conductor.engine.workflow import RunContext, WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import CheckpointError, ConfigurationError, ExecutionError, TemplateError
from conductor.execution import (
    BundleRef,
    CommandResult,
    CommandSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    WorkspaceIdentity,
    WorkspaceLease,
)
from conductor.execution.errors import WorkspaceAttachError
from conductor.providers.copilot import CopilotProvider


def _engine(
    tmp_path: Path,
    *,
    persistence: Literal["durable", "on-failure"] = "durable",
    restart: Literal["rerun", "fail"] = "rerun",
) -> WorkflowEngine:
    path = tmp_path / "workflow.yaml"
    path.write_text("workflow: test\n", encoding="utf-8")
    document = EnvironmentDocument(
        default="docker",
        profiles={
            "docker": ProfileDefinition(
                backend="docker", docker=DockerProfileOptions(image="busybox:latest")
            )
        },
    )
    environment = ResolvedEnvironment(
        document=document, name="test", source="path", path=None, digest="sha256:env"
    )
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="test", entry_point="script", workspace=WorkspaceConfig(persistence=persistence)
        ),
        agents=[
            ScriptStepDef(
                name="script",
                command="true",
                timeout=None,
                execution=StepExecutionConfig(profile="docker"),
                restart=RestartConfig(mode=restart),
                routes=[RouteDef(to="$end")],
            )
        ],
    )
    return WorkflowEngine(
        config,
        workflow_path=path,
        execution_environment=environment,
        run_context=RunContext(run_id="lifecycle-test"),
    )


def _checkpoint(engine: WorkflowEngine) -> CheckpointData:
    return CheckpointData(
        version=1,
        workflow_path=str(engine.workflow_path),
        workflow_hash="sha256:ok",
        created_at="2026-01-01T00:00:00Z",
        failure={},
        inputs={},
        current_agent="script",
        context={},
        limits={},
        run_id="lifecycle-test",
        resume_contract=engine._resume_contract(),
        workspace={
            "policy": "durable",
            "identities": {
                "docker": {
                    "backend": "docker",
                    "lease_id": "lifecycle-test",
                    "incarnation": "first",
                    "location": None,
                }
            },
            "executed_backends": [],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "workflow_digest",
        "environment_name",
        "environment_digest",
        "manifest_digest",
    ],
)
async def test_digest_gate_refuses_before_backend_or_dispatch(tmp_path: Path, field: str) -> None:
    # Requirement: each resume identity field is checked before backend preparation or dispatch.
    engine = _engine(tmp_path)
    checkpoint = _checkpoint(engine)
    assert checkpoint.resume_contract is not None
    checkpoint.resume_contract[field] = "recorded-other"
    with (
        patch.object(engine._execution_session, "ensure_backends", new_callable=AsyncMock) as prep,
        patch.object(engine, "_execute_loop", new_callable=AsyncMock) as dispatch,
        pytest.raises(CheckpointError, match=field) as rejected,
    ):
        await engine.resume("script", checkpoint=checkpoint)
    assert "recorded-other" in str(rejected.value)
    assert "--environment" in str(rejected.value)
    assert "current" in str(rejected.value)
    prep.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_retained_resume_requires_checkpoint_and_new_contract(tmp_path: Path) -> None:
    # Requirement: direct callers cannot bypass lifecycle checks using an old checkpoint.
    engine = _engine(tmp_path)
    with pytest.raises(CheckpointError, match="checkpoint data"):
        await engine.resume("script")
    checkpoint = _checkpoint(engine)
    checkpoint.resume_contract = None
    with pytest.raises(CheckpointError, match="fresh run"):
        await engine.resume("script", checkpoint=checkpoint)


@pytest.mark.asyncio
async def test_restart_fail_blocks_attach_before_any_dispatch(tmp_path: Path) -> None:
    # Requirement: a failed in-flight step with restart=fail cannot attach its workspace.
    engine = _engine(tmp_path, restart="fail")
    checkpoint = _checkpoint(engine)
    with (
        patch.object(engine._execution_session, "ensure_backends", new_callable=AsyncMock) as prep,
        patch.object(engine, "_execute_loop", new_callable=AsyncMock) as dispatch,
        pytest.raises(CheckpointError, match="script.*restart.mode: fail"),
    ):
        await engine.resume("script", checkpoint=checkpoint)
    prep.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_docker_use_after_set_checkpoint_is_not_marked_staged(
    tmp_path: Path,
) -> None:
    # Requirement: a set-step history never implies a Docker marker already exists.
    engine = _engine(tmp_path)
    checkpoint = _checkpoint(engine)
    checkpoint.trigger = "periodic"
    checkpoint.context["execution_history"] = [{"agent_name": "set"}]
    with (
        patch("conductor.engine.workflow.acquire_workspace_claim"),
        patch("conductor.engine.workflow.prepare_run_bundle", new_callable=AsyncMock),
        patch.object(engine._execution_session, "ensure_backends", new_callable=AsyncMock),
        patch.object(engine._execution_session, "prepare_leases", new_callable=AsyncMock) as prep,
        patch.object(engine._execution_session, "finalize_leases", new_callable=AsyncMock),
        patch.object(engine, "_execute_loop", new_callable=AsyncMock, return_value={}),
    ):
        await engine.resume("script", checkpoint=checkpoint)
    assert prep.await_args is not None
    assert prep.await_args.kwargs["expect_staged"] == {"docker": False}


def test_second_generation_checkpoint_keeps_prior_executed_backend(tmp_path: Path) -> None:
    # Requirement: a resumed failure on an engine-local step cannot erase Docker execution.
    engine = _engine(tmp_path)
    engine._execution_session.restore_executed_backends(["docker"])
    engine._execution_session.record_backend_execution("local")
    engine._current_agent_name = "script"
    with patch("conductor.engine.workflow.CheckpointManager.save_checkpoint") as save:
        engine._write_checkpoint(RuntimeError("failed"), trigger="failure")
    assert save.call_args.kwargs["workspace"]["executed_backends"] == ["docker", "local"]
    assert save.call_args.kwargs["interrupted_step"] == {
        "name": "script",
        "status": "unknown",
        "attempt_id": None,
    }


def test_agent_attempt_event_field_requires_restart_and_retained_backend(tmp_path: Path) -> None:
    # Requirement: legacy agent events omit attempt_id until both contract arms apply.
    original = _engine(tmp_path)
    agent = AgentDef(
        name="worker",
        model="gpt-4",
        prompt="Return a value",
        output={"value": OutputField(type="string")},
        restart=RestartConfig(mode="rerun"),
        routes=[RouteDef(to="$end")],
    )
    original.config.workflow.entry_point = agent.name
    original.config.workflow.workspace = None
    original.config.agents = [agent]
    engine = WorkflowEngine(
        original.config,
        workflow_path=original.workflow_path,
        execution_environment=builtin_local_environment(),
    )
    engine._mint_attempt(agent.name)
    with (
        patch.object(engine, "_workspace_persistence", return_value="durable"),
        patch.object(
            engine._execution_resolver, "backend_for_step", return_value=_RetainedDocker()
        ),
    ):
        assert engine._agent_attempt_field(agent, agent.name) == {
            "attempt_id": engine._in_flight_attempts[agent.name]
        }
        assert (
            engine._agent_attempt_field(agent.model_copy(update={"restart": None}), agent.name)
            == {}
        )
    assert engine._agent_attempt_field(agent, agent.name) == {}


@pytest.mark.asyncio
async def test_subworkflow_view_inherits_root_retained_identity(tmp_path: Path) -> None:
    # Requirement: a child uses the root's physical lease and incarnation, not a new volume.
    root = _engine(tmp_path, persistence="on-failure")
    backend = _RetainedDocker()
    root._execution_session.backends["docker"] = backend
    await root._execution_session.prepare_leases(
        RunSpec("lifecycle-test", workspace_persistence="on-failure")
    )
    child_config = WorkflowConfig(
        workflow=WorkflowDef(name="child", entry_point="script"),
        agents=[
            ScriptStepDef(
                name="script",
                command="true",
                timeout=None,
                execution=StepExecutionConfig(profile="docker"),
            )
        ],
    )
    child = WorkflowEngine(
        child_config,
        workflow_path=root.workflow_path,
        _subworkflow_depth=1,
        _execution_session=root._execution_session,
    )
    assert (
        child._execution_session.workspace_identities()
        == root._execution_session.workspace_identities()
    )
    assert child._execution_session.lease_for_backend(
        "docker"
    ) is root._execution_session.lease_for_backend("docker")
    await root._execution_session.finalize_leases("failed", retain=True)


@pytest.mark.asyncio
async def test_legacy_agent_started_event_omits_attempt_id(tmp_path: Path) -> None:
    # Requirement: agent event payload stays byte-compatible without a retained restart contract.
    path = tmp_path / "legacy.yaml"
    path.write_text("workflow: legacy\n", encoding="utf-8")
    agent = AgentDef(
        name="worker",
        model="gpt-4",
        prompt="Return a value",
        output={"value": OutputField(type="string")},
        routes=[RouteDef(to="$end")],
    )
    emitter = WorkflowEventEmitter()
    events: list[WorkflowEvent] = []
    emitter.subscribe(events.append)
    engine = WorkflowEngine(
        WorkflowConfig(workflow=WorkflowDef(name="legacy", entry_point="worker"), agents=[agent]),
        CopilotProvider(mock_handler=lambda _agent, _prompt, _context: {"value": "ok"}),
        workflow_path=path,
        event_emitter=emitter,
    )
    await engine.run({})
    started = [event for event in events if event.type == "agent_started"]
    assert len(started) == 1
    assert "attempt_id" not in started[0].data


class _RetainedDocker:
    """Stateful fake preserving one volume and its marker across engine instances."""

    def __init__(self) -> None:
        self.volume = False
        self.marker = False
        self.fail_command = True
        self.outcomes: list[tuple[RunOutcome, bool]] = []
        self.attempts: list[str | None] = []

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=True,
            sessions=False,
            shared_workspace=False,
            snapshots=False,
            retained_workspace=True,
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        assert not self.volume
        assert run.workspace_persistence == "on-failure"
        self.volume = True
        return WorkspaceLease(run.run_id, "docker", "incarnation", "volume")

    async def attach_run(
        self, run: RunSpec, identity: WorkspaceIdentity, *, expect_staged: bool = False
    ) -> WorkspaceLease:
        assert self.volume and identity.lease_id == run.run_id
        if expect_staged and not self.marker:
            raise ExecutionError("missing marker")
        return WorkspaceLease(run.run_id, "docker", identity.incarnation, "volume")

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics=None,
        on_dispatch=None,
    ) -> CommandResult:
        assert lease is not None and self.volume
        if on_dispatch is not None:
            on_dispatch()
        self.marker = True
        self.attempts.append(spec.attempt_id)
        if self.fail_command:
            raise ExecutionError("fake command failure")
        return CommandResult(outcome="completed", stdout="ok", exit_code=0)

    async def finalize_run(
        self, lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
    ) -> None:
        self.outcomes.append((outcome, retain))
        if not retain:
            self.volume = False


@pytest.mark.asyncio
async def test_failed_run_retains_docker_and_successful_resume_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a real engine failure retains a staged volume for attach and success removes it.
    backend = _RetainedDocker()
    monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: backend)
    bundle = BundleRef("sha256:bundle", str(tmp_path))
    engine = _engine(tmp_path, persistence="on-failure")
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        pytest.raises(ExecutionError, match="fake command failure"),
    ):
        await engine.run({})
    assert backend.volume is True
    assert backend.outcomes == [("failed", True)]
    assert engine._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
    assert checkpoint.workspace is not None
    assert checkpoint.workspace["executed_backends"] == ["docker"]
    assert checkpoint.interrupted_step is not None
    assert backend.attempts[0] is not None
    assert checkpoint.interrupted_step["attempt_id"] == backend.attempts[0]

    backend.fail_command = False
    resumed = _engine(tmp_path, persistence="on-failure")
    with patch(
        "conductor.engine.workflow.prepare_run_bundle", new_callable=AsyncMock, return_value=bundle
    ):
        await resumed.resume("script", checkpoint=checkpoint)
    assert backend.outcomes == [("failed", True), ("succeeded", False)]
    assert backend.volume is False
    assert backend.attempts[0] != backend.attempts[1]


@pytest.mark.asyncio
async def test_precreate_verification_failure_does_not_mark_backend_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: attached verification before create cannot claim a Docker command ran.
    class VerifyBeforeCreate(_RetainedDocker):
        reject = True

        async def run_command(
            self,
            spec: CommandSpec,
            lease: WorkspaceLease | None,
            *,
            diagnostics=None,
            on_dispatch=None,
        ) -> CommandResult:
            if self.reject:
                raise WorkspaceAttachError("pre-create verification refused")
            return await super().run_command(
                spec, lease, diagnostics=diagnostics, on_dispatch=on_dispatch
            )

    backend = VerifyBeforeCreate()
    backend.fail_command = False
    monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: backend)
    bundle = BundleRef("sha256:bundle", str(tmp_path))
    engine = _engine(tmp_path, persistence="on-failure")
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        pytest.raises(ConfigurationError, match="pre-create verification refused"),
    ):
        await engine.run({})

    assert backend.attempts == [] and backend.marker is False
    assert engine._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
    assert checkpoint.workspace is not None
    assert checkpoint.workspace["executed_backends"] == []

    backend.reject = False
    resumed = _engine(tmp_path, persistence="on-failure")
    with patch(
        "conductor.engine.workflow.prepare_run_bundle", new_callable=AsyncMock, return_value=bundle
    ):
        await resumed.resume("script", checkpoint=checkpoint)
    assert backend.marker is True
    assert len(backend.attempts) == 1


@pytest.mark.asyncio
async def test_plain_docker_run_has_no_attempt_id_or_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: Docker commands without a retained policy keep the legacy spec and labels.
    class PlainDocker(_RetainedDocker):
        async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
            assert run.workspace_persistence is None
            self.volume = True
            return WorkspaceLease(run.run_id, "docker", "ephemeral", "volume")

    backend = PlainDocker()
    backend.fail_command = False
    monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: backend)
    base = _engine(tmp_path)
    base.config.workflow.workspace = None
    engine = WorkflowEngine(
        base.config,
        workflow_path=base.workflow_path,
        execution_environment=base._execution_session.environment,
        run_context=RunContext(run_id="plain-docker"),
    )
    bundle = BundleRef("sha256:bundle", str(tmp_path))
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=bundle,
        ),
        patch.object(engine, "_mint_attempt", wraps=engine._mint_attempt) as mint,
    ):
        await engine.run({})
    mint.assert_not_called()
    assert backend.attempts == [None]


@pytest.mark.asyncio
async def test_template_failure_does_not_mark_docker_executed_before_first_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: render failures cannot make a retained first-use attach require a marker.
    backend = _RetainedDocker()
    backend.fail_command = False
    monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: backend)
    bundle = BundleRef("sha256:bundle", str(tmp_path))
    engine = _engine(tmp_path, persistence="on-failure")
    engine.config.agents[0].command = "{{ missing }}"
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        pytest.raises(TemplateError),
    ):
        await engine.run({})

    assert backend.attempts == []
    assert engine._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
    assert checkpoint.workspace is not None
    assert checkpoint.workspace["executed_backends"] == []
    assert backend.volume is True and backend.marker is False

    resumed = _engine(tmp_path, persistence="on-failure")
    with patch(
        "conductor.engine.workflow.prepare_run_bundle", new_callable=AsyncMock, return_value=bundle
    ):
        await resumed.resume("script", checkpoint=checkpoint)
    assert len(backend.attempts) == 1
    assert backend.marker is True


@pytest.mark.asyncio
async def test_plain_workflow_failure_checkpoint_keeps_legacy_envelope(tmp_path: Path) -> None:
    # Requirement: without a workspace block, lifecycle fields are absent from file and event.
    path = tmp_path / "legacy.yaml"
    path.write_text("workflow: legacy\n", encoding="utf-8")
    emitter = WorkflowEventEmitter()
    events: list[WorkflowEvent] = []
    emitter.subscribe(events.append)
    config = WorkflowConfig(
        workflow=WorkflowDef(name="legacy", entry_point="script"),
        agents=[ScriptStepDef(name="script", command="{{ missing }}", timeout=None)],
    )
    engine = WorkflowEngine(config, workflow_path=path, event_emitter=emitter)
    with (
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        pytest.raises(TemplateError),
    ):
        await engine.run({})

    assert engine._last_checkpoint_path is not None
    saved = json.loads(engine._last_checkpoint_path.read_text(encoding="utf-8"))
    assert set(saved) == {
        "version",
        "workflow_path",
        "workflow_hash",
        "created_at",
        "trigger",
        "failure",
        "inputs",
        "current_agent",
        "context",
        "limits",
        "copilot_session_ids",
        "copilot_session_cwds",
        "system",
        "instructions_preamble",
        "run_id",
        "event_log_path",
    }
    checkpoint_events = [event for event in events if event.type == "checkpoint_saved"]
    assert len(checkpoint_events) == 1
    assert "interrupted_step" not in checkpoint_events[0].data


@pytest.mark.parametrize(
    ("persistence", "outcome", "explicit", "retain"),
    [
        ("durable", "succeeded", False, True),
        ("durable", "failed", True, True),
        ("on-failure", "succeeded", False, False),
        ("on-failure", "failed", False, True),
        ("on-failure", "cancelled", False, True),
        ("on-failure", "failed", True, False),
    ],
)
def test_retention_disposition_table(
    tmp_path: Path,
    persistence: Literal["durable", "on-failure"],
    outcome: RunOutcome,
    explicit: bool,
    retain: bool,
) -> None:
    # Requirement: explicit failed termination removes only on-failure workspaces.
    engine = _engine(tmp_path, persistence=persistence)
    engine._explicit_terminal_failure = explicit
    assert engine._compute_workspace_retain(outcome) is retain
