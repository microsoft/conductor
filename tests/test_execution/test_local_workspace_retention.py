"""Filesystem ownership and real managed batch execution contracts."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from conductor.execution import local_process_scope as process_core
from conductor.execution import local_workspace
from conductor.execution.errors import ExecutionSpecError, WorkspaceAttachError
from conductor.execution.local import LocalRunnerBackend
from conductor.execution.types import (
    AgentResult,
    AgentSpec,
    BundleRef,
    CommandSpec,
    RunSpec,
    WorkspaceIdentity,
    WorkspaceLease,
)


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunSpec:
    home = tmp_path / "home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    store = tmp_path / "cas"
    for namespace in ("main", "additional/assets", "registry/child", "plugins/tool"):
        path = store / "tree" / namespace
        path.mkdir(parents=True)
        (path / "source").write_bytes(namespace.encode())
    return RunSpec(
        "run-1",
        bundle=BundleRef("sha256:bundle", str(store), source_roots=(("host", "main"),)),
        workspace_persistence="durable",
        local_workspace_root=str(home / "workspaces/run-1/local"),
    )


def _checkpoint(lease: WorkspaceLease) -> WorkspaceIdentity:
    return WorkspaceIdentity(lease.backend, lease.lease_id, lease.incarnation, lease.location)


async def _release(backend: LocalRunnerBackend, lease: WorkspaceLease) -> None:
    controller = backend._managed[lease.lease_id].controller
    assert (await controller.await_release()).status == "released"
    await backend.confirm_workspace_released(lease)


@pytest.fixture
async def managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[LocalRunnerBackend, RunSpec, WorkspaceLease]]:
    run = _run(tmp_path, monkeypatch)
    backend = LocalRunnerBackend()
    lease = await backend.prepare_run(run)
    controller = backend._managed[lease.lease_id].controller
    try:
        yield backend, run, lease
    finally:
        await controller.close()
        if controller._tasks:
            await asyncio.wait_for(
                asyncio.gather(*tuple(controller._tasks), return_exceptions=True), 10
            )
        controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()


async def test_mark_before_dispatch_refuses_sealed_nonterminal_root(managed) -> None:
    # Requirement: backend marking honors admission, not the still-valid owner mutation rights.
    backend, _run_spec, lease = managed
    binding = backend.local_workspace_binding(lease)
    assert binding is not None
    controller = binding.runtime_context.process_scopes
    owner: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    controller.owners.register(None, owner)
    marker = Path(binding.workspace_root) / "identity.json"
    try:
        backend.mark_workspace_in_use(lease)
        before = marker.read_bytes()
        controller.begin_teardown()
        assert controller._root.sealed and controller._root.result is None
        assert controller.owners.mutations_allowed(None)
        with pytest.raises(ExecutionSpecError, match="root is sealed"):
            backend.mark_workspace_in_use(lease)
        assert marker.read_bytes() == before
    finally:
        owner.set_result(None)
        await _release(backend, lease)


async def test_complete_tree_is_mutable_separate_and_published_identity_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: all CAS namespaces publish atomically, without hardlinks or source mutation.
    run = _run(tmp_path, monkeypatch)
    assert run.bundle is not None and run.local_workspace_root is not None
    source = Path(run.bundle.store_path) / "tree"
    root = Path(run.local_workspace_root)
    (source / "main/link").symlink_to("../additional/assets", target_is_directory=True)
    original = local_workspace.write_identity
    observed: list[Path] = []

    def last(temporary: Path, identity: local_workspace.LocalIdentity) -> None:
        assert not root.exists()
        assert (temporary / "work/registry/child/source").read_bytes() == b"registry/child"
        assert not (temporary / "identity.json").exists()
        observed.append(temporary)
        original(temporary, identity)

    monkeypatch.setattr(local_workspace, "write_identity", last)
    backend = LocalRunnerBackend()
    lease = await backend.prepare_run(run)
    controller = backend._managed[lease.lease_id].controller
    try:
        assert len(observed) == 1 and not observed[0].exists()
        work = root / "work"
        for path in source.rglob("source"):
            copy = work / path.relative_to(source)
            assert copy.read_bytes() == path.read_bytes()
            assert not copy.samefile(path)
            assert copy.stat().st_nlink == 1
        assert os.readlink(work / "main/link") == "../additional/assets"
        (work / "main/source").write_bytes(b"mutable")
        assert (source / "main/source").read_bytes() == b"main"
        marker = json.loads((root / "identity.json").read_text())
        assert marker["version"] == 1 and marker["runtime_state"] == "idle"
        assert (marker["st_dev"], marker["st_ino"]) == (root.stat().st_dev, root.stat().st_ino)
        monkeypatch.setattr(local_workspace, "write_identity", original)
        await _release(backend, lease)
        await backend.finalize_run(lease, "succeeded")
        assert not root.exists()
    finally:
        await controller.close()


async def test_retain_attach_preserves_modified_bytes_then_verified_delete(managed) -> None:
    # Requirement: resume uses the same mutable incarnation and never restages changed files.
    backend, run, lease = managed
    binding = backend.local_workspace_binding(lease)
    assert binding is not None
    output = Path(binding.work_root) / "main/source"
    output.write_bytes(b"retained modification")
    await _release(backend, lease)
    await backend.finalize_run(lease, "failed", retain=True)
    with pytest.raises(ExecutionSpecError):
        backend.local_workspace_binding(lease)
    resumed = LocalRunnerBackend()
    attached = await resumed.attach_run(run, _checkpoint(lease), expect_staged=False)
    controller = resumed._managed[attached.lease_id].controller
    try:
        assert attached == lease
        assert output.read_bytes() == b"retained modification"
        assert await resumed.prepare_run(run) == attached
        await _release(resumed, attached)
        await resumed.finalize_run(attached, "succeeded")
        assert not Path(binding.workspace_root).exists()
    finally:
        await controller.close()


@pytest.mark.parametrize("operation", ["binding", "mark", "command", "agent"])
async def test_managed_authority_refuses_equal_reconstructed_lease(managed, operation: str) -> None:
    # Requirement: an equal reconstructed lease cannot access, activate or dispatch owned work.
    backend, _run_spec, lease = managed
    clone = replace(lease)
    assert clone == lease and clone is not lease
    binding = backend.local_workspace_binding(lease)
    assert binding is not None
    marker = Path(binding.workspace_root) / "identity.json"
    before = marker.read_bytes()
    sentinel = Path(binding.work_root) / "executed"
    dispatched: list[str] = []
    command = CommandSpec(
        sys.executable,
        ("-c", "from pathlib import Path; Path('executed').write_text('owned'); print('done')"),
        working_dir=binding.work_root,
        stdin=b"",
    )
    agent = AgentSpec("owned", "call", "openai", None, "raw")
    agent_result = AgentResult({"owned": True})

    async def execute_agent() -> AgentResult:
        sentinel.write_text("owned")
        return agent_result

    with pytest.raises(ExecutionSpecError):
        if operation == "binding":
            backend.local_workspace_binding(clone)
        elif operation == "mark":
            backend.mark_workspace_in_use(clone)
        elif operation == "command":
            await backend.run_command(
                command, clone, on_dispatch=lambda: dispatched.append("command")
            )
        else:
            await backend.run_agent(
                agent,
                clone,
                execute_local=execute_agent,
                on_dispatch=lambda: dispatched.append("agent"),
            )
    assert not sentinel.exists() and not dispatched
    assert marker.read_bytes() == before
    assert backend._managed[lease.lease_id].controller.proofs == ()
    assert backend.local_workspace_binding(lease) is binding
    if operation == "command":
        result = await backend.run_command(command, lease)
        assert result.outcome == "completed" and result.stdout == "done\n"
        assert sentinel.read_text() == "owned"
    elif operation == "agent":
        assert await backend.run_agent(agent, lease, execute_local=execute_agent) is agent_result
        assert sentinel.read_text() == "owned"
    elif operation == "mark":
        backend.mark_workspace_in_use(lease)
        assert json.loads(marker.read_text())["runtime_state"] == "active"
    await _release(backend, lease)


async def test_equal_reconstructed_lease_cannot_confirm_real_root_release(managed) -> None:
    # Requirement: actual root release does not grant a reconstructed handle marker authority.
    backend, _run_spec, lease = managed
    clone = replace(lease)
    assert clone == lease and clone is not lease
    backend.mark_workspace_in_use(lease)
    controller = backend._managed[lease.lease_id].controller
    assert (await controller.await_release()).status == "released"
    marker = Path(lease.location) / "identity.json"
    before = marker.read_bytes()
    with pytest.raises(ExecutionSpecError):
        await backend.confirm_workspace_released(clone)
    assert marker.read_bytes() == before
    assert not backend._managed[lease.lease_id].released
    await backend.confirm_workspace_released(lease)
    assert json.loads(marker.read_text())["runtime_state"] == "idle"


@pytest.mark.parametrize("retain", [False, True])
async def test_equal_reconstructed_lease_cannot_finalize_released_workspace(
    managed, retain: bool
) -> None:
    # Requirement: only the issued handle may finalize or delete even a genuinely released root.
    backend, _run_spec, lease = managed
    clone = replace(lease)
    assert clone == lease and clone is not lease
    binding = backend.local_workspace_binding(lease)
    assert binding is not None
    await _release(backend, lease)
    root = Path(binding.workspace_root)
    before = (root / "identity.json").read_bytes()
    await backend.finalize_run(clone, "succeeded", retain=retain)
    assert root.is_dir()
    assert (root / "identity.json").read_bytes() == before
    assert backend.local_workspace_binding(lease) is binding
    await backend.finalize_run(lease, "succeeded")
    assert not root.exists()


async def test_freshly_deserialized_identity_attaches_and_issues_new_handle(managed) -> None:
    # Requirement: deserialized identities attach by value; returned leases are issued handles.
    backend, run, lease = managed
    identity = _checkpoint(lease)
    deserialized = WorkspaceIdentity(**json.loads(json.dumps(asdict(identity))))
    assert deserialized == identity and deserialized is not identity
    await _release(backend, lease)
    await backend.finalize_run(lease, "failed", retain=True)
    resumed = LocalRunnerBackend()
    attached = await resumed.attach_run(run, deserialized, expect_staged=False)
    controller = resumed._managed[attached.lease_id].controller
    try:
        assert attached == lease and attached is not lease
        assert await resumed.prepare_run(run) is attached
        assert resumed.local_workspace_binding(attached) is not None
        with pytest.raises(ExecutionSpecError):
            resumed.local_workspace_binding(replace(attached))
        await _release(resumed, attached)
        await resumed.finalize_run(attached, "succeeded")
    finally:
        await controller.close()
        controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()


async def test_legacy_equal_reconstructed_lease_keeps_none_location_behavior(
    tmp_path: Path,
) -> None:
    # Requirement: managed object-identity checks do not change legacy None-location behavior.
    backend = LocalRunnerBackend()
    lease = await backend.prepare_run(RunSpec("legacy-clone"))
    clone = replace(lease)
    assert clone == lease and clone is not lease and clone.location is None
    before = tuple(tmp_path.iterdir())
    assert backend.local_workspace_binding(clone) is None
    backend.mark_workspace_in_use(clone)
    await backend.confirm_workspace_released(clone)
    await backend.finalize_run(clone, "succeeded")
    with pytest.raises(ExecutionSpecError):
        backend.local_workspace_binding(lease)
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "field",
    [
        "backend",
        "run_id",
        "lease_id",
        "incarnation",
        "bundle_digest",
        "readiness",
        "st_dev",
        "st_ino",
        "version",
        "active",
        "uncertain",
        "partial",
        "missing",
    ],
)
@pytest.mark.parametrize("expect_staged", [False, True])
async def test_attach_rejects_missing_partial_mismatched_or_nonidle_identity(
    managed, field: str, expect_staged: bool
) -> None:
    # Requirement: attach always verifies complete ready/idle identity, including unstaged policy.
    backend, run, lease = managed
    await _release(backend, lease)
    root = Path(lease.location)
    marker = root / "identity.json"
    data = json.loads(marker.read_text())
    if field == "missing":
        marker.unlink()
    else:
        if field == "partial":
            data.pop("readiness")
        elif field in ("active", "uncertain"):
            data["runtime_state"] = field
        elif field in ("st_dev", "st_ino", "version"):
            data[field] += 1
        else:
            data[field] = "mismatch"
        marker.write_text(json.dumps(data))
    before = tuple(root.rglob("*"))
    with pytest.raises(WorkspaceAttachError):
        await LocalRunnerBackend().attach_run(run, _checkpoint(lease), expect_staged=expect_staged)
    assert tuple(root.rglob("*")) == before


@pytest.mark.parametrize("swap", ["root", "parent", "work", "marker"])
async def test_attach_never_follows_swapped_components(managed, swap: str) -> None:
    # Requirement: a symlink at any owned path component cannot authorize attach.
    backend, run, lease = managed
    await _release(backend, lease)
    root = Path(lease.location)
    candidates = {
        "root": root,
        "parent": root.parent,
        "work": root / "work",
        "marker": root / "identity.json",
    }
    path = candidates[swap]
    saved = path.with_name(path.name + "-original")
    path.rename(saved)
    path.symlink_to(saved, target_is_directory=swap != "marker")
    with pytest.raises(WorkspaceAttachError):
        await LocalRunnerBackend().attach_run(run, _checkpoint(lease))
    assert saved.exists()


@pytest.mark.parametrize("fault", ["exception", "cancellation", "publish-race"])
async def test_failed_publication_cleans_only_its_own_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    # Requirement: failures before atomic publication cannot overwrite or delete a foreign root.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    root = Path(run.local_workspace_root)
    original = local_workspace._publish

    def fail(temporary: Path, destination: Path) -> None:
        if fault == "cancellation":
            raise asyncio.CancelledError
        if fault == "exception":
            raise OSError(errno.EIO, "injected copy/publish failure")
        destination.mkdir()
        (destination / "foreign").write_bytes(b"keep")
        original(temporary, destination)

    monkeypatch.setattr(local_workspace, "_publish", fail)
    with pytest.raises((OSError, asyncio.CancelledError)):
        await LocalRunnerBackend().prepare_run(run)
    assert not tuple(root.parent.glob(".local-*"))
    if fault == "publish-race":
        assert (root / "foreign").read_bytes() == b"keep"
    else:
        assert not root.exists()


@pytest.mark.parametrize("target", ["../../outside", "/outside", "C:\\outside", "../link"])
async def test_staging_rejects_escaping_absolute_and_cyclic_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    # Requirement: no link may copy an external target or flatten an invalid topology.
    run = _run(tmp_path, monkeypatch)
    assert run.bundle is not None and run.local_workspace_root is not None
    source = Path(run.bundle.store_path) / "tree"
    (source / "main/link").symlink_to(target)
    if target == "../link":
        (source / "link").symlink_to("main/link")
    with pytest.raises(ExecutionSpecError):
        await LocalRunnerBackend().prepare_run(run)
    assert not Path(run.local_workspace_root).exists()
    assert not tuple(Path(run.local_workspace_root).parent.glob(".local-*"))


@pytest.mark.parametrize("name", ["CON", "lpt1.txt", "file.", "file ", "x:y", "a\\b", "x\x01"])
def test_windows_unrepresentable_names_are_explicitly_refused(name: str) -> None:
    # Requirement: Windows representation restrictions refuse rather than rename or flatten.
    with pytest.raises(ExecutionSpecError):
        local_workspace._windows_name(name)


def test_windows_case_collision_refuses_without_renaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: Windows lexical collision checks reject rather than flatten distinct entries.
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    (source / "Case").write_bytes(b"first")
    (source / "case").write_bytes(b"second")
    with monkeypatch.context() as context:
        context.setattr(local_workspace.sys, "platform", "win32")
        with pytest.raises(ExecutionSpecError, match="case-colliding"):
            local_workspace._copy_tree(source, destination, source)
    assert (source / "Case").read_bytes() == b"first"
    assert (source / "case").read_bytes() == b"second"


@pytest.mark.parametrize("field", [1, 2])
def test_filesystem_without_positive_stable_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: int
) -> None:
    # Requirement: marker-only ownership never substitutes for positive device and inode ids.
    original = Path.lstat

    def unavailable(path: Path) -> os.stat_result:
        values = list(original(path))
        if path == tmp_path:
            values[field] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", unavailable)
    with pytest.raises(ExecutionSpecError, match="stable positive"):
        local_workspace._directory(tmp_path)


async def test_location_is_not_delete_authority_and_root_drift_warns(managed, caplog) -> None:
    # Requirement: deletion refuses stale identity and leaves both a swapped root and its files.
    backend, _run_spec, lease = managed
    await _release(backend, lease)
    root = Path(lease.location)
    saved = root.with_name("original")
    root.rename(saved)
    root.mkdir()
    (root / "foreign").write_bytes(b"keep")
    await backend.finalize_run(lease, "succeeded")
    assert saved.is_dir() and (root / "foreign").read_bytes() == b"keep"
    assert "Leaving unverifiable" in caplog.text


async def test_active_cannot_confirm_without_common_root_proof_or_delete(managed, caplog) -> None:
    # Requirement: active is written before dispatch and only the common root proof permits idle.
    backend, _run_spec, lease = managed
    backend.mark_workspace_in_use(lease)
    marker = Path(lease.location) / "identity.json"
    assert json.loads(marker.read_text())["runtime_state"] == "active"
    with pytest.raises(ExecutionSpecError, match="Common root"):
        await backend.confirm_workspace_released(lease)
    await backend.finalize_run(lease, "failed")
    assert marker.is_file() and "unconfirmed" in caplog.text
    await _release(backend, lease)
    assert json.loads(marker.read_text())["runtime_state"] == "idle"
    await backend.finalize_run(lease, "succeeded")


async def test_managed_script_preserves_raw_io_environment_argv_and_default_cwd(managed) -> None:
    # Requirement: direct scripts use custody without a facade and preserve legacy command data.
    backend, _run_spec, lease = managed
    binding = backend.local_workspace_binding(lease)
    result = await backend.run_command(
        CommandSpec(
            sys.executable,
            (
                "-c",
                "import sys,os,json; print(json.dumps([sys.argv[1],os.getcwd(),"
                "os.environ['VALUE'],os.environ['PYTHONUTF8'],sys.stdin.read()])); "
                "sys.stderr.write('DONE SCOPE_EMPTY'); sys.exit(42)",
                "raw; $(false)",
            ),
            env={"VALUE": "value"},
            stdin=b"payload",
        ),
        lease,
    )
    assert result.outcome == "completed" and result.exit_code == 42
    assert json.loads(result.stdout) == [
        "raw; $(false)",
        str(Path(binding.work_root) / "main"),
        "value",
        "1",
        "payload",
    ]
    assert result.stderr == "DONE SCOPE_EMPTY"
    controller = backend._managed[lease.lease_id].controller
    assert all(
        proof.kernel_empty and proof.streams_drained and proof.custodian_reaped
        for proof in controller.proofs
    )
    assert not tuple(Path(lease.location).glob(".launchers/*"))
    assert (
        json.loads((Path(lease.location) / "identity.json").read_text())["runtime_state"]
        == "active"
    )
    await _release(backend, lease)


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux setsid/double-fork proof")
async def test_detached_redirected_descendants_are_reaped_before_backend_result(managed) -> None:
    # Requirement: batch completion waits for kernel-empty even after all descendant stdio closes.
    backend, _run_spec, lease = managed
    payload = (
        "import os,signal\n"
        "r,w=os.pipe(); pid=os.fork()\n"
        "if not pid:\n"
        " os.close(r); os.setsid()\n"
        " if os.fork(): os._exit(0)\n"
        " null=os.open(os.devnull,os.O_RDWR)\n"
        " for fd in (0,1,2): os.dup2(null,fd)\n"
        " os.write(w,str(os.getpid()).encode()); os.close(w); signal.pause()\n"
        "os.close(w); print(os.read(r,100).decode(),flush=True); os.close(r)\n"
    )
    result = await asyncio.wait_for(
        backend.run_command(CommandSpec(sys.executable, ("-c", payload), stdin=b""), lease), 10
    )
    assert result.outcome == "completed" and result.exit_code == 0
    with pytest.raises(ProcessLookupError):
        os.pidfd_open(int(result.stdout))
    await _release(backend, lease)


async def test_backend_waits_for_output_owner_join(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: drained kernel proofs alone cannot return a result while a producer remains.
    backend, _run_spec, lease = managed
    controller = backend._managed[lease.lease_id].controller
    original = controller._output
    drained, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def held(*args) -> None:
        nonlocal count
        await original(*args)
        count += 1
        if count == 2:
            drained.set()
        await release.wait()

    monkeypatch.setattr(controller, "_output", held)
    command = asyncio.create_task(
        backend.run_command(CommandSpec(sys.executable, ("-c", "print('bytes')"), stdin=b""), lease)
    )
    try:
        await asyncio.wait_for(drained.wait(), 10)
        assert not command.done()
        release.set()
        assert (await asyncio.wait_for(command, 10)).stdout == "bytes\n"
        await _release(backend, lease)
    finally:
        release.set()
        await command


@pytest.mark.parametrize("fault", ["helper-crash", "malformed-proof"])
async def test_infrastructure_loss_is_sticky_uncertain_not_payload_exit(
    managed, monkeypatch: pytest.MonkeyPatch, fault: str, caplog
) -> None:
    # Requirement: helper loss or invalid proof can never authorize idle, attach or delete.
    backend, run, lease = managed
    spawn = asyncio.create_subprocess_exec
    receive = process_core.receive_frame

    async def crash(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        process.kill()
        await process.wait()
        return process

    def malformed(channel):
        frame = receive(channel)
        if frame["event"] == "SCOPE_EMPTY":
            return {"event": "DONE"}
        return frame

    with monkeypatch.context() as patcher:
        if fault == "helper-crash":
            patcher.setattr(asyncio, "create_subprocess_exec", crash)
        else:
            patcher.setattr(process_core, "receive_frame", malformed)
        result = await asyncio.wait_for(
            backend.run_command(CommandSpec(sys.executable, ("-c", ""), stdin=b""), lease), 10
        )
    assert result.outcome == "start_failed" and result.exit_code is None
    assert result.start_error is not None and result.start_error.kind == "os_error"
    assert result.start_error.errno is None
    assert (
        json.loads((Path(lease.location) / "identity.json").read_text())["runtime_state"]
        == "uncertain"
    )
    controller = backend._managed[lease.lease_id].controller
    assert (await controller.await_release()).status == "unconfirmed"
    with pytest.raises(ExecutionSpecError):
        await backend.confirm_workspace_released(lease)
    with pytest.raises(WorkspaceAttachError):
        await LocalRunnerBackend().attach_run(run, _checkpoint(lease))
    await backend.finalize_run(lease, "failed")
    assert Path(lease.location).exists() and "unconfirmed" in caplog.text


@pytest.mark.skipif(sys.platform != "linux", reason="Native readiness FIFO")
@pytest.mark.parametrize("timeout", [False, True])
async def test_confirmed_cancel_or_timeout_joins_before_return(
    managed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: bool
) -> None:
    # Requirement: readiness precedes cancel/timeout and confirmed cleanup never poisons the root.
    backend, _run_spec, lease = managed
    fifo = tmp_path / "ready"
    os.mkfifo(fifo)
    descriptor = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()

    def readable() -> None:
        if os.read(descriptor, 1) == b"R":
            loop.remove_reader(descriptor)
            ready.set()

    loop.add_reader(descriptor, readable)
    original_wait = asyncio.wait

    async def deadline(fs, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
        if timeout == 12345:
            await ready.wait()
            return set(), set(fs)
        return await original_wait(fs, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(asyncio, "wait", deadline)
    payload = (
        "import os,signal; "
        f"fd=os.open({str(fifo)!r},os.O_WRONLY); os.write(fd,b'R'); os.close(fd); signal.pause()"
    )
    command = asyncio.create_task(
        backend.run_command(
            CommandSpec(
                sys.executable, ("-c", payload), stdin=b"", timeout=12345 if timeout else None
            ),
            lease,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 10)
        if timeout:
            assert (await asyncio.wait_for(command, 10)).outcome == "timed_out"
        else:
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(command, 10)
        assert not backend._managed[lease.lease_id].uncertain
        assert all(
            proof.custodian_reaped for proof in backend._managed[lease.lease_id].controller.proofs
        )
        await _release(backend, lease)
    finally:
        loop.remove_reader(descriptor)
        os.close(descriptor)
        if not command.done():
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command


@pytest.mark.parametrize("failure", ["missing", "not-directory"])
async def test_managed_start_errors_preserve_native_cause_fields(
    managed, tmp_path: Path, failure
) -> None:
    # Requirement: managed target failures preserve legacy exception type, text, errno and filename.
    backend, _run_spec, lease = managed
    command = str(tmp_path / "missing") if failure == "missing" else sys.executable
    bad_cwd = tmp_path / "not-directory"
    bad_cwd.write_bytes(b"keep")
    spec = CommandSpec(
        command, ("-c", ""), working_dir=str(bad_cwd) if failure != "missing" else None
    )
    legacy = await LocalRunnerBackend().run_command(spec, None)
    actual = await backend.run_command(spec, lease)
    assert actual.outcome == legacy.outcome and actual.exit_code is None
    assert actual.start_error == legacy.start_error
    assert actual.start_error is not None
    error = actual.start_error
    cause = OSError(error.errno, error.message, error.filename)
    expected = legacy.start_error
    assert expected is not None
    original = OSError(expected.errno, expected.message, expected.filename)
    assert type(cause) is type(original) and str(cause) == str(original)
    assert (cause.errno, cause.filename) == (original.errno, original.filename)
    await _release(backend, lease)
