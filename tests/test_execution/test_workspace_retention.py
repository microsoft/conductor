"""Cross-process retention contract against the disk-backed Docker CLI fake."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.environment import (
    DockerProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
)
from conductor.config.schema import (
    RouteDef,
    ScriptStepDef,
    SetStepDef,
    StepExecutionConfig,
    TerminateStepDef,
    WaitStepDef,
    WorkflowConfig,
    WorkflowDef,
    WorkspaceConfig,
)
from conductor.engine.checkpoint import CheckpointData, CheckpointManager
from conductor.engine.context import WorkflowContext
from conductor.engine.execution_resolution import BACKEND_FACTORIES
from conductor.engine.limits import LimitEnforcer
from conductor.engine.workflow import RunContext, WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ConfigurationError, WorkflowTerminated
from conductor.execution import (
    BundleRef,
    CommandSpec,
    ResolvedExecutionSpec,
    RunnerCapabilities,
    RunSpec,
    WorkspaceIdentity,
)
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.errors import WorkspaceAttachError


@pytest.fixture
def docker_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DockerRunnerBackend, str, Path, Path]:
    """Give two backend instances the same persistent fake daemon and CLI log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    helper = Path(__file__).with_name("docker_fake_stateful.py")
    wrapper = bin_dir / ("docker.cmd" if sys.platform == "win32" else "docker")
    if sys.platform == "win32":
        # cmd.exe must forward the complete Docker argv, not only its first argument.
        wrapper.write_text(f'@"{sys.executable}" "{helper}" %*\n', encoding="utf-8")
    else:
        wrapper.write_text(
            f"#!{sys.executable}\nexec(open({str(helper)!r}).read())\n", encoding="utf-8"
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.jsonl"
    store = tmp_path / "daemon"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_STORE", str(store))
    return DockerRunnerBackend(wrapper.name), wrapper.name, store, log


def commands(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def bundle(tmp_path: Path) -> BundleRef:
    tree = tmp_path / "bundle" / "tree" / "main"
    tree.mkdir(parents=True)
    (tree / "payload").write_text("retained", encoding="utf-8")
    return BundleRef("sha256:retained", str(tmp_path / "bundle"))


def command() -> CommandSpec:
    return CommandSpec("true", execution=ResolvedExecutionSpec(image="busybox:latest"))


@pytest.mark.asyncio
async def test_retained_generation_publishes_marker_then_attaches_read_only(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path], tmp_path: Path
) -> None:
    # Requirement: A publishes the marker after the tree; B probes outward and never copies in.
    owner, binary, store, log = docker_processes
    run = RunSpec("cross-process", bundle=bundle(tmp_path), workspace_persistence="on-failure")
    assert run.bundle is not None
    lease = await owner.prepare_run(run)
    volume = store / "volumes" / "conductor-ws-cross-process"
    labels = json.loads((volume / "labels.json").read_text(encoding="utf-8"))
    assert labels["io.conductor.retention"] == "on-failure"
    assert labels["io.conductor.incarnation"] == lease.incarnation
    assert not (volume / ".conductor-staged").exists()
    assert (await owner.run_command(command(), lease)).outcome == "completed"
    copies = [argv for argv in commands(log) if argv[0] == "cp"]
    assert len(copies) == 3  # eager volume is probed before first-use staging
    assert copies[0][1].endswith(":/workspace/.conductor-staged")
    assert copies[1][1] == "-a" and copies[1][-1].endswith(":/workspace")
    assert copies[1][-2].endswith("/.")
    assert copies[2][1] == "-a" and copies[2][-1].endswith(":/workspace/.conductor-staged")
    assert (volume / ".conductor-staged").read_text(encoding="utf-8") == run.bundle.digest
    await owner.finalize_run(lease, "failed", retain=True)

    start = len(commands(log))
    other = DockerRunnerBackend(binary)
    attached = await other.attach_run(
        run, WorkspaceIdentity("docker", lease.lease_id, lease.incarnation), expect_staged=True
    )
    assert (await other.run_command(command(), attached)).outcome == "completed"
    await other.finalize_run(attached, "succeeded", retain=True)
    later = commands(log)[start:]
    probes = [argv for argv in later if argv[0] == "cp"]
    assert len(probes) == 1
    assert probes[0][1].endswith(":/workspace/.conductor-staged")
    assert not probes[0][-1].startswith("conductor-")
    assert not any(argv[:2] in (["volume", "create"], ["volume", "rm"]) for argv in later)
    assert volume.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker", "expect_staged", "allowed"),
    [
        ("missing", False, True),
        ("missing", True, False),
        ("foreign", False, False),
        ("foreign", True, False),
    ],
)
async def test_marker_policy_across_processes(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path],
    tmp_path: Path,
    marker: str,
    expect_staged: bool,
    allowed: bool,
) -> None:
    # Requirement: only an absent marker with no previous Docker execution permits first use.
    owner, binary, store, log = docker_processes
    run = RunSpec("marker-case", bundle=bundle(tmp_path), workspace_persistence="durable")
    assert run.bundle is not None
    lease = await owner.prepare_run(run)
    marker_path = store / "volumes" / "conductor-ws-marker-case" / ".conductor-staged"
    if marker == "foreign":
        marker_path.write_text("sha256:other", encoding="utf-8")
    await owner.finalize_run(lease, "failed", retain=True)
    other = DockerRunnerBackend(binary)
    attached = await other.attach_run(
        run,
        WorkspaceIdentity("docker", lease.lease_id, lease.incarnation),
        expect_staged=expect_staged,
    )
    start = len(commands(log))
    if allowed:
        assert (await other.run_command(command(), attached)).outcome == "completed"
        assert marker_path.read_text(encoding="utf-8") == run.bundle.digest
    else:
        with pytest.raises(WorkspaceAttachError, match="missing|digest mismatch"):
            await other.run_command(command(), attached)
    later = commands(log)[start:]
    assert not any(argv[:2] == ["volume", "rm"] for argv in later)
    assert any(argv[0] == "cp" and argv[1].endswith("/.conductor-staged") for argv in later)
    assert any(argv[0] == "cp" and argv[1] == "-a" for argv in later) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["gone", "drift", "auto-create"])
