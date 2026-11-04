"""Prepared managed-local consumers; public lifecycle enablement is separate."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conductor.config.schema import (
    ForEachDef,
    MCPServerDef,
    MCPStepDef,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    WorkflowConfig,
    WorkflowDef,
    WorkflowStepDef,
)
from conductor.engine.local_workspace import LocalWorkspacePathError
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEventEmitter
from conductor.exceptions import ExecutionError
from conductor.execution.errors import WorkspaceAttachError
from conductor.execution.local import LocalRunnerBackend
from conductor.execution.local_process_scope import LocalProcessScopeController
from conductor.execution.types import BundleRef, RunSpec, WorkspaceIdentity
from conductor.executor.script import ScriptExecutor

MCP_READER = """
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP("workspace-reader")
@server.tool()
def read_file() -> str:
    return Path("file").read_text()
server.run(transport="stdio")
"""


def config(*, cwd: str | None = None) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="consumers",
            entry_point="write",
            runtime=RuntimeConfig(
                working_dir=cwd,
                mcp_servers={
                    "reader": MCPServerDef(command=sys.executable, args=["-c", MCP_READER])
                },
            ),
        ),
        agents=[
            ScriptStepDef(
                name="write",
                command=sys.executable,
                args=[
                    "-c",
                    "from pathlib import Path; "
                    "Path('repo').mkdir(); Path('repo/file').write_text('staged')",
                ],
                routes=[RouteDef(to="$end")],
            ),
            MCPStepDef(name="read", server="reader", tool="read_file", routes=[]),
        ],
    )


@asynccontextmanager
async def prepared(
    tmp_path: Path, *, workflow_config: WorkflowConfig | None = None, persistence="durable"
):
    """Issue a real lease and physically close its controller before fixture removal."""
    cfg = workflow_config or config()
    source = tmp_path / "source"
    source.mkdir()
    store = tmp_path / "cas"
    (store / "tree/main").mkdir(parents=True)
    (store / "tree/main/child").mkdir()
    home = Path(os.environ["CONDUCTOR_HOME"])
    run = RunSpec(
        tmp_path.name,
        bundle=BundleRef(
            "sha256:consumers",
            str(store),
            source_roots=((str(source), "main"),),
        ),
        workspace_persistence=persistence,
        local_workspace_root=str(home / "workspaces" / tmp_path.name / "local"),
    )
    backend = LocalRunnerBackend()
    lease = await backend.prepare_run(run)
    binding = backend.local_workspace_binding(lease)
    assert binding is not None
    controller = binding.runtime_context.process_scopes
    assert isinstance(controller, LocalProcessScopeController)
    provider = MagicMock()
    provider.get_session_ids.return_value = {}
    provider.get_session_cwds.return_value = {}
    engine = WorkflowEngine(
        cfg, provider, workflow_path=source / "flow.yaml", event_emitter=WorkflowEventEmitter()
    )
    engine._execution_session.backends["local"] = backend
    engine._execution_session.leases["local"] = lease
    try:
        yield engine, backend, lease, binding, run
    finally:
        await engine._close_mcp_step_managers()
        result = await controller.await_release()
        if result.status == "released":
            await backend.confirm_workspace_released(lease)
            await backend.finalize_run(lease, "succeeded")
        await controller.close()
        # Test resource accounting is deliberately stronger than API close.
        # Observe exact tasks/threads, not names, PIDs or a timed grace sleep.
        pending = tuple(controller._tasks)
        if pending:
            done, unfinished = await asyncio.wait(pending, timeout=10)
            assert not unfinished
            for task in done:
                if not task.cancelled():
                    task.result()
        controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()
        assert not controller.owners.tasks
        assert not controller._connections
        assert not controller._send_tails
        assert all(
            not scope.resources and (scope.process is None or scope.reaped)
            for scope in controller._scopes.values()
        )


async def test_script_creates_live_cwd_read_by_real_mcp(tmp_path, monkeypatch):
    # Requirement: a real script-created directory is consumed by real stdio MCP, not host/CAS.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    async with prepared(tmp_path) as (engine, _backend, _lease, binding, _run):
        script = engine.config.agents[0]
        assert isinstance(script, ScriptStepDef)
        result = await engine._execute_script(script, {})
        assert result.exit_code == 0
        engine.config.workflow.runtime.working_dir = "repo"
        reader = engine.config.agents[1]
        assert isinstance(reader, MCPStepDef)
        envelope = await engine._run_mcp_step(reader, {})
        assert envelope["content"][0]["text"] == "staged"
        assert not (tmp_path / "source/repo").exists()
        assert not (tmp_path / "cas/tree/main/repo").exists()
        manager = next(iter(engine._mcp_step_managers.values()))
        assert manager.managed_workspace is binding.runtime_context
        assert next(iter(engine._mcp_step_managers)) == (
            "reader",
            str(Path(binding.work_root) / "main/repo"),
            binding.workspace_root,
            binding.incarnation,
        )
        assert engine._execution_session.executed_backends() == {"local"}
        assert engine._in_flight_attempts["write"]
        marker = json.loads((Path(binding.workspace_root) / "identity.json").read_text())
        assert marker["runtime_state"] == "active"


@pytest.mark.parametrize(
    "step_cwd,runtime_cwd", [(None, None), (None, "{{ dir }}"), ("{{ dir }}", "bad")]
)
async def test_effective_cwd_is_rendered_once_without_config_mutation(
    tmp_path, monkeypatch, step_cwd, runtime_cwd
):
    # Requirement: step/runtime/default cwd is selected once; remaining Jinja text is literal.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    cfg = config(cwd=runtime_cwd)
    script = ScriptStepDef(
        name="write",
        command=sys.executable,
        args=["-c", "import os; print(os.getcwd())"],
        working_dir=step_cwd,
    )
    cfg.agents[0] = script
    async with prepared(tmp_path, workflow_config=cfg) as (engine, backend, lease, binding, _run):
        literal = "{{ not_a_second_render }}"
        destination = Path(binding.work_root) / "main"
        (destination / literal).mkdir()
        if step_cwd is not None or runtime_cwd is not None:
            destination /= literal
        original = backend.run_command
        seen = []

        async def capture(spec, passed_lease, **kwargs):
            assert passed_lease is lease
            seen.append(spec)
            return await original(spec, passed_lease, **kwargs)

        monkeypatch.setattr(backend, "run_command", capture)
        output = await engine._execute_script(script, {"dir": literal})
        assert Path(output.stdout.strip()) == destination
        assert len(seen) == 1 and seen[0].attempt_id
        assert script.working_dir == step_cwd
        assert cfg.workflow.runtime.working_dir == runtime_cwd


@pytest.mark.parametrize("consumer", ["script", "mcp"])
async def test_mapping_refusal_prevents_dispatch(tmp_path, monkeypatch, consumer):
    # Requirement: refused cwd cannot create a scope/manager or execution history.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    async with prepared(tmp_path) as (engine, _backend, _lease, binding, _run):
        if consumer == "script":
            step = ScriptStepDef(name="write", command=sys.executable, working_dir="../escape")
            with pytest.raises(LocalWorkspacePathError):
                await engine._execute_script(step, {})
        else:
            engine.config.workflow.runtime.working_dir = "../escape"
            with pytest.raises(ExecutionError):
                await engine._run_mcp_step(engine.config.agents[1], {})
        assert not binding.runtime_context.process_scopes.proofs
        assert not engine._managed_mcp_step_managers
        assert not engine._execution_session.executed_backends()
        marker = json.loads((Path(binding.workspace_root) / "identity.json").read_text())
        assert marker["runtime_state"] == "idle"


async def test_parallel_and_for_each_use_mapped_pools(tmp_path, monkeypatch):
    # Requirement: main/group/item handlers preserve cwd templating and per-cwd pools.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    cfg = config()
    cfg.agents.append(MCPStepDef(name="other", server="reader", tool="read_file", routes=[]))
    group = ParallelGroup(name="parallel", agents=["read", "other"], routes=[])
    loop = ForEachDef.model_validate(
        {
            "name": "loop",
            "type": "for_each",
            "source": "workflow.input.dirs",
            "as": "item",
            "agent": MCPStepDef(name="item_read", server="reader", tool="read_file", routes=[]),
            "routes": [],
        }
    )
    cfg.parallel = [group]
    cfg.for_each = [loop]
    async with prepared(tmp_path, workflow_config=cfg) as (engine, _backend, _lease, binding, _run):
        for name in ("one", "two"):
            directory = Path(binding.work_root) / "main" / name
            directory.mkdir()
            (directory / "file").write_text(name)
        cfg.workflow.runtime.working_dir = "one"
        parallel = await engine._execute_parallel_group(group)
        assert parallel.outputs["read"]["content"][0]["text"] == "one"
        assert parallel.outputs["other"]["content"][0]["text"] == "one"
        cfg.workflow.runtime.working_dir = "{{ item }}"
        engine.context.set_workflow_inputs({"dirs": ["one", "two"]})
        output = await engine._execute_for_each_group(loop)
        assert sorted(item["content"][0]["text"] for item in output.outputs) == ["one", "two"]
        assert len(engine._mcp_step_managers) == 2
        assert len(engine._managed_mcp_step_managers) == 2


@pytest.mark.parametrize("failure", ["missing", "not_directory"])
async def test_native_spawn_errors_preserve_legacy_cause(tmp_path, monkeypatch, failure):
    # Requirement: managed and legacy ScriptExecutor expose identical native spawn causes.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    async with prepared(tmp_path) as (_engine, backend, lease, binding, _run):
        target = Path(binding.work_root) / "main/missing"
        if failure == "not_directory":
            target.write_text("not a directory")
            target /= "child"
        step = ScriptStepDef(name="write", command=str(target))
        errors = []
        for runner, handle in ((LocalRunnerBackend(), None), (backend, lease)):
            with pytest.raises(ExecutionError) as raised:
                await ScriptExecutor(runner).execute(
                    step, {}, lease=handle, working_directory_mapper=lambda _: binding.work_root
                )
            cause = raised.value.__cause__
            assert isinstance(cause, OSError)
            errors.append((type(cause), str(cause), cause.errno, cause.filename))
        assert errors[0] == errors[1]


async def test_nonzero_exit_is_completed_data(tmp_path, monkeypatch):
    # Requirement: a successfully spawned target's nonzero exit remains data, not spawn failure.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    async with prepared(tmp_path) as (engine, _backend, _lease, _binding, _run):
        result = await engine._execute_script(
            ScriptStepDef(
                name="write", command=sys.executable, args=["-c", "raise SystemExit(17)"]
            ),
            {},
        )
        assert result.exit_code == 17


async def test_failed_startup_remains_registered_before_pool_insertion(tmp_path, monkeypatch):
    # Requirement: failed zero-success manager stays owned/active and cannot attach before release.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    cfg = config()
    cfg.workflow.runtime.mcp_servers["reader"].command = str(tmp_path / "missing-mcp")
    async with prepared(tmp_path, workflow_config=cfg) as (engine, _backend, lease, binding, run):
        with pytest.raises(ExecutionError):
            await engine._run_mcp_step(cfg.agents[1], {})
        assert not engine._mcp_step_managers
        assert len(engine._managed_mcp_step_managers) == 1
        manager = next(iter(engine._managed_mcp_step_managers))
        assert manager.managed_workspace is binding.runtime_context
        assert binding.runtime_context.process_scopes.owners.tasks
        identity = WorkspaceIdentity(
            lease.backend, lease.lease_id, lease.incarnation, lease.location
        )
        with pytest.raises(WorkspaceAttachError):
            await LocalRunnerBackend().attach_run(run, identity)


async def test_nested_child_script_and_mcp_share_issued_binding(tmp_path, monkeypatch):
    # Requirement: actual child run/cleanup uses its own staged namespace and the same lease.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    async with prepared(tmp_path) as (engine, _backend, _lease, binding, _run):
        child = config(cwd="repo")
        script, reader = child.agents[:2]
        assert isinstance(script, ScriptStepDef) and isinstance(reader, MCPStepDef)
        script.working_dir = "."
        script.routes = [RouteDef(to="read")]
        reader.routes = [RouteDef(to="$end")]
        child.output = {"seen": "{{ read.output.content[0].text }}"}
        child_path = tmp_path / "source/child/child.yaml"
        child_path.parent.mkdir()
        child_path.write_text(json.dumps(child.model_dump(mode="json")))
        step = WorkflowStepDef(name="child", workflow="child/child.yaml", routes=[])
        output, _usage = await engine._execute_subworkflow_with_inputs(step, {})
        assert output == {"seen": "staged"}
        assert (Path(binding.work_root) / "main/child/repo/file").read_text() == "staged"
        assert not (child_path.parent / "repo").exists()
        assert binding.runtime_context.process_scopes.proofs


class _Timer:
    def __init__(self, at: float, callback: Callable[[], None]) -> None:
        self.at, self.callback, self.cancelled = at, callback, False

    def cancel(self) -> None:
        self.cancelled = True


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.timers: list[_Timer] = []

    def now(self) -> float:
        return self.value

    def at(self, deadline: float, callback: Callable[[], None]) -> _Timer:
        timer = _Timer(deadline, callback)
        self.timers.append(timer)
        return timer

    def advance(self, value: float) -> None:
        self.value = value
        for timer in sorted(self.timers, key=lambda item: item.at):
            if timer.at <= value and not timer.cancelled:
                timer.cancelled = True
                timer.callback()


@pytest.mark.skipif(sys.platform != "linux", reason="Native SIGSTOP/pidfd fault requires Linux")
@pytest.mark.parametrize("cancel", [False, True])
async def test_startup_teardown_deadline_keeps_failed_manager_and_primary(
    tmp_path, monkeypatch, cancel
):
    # Requirement: failed/cancelled zero-success startup cannot erase an unjoined native owner.
    from conductor.mcp import manager as manager_module

    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    cfg = config()
    # Invalid initialize response is a real wire failure, not a mocked SDK result.
    cfg.workflow.runtime.mcp_servers["reader"].args = [
        "-B",
        "-c",
        "import json,sys,socket,os; request=json.loads(sys.stdin.readline()); "
        "s=socket.socket(socket.AF_UNIX); "
        "s.connect('\\0'+os.environ['QA_READY']); "
        "s.sendall(str(os.getpid()).encode()); s.close(); "
        + (
            "print(json.dumps({'jsonrpc':'2.0','id':request['id'],"
            "'error':{'code':-32603,'message':'initialize refused'}}),flush=True); "
            if not cancel
            else ""
        )
        + "sys.stdin.read()",
    ]
    original_stdio = manager_module.stdio_client
    assert original_stdio is not None
    entered = asyncio.Event()
    teardown = asyncio.Event()
    allow_teardown = asyncio.Event()
    scope_holder = []
    handles = []
    clock = _Clock()
    address = "conductor-task6-" + uuid.uuid4().hex
    cfg.workflow.runtime.mcp_servers["reader"].env = {"QA_READY": address}
    payload_ready = asyncio.get_running_loop().create_future()

    async def ready_connection(reader, writer):
        try:
            pid = int(await reader.read())
            if not payload_ready.done():
                payload_ready.set_result(pid)
        finally:
            writer.close()
            await writer.wait_closed()

    readiness = await asyncio.start_unix_server(ready_connection, path="\0" + address)
    async with prepared(tmp_path, workflow_config=cfg) as (engine, _backend, lease, binding, run):
        controller = binding.runtime_context.process_scopes
        assert isinstance(controller, LocalProcessScopeController)
        controller._clock = clock

        @asynccontextmanager
        async def native_transport_with_blocked_exit(params):
            async with original_stdio(params) as streams:
                try:
                    entered.set()
                    yield streams
                finally:
                    scope = next(iter(controller._scopes.values()))
                    await scope.ready.wait()
                    assert scope.process is not None
                    handles.append(os.pidfd_open(scope.process.pid))
                    scope_holder.append(scope)
                    scope.process.send_signal(signal.SIGSTOP)
                    _pid, status = await asyncio.to_thread(
                        os.waitpid, scope.process.pid, os.WUNTRACED
                    )
                    assert os.WIFSTOPPED(status)
                    teardown.set()
                    await allow_teardown.wait()

        monkeypatch.setattr(manager_module, "stdio_client", native_transport_with_blocked_exit)
        owner = asyncio.create_task(engine._run_mcp_step(cfg.agents[1], {}))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            await asyncio.wait_for(asyncio.shield(payload_ready), 10)
            if cancel:
                owner.cancel()
            await asyncio.wait_for(teardown.wait(), 10)
            manager = next(iter(engine._managed_mcp_step_managers))
            connection = next(iter(manager._owners.values()))
            primary = connection.primary_error
            assert primary is not None
            assert isinstance(primary, asyncio.CancelledError) == cancel
            if cancel:
                original_begin = controller.begin_teardown
                repeated = asyncio.Event()
                deadline = controller.owners.deadline()

                def observed_begin(invocation_id=None, *, deadline=None):
                    original_begin(invocation_id, deadline=deadline)
                    repeated.set()

                monkeypatch.setattr(controller, "begin_teardown", observed_begin)
                owner.cancel()
                await asyncio.wait_for(repeated.wait(), 10)
                assert controller.owners.deadline() == deadline
            clock.advance(30)
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(owner, 10)
            else:
                with pytest.raises(ExecutionError):
                    await asyncio.wait_for(owner, 10)
            assert not engine._mcp_step_managers
            assert manager in engine._managed_mcp_step_managers
            await engine._close_mcp_step_managers()
            assert manager._cleanup_uncertain
            assert controller.owners.quarantined
            assert (await controller.await_release()).status == "unconfirmed"
            assert connection.primary_error is primary
            identity = WorkspaceIdentity(
                lease.backend, lease.lease_id, lease.incarnation, lease.location
            )
            with pytest.raises(WorkspaceAttachError):
                await LocalRunnerBackend().attach_run(run, identity)
        finally:
            allow_teardown.set()
            for scope in scope_holder:
                assert scope.process is not None
                if scope.process.returncode is None:
                    scope.process.send_signal(signal.SIGCONT)
            native_owners = {
                task
                for attempted in engine._managed_mcp_step_managers
                for task in attempted._owners
            }
            if native_owners:
                done, pending = await asyncio.wait(native_owners, timeout=10)
                assert not pending
                for task in done:
                    if not task.cancelled():
                        task.exception()  # The expected native startup/exit failure is observed.
            if not owner.done():
                owner.cancel()
                done, pending = await asyncio.wait({owner}, timeout=10)
                assert not pending
                if not owner.cancelled():
                    owner.exception()
            for handle in handles:
                os.close(handle)
            readiness.close()
            await readiness.wait_closed()
        assert (await controller.await_release()).status == "unconfirmed"


async def test_helper_infrastructure_failure_is_not_target_missing(tmp_path, monkeypatch):
    # Requirement: broken helper infrastructure cannot impersonate target FileNotFoundError.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    async with prepared(tmp_path) as (engine, _backend, _lease, binding, _run):
        controller = binding.runtime_context.process_scopes
        assert isinstance(controller, LocalProcessScopeController)
        original_descriptor = controller._descriptor

        def corrupt_private_descriptor(directory, role, **values):
            path = original_descriptor(directory, role, **values)
            metadata = directory / "descriptor.json"
            metadata.chmod(0o600)
            metadata.write_text("{")
            metadata.chmod(0o400)
            return path

        # Run the actual installed helper with a corrupted private descriptor.
        # No target start/error or process proof is fabricated by the harness.
        monkeypatch.setattr(controller, "_descriptor", corrupt_private_descriptor)
        with pytest.raises(ExecutionError) as raised:
            await engine._execute_script(ScriptStepDef(name="write", command=sys.executable), {})
        cause = raised.value.__cause__
        assert isinstance(cause, OSError) and not isinstance(cause, FileNotFoundError)
        assert cause.errno is None and cause.filename is None
        assert not any("COMMAND_STARTED" in proof.events for proof in controller.proofs)


async def test_native_eviction_closes_only_evicted_pool_without_declaring_idle(
    tmp_path, monkeypatch
):
    # Requirement: strict child/pool close joins native resources without confirming root idle.
    from conductor.engine import workflow as workflow_module

    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(workflow_module, "_MCP_STEP_POOL_MAX", 1)
    async with prepared(tmp_path) as (engine, _backend, _lease, binding, _run):
        reader = engine.config.agents[1]
        assert isinstance(reader, MCPStepDef)
        for name in ("first", "second"):
            path = Path(binding.work_root) / "main" / name
            path.mkdir()
            (path / "file").write_text(name)
            engine.config.workflow.runtime.working_dir = name
            envelope = await engine._run_mcp_step(reader, {})
            assert envelope["content"][0]["text"] == name
        attempted = list(engine._managed_mcp_step_managers)
        assert len(attempted) == 2 and len(engine._mcp_step_managers) == 1
        assert attempted[0]._managed_owner.done()
        assert not attempted[1]._managed_owner.done()
        assert all(
            proof.kernel_empty and proof.streams_drained and proof.custodian_reaped
            for proof in binding.runtime_context.process_scopes.proofs[:1]
        )
        marker = json.loads((Path(binding.workspace_root) / "identity.json").read_text())
        assert marker["runtime_state"] == "active"


async def test_native_workflow_failure_keeps_on_failure_tree(tmp_path, monkeypatch):
    # Requirement: actual engine failure and dispatch history leave an on-failure tree intact.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    cfg = config()
    script = cfg.agents[0]
    assert isinstance(script, ScriptStepDef)
    script.command = str(tmp_path / "missing-target")
    async with prepared(tmp_path, workflow_config=cfg, persistence="on-failure") as (
        engine,
        backend,
        lease,
        binding,
        _run,
    ):
        events = []
        engine._event_emitter.subscribe(lambda event: events.append(event))
        with pytest.raises(ExecutionError) as raised:
            await engine.run({})
        assert isinstance(raised.value.__cause__, FileNotFoundError)
        assert any(event.type == "workflow_failed" for event in events)
        assert engine._execution_session.executed_backends() == {"local"}
        await backend.finalize_run(lease, "failed", retain=True)
        assert Path(binding.work_root).is_dir()
        marker = json.loads((Path(binding.workspace_root) / "identity.json").read_text())
        assert marker["runtime_state"] == "active"


async def test_authored_argv_and_env_are_not_path_rewritten(tmp_path, monkeypatch):
    # Requirement: only typed cwd is mapped; argv/env strings remain authored payload data.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    async with prepared(tmp_path) as (engine, _backend, _lease, _binding, _run):
        authored = str(tmp_path / "source") + "/{{ still_literal }}"
        step = ScriptStepDef(
            name="write",
            command=sys.executable,
            args=[
                "-c",
                "import sys,os,json; print(json.dumps([sys.argv[1],os.environ['OPAQUE']]))",
                "{{ authored }}",
            ],
            env={"OPAQUE": authored},
        )
        result = await engine._execute_script(step, {"authored": authored})
        assert json.loads(result.stdout) == [authored, authored]


MCP_COUNTED_READER = """
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP("admission-reader")
@server.tool()
def read_file() -> str:
    counter = Path("rpc-count")
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    return Path("file").read_text()
server.run(transport="stdio")
"""


@pytest.mark.parametrize("held_connect", [False, True], ids=["cached", "inflight"])
async def test_root_seal_refuses_cached_or_inflight_native_mcp(tmp_path, monkeypatch, held_connect):
    # Requirement: actual nonterminal root seal forbids a new RPC and late pool insertion.
    from conductor.mcp.manager import MCPManager

    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    cfg = config()
    cfg.workflow.runtime.mcp_servers["reader"].args = ["-B", "-c", MCP_COUNTED_READER]
    connected = asyncio.Event()
    release = asyncio.Event()
    native_connect = MCPManager.connect_server

    async def real_connect_then_hold(manager, *args, **kwargs):
        result = await native_connect(manager, *args, **kwargs)
        connected.set()
        await release.wait()
        return result

    async with prepared(tmp_path, workflow_config=cfg) as (engine, backend, lease, binding, _run):
        controller = binding.runtime_context.process_scopes
        assert isinstance(controller, LocalProcessScopeController)
        live = Path(binding.work_root) / "main"
        (live / "file").write_text("before-seal")
        reader = cfg.agents[1]
        assert isinstance(reader, MCPStepDef)
        outer = None
        if held_connect:
            monkeypatch.setattr(MCPManager, "connect_server", real_connect_then_hold)
            outer = asyncio.create_task(engine._run_mcp_step(reader, {}))
            await asyncio.wait_for(connected.wait(), 10)
            assert not engine._mcp_step_managers and engine._mcp_step_pending == 1
        else:
            result = await engine._run_mcp_step(reader, {})
            assert result["content"][0]["text"] == "before-seal"
            assert (live / "rpc-count").read_text() == "1"
        attempted = next(iter(engine._managed_mcp_step_managers))
        assert attempted.managed_workspace is binding.runtime_context
        assert backend.local_workspace_binding(lease) is binding
        controller.begin_teardown()
        assert controller._root.sealed and controller._root.result is None
        assert controller.owners.mutations_allowed(None)
        (live / "file").write_text("after-seal")
        refused = False
        try:
            release.set()
            try:
                if outer is not None:
                    done, pending = await asyncio.wait({outer}, timeout=10)
                    assert not pending
                    outer.result()
                else:
                    await engine._run_mcp_step(reader, {})
            except ExecutionError:
                refused = True
            counter = live / "rpc-count"
            count = int(counter.read_text()) if counter.exists() else 0
            print(
                json.dumps(
                    {
                        "admission_case": "inflight" if held_connect else "cached",
                        "refused": refused,
                        "rpc_count": count,
                        "pool_entries": len(engine._mcp_step_managers),
                        "attempted": len(engine._managed_mcp_step_managers),
                    }
                )
            )
            assert refused, "sealed nonterminal root admitted native RPC"
            assert count == (0 if held_connect else 1)
            assert engine._mcp_step_pending == 0
            if held_connect:
                assert not engine._mcp_step_managers
                assert attempted._managed_owner is not None and attempted._managed_owner.done()
            assert attempted in engine._managed_mcp_step_managers
        finally:
            release.set()
            if outer is not None:
                done, pending = await asyncio.wait({outer}, timeout=10)
                assert not pending
                if not outer.cancelled():
                    outer.exception()


async def test_final_mcp_admission_and_sibling_root_isolation(tmp_path, monkeypatch):
    # Requirement: final admission refuses a late seal while an unrelated root still serves RPC.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    async with prepared(left) as first, prepared(right) as second:
        engine, backend, lease, binding, _ = first
        sibling, _, _, sibling_binding, _ = second
        for active, bound in ((engine, binding), (sibling, sibling_binding)):
            (Path(bound.work_root) / "main/file").write_text("same-issued-positive")
            result = await active._run_mcp_step(active.config.agents[1], {})
            assert result["content"][0]["text"] == "same-issued-positive"
        controller = binding.runtime_context.process_scopes
        assert isinstance(controller, LocalProcessScopeController)
        native_get = engine._get_mcp_step_manager

        async def get_then_seal(server, cwd):
            manager = await native_get(server, cwd)
            controller.begin_teardown()
            assert controller._root.sealed and controller._root.result is None
            return manager

        monkeypatch.setattr(engine, "_get_mcp_step_manager", get_then_seal)
        with pytest.raises(ExecutionError):
            await engine._run_mcp_step(engine.config.agents[1], {})
        assert backend.local_workspace_binding(lease) is binding
        result = await sibling._run_mcp_step(sibling.config.agents[1], {})
        assert result["content"][0]["text"] == "same-issued-positive"
        assert not sibling_binding.runtime_context.process_scopes._root.sealed


@pytest.mark.skipif(sys.platform != "linux", reason="Native abstract Unix readiness channel")
async def test_previously_admitted_native_rpc_drains_after_seal(tmp_path, monkeypatch):
    # Requirement: sealing refuses new admission, not an RPC already running on the native server.
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    cfg = config()
    address = "admitted-rpc-" + uuid.uuid4().hex
    cfg.workflow.runtime.mcp_servers["reader"].env = {"QA_READY": address}
    cfg.workflow.runtime.mcp_servers["reader"].args = [
        "-B",
        "-c",
        MCP_READER.replace(
            '    return Path("file").read_text()',
            "    import os,socket\n"
            '    s=socket.socket(socket.AF_UNIX); s.connect("\\0"+os.environ["QA_READY"])\n'
            '    s.sendall(b"ready\\n"); s.recv(1); s.close()\n'
            '    return Path("file").read_text()',
        ),
    ]
    ready = asyncio.get_running_loop().create_future()
    callbacks = set()
    writers = set()

    async def on_ready(reader, writer):
        callbacks.add(asyncio.current_task())
        writers.add(writer)
        try:
            assert await reader.readline() == b"ready\n"
            ready.set_result(writer)
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(on_ready, path="\0" + address)
    try:
        async with prepared(tmp_path, workflow_config=cfg) as (engine, _, _, binding, _):
            (Path(binding.work_root) / "main/file").write_text("admitted-before-seal")
            reader = cfg.agents[1]
            assert isinstance(reader, MCPStepDef)
            outer = asyncio.create_task(engine._run_mcp_step(reader, {}))
            released = False
            try:
                writer = await asyncio.wait_for(asyncio.shield(ready), 10)
                controller = binding.runtime_context.process_scopes
                assert isinstance(controller, LocalProcessScopeController)
                controller.begin_teardown()
                assert controller._root.sealed and controller._root.result is None
                assert controller.owners.mutations_allowed(None)
                writer.write(b"x")
                await writer.drain()
                released = True
                done, pending = await asyncio.wait({outer}, timeout=10)
                assert not pending
                assert outer.result()["content"][0]["text"] == "admitted-before-seal"
            finally:
                if not released:
                    for writer in writers:
                        writer.write(b"x")
                        await writer.drain()
                done, pending = await asyncio.wait({outer}, timeout=10)
                assert not pending
                if not outer.cancelled():
                    outer.exception()
    finally:
        server.close()
        await server.wait_closed()
        if callbacks:
            done, pending = await asyncio.wait(callbacks, timeout=10)
            assert not pending
            for task in done:
                task.result()
