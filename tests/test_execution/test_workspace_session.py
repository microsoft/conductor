"""Session wiring requirements for retained execution workspaces."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from conductor.config.environment import builtin_local_environment
from conductor.config.loader import load_config
from conductor.engine.execution_resolution import BACKEND_FACTORIES, ExecutionResolverSession
from conductor.engine.run_manifest import compile_run_manifest
from conductor.engine.workspace_claim import acquire_workspace_claim
from conductor.exceptions import ConfigurationError, ExecutionError
from conductor.execution import (
    LocalRunnerBackend,
    RunnerCapabilities,
    RunSpec,
    WorkspaceIdentity,
    WorkspaceLease,
)
from conductor.execution.types import BundleRef, CommandSpec


@pytest.mark.parametrize("pending", ["staging", "sdk"])
async def test_fence_refuses_new_session_after_expired_owner_with_empty_inventory(
    tmp_path, monkeypatch, pending
):
    # Requirement: a new controller home cannot bypass a retained guard using empty MCP inventory.
    import asyncio
    import json
    from contextlib import asynccontextmanager

    import conductor.mcp.manager as manager_module
    from conductor.config.environment import EnvironmentDocument, ResolvedEnvironment
    from conductor.config.schema import WorkflowConfig
    from conductor.engine.checkpoint import CheckpointManager
    from conductor.engine.workflow import RunContext, WorkflowEngine
    from conductor.execution.docker import DockerRunnerBackend
    from conductor.execution.errors import WorkspaceAttachError
    from tests.test_engine.mcp_placement import owned_opening, preserve_primary
    from tests.test_execution.test_workspace_retention import restore
    from tests.test_mcp.fixtures.docker_realm import create_realm

    realm = create_realm(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original_stage = realm.backend._stage_workspace
    original_stdio = manager_module.stdio_client

    async def held_stage(*args):
        entered.set()
        await release.wait()
        await original_stage(*args)

    @asynccontextmanager
    async def held_stdio(params, *, errlog):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        async with original_stdio(params, errlog=errlog) as transport:
            yield transport

    source = tmp_path / "source"
    source.mkdir()
    config = WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "fence-root",
                "entry_point": "call",
                "workspace": {"persistence": "on-failure"},
                "runtime": {"mcp_servers": {"srv": {"command": "peer"}}},
            },
            "agents": [
                {
                    "name": "call",
                    "type": "mcp",
                    "server": "srv",
                    "tool": "receive",
                    "routes": [{"to": "$end"}],
                }
            ],
        }
    )
    path = source / "workflow.yaml"
    path.write_text(config.model_dump_json(by_alias=True))
    environment = ResolvedEnvironment(
        document=EnvironmentDocument.model_validate(
            {
                "default": "docker",
                "profiles": {"docker": {"backend": "docker", "docker": {"image": "peer:image"}}},
            }
        ),
        name="fence",
        source="path",
        path=None,
        digest="sha256:fence",
    )
    engine = WorkflowEngine(
        config,
        workflow_path=path,
        execution_environment=environment,
        run_context=RunContext(run_id="fence-root"),
    )
    session = engine._execution_session
    session.backends["docker"] = realm.backend
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()
    monkeypatch.setattr(CheckpointManager, "get_checkpoints_dir", lambda: checkpoints)
    staged: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    if pending == "staging":
        monkeypatch.setattr(realm.backend, "_stage_workspace", held_stage)
    else:
        monkeypatch.setattr(manager_module, "stdio_client", held_stdio)
        execute_loop = engine._execute_loop

        async def staged_loop(current_agent_name):
            # run() resets lifecycle views before preparation: stage its actual root lease here.
            execution = engine._execution_resolver.manifest.profiles["call"].execution
            assert execution is not None
            await realm.backend._ensure_staged(session.leases["docker"], execution, None)
            staged.set_result(None)
            return await execute_loop(current_agent_name)

        monkeypatch.setattr(engine, "_execute_loop", staged_loop)
    try:
        async with owned_opening(engine.run({}), release) as opening:
            if pending == "sdk":
                # Ordinary staging has its own backend bounds, not the SDK marker budget.
                done, _ = await asyncio.wait({opening, staged}, return_when=asyncio.FIRST_COMPLETED)
                if opening in done:
                    await opening
            await asyncio.wait_for(entered.wait(), 5)
            session = engine._execution_session
            lease = session.leases["docker"]
            session.begin_teardown("cancelled", retain=True)
            deadline = session.cleanup_deadline
            loop = asyncio.get_running_loop()
            clock = loop.time
            monkeypatch.setattr(loop, "time", lambda: clock() + 31)
            await session.finalize_leases("cancelled", retain=True)
            monkeypatch.setattr(loop, "time", clock)
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(opening, 5)
            checkpoint_path = engine.handle_dashboard_stop("cancelled for fence regression")
            assert checkpoint_path is not None
            checkpoint = CheckpointManager.load_checkpoint(checkpoint_path)
            assert checkpoint.workspace is not None
            assert checkpoint.workspace["identities"]["docker"]["incarnation"] == lease.incarnation
            assert session.cleanup_uncertain and session.cleanup_deadline == deadline
            state = json.loads((tmp_path / "state.json").read_text())
            assert {
                item["Config"]["Labels"]["io.conductor.resource"]
                for item in state["containers"].values()
            } == {"mcp-fence"}
            before = len((tmp_path / "docker.jsonl").read_text().splitlines())
            home = tmp_path / "new-controller"
            home.mkdir()
            monkeypatch.setenv("HOME", str(home))
            monkeypatch.setenv("CONDUCTOR_HOME", str(home / ".conductor"))
            competitor = WorkflowEngine(
                config,
                workflow_path=path,
                execution_environment=environment,
                run_context=RunContext(run_id="fence-root"),
            )
            competitor._execution_session.backends["docker"] = DockerRunnerBackend("docker")
            restore(competitor, checkpoint)
            with pytest.raises(WorkspaceAttachError, match="fence"):
                await competitor.resume(checkpoint.current_agent, checkpoint=checkpoint)
            later = [
                json.loads(line)["argv"]
                for line in (tmp_path / "docker.jsonl").read_text().splitlines()[before:]
            ]
            assert not any(row[0] in {"cp", "run", "start", "rm", "volume"} for row in later)
            assert json.loads((tmp_path / "state.json").read_text()) == state
            print(
                {
                    "case": "cross-home-fence",
                    "pending": pending,
                    "deadline": deadline,
                    "checkpoint": checkpoint.workspace,
                    "new_controller_commands": later,
                    "guard_preserved": state["containers"],
                }
            )
    finally:
        with preserve_primary():
            for attempt in session.mcp_lifecycle.attempts:
                lease = attempt.lease
                if lease is None:
                    continue
                for entry in attempt.entries:
                    if entry.allocation is not None:
                        done, unfinished = await asyncio.wait({entry.allocation}, timeout=10)
                        assert done and not unfinished
                    for owner in tuple(entry.manager._owners):
                        done, unfinished = await asyncio.wait({owner}, timeout=10)
                        assert done and not unfinished
                    assert entry._cleanup is not None
                    await asyncio.wait_for(asyncio.shield(entry._cleanup), 10)
                mcp = realm.backend._lease_states[realm.backend._lease_key(lease)].mcp
                assert mcp is not None
                for operation in (*mcp.operations, *mcp.probes):
                    done, unfinished = await asyncio.wait({operation}, timeout=10)
                    assert done and not unfinished
                assert engine._mcp_step_managers == {}
                # QA disposal follows independent terminal proof; it is not product recovery.
                state = json.loads((tmp_path / "state.json").read_text())
                for metadata in state["containers"].values():
                    labels = metadata["Config"]["Labels"]
                    assert labels["io.conductor.run_id"] == lease.lease_id
                    assert labels["io.conductor.incarnation"] == lease.incarnation
                    assert (await realm.backend._run_docker(("rm", "-f", metadata["Id"])))[0] == 0
                for volume, labels in state["volumes"].items():
                    assert volume == realm.backend._volume_name(lease)
                    assert labels["io.conductor.incarnation"] == lease.incarnation
                    assert (await realm.backend._run_docker(("volume", "rm", "-f", volume)))[0] == 0


@pytest.mark.parametrize("winner", ["first", "new"])
async def test_fence_atomic_acquire_has_only_one_admitted_controller(tmp_path, monkeypatch, winner):
    # Requirement: either daemon create winner excludes the other before any workspace consumer.
    import asyncio
    import json

    from conductor.execution.docker import DockerRunnerBackend
    from conductor.execution.errors import ExecutionSpecError, WorkspaceAttachError
    from tests.test_mcp.fixtures.docker_realm import create_realm

    realm = create_realm(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = realm.backend._run_docker

    async def held_create(argv, **kwargs):
        if argv[0] == "create":
            entered.set()
            await release.wait()
        return await original(argv, **kwargs)

    monkeypatch.setattr(realm.backend, "_run_docker", held_create)
    first = ExecutionResolverSession(builtin_local_environment())
    first.backends["docker"] = realm.backend
    preparing = asyncio.create_task(first.prepare_leases(realm.run))
    await asyncio.wait_for(entered.wait(), 5)
    second = ExecutionResolverSession(builtin_local_environment())
    second_backend = DockerRunnerBackend("docker")
    second.backends["docker"] = second_backend
    if winner == "first":
        release.set()
        await preparing
        lease = first.leases["docker"]
        with pytest.raises(WorkspaceAttachError, match="fence"):
            await second.prepare_leases(
                realm.run,
                resume_identities={
                    "docker": WorkspaceIdentity("docker", lease.lease_id, lease.incarnation)
                },
            )
        await second.finalize_leases("failed", retain=True)
        await first.finalize_leases("succeeded")
    else:
        await second.prepare_leases(realm.run)
        first.begin_teardown("cancelled", retain=True)
        release.set()
        with pytest.raises(ExecutionSpecError, match="sealed"):
            await preparing
        await first.finalize_leases("cancelled", retain=True)
        live = json.loads((tmp_path / "state.json").read_text())
        assert len(live["containers"]) == 1
        await second.finalize_leases("succeeded")
    final = json.loads((tmp_path / "state.json").read_text())
    assert final["containers"] == {} and final["volumes"] == {}
    rows = [
        json.loads(line)["argv"] for line in (tmp_path / "docker.jsonl").read_text().splitlines()
    ]
    assert not any(row[0] in {"cp", "run", "start"} for row in rows)
    print({"case": "atomic-fence", "winner": winner, "commands": rows, "final": final})


@dataclass
class RetainedBackend(LocalRunnerBackend):
    prepared: list[RunSpec] = field(default_factory=list)
    attached: list[tuple[RunSpec, WorkspaceIdentity, bool]] = field(default_factory=list)
    finalized: list[tuple[WorkspaceLease, str, bool]] = field(default_factory=list)

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(True, False, True, False, retained_workspace=True)

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        self.prepared.append(run)
        return WorkspaceLease(run.run_id, "docker", "new")

    async def attach_run(
        self, run: RunSpec, identity: WorkspaceIdentity, *, expect_staged: bool = False
    ) -> WorkspaceLease:
        self.attached.append((run, identity, expect_staged))
        return WorkspaceLease(identity.lease_id, "docker", identity.incarnation)

    async def finalize_run(self, lease, outcome, *, retain: bool = False) -> None:
        self.finalized.append((lease, outcome, retain))


def _session(docker: RetainedBackend) -> ExecutionResolverSession:
    session = ExecutionResolverSession(builtin_local_environment())
    session.backends["docker"] = docker
    return session


@pytest.mark.asyncio
async def test_local_lease_gets_no_retention_while_docker_gets_policy() -> None:
    # Requirement: local prepare succeeds even for a Docker-retained run.
    docker = RetainedBackend()
    session = _session(docker)
    await session.prepare_leases(RunSpec("run", workspace_persistence="durable"))
    assert session.leases["local"].backend == "local"
    assert docker.prepared[0].workspace_persistence == "durable"
    assert set(session.workspace_identities()) == {"docker"}
    await session.finalize_leases("succeeded")


@pytest.mark.asyncio
async def test_resume_attaches_only_named_backend_with_staging_flag() -> None:
    # Requirement: identity routes to attach while the local backend prepares normally.
    docker = RetainedBackend()
    session = _session(docker)
    identity = WorkspaceIdentity("docker", "run", "saved")
    await session.prepare_leases(
        RunSpec("run", workspace_persistence="on-failure"),
        resume_identities={"docker": identity},
        expect_staged={"docker": True, "local": False},
    )
    assert docker.attached == [(RunSpec("run", workspace_persistence="on-failure"), identity, True)]
    assert docker.prepared == []
    assert session.leases["local"].backend == "local"
    assert session.workspace_identities()["docker"].incarnation == "saved"
    await session.finalize_leases("succeeded", retain=True)
    assert docker.finalized[0][1:] == ("succeeded", True)


@pytest.mark.asyncio
async def test_unknown_backend_identity_is_configuration_error() -> None:
    # Requirement: checkpoint identities never disappear silently.
    session = ExecutionResolverSession(builtin_local_environment())
    with pytest.raises(ConfigurationError, match="missing"):
        await session.prepare_leases(
            RunSpec("run"), resume_identities={"missing": WorkspaceIdentity("missing", "run", "x")}
        )
    assert session.leases == {}


@pytest.mark.asyncio
async def test_late_backend_attaches_using_saved_identity(monkeypatch) -> None:
    # Requirement: a late-discovered backend uses the stored per-backend resume map.
    docker = RetainedBackend()
    monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: docker)
    session = ExecutionResolverSession(builtin_local_environment())
    identity = WorkspaceIdentity("docker", "run", "saved")
    await session.prepare_leases(
        RunSpec("run", bundle=BundleRef("sha256:x", "/tmp/store")),
        resume_identities={"docker": identity},
    )
    await session.ensure_backends(["docker"])
    assert docker.attached[0][1:] == (identity, False)


def test_executed_backend_history_is_union() -> None:
    # Requirement: a fresh process cannot erase history restored from checkpoint.
    session = ExecutionResolverSession(builtin_local_environment())
    session.restore_executed_backends(["docker"])
    session.record_backend_execution("local")
    session.restore_executed_backends([])
    assert session.executed_backends() == frozenset({"docker", "local"})


async def test_technical_local_is_not_retained_even_when_capable(monkeypatch) -> None:
    # Requirement: capability alone never creates or exports a technical local filesystem identity.
    original = LocalRunnerBackend.capabilities
    monkeypatch.setattr(
        LocalRunnerBackend,
        "capabilities",
        lambda self: replace(original(self), retained_workspace=True),
    )
    docker = RetainedBackend()
    session = _session(docker)
    await session.prepare_leases(RunSpec("technical", workspace_persistence="durable"))
    assert session.leases["local"].location is None
    assert set(session.workspace_identities()) == {"docker"}
    await session.finalize_leases("succeeded")


@pytest.mark.parametrize("claim_active", [False, True])
async def test_late_local_consumer_uses_original_bundle_under_claim(
    tmp_path: Path, monkeypatch, claim_active: bool
) -> None:
    # Requirement: late preparation is claim-fenced and binds its original published topology.
    original = LocalRunnerBackend.capabilities
    monkeypatch.setattr(
        LocalRunnerBackend,
        "capabilities",
        lambda self: replace(original(self), retained_workspace=True),
    )
    path = tmp_path / "root.yaml"
    path.write_text(
        "workflow:\n  name: late\n  entry_point: mark\n"
        "  bundle: {assets: [late.py]}\n"
        "agents:\n  - {name: mark, type: set, value: ok, routes: [{to: $end}]}\n"
    )
    (tmp_path / "late.py").write_text(
        "from pathlib import Path\nPath('late-result').write_text('late bytes')\n"
    )
    child_path = tmp_path / "child.yaml"
    child_path.write_text(
        "workflow: {name: child, entry_point: script}\n"
        "agents:\n  - name: script\n    type: script\n    command: python\n"
        "    routes: [{to: $end}]\n"
    )
    environment = builtin_local_environment()
    session = ExecutionResolverSession(environment)
    claim = acquire_workspace_claim("late-local") if claim_active else None
    try:
        await session.prepare_leases(
            RunSpec("late-local", workspace_persistence="durable"),
            workflow_path=path,
            claim_active=claim is not None,
        )
        technical = session.leases["local"]
        assert technical.location is None and not session.workspace_identities()
        child = load_config(child_path)
        session.note_participants(
            child, compile_run_manifest(child, workflow_path=child_path, environment=environment)
        )
        if not claim_active:
            with pytest.raises(ConfigurationError, match="claim"):
                await session.ensure_backends(["local"])
            assert session.leases["local"] is technical
            assert session.workspace_identities() == {}
        else:
            await session.ensure_backends(["local"])
            backend = session.backends["local"]
            assert isinstance(backend, LocalRunnerBackend)
            lease = session.leases["local"]
            binding = backend.local_workspace_binding(lease)
            assert binding is not None and session.run_bundle is binding.bundle
            assert session.agent_dependencies == ()
            result = await backend.run_command(
                CommandSpec(
                    str(Path(sys.executable)),
                    ("./late.py",),
                    working_dir=str(Path(binding.work_root) / "main"),
                ),
                lease,
                on_dispatch=lambda: session.record_backend_execution("local"),
            )
            assert result.exit_code == 0
            assert (Path(binding.work_root) / "main/late-result").read_text() == "late bytes"
            assert session.executed_backends() == {"local"}
    finally:
        await session.finalize_leases("failed", retain=True)
        if claim is not None:
            claim.release()


async def test_preparation_deadline_late_acquire_after_new_root_release(tmp_path, monkeypatch):
    # Requirement: an expired root retains late acquisition and cannot touch a reused workspace.
    import asyncio
    import json

    from conductor.execution.docker import DockerRunnerBackend
    from conductor.execution.errors import ExecutionSpecError
    from tests.test_mcp.fixtures.docker_realm import create_realm

    realm = create_realm(tmp_path, monkeypatch)
    session = ExecutionResolverSession(builtin_local_environment())
    session.backends["docker"] = realm.backend
    entered, release, sealed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = realm.backend._run_docker
    seal = realm.backend.seal_mcp_cleanup

    async def held_create(argv, **kwargs):
        if argv[0] == "create":
            entered.set()
            await release.wait()
        return await original(argv, **kwargs)

    def record_seal(lease, deadline, *, uncertain=False):
        seal(lease, deadline, uncertain=uncertain)
        sealed.set()

    monkeypatch.setattr(realm.backend, "_run_docker", held_create)
    monkeypatch.setattr(realm.backend, "seal_mcp_cleanup", record_seal)
    preparing = asyncio.create_task(session.prepare_leases(realm.run))
    await asyncio.wait_for(entered.wait(), 5)
    preparing.cancel()
    await asyncio.wait_for(sealed.wait(), 5)
    deadline = session.cleanup_deadline
    assert session.sealed and deadline is not None
    attempt = session.mcp_lifecycle.attempts[0]
    assert attempt.backend is realm.backend and attempt.abort is not None
    assert "docker" not in session.leases
    loop = asyncio.get_running_loop()
    clock = loop.time
    monkeypatch.setattr(loop, "time", lambda: clock() + 31)
    with pytest.raises(asyncio.CancelledError):
        await preparing
    await session.finalize_leases("cancelled")
    assert session.cleanup_deadline == deadline and session.cleanup_uncertain
    second = DockerRunnerBackend(realm.backend._docker_binary)
    new_lease = await second.prepare_run(realm.run)
    await second.finalize_run(new_lease, "succeeded")
    rows_before = len((realm.root / "docker.jsonl").read_text().splitlines())
    release.set()
    assert attempt.task is not None
    with pytest.raises(ExecutionSpecError, match="sealed"):
        await asyncio.wait_for(asyncio.shield(attempt.task), 5)
    rows = [
        json.loads(line)["argv"]
        for line in (realm.root / "docker.jsonl").read_text().splitlines()[rows_before:]
    ]
    assert [row[0] for row in rows] == ["create"]
    state = json.loads((realm.root / "state.json").read_text())
    assert state["volumes"] == {} and len(state["containers"]) == 1
    with pytest.raises(ExecutionError, match="unconfirmed"):
        session.begin_generation()
    # Independent QA disposal: the late controller is now terminal and the
    # exact immutable guard ID belongs to this provisional incarnation.
    assert attempt.lease is not None
    metadata = next(iter(state["containers"].values()))
    assert metadata["Config"]["Labels"]["io.conductor.incarnation"] == attempt.lease.incarnation
    rc, _out, _err = await original(("rm", "-f", metadata["Id"]))
    assert rc == 0


@pytest.mark.parametrize("attach", [False, True])
@pytest.mark.parametrize("late", [False, True])
async def test_preparation_failure_releases_root_owned_guard(
    tmp_path, monkeypatch, attach, late
) -> None:
    # Requirement: a failed preparation releases its verified guard without a returned lease.
    import asyncio
    import json

    from conductor.execution.errors import ExecutionSpecError
    from tests.test_mcp.fixtures.docker_realm import create_realm

    realm = create_realm(tmp_path, monkeypatch)
    session = ExecutionResolverSession(builtin_local_environment())
    if not late:
        session.backends["docker"] = realm.backend
    monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: realm.backend)
    attempts = []
    original = realm.backend._prepare_fenced

    async def observe(lease, state):
        registered = state.run.on_prepare_attempt

        def capture(provisional, abort):
            attempts.append((provisional, abort))
            if registered is not None:
                registered(provisional, abort)

        state.run = replace(state.run, on_prepare_attempt=capture)
        await original(lease, state)

    async def refuse_volume(lease, state):
        raise ExecutionSpecError("controlled volume refusal")

    monkeypatch.setattr(realm.backend, "_prepare_fenced", observe)
    monkeypatch.setattr(realm.backend, "_ensure_volume", refuse_volume)
    identities = (
        {"docker": WorkspaceIdentity("docker", realm.run.run_id, "retained")} if attach else None
    )
    try:
        if late:
            await session.prepare_leases(realm.run, resume_identities=identities)
        with pytest.raises(ExecutionSpecError, match="controlled volume refusal"):
            if late:
                await session.ensure_backends(["docker"])
            else:
                await session.prepare_leases(realm.run, resume_identities=identities)
        assert session.sealed
        assert session.cleanup_deadline is not None
        state = json.loads((realm.root / "state.json").read_text())
        assert state["containers"] == {}
        assert "docker" not in session.leases
        if late:
            assert "docker" not in session.backends
    finally:
        for _lease, abort in attempts:
            await abort(asyncio.get_running_loop().time() + 30)
        await session.finalize_leases("failed", retain=True)


async def test_docker_only_run_resume_use_actual_root_cleanup(tmp_path, monkeypatch):
    # Requirement: both real engine entry points close SDK and target before Docker finalization.
    import json

    from conductor.config.environment import EnvironmentDocument, ResolvedEnvironment
    from conductor.config.schema import WorkflowConfig
    from conductor.engine.workflow import WorkflowEngine
    from conductor.events import WorkflowEventEmitter
    from conductor.mcp.manager import MCPManager
    from tests.test_mcp.fixtures.docker_realm import create_realm

    realm = create_realm(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    source = tmp_path / "source"
    source.mkdir()
    config = WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "root-lifecycle",
                "entry_point": "call",
                "runtime": {"mcp_servers": {"srv": {"command": "peer"}}},
            },
            "agents": [
                {
                    "name": "call",
                    "type": "mcp",
                    "server": "srv",
                    "tool": "receive",
                    "routes": [{"to": "$end"}],
                }
            ],
        }
    )
    workflow = source / "workflow.yaml"
    workflow.write_text(config.model_dump_json())
    environment = ResolvedEnvironment(
        document=EnvironmentDocument.model_validate(
            {
                "default": "docker",
                "profiles": {"docker": {"backend": "docker", "docker": {"image": "peer:image"}}},
            }
        ),
        name="root",
        source="path",
        path=None,
        digest="sha256:root",
    )
    engine = WorkflowEngine(
        config,
        workflow_path=workflow,
        execution_environment=environment,
        event_emitter=WorkflowEventEmitter(),
    )
    session = engine._execution_session
    session.backends["docker"] = realm.backend
    original_close = MCPManager.close
    original_open = realm.backend.open_mcp_stdio
    original_finalize = realm.backend.finalize_run
    order, deadlines = [], []

    async def close_manager(manager, *, strict=False):
        await original_close(manager, strict=strict)
        order.append("manager")

    async def open_target(spec, lease):
        target = await original_open(spec, lease)

        async def close_target():
            assert order[-1] == "manager"
            order.append("target")
            await target.aclose()

        return replace(target, aclose=close_target)

    async def finalize(lease, outcome, *, retain=False):
        assert order[-1] == "target"
        order.append("finalize")
        await original_finalize(lease, outcome, retain=retain)

    monkeypatch.setattr(MCPManager, "close", close_manager)
    monkeypatch.setattr(realm.backend, "open_mcp_stdio", open_target)
    monkeypatch.setattr(realm.backend, "finalize_run", finalize)
    await engine.run({})
    deadlines.append(session.cleanup_deadline)
    await engine.resume("call")
    deadlines.append(session.cleanup_deadline)
    assert order == ["manager", "target", "finalize"] * 2
    assert all(deadline is not None for deadline in deadlines)
    assert deadlines[1] > deadlines[0]
    assert engine._mcp_step_managers == {} and not session.quarantined
    state = json.loads((realm.root / "state.json").read_text())
    assert state["containers"] == {} and state["volumes"] == {}
    print({"scenario": "actual-run-resume", "order": order, "deadlines": deadlines})


async def test_preparation_cancel_after_acquire_before_return(tmp_path, monkeypatch):
    # Requirement: cancellation before commit releases only the verified guard of its attempt.
    import asyncio
    import json

    from conductor.execution.errors import ExecutionSpecError
    from tests.test_mcp.fixtures.docker_realm import create_realm

    realm = create_realm(tmp_path, monkeypatch)
    session = ExecutionResolverSession(builtin_local_environment())
    session.backends["docker"] = realm.backend
    entered, release, sealed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_volume = realm.backend._ensure_volume
    original_seal = realm.backend.seal_mcp_cleanup

    async def hold_volume(lease, state):
        result = await original_volume(lease, state)
        entered.set()
        await release.wait()
        return result

    def seal(lease, deadline, *, uncertain=False):
        original_seal(lease, deadline, uncertain=uncertain)
        sealed.set()

    monkeypatch.setattr(realm.backend, "_ensure_volume", hold_volume)
    monkeypatch.setattr(realm.backend, "seal_mcp_cleanup", seal)
    preparing = asyncio.create_task(session.prepare_leases(realm.run))
    await asyncio.wait_for(entered.wait(), 5)
    preparing.cancel()
    await asyncio.wait_for(sealed.wait(), 5)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await preparing
    attempt = session.mcp_lifecycle.attempts[0]
    assert not attempt.committed and attempt.task is not None
    with pytest.raises(ExecutionSpecError, match="sealed"):
        attempt.task.result()
    state = json.loads((realm.root / "state.json").read_text())
    assert state["containers"] == {} and len(state["volumes"]) == 1
    assert not session.quarantined
    assert attempt.lease is not None
    volume = realm.backend._volume_name(attempt.lease)
    assert state["volumes"][volume]["io.conductor.incarnation"] == attempt.lease.incarnation
    rc, _out, _err = await realm.backend._run_docker(("volume", "rm", "-f", volume))
    assert rc == 0


async def test_late_acquire_root_seal_stops_shielded_staging_before_copy(tmp_path, monkeypatch):
    # Requirement: the retained root admission callback fences shielded staging after an await.
    import asyncio
    import json

    from conductor.execution.errors import ExecutionSpecError
    from tests.test_engine.mcp_placement import placement_config, placement_engine
    from tests.test_mcp.fixtures.docker_realm import create_realm

    realm = create_realm(tmp_path, monkeypatch)
    async with placement_engine(placement_config(), realm) as engine:
        entered, release = asyncio.Event(), asyncio.Event()
        original = realm.backend._ensure_image

        async def hold_image(execution, diagnostics):
            await original(execution, diagnostics)
            entered.set()
            await release.wait()

        monkeypatch.setattr(realm.backend, "_ensure_image", hold_image)
        opening = asyncio.create_task(engine._get_mcp_step_manager("srv", None, step_key="call"))
        await asyncio.wait_for(entered.wait(), 5)
        session = engine._execution_session
        session.begin_teardown("cancelled", retain=False)
        release.set()
        with pytest.raises(ExecutionSpecError, match="sealed"):
            await opening
        await session.finalize_leases("cancelled")
        rows = [
            json.loads(line)["argv"]
            for line in (realm.root / "docker.jsonl").read_text().splitlines()
        ]
        assert not any(row[0] in {"cp", "run"} for row in rows)
        assert not engine._mcp_step_managers and not session.quarantined


@pytest.mark.parametrize(
    ("fenced", "managed", "pending"),
    [(True, False, True), (True, False, False), (True, True, False), (False, True, False)],
    ids=["pending-docker", "committed-docker", "mixed-native-budget", "local-only-control"],
)
async def test_teardown_seals_mcp_before_workspace_snapshot(
    tmp_path, monkeypatch, fenced, managed, pending
) -> None:
    # Requirement: real identity snapshots see sealed MCP admission and the original root budget.
    # The local-only control preserves identities and the earlier native deadline without MCP.
    import asyncio
    import json
    import math
    from dataclasses import asdict

    from conductor.execution.errors import ExecutionSpecError
    from conductor.execution.local_process_scope import LocalProcessScopeController
    from tests.test_mcp.fixtures.docker_realm import create_realm

    realm = create_realm(tmp_path, monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    session = ExecutionResolverSession(builtin_local_environment())
    if fenced:
        session.backends["docker"] = realm.backend
    run = replace(
        realm.run,
        workspace_persistence="durable",
        local_workspace_root=(
            str(home / "workspaces" / realm.run.run_id / "local") if managed else None
        ),
        mcp_fence_execution=realm.run.mcp_fence_execution if fenced else None,
    )
    claim = acquire_workspace_claim(run.run_id) if managed else None
    entered, release = asyncio.Event(), asyncio.Event()
    preparing = None
    controller = None
    native_owner = None
    seen_seals, seen_deadlines, seen_refusals = [], [], []
    original_image = realm.backend._ensure_image

    async def hold_image(execution, diagnostics):
        await original_image(execution, diagnostics)
        entered.set()
        await release.wait()

    try:
        if pending:
            monkeypatch.setattr(realm.backend, "_ensure_image", hold_image)
            preparing = asyncio.create_task(session.prepare_leases(run))
            await asyncio.wait_for(entered.wait(), 5)
            assert "docker" not in session.leases
        else:
            await session.prepare_leases(run, claim_active=claim is not None)
        attempt = session.mcp_lifecycle.attempts[0] if fenced else None
        if attempt is not None:
            assert attempt.backend is realm.backend and attempt.abort is not None
            assert attempt.committed is (not pending)
        snapshot = session.workspace_identities
        expected = snapshot()
        attached = dict(session.leases)
        native_deadline = None
        if managed:
            binding = session._managed_binding
            assert binding is not None
            scopes = binding.runtime_context.process_scopes
            assert isinstance(scopes, LocalProcessScopeController)
            controller = scopes
            native_owner = asyncio.create_task(release.wait())
            controller.owners.register(None, native_owner)
            native_deadline = asyncio.get_running_loop().time() + 20
            controller.begin_teardown(deadline=native_deadline)
            assert controller.owners.deadline() == native_deadline

        def observe_snapshot():
            seen_seals.append(session.mcp_lifecycle.sealed)
            seen_deadlines.append(session.cleanup_deadline)
            try:
                session.mcp_lifecycle.check_admission()
            except ExecutionSpecError:
                refused = True
            else:
                refused = False
            seen_refusals.append(refused)
            identities = snapshot()
            assert all(session.leases.get(name) is lease for name, lease in attached.items())
            print(
                json.dumps(
                    {
                        "scenario": "seal-before-snapshot",
                        "fenced": fenced,
                        "managed": managed,
                        "pending_preparation": pending,
                        "mcp_sealed": seen_seals[-1],
                        "deadline": seen_deadlines[-1],
                        "native_deadline": native_deadline,
                        "admission_refused": refused,
                        "identities": {
                            name: asdict(identity) for name, identity in identities.items()
                        },
                        "attempt": id(attempt) if attempt is not None else None,
                        "backend": id(realm.backend) if fenced else None,
                    }
                )
            )
            return identities

        monkeypatch.setattr(session, "workspace_identities", observe_snapshot)
        session.begin_teardown("cancelled", retain=False)
        deadline = session.cleanup_deadline
        if fenced:
            assert seen_seals == [True]
            assert seen_refusals == [True]
            assert seen_deadlines == [deadline]
            assert deadline is not None and math.isfinite(deadline)
        if managed:
            assert deadline == native_deadline
        assert snapshot() == expected
        assert session.leases == {}
        session.begin_teardown("cancelled", retain=False)
        assert session.cleanup_deadline == deadline
    finally:
        release.set()
        if native_owner is not None:
            await native_owner
        if preparing is not None:
            with pytest.raises(ExecutionSpecError, match="sealed"):
                await preparing
        await session.finalize_leases("cancelled", retain=False)
        if controller is not None:
            await controller.close()
        if claim is not None:
            claim.release()
    assert session.cleanup_deadline == deadline
    assert len(seen_seals) == 1
    assert snapshot() == expected and not session.quarantined