async def test_attached_volume_race_fails_closed(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    # Requirement: missing or relabeled volumes cannot be replaced on attached dispatch.
    owner, binary, store, log = docker_processes
    run = RunSpec("race-case", workspace_persistence="durable")
    lease = await owner.prepare_run(run)
    other = DockerRunnerBackend(binary)
    attached = await other.attach_run(
        run, WorkspaceIdentity("docker", lease.lease_id, lease.incarnation)
    )
    volume = store / "volumes" / "conductor-ws-race-case"
    if change == "gone":
        shutil.rmtree(volume)
    elif change == "drift":
        (volume / "labels.json").write_text("{}", encoding="utf-8")
    else:
        # The fake auto-creates an unlabeled volume when -v sees one that just vanished.
        monkeypatch.setenv("FAKE_DOCKER_DROP_ON_EXEC_CREATE", "1")
        other._cli_env = dict(os.environ)
    start = len(commands(log))
    with pytest.raises(WorkspaceAttachError, match="missing|mismatch"):
        await other.run_command(command(), attached)
    later = commands(log)[start:]
    assert not any(argv[:2] in (["volume", "create"], ["volume", "rm"]) for argv in later)
    if change == "auto-create":
        assert json.loads((volume / "labels.json").read_text(encoding="utf-8")) == {}
        create = next(argv for argv in later if argv[0] == "create" and "--env-file" in argv)
        container_name = create[create.index("--name") + 1]
        assert ["rm", "-f", container_name] in later
        containers = json.loads((store / "containers.json").read_text(encoding="utf-8"))
        assert container_name not in containers
    else:
        assert not any(argv[0] == "create" for argv in later)


def lifecycle_engine(
    tmp_path: Path,
    *,
    persistence: Literal["durable", "on-failure"] | None = "on-failure",
    entry: str = "first",
    last: str = "$end",
) -> WorkflowEngine:
    path = tmp_path / "workflow.yaml"
    if not path.exists():
        path.write_text("workflow: test", encoding="utf-8")
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="retention",
            entry_point=entry,
            workspace=WorkspaceConfig(persistence=persistence) if persistence is not None else None,
        ),
        agents=[
            ScriptStepDef(
                name="first",
                command="true",
                timeout=None,
                execution=StepExecutionConfig(profile="container"),
                routes=[RouteDef(to=last)],
            ),
            SetStepDef(name="local", value="ready", routes=[RouteDef(to="second")]),
            ScriptStepDef(
                name="second",
                command="true",
                timeout=None,
                execution=StepExecutionConfig(profile="container"),
                routes=[RouteDef(to="$end")],
            ),
            TerminateStepDef(name="stop", status="failed", reason="intentional"),
            WaitStepDef(name="pause", duration="30s", routes=[RouteDef(to="$end")]),
        ],
    )
    environment = ResolvedEnvironment(
        document=EnvironmentDocument(
            default="local",
            profiles={
                "local": ProfileDefinition(backend="local"),
                "container": ProfileDefinition(
                    backend="docker", docker=DockerProfileOptions(image="busybox:latest")
                ),
            },
        ),
        name="retention-env",
        source="path",
        path=None,
        digest="sha256:env",
    )
    return WorkflowEngine(
        config,
        workflow_path=path,
        execution_environment=environment,
        run_context=RunContext(run_id="retention-engine"),
    )


def restore(engine: WorkflowEngine, checkpoint: CheckpointData) -> None:
    engine.set_context(WorkflowContext.from_dict(checkpoint.context))
    engine.set_limits(
        LimitEnforcer.from_dict(
            checkpoint.limits,
            timeout_seconds=engine.config.workflow.limits.timeout_seconds,
            budget_usd=engine.config.workflow.limits.budget_usd,
            budget_mode=engine.config.workflow.limits.budget_mode,
        )
    )


@pytest.mark.asyncio
async def test_in_flight_script_checkpoint_retains_volume_and_resume_cleans_up(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path], tmp_path: Path
) -> None:
    # Requirement: failure after dispatch but before history records the script remains in-flight.
    _owner, _binary, store, log = docker_processes
    engine = lifecycle_engine(tmp_path)
    run_bundle = bundle(tmp_path)
    original_store = engine.context.store

    def reject_script(name: str, value: object) -> None:
        if name == "first":
            raise RuntimeError("store failed after Docker execution")
        original_store(name, value)

    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=run_bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        patch.object(engine.context, "store", side_effect=reject_script),
        pytest.raises(RuntimeError, match="store failed"),
    ):
        await engine.run({})
    assert engine._last_checkpoint_path is not None
    cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
    assert cp.interrupted_step is not None
    assert cp.interrupted_step["name"] == "first"
    assert cp.interrupted_step["attempt_id"]
    assert cp.workspace is not None and cp.workspace["executed_backends"] == ["docker"]
    volume = store / "volumes" / "conductor-ws-retention-engine"
    assert volume.is_dir()
    start = len(commands(log))
    resumed = lifecycle_engine(tmp_path)
    restore(resumed, cp)
    with patch(
        "conductor.engine.workflow.prepare_run_bundle",
        new_callable=AsyncMock,
        return_value=run_bundle,
    ):
        await resumed.resume(cp.current_agent, checkpoint=cp)
    assert not volume.exists()
    later = commands(log)[start:]
    assert any(argv[0] == "cp" and argv[1].endswith("/.conductor-staged") for argv in later)
    assert not any(argv[0] == "cp" and argv[1] == "-a" for argv in later)
    assert sum(argv[:2] == ["volume", "rm"] for argv in later) == 1


@pytest.mark.asyncio
async def test_two_resume_generations_keep_executed_backend_history(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path], tmp_path: Path
) -> None:
    # Requirement: a second checkpoint after a local failure cannot permit staging a lost marker.
    _owner, _binary, store, log = docker_processes
    run_bundle = bundle(tmp_path)
    first = lifecycle_engine(tmp_path, last="local")
    original_store = first.context.store

    def fail_local(name: str, value: object) -> None:
        if name == "local":
            raise RuntimeError("local step failed")
        original_store(name, value)

    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=run_bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        patch.object(first.context, "store", side_effect=fail_local),
        pytest.raises(RuntimeError, match="local step"),
    ):
        await first.run({})
    assert first._last_checkpoint_path is not None
    cp1 = CheckpointManager.load_checkpoint(first._last_checkpoint_path)
    assert cp1.workspace is not None and cp1.workspace["executed_backends"] == ["docker"]
    second = lifecycle_engine(tmp_path, last="local")
    restore(second, cp1)
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=run_bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        patch.object(second.context, "store", side_effect=RuntimeError("local step failed again")),
        pytest.raises(RuntimeError, match="local step failed again"),
    ):
        await second.resume("local", checkpoint=cp1)
    assert second._last_checkpoint_path is not None
    cp2 = CheckpointManager.load_checkpoint(second._last_checkpoint_path)
    assert cp2.workspace is not None and cp2.workspace["executed_backends"] == ["docker"]
    marker = store / "volumes" / "conductor-ws-retention-engine" / ".conductor-staged"
    marker.unlink()
    third = lifecycle_engine(tmp_path, last="local")
    restore(third, cp2)
    start = len(commands(log))
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=run_bundle,
        ),
        pytest.raises(ConfigurationError, match="marker is missing") as rejected,
    ):
        await third.resume("local", checkpoint=cp2)
    assert isinstance(rejected.value.__cause__, WorkspaceAttachError)
    later = commands(log)[start:]
    assert any(argv[0] == "cp" and argv[1].endswith("/.conductor-staged") for argv in later)
    assert not any(argv[0] == "cp" and argv[1] == "-a" for argv in later)
    assert not any(argv[0] == "start" for argv in later)


@pytest.mark.asyncio
async def test_first_docker_use_after_local_checkpoint_stages_missing_marker(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path], tmp_path: Path
) -> None:
    # Requirement: a set-step checkpoint has no executed Docker backend despite existing history.
    _owner, _binary, store, log = docker_processes
    run_bundle = bundle(tmp_path)
    engine = lifecycle_engine(tmp_path, entry="local")
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=run_bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        patch.object(engine, "_execute_script", side_effect=RuntimeError("pre-dispatch failure")),
        pytest.raises(RuntimeError, match="pre-dispatch failure"),
    ):
        await engine.run({})
    assert engine._last_checkpoint_path is not None
    cp = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
    assert cp.workspace is not None and cp.workspace["executed_backends"] == []
    assert cp.context["execution_history"] == ["local"]
    marker = store / "volumes" / "conductor-ws-retention-engine" / ".conductor-staged"
    assert not marker.exists()
    resumed = lifecycle_engine(tmp_path, entry="local")
    restore(resumed, cp)
    start = len(commands(log))
    with patch(
        "conductor.engine.workflow.prepare_run_bundle",
        new_callable=AsyncMock,
        return_value=run_bundle,
    ):
        await resumed.resume("second", checkpoint=cp)
    assert any(argv[0] == "cp" and argv[1] == "-a" for argv in commands(log)[start:])


@pytest.mark.asyncio
async def test_partial_prepare_preserves_previously_attached_volume(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path], tmp_path: Path
) -> None:
    # Requirement: failed attachment of backend two never removes backend one's retained volume.
    _owner, _binary, store, log = docker_processes
    run_bundle = bundle(tmp_path)
    first = lifecycle_engine(tmp_path)
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=run_bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        patch.object(first.context, "store", side_effect=RuntimeError("after dispatch")),
        pytest.raises(RuntimeError, match="after dispatch"),
    ):
        await first.run({})
    assert first._last_checkpoint_path is not None
    cp = CheckpointManager.load_checkpoint(first._last_checkpoint_path)
    assert cp.workspace is not None
    cp.workspace["identities"]["zz"] = {
        "backend": "zz",
        "lease_id": "retention-engine",
        "incarnation": "other",
        "location": None,
    }
    rejecting = MagicMock()
    rejecting.capabilities.return_value = RunnerCapabilities(
        batch=True,
        sessions=False,
        shared_workspace=True,
        snapshots=False,
        retained_workspace=True,
    )
    rejecting.attach_run = AsyncMock(side_effect=WorkspaceAttachError("second backend rejected"))
    second = lifecycle_engine(tmp_path)
    restore(second, cp)
    second._execution_session.backends["zz"] = rejecting
    start = len(commands(log))
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=run_bundle,
        ),
        pytest.raises(WorkspaceAttachError, match="second backend rejected"),
    ):
        await second.resume("first", checkpoint=cp)
    rejecting.attach_run.assert_awaited_once()
    assert (store / "volumes" / "conductor-ws-retention-engine").exists()
    assert not any(argv[:2] == ["volume", "rm"] for argv in commands(log)[start:])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "persistence", "kept"),
    [
        ("first", "durable", True),
        ("stop", "on-failure", False),
    ],
)
async def test_engine_disposition_on_success_or_explicit_termination(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path],
    tmp_path: Path,
    entry: str,
    persistence: Literal["durable", "on-failure"],
    kept: bool,
) -> None:
    # Requirement: durable success keeps its volume; explicit failure removes on-failure volume.
    _owner, _binary, store, _log = docker_processes
    run_bundle = bundle(tmp_path)
    engine = lifecycle_engine(tmp_path, persistence=persistence, entry=entry)
    with patch(
        "conductor.engine.workflow.prepare_run_bundle",
        new_callable=AsyncMock,
        return_value=run_bundle,
    ):
        if entry == "stop":
            with pytest.raises(WorkflowTerminated):
                await engine.run({})
        else:
            await engine.run({})
    assert (store / "volumes" / "conductor-ws-retention-engine").exists() is kept


@pytest.mark.asyncio
async def test_resumed_failure_keeps_attached_volume(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path], tmp_path: Path
) -> None:
    # Requirement: failure in a resumed Docker step retains the same workspace again.
    _owner, _binary, store, log = docker_processes
    run_bundle = bundle(tmp_path)
    original = lifecycle_engine(tmp_path)
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=run_bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        patch.object(original.context, "store", side_effect=RuntimeError("original failed")),
        pytest.raises(RuntimeError, match="original failed"),
    ):
        await original.run({})
    assert original._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(original._last_checkpoint_path)
    resumed = lifecycle_engine(tmp_path)
    restore(resumed, checkpoint)
    start = len(commands(log))
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=run_bundle,
        ),
        patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
        patch.object(resumed.context, "store", side_effect=RuntimeError("resume failed")),
        pytest.raises(RuntimeError, match="resume failed"),
    ):
        await resumed.resume(checkpoint.current_agent, checkpoint=checkpoint)
    assert (store / "volumes" / "conductor-ws-retention-engine").exists()
    assert not any(argv[:2] == ["volume", "rm"] for argv in commands(log)[start:])
    assert resumed._last_checkpoint_path is not None
    after = CheckpointManager.load_checkpoint(resumed._last_checkpoint_path)
    assert after.workspace is not None and after.workspace["executed_backends"] == ["docker"]


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence", [None, "durable"])
async def test_docker_argv_attempt_id_requires_retained_policy(
    docker_processes: tuple[DockerRunnerBackend, str, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistence: Literal["durable"] | None,
) -> None:
    # Requirement: ordinary Docker runs have no lifecycle claim, retention label, or attempt id.
    backend, _binary, _store, log = docker_processes
    monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: backend)
    emitter = WorkflowEventEmitter()
    events: list[WorkflowEvent] = []
    emitter.subscribe(events.append)
    engine = lifecycle_engine(tmp_path, persistence=persistence)
    engine._event_emitter = emitter
    with (
        patch(
            "conductor.engine.workflow.prepare_run_bundle",
            new_callable=AsyncMock,
            return_value=bundle(tmp_path),
        ),
        patch("conductor.engine.workflow.acquire_workspace_claim") as claim,
        patch.object(backend, "run_command", wraps=backend.run_command) as dispatched,
    ):
        await engine.run({})
    dispatched.assert_awaited_once()
    dispatch_call = dispatched.await_args
    assert dispatch_call is not None
    spec = dispatch_call.args[0]
    assert isinstance(spec, CommandSpec)
    retained = persistence is not None
    assert (spec.attempt_id is not None) is retained
    argv = commands(log)
    create = next(args for args in argv if args[0] == "create" and "--env-file" in args)
    assert any(arg.startswith("io.conductor.attempt_id=") for arg in create) is retained
    volume_create = next(args for args in argv if args[:2] == ["volume", "create"])
    assert any(arg.startswith("io.conductor.retention=") for arg in volume_create) is retained
    if retained:
        claim.assert_called_once()
        assert f"io.conductor.attempt_id={spec.attempt_id}" in create
    else:
        claim.assert_not_called()
        assert not (tmp_path / "conductor-home" / "workspaces" / "claims").exists()
        assert all("attempt_id" not in event.data for event in events)
