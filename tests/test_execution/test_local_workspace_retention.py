"""Filesystem ownership and real managed batch execution contracts."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import stat
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conductor.execution import local as local_backend
from conductor.execution import local_process_scope as process_core
from conductor.execution import local_scope_launcher as scope_launcher
from conductor.execution import local_workspace
from conductor.execution.errors import ExecutionSpecError, WorkspaceAttachError
from conductor.execution.local import LocalRunnerBackend
from conductor.execution.types import (
    AgentResult,
    AgentSpec,
    BundleRef,
    CommandSpec,
    LocalWorkspaceBinding,
    RunSpec,
    WorkspaceIdentity,
    WorkspaceLease,
)
from conductor.redaction import REDACTED_MARKER, RunRedactor


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
    active_transitions: list[local_workspace.LocalIdentity] = []

    def last(temporary: Path, identity: local_workspace.LocalIdentity) -> None:
        if temporary == root:
            assert identity.runtime_state == "active"
            assert json.loads((root / "identity.json").read_text())["runtime_state"] == "idle"
            active_transitions.append(identity)
        else:
            assert not root.exists()
            assert (temporary / "work/registry/child/source").read_bytes() == b"registry/child"
            assert not (temporary / "identity.json").exists()
            assert identity.runtime_state == "idle"
            observed.append(temporary)
        original(temporary, identity)

    monkeypatch.setattr(local_workspace, "write_identity", last)
    backend = LocalRunnerBackend()
    lease = await backend.prepare_run(run)
    controller = backend._managed[lease.lease_id].controller
    try:
        assert len(observed) == 1 and not observed[0].exists()
        assert len(active_transitions) == 1
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
        assert marker["version"] == 1 and marker["runtime_state"] == "active"
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
    first, second = source / "first", source / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    scandir = os.scandir

    @contextmanager
    def colliding(path):
        with scandir(path) as entries:
            yield (
                SimpleNamespace(
                    name="Case" if entry.name == "first" else "case",
                    path=entry.path,
                    stat=entry.stat,
                )
                for entry in entries
            )

    with monkeypatch.context() as context:
        context.setattr(local_workspace.sys, "platform", "win32")
        context.setattr(local_workspace.os, "scandir", colliding)
        with pytest.raises(ExecutionSpecError, match="case-colliding"):
            local_workspace._copy_tree(source, destination, source)
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert {path.name for path in source.iterdir()} == {"first", "second"}


@pytest.mark.parametrize("field", [1, 2])
def test_filesystem_without_positive_stable_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: int
) -> None:
    # Requirement: marker-only ownership never substitutes for positive device and inode ids.
    original = Path.lstat

    def unavailable(path: Path) -> os.stat_result:
        info = original(path)
        if path != tmp_path:
            return info
        values = list(info)
        values[field] = 0
        tuple_fields = {"st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"}
        named = {
            name: getattr(info, name)
            for name in dir(info)
            if name.startswith("st_") and name not in tuple_fields
        }
        return os.stat_result(values, named)

    monkeypatch.setattr(Path, "lstat", unavailable)
    with pytest.raises(ExecutionSpecError, match="stable positive"):
        local_workspace._directory(tmp_path)


def _file_metadata(info: os.stat_result, **changes: int) -> SimpleNamespace:
    fields = {
        "st_mode": info.st_mode,
        "st_dev": info.st_dev,
        "st_ino": info.st_ino,
        "st_file_attributes": getattr(info, "st_file_attributes", 0),
    }
    return SimpleNamespace(**(fields | changes))


def test_copy_accepts_unavailable_enumeration_identity_with_real_file_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: Windows enumeration zeros cannot reject a genuinely identified disk file.
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    original = source / "payload"
    original.write_bytes(b"immutable CAS\r\n")
    original.chmod(stat.S_IRUSR)
    scandir = os.scandir

    @contextmanager
    def without_ids(path):
        with scandir(path) as entries:
            yield (
                SimpleNamespace(
                    name=entry.name,
                    path=entry.path,
                    stat=lambda *, follow_symlinks=False, entry=entry: _file_metadata(
                        entry.stat(follow_symlinks=follow_symlinks), st_dev=0, st_ino=0
                    ),
                )
                for entry in entries
            )

    with monkeypatch.context() as context:
        context.setattr(local_workspace.os, "scandir", without_ids)
        local_workspace._copy_tree(source, destination, source)
    copy = destination / "payload"
    assert copy.read_bytes() == b"immutable CAS\r\n"
    assert not copy.samefile(original)
    assert copy.stat().st_mode & stat.S_IWUSR
    copy.write_bytes(b"mutable copy")
    assert original.read_bytes() == b"immutable CAS\r\n"


@pytest.mark.parametrize(
    "boundary", ["before-lstat", "symlink-before-lstat", "before-open", "symlink-before-open"]
)
def test_copy_refuses_real_replacement_at_each_identity_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    # Requirement: neither an early positive identity nor a fresh snapshot may be replaced.
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    original, saved, foreign = source / "payload", tmp_path / "saved", tmp_path / "foreign"
    original.write_bytes(b"original CAS")
    foreign.write_bytes(b"foreign bytes")
    enumeration = original.lstat()
    scandir, open_file = os.scandir, os.open

    def observe(*, follow_symlinks=False):
        assert not follow_symlinks
        if boundary in ("before-lstat", "symlink-before-lstat"):
            original.rename(saved)
            if boundary == "symlink-before-lstat":
                original.symlink_to(foreign)
            else:
                original.write_bytes(b"replacement")
        return enumeration

    @contextmanager
    def anchored(path):
        with scandir(path) as entries:
            yield (
                SimpleNamespace(name=entry.name, path=entry.path, stat=observe) for entry in entries
            )

    def replace_before_open(path, flags, *args, **kwargs):
        if Path(path) == original and boundary in ("before-open", "symlink-before-open"):
            original.rename(saved)
            if boundary == "symlink-before-open":
                original.symlink_to(foreign)
            else:
                original.write_bytes(b"replacement")
        return open_file(path, flags, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(local_workspace.os, "scandir", anchored)
        context.setattr(local_workspace.os, "open", replace_before_open)
        with pytest.raises((ExecutionSpecError, OSError)) as caught:
            local_workspace._copy_tree(source, destination, source)
    if isinstance(caught.value, OSError):
        assert boundary == "symlink-before-open" and caught.value.errno == errno.ELOOP
    elif boundary == "symlink-before-lstat":
        assert "regular non-link" in str(caught.value)
    else:
        assert "identity changed" in str(caught.value)
    copied = destination / "payload"
    assert not copied.exists() or copied.read_bytes() == b""
    assert saved.read_bytes() == b"original CAS"
    assert foreign.read_bytes() == b"foreign bytes"


@pytest.mark.parametrize(
    ("observation", "changes", "reason"),
    [
        ("enumeration", {"st_dev": 0}, "enumeration identity"),
        ("enumeration", {"st_ino": 0}, "enumeration identity"),
        ("enumeration", {"st_dev": -1}, "enumeration identity"),
        ("enumeration", {"st_ino": -1}, "enumeration identity"),
        ("enumeration", {"st_file_attributes": 0x400}, "reparse point"),
        ("fresh", {"st_dev": 0}, "stable positive"),
        ("fresh", {"st_ino": 0}, "stable positive"),
        ("opened", {"st_dev": 0}, "stable positive"),
        ("opened", {"st_ino": 0}, "stable positive"),
        ("fresh", {"st_mode": stat.S_IFLNK | stat.S_IRUSR}, "regular non-link"),
        ("opened", {"st_mode": stat.S_IFLNK | stat.S_IRUSR}, "regular non-link"),
        ("fresh", {"st_file_attributes": 0x400}, "regular non-link"),
        ("opened", {"st_file_attributes": 0x400}, "regular non-link"),
    ],
)
def test_copy_refuses_invalid_file_observations_before_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observation, changes, reason
) -> None:
    # Requirement: only regular non-reparse files with complete positive IDs authorize copying.
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    original = source / "payload"
    original.write_bytes(b"unchanged CAS")
    info = original.lstat()
    scandir, lstat, fstat = os.scandir, Path.lstat, os.fstat

    @contextmanager
    def observed(path):
        with scandir(path) as entries:
            yield (
                SimpleNamespace(
                    name=entry.name,
                    path=entry.path,
                    stat=lambda *, follow_symlinks=False: _file_metadata(
                        info, **(changes if observation == "enumeration" else {})
                    ),
                )
                for entry in entries
            )

    def fresh(path):
        result = lstat(path)
        if path == original and observation == "fresh":
            return _file_metadata(result, **changes)
        return result

    def opened(descriptor):
        result = fstat(descriptor)
        if observation == "opened":
            return _file_metadata(result, **changes)
        return result

    with monkeypatch.context() as context:
        context.setattr(local_workspace.os, "scandir", observed)
        context.setattr(Path, "lstat", fresh)
        context.setattr(local_workspace.os, "fstat", opened)
        with pytest.raises(ExecutionSpecError, match=reason):
            local_workspace._copy_tree(source, destination, source)
    copied = destination / "payload"
    assert not copied.exists() or copied.read_bytes() == b""
    assert original.read_bytes() == b"unchanged CAS"


@pytest.mark.parametrize("boundary", ["enumeration-fresh", "fresh-opened"])
def test_copy_compares_all_128_inode_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    # Requirement: IDs sharing their low 64 bits cannot authorize a different filesystem object.
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    original = source / "payload"
    original.write_bytes(b"unchanged CAS")
    info = original.lstat()
    first, second = (1 << 100) | 7, (2 << 100) | 7
    scandir, lstat, fstat = os.scandir, Path.lstat, os.fstat

    @contextmanager
    def observed(path):
        with scandir(path) as entries:
            yield (
                SimpleNamespace(
                    name=entry.name,
                    path=entry.path,
                    stat=lambda *, follow_symlinks=False: _file_metadata(info, st_ino=first),
                )
                for entry in entries
            )

    def fresh(path):
        result = lstat(path)
        if path == original:
            return _file_metadata(
                result, st_ino=second if boundary == "enumeration-fresh" else first
            )
        return result

    with monkeypatch.context() as context:
        context.setattr(local_workspace.os, "scandir", observed)
        context.setattr(Path, "lstat", fresh)
        context.setattr(
            local_workspace.os, "fstat", lambda fd: _file_metadata(fstat(fd), st_ino=second)
        )
        with pytest.raises(ExecutionSpecError, match="identity changed"):
            local_workspace._copy_tree(source, destination, source)
    copied = destination / "payload"
    assert not copied.exists() or copied.read_bytes() == b""
    assert original.read_bytes() == b"unchanged CAS"


def test_copy_uses_fresh_permissions_instead_of_enumeration_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: the copied file keeps fresh executable bits and gains owner write access.
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    original = source / "payload"
    original.write_bytes(b"CAS executable")
    original.chmod(stat.S_IRUSR | stat.S_IXUSR)
    info = original.lstat()
    scandir = os.scandir

    @contextmanager
    def old_permissions(path):
        with scandir(path) as entries:
            yield (
                SimpleNamespace(
                    name=entry.name,
                    path=entry.path,
                    stat=lambda *, follow_symlinks=False: _file_metadata(
                        info, st_mode=stat.S_IFREG | stat.S_IRUSR
                    ),
                )
                for entry in entries
            )

    with monkeypatch.context() as context:
        context.setattr(local_workspace.os, "scandir", old_permissions)
        local_workspace._copy_tree(source, destination, source)
    copied = destination / "payload"
    assert copied.read_bytes() == b"CAS executable"
    assert copied.stat().st_mode & stat.S_IWUSR
    if os.name != "nt":
        assert copied.stat().st_mode & stat.S_IXUSR
    assert original.read_bytes() == b"CAS executable"


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


async def test_delete_warning_scrubs_registered_path_secret(
    managed, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Requirement: failed verified deletion retains storage and scrubs the exception's path.
    backend, _run_spec, lease = managed
    secret = "registered-workspace-path-secret"
    redactor = RunRedactor()
    redactor.register([secret])
    backend._managed[lease.lease_id].controller.scrub = redactor.scrub
    await _release(backend, lease)

    def denied(path: Path) -> None:
        raise PermissionError(13, "workspace cleanup denied", str(path / secret))

    with monkeypatch.context() as patch:
        patch.setattr(local_workspace.shutil, "rmtree", denied)
        await backend.finalize_run(lease, "succeeded")
    assert Path(lease.location).is_dir()
    formatted = "\n".join(logging.Formatter().format(record) for record in caplog.records)
    assert secret not in formatted
    assert REDACTED_MARKER in formatted and "workspace cleanup denied" in formatted


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


def _installation_socket_fd(address: str) -> int:
    """Find the actual owned socket by its kernel inode, independent of CPython internals."""
    socket_inode = next(
        int(fields[6])
        for line in Path("/proc/net/unix").read_text().splitlines()[1:]
        if (fields := line.split())[-1] == address
    )
    descriptors = []
    for path in list(Path("/proc/self/fd").iterdir()):
        fd = int(path.name)
        try:
            info = os.fstat(fd)
        except OSError as error:
            assert error.errno == errno.EBADF
            continue
        if stat.S_ISSOCK(info.st_mode) and info.st_ino == socket_inode:
            descriptors.append(fd)
    assert len(descriptors) == 1
    return descriptors[0]


def _installation_fd_live(fd, identity):
    try:
        info = os.fstat(fd)
    except OSError as error:
        assert error.errno == errno.EBADF
        return False
    return (info.st_dev, info.st_ino) == identity


@pytest.fixture
def installation_resources(monkeypatch):
    if sys.platform != "linux":
        pytest.skip("Requires Linux procfs native descriptor observation")
    listeners = []
    threads = []
    real_listener = scope_launcher.Listener
    real_thread = threading.Thread

    def listener(*args, **kwargs):
        result = real_listener(*args, **kwargs)
        address = result.address
        assert isinstance(address, str)
        fd = _installation_socket_fd(address)
        info = os.fstat(fd)
        directory_fd = int(address.split("/fd/")[1].split("/")[0])
        directory_info = os.fstat(directory_fd)
        listeners.append(
            (
                result,
                result.close,
                fd,
                (info.st_dev, info.st_ino),
                directory_fd,
                (directory_info.st_dev, directory_info.st_ino),
                address,
            )
        )
        return result

    def thread(*args, **kwargs):
        result = real_thread(*args, **kwargs)
        threads.append((result, kwargs.get("target"), kwargs.get("args", ())))
        return result

    monkeypatch.setattr(scope_launcher, "Listener", listener)
    monkeypatch.setattr(threading, "Thread", thread)
    try:
        yield listeners, threads
    finally:
        # Test-owned recovery is deliberately separate from product cleanup evidence.
        for native, target, _args in threads:
            owner = getattr(target, "__self__", None)
            if isinstance(owner, process_core.LocalProcessScopeController):
                owner._closed = True
            if isinstance(owner, process_core._ControllerInstallation):
                owner.aborted = True
                owner.gate.set()
            if native.is_alive():
                for _listener, _close, _fd, _identity, _dfd, _did, address in listeners:
                    try:
                        with scope_launcher.connect({"endpoint": address}, authenticate=False):
                            pass
                    except OSError:
                        pass
                native.join(2)
            assert not native.is_alive()
        for _listener, close, _fd, _identity, dfd, did, _address in listeners:
            close()
            if _installation_fd_live(dfd, did):
                os.close(dfd)


@pytest.mark.parametrize("fault", ["helper", "platform"])
async def test_installation_preflight_precedes_publication(tmp_path, monkeypatch, fault):
    # Requirement: a deterministic unavailable runtime never publishes a workspace.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    if fault == "helper":
        monkeypatch.setattr(
            process_core.sysconfig, "get_path", lambda _name: str(tmp_path / "missing")
        )
    else:
        monkeypatch.setattr(process_core.sys, "platform", "freebsd")
    with pytest.raises(ExecutionSpecError):
        await LocalRunnerBackend().prepare_run(run)
    assert not Path(run.local_workspace_root).exists()


@pytest.mark.parametrize(
    "fault", ["chmod", "thread-construction", "real-start", "unknown-start", "context", "binding"]
)
async def test_installation_resources_abort(tmp_path, monkeypatch, installation_resources, fault):
    # Requirement: acquired startup resources close before a fresh root can be rolled back.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    listeners, threads = installation_resources
    primary = PermissionError(
        errno.EACCES, "installation-private-primary", "installation-private-filename"
    )
    original_args = primary.args
    original_cause = RuntimeError("installation-private-cause")
    primary.__cause__ = original_cause
    original_chmod = os.chmod
    original_thread = threading.Thread

    def denied(path, *args, **kwargs):
        if str(path).startswith("/proc/") and str(path).endswith("/control"):
            raise primary
        return original_chmod(path, *args, **kwargs)

    def broken_thread(*args, **kwargs):
        if fault == "thread-construction":
            raise primary
        result = original_thread(*args, **kwargs)
        original_start = result.start

        def start():
            if fault == "real-start":
                original_start()
            raise primary

        result.start = start
        return result

    def fail(*args, **kwargs):
        raise primary

    if fault == "chmod":
        monkeypatch.setattr(os, "chmod", denied)
    elif fault in ("thread-construction", "real-start", "unknown-start"):
        monkeypatch.setattr(threading, "Thread", broken_thread)
    elif fault == "context":
        monkeypatch.setattr(local_backend, "ManagedWorkspaceContext", fail)
    else:
        monkeypatch.setattr(local_backend, "LocalWorkspaceBinding", fail)
    backend = LocalRunnerBackend()
    with pytest.raises(PermissionError) as caught:
        await backend.prepare_run(run)
    assert caught.value is primary and primary.args == original_args
    assert primary.__cause__ is original_cause and primary.__context__ is None
    assert not backend._bindings and not backend._leases and not backend._managed
    assert listeners
    for _listener, _close, fd, identity, dfd, did, _address in listeners:
        assert not _installation_fd_live(fd, identity)
        assert not _installation_fd_live(dfd, did)
    assert all(not thread.is_alive() for thread, _target, _args in threads)
    root = Path(run.local_workspace_root)
    if fault == "unknown-start":
        assert json.loads((root / "identity.json").read_text())["runtime_state"] in (
            "active",
            "uncertain",
        )
    else:
        assert not root.exists()


@pytest.mark.parametrize("after_replace", [False, True])
async def test_installation_active_write_failure_never_starts_controller(
    tmp_path, monkeypatch, installation_resources, after_replace
):
    # Requirement: exception after marker replacement cannot be mistaken for unchanged idle.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    original = local_workspace.set_state
    primary = OSError(errno.EIO, "installation-marker-secret")

    def fence(*args, **kwargs):
        if args[-1] == "active":
            if after_replace:
                original(*args, **kwargs)
            raise primary
        return original(*args, **kwargs)

    monkeypatch.setattr(local_workspace, "set_state", fence)
    backend = LocalRunnerBackend()
    try:
        with pytest.raises(OSError) as caught:
            await backend.prepare_run(run)
        assert caught.value is primary
        assert not installation_resources[0] and not installation_resources[1]
        root = Path(run.local_workspace_root)
        if after_replace:
            assert json.loads((root / "identity.json").read_text())["runtime_state"] == "active"
        else:
            assert not root.exists()
    finally:
        if backend._managed:
            controller = next(iter(backend._managed.values())).controller
            await controller.close()


async def test_installation_double_fault_retains_real_listener_and_fence(
    tmp_path, monkeypatch, installation_resources, caplog
):
    # Requirement: failed disposal and failed uncertain write still block independent attachment.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    original_listener = scope_launcher.Listener
    original_state = local_workspace.set_state
    primary = asyncio.CancelledError("installation-cancellation-secret")
    secondary = OSError(errno.EIO, "installation-cleanup-secret")

    def listener(*args, **kwargs):
        result = original_listener(*args, **kwargs)

        def close():
            raise secondary

        result.close = close
        return result

    def fail(*args, **kwargs):
        raise primary

    def uncertain(*args, **kwargs):
        if args[-1] == "uncertain":
            raise secondary
        return original_state(*args, **kwargs)

    monkeypatch.setattr(scope_launcher, "Listener", listener)
    monkeypatch.setattr(local_backend, "ManagedWorkspaceContext", fail)
    monkeypatch.setattr(local_workspace, "set_state", uncertain)
    backend = LocalRunnerBackend()
    with pytest.raises(asyncio.CancelledError) as caught:
        await backend.prepare_run(run)
    assert caught.value is primary and primary.args == ("installation-cancellation-secret",)
    root = Path(run.local_workspace_root)
    marker = json.loads((root / "identity.json").read_text())
    assert marker["runtime_state"] == "active"
    identity = WorkspaceIdentity("local", marker["lease_id"], marker["incarnation"], str(root))
    with pytest.raises(WorkspaceAttachError):
        await LocalRunnerBackend().attach_run(run, identity)
    assert backend._failed_installations
    assert any(
        _installation_fd_live(fd, identity)
        for _, _, fd, identity, _, _, _ in installation_resources[0]
    )
    assert caplog.records and all(record.exc_info is None for record in caplog.records)
    assert "installation-" not in caplog.text


async def test_installation_attached_abort_restores_idle_and_preserves_modified_work(
    tmp_path, monkeypatch, installation_resources
):
    # Requirement: confirmed attachment abort closes its resources but never deletes user work.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    backend = LocalRunnerBackend()
    lease = await backend.prepare_run(run)
    await _release(backend, lease)
    assert lease.location is not None
    root = Path(lease.location)
    previous_ipc = set(root.glob(".scope-ipc-*"))
    modified = root / "work/main/source"
    modified.write_bytes(b"user modified")

    def fail(*args, **kwargs):
        raise RuntimeError("installation interrupted")

    monkeypatch.setattr(local_backend, "ManagedWorkspaceContext", fail)
    with pytest.raises(RuntimeError):
        await LocalRunnerBackend().attach_run(run, _checkpoint(lease))
    assert modified.read_bytes() == b"user modified"
    assert json.loads((root / "identity.json").read_text())["runtime_state"] == "idle"
    assert set(root.glob(".scope-ipc-*")) == previous_ipc


async def test_installation_fd_allocation_after_side_effect_cannot_be_fake_confirmed(
    tmp_path, monkeypatch
):
    # Requirement: an unreturned native directory fd remains unknown, fenced and retained.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    primary = OSError(errno.EMFILE, "directory-fd allocation failed after side effect")
    real_open = os.open
    acquired = []

    def opened(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if ".scope-ipc-" in str(path):
            info = os.fstat(fd)
            acquired.append((fd, (info.st_dev, info.st_ino)))
            raise primary
        return fd

    monkeypatch.setattr(os, "open", opened)
    backend = LocalRunnerBackend()
    try:
        with pytest.raises(OSError) as caught:
            await backend.prepare_run(run)
        assert caught.value is primary
        assert acquired and all(_installation_fd_live(fd, identity) for fd, identity in acquired)
        marker = json.loads((Path(run.local_workspace_root) / "identity.json").read_text())
        assert marker["runtime_state"] in ("active", "uncertain")
        assert not backend._leases and not backend._bindings
    finally:
        for fd, identity in acquired:
            if _installation_fd_live(fd, identity):
                os.close(fd)


async def test_installation_rollback_uses_original_stage_authority_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation_resources
) -> None:
    # Requirement: confirmed fresh abort deletes only with the original stage-issued objects.
    run = _run(tmp_path, monkeypatch)
    assert run.bundle is not None and run.local_workspace_root is not None
    source = Path(run.bundle.store_path) / "tree"
    before = {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino)
        for path in source.rglob("source")
    }
    backend = LocalRunnerBackend()
    issued = []
    deleted = []
    real_stage = local_workspace.stage
    real_delete = local_workspace.delete
    primary = OSError(errno.EIO, "assembly failed")

    def stage(run, lease, bundle):
        identity = real_stage(run, lease, bundle)
        issued.append((lease, identity))
        return identity

    def delete(run, lease, bundle, identity):
        assert lease is issued[0][0] and identity is issued[0][1]
        assert local_workspace.verify(run, lease, bundle, previous=identity) == identity
        deleted.append(identity)
        real_delete(run, lease, bundle, identity)

    def fail(*args, **kwargs):
        raise primary

    with monkeypatch.context() as fault:
        fault.setattr(local_workspace, "stage", stage)
        fault.setattr(local_workspace, "delete", delete)
        fault.setattr(local_backend, "ManagedWorkspaceContext", fail)
        with pytest.raises(OSError) as caught:
            await backend.prepare_run(run)
        assert caught.value is primary
    assert len(deleted) == 1 and deleted[0] is issued[0][1]
    assert not Path(run.local_workspace_root).exists()
    assert {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino)
        for path in source.rglob("source")
    } == before
    lease = await backend.prepare_run(run)
    try:
        assert lease.location is not None
        assert lease.incarnation != issued[0][0].incarnation
        assert await backend.prepare_run(run) is lease
        assert (
            json.loads((Path(lease.location) / "identity.json").read_text())["runtime_state"]
            == "active"
        )
        await _release(backend, lease)
        await backend.finalize_run(lease, "succeeded")
    finally:
        if backend._managed:
            await backend._managed[lease.lease_id].controller.close()


@pytest.mark.parametrize("boundary", ["context", "state", "binding", "mappings"])
async def test_installation_assembly_failure_preserves_real_foreign_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation_resources, boundary: str
) -> None:
    # Requirement: assembly abort never publishes its binding or damages a live foreign lease.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    root = Path(run.local_workspace_root)
    other = replace(
        run, run_id="other", local_workspace_root=str(root.parent.parent / "other/local")
    )
    backend = LocalRunnerBackend()
    foreign = await backend.prepare_run(other)
    controller = backend._managed[foreign.lease_id].controller
    primary = MemoryError("candidate assembly failed")
    listeners, _threads = installation_resources
    previous_count = len(listeners)

    class FailingCopy(dict[str, LocalWorkspaceBinding]):
        def copy(self) -> dict[str, LocalWorkspaceBinding]:
            raise primary

    def fail(*args, **kwargs):
        raise primary

    if boundary == "mappings":
        backend._bindings = FailingCopy(backend._bindings)
    registries = backend._leases, backend._managed, backend._bindings
    try:
        with monkeypatch.context() as fault:
            if boundary != "mappings":
                name = {
                    "context": "ManagedWorkspaceContext",
                    "state": "_ManagedLease",
                    "binding": "LocalWorkspaceBinding",
                }[boundary]
                fault.setattr(local_backend, name, fail)
            with pytest.raises(MemoryError) as caught:
                await backend.prepare_run(run)
            assert caught.value is primary
        assert all(
            current is previous
            for current, previous in zip(
                (backend._leases, backend._managed, backend._bindings), registries, strict=True
            )
        )
        assert backend._leases == {"other": foreign}
        assert not root.exists()
        for _listener, _close, fd, identity, dfd, did, _address in listeners[previous_count:]:
            assert not _installation_fd_live(fd, identity)
            assert not _installation_fd_live(dfd, did)
        result = await backend.run_command(
            CommandSpec(sys.executable, ("-c", "print('foreign-live')"), stdin=b""), foreign
        )
        assert result.stdout == "foreign-live\n" and result.exit_code == 0
    finally:
        await _release(backend, foreign)
        await backend.finalize_run(foreign, "succeeded")
        await controller.close()


@pytest.mark.parametrize("fault", ["before-transfer", "after-transfer"])
async def test_installation_unknown_listener_transfer_is_never_fake_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation_resources, fault: str
) -> None:
    # Requirement: an interrupted native listener transfer retains its dependent fd and fence.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    backend = LocalRunnerBackend()
    primary = OSError(errno.EIO, "listener transfer interrupted")
    original_listener = scope_launcher.Listener
    original_transfer = process_core._ControllerInstallation.receive_listener

    def allocate(*args, **kwargs):
        original_listener(*args, **kwargs)
        raise primary

    def transfer(owner, listener):
        original_transfer(owner, listener)
        raise primary

    if fault == "before-transfer":
        monkeypatch.setattr(scope_launcher, "Listener", allocate)
    else:
        monkeypatch.setattr(process_core._ControllerInstallation, "receive_listener", transfer)
    with pytest.raises(OSError) as caught:
        await backend.prepare_run(run)
    assert caught.value is primary
    record = backend._failed_installations[run.run_id]
    assert record.outcome == "unconfirmed"
    assert record.fd is not None
    assert record.directory_identity is not None
    assert os.fstat(record.fd).st_ino == record.directory_identity[1]
    marker = json.loads((Path(run.local_workspace_root) / "identity.json").read_text())
    assert marker["runtime_state"] == "uncertain"
    with pytest.raises(ExecutionSpecError):
        await backend.prepare_run(run)


async def test_installation_late_thread_exit_never_upgrades_unknown_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation_resources
) -> None:
    # Requirement: late native exit cannot upgrade unknown startup or authorize deletion.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    root = Path(run.local_workspace_root)
    original_thread = threading.Thread
    primary = OSError(errno.EAGAIN, "native startup unknown")
    native_starts = []

    def thread(*args, **kwargs):
        result = original_thread(*args, **kwargs)
        native_starts.append(result.start)

        def start():
            raise primary

        result.start = start
        return result

    monkeypatch.setattr(threading, "Thread", thread)
    backend = LocalRunnerBackend()
    with pytest.raises(OSError) as caught:
        await backend.prepare_run(run)
    assert caught.value is primary
    record = backend._failed_installations[run.run_id]
    before = (root / "identity.json").read_bytes()
    native_starts[0]()
    assert record.thread is not None
    record.thread.join(1)
    assert record.entered.is_set() and not record.thread.is_alive()
    assert not Path(f"/proc/self/task/{record.thread.native_id}").exists()
    assert record.abort(primary) == "unconfirmed"
    assert (root / "identity.json").read_bytes() == before
    assert record.outcome == "unconfirmed" and root.exists()


@pytest.mark.parametrize("swap", ["root", "parent", "work", "marker"])
async def test_installation_abort_detected_drift_never_deletes_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation_resources, caplog, swap: str
) -> None:
    # Requirement: observed recovery drift leaves replacement storage and moved data intact.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    root = Path(run.local_workspace_root)
    target = {
        "root": root,
        "parent": root.parent,
        "work": root / "work",
        "marker": root / "identity.json",
    }[swap]
    saved = target.with_name(target.name + "-saved")
    primary = OSError(errno.EIO, "assembly failure")

    def fail(*args, **kwargs):
        target.rename(saved)
        if swap in ("root", "parent"):
            target.mkdir()
            (target / "foreign").write_bytes(b"foreign bytes")
        else:
            target.symlink_to(saved, target_is_directory=swap == "work")
        raise primary

    monkeypatch.setattr(local_backend, "ManagedWorkspaceContext", fail)
    backend = LocalRunnerBackend()
    with pytest.raises(OSError) as caught:
        await backend.prepare_run(run)
    assert caught.value is primary
    assert saved.exists()
    if swap in ("root", "parent"):
        assert (target / "foreign").read_bytes() == b"foreign bytes"
        original_root = saved if swap == "root" else saved / "local"
        assert (original_root / "work/main/source").read_bytes() == b"main"
    else:
        assert target.is_symlink()
    assert not backend._leases and not backend._bindings
    assert "retained" in caplog.text


@pytest.mark.parametrize("after_replace", [False, True])
async def test_installation_idle_restore_failure_never_retries_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation_resources, after_replace: bool
) -> None:
    # Requirement: an unreturned idle restore never authorizes destructive recovery by inference.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    primary = OSError(errno.EIO, "primary startup failure")
    original_state = local_workspace.set_state

    def restore(*args, **kwargs):
        if args[-1] == "idle":
            if after_replace:
                original_state(*args, **kwargs)
            raise PermissionError(errno.EACCES, "secondary restore failure")
        return original_state(*args, **kwargs)

    def fail(*args, **kwargs):
        raise primary

    monkeypatch.setattr(local_workspace, "set_state", restore)
    monkeypatch.setattr(local_backend, "ManagedWorkspaceContext", fail)
    with pytest.raises(OSError) as caught:
        await LocalRunnerBackend().prepare_run(run)
    assert caught.value is primary
    root = Path(run.local_workspace_root)
    assert (root / "work/main/source").read_bytes() == b"main"
    marker = json.loads((root / "identity.json").read_text())
    assert marker["runtime_state"] == ("idle" if after_replace else "active")
    for _listener, _close, fd, identity, dfd, did, _address in installation_resources[0]:
        assert not _installation_fd_live(fd, identity)
        assert not _installation_fd_live(dfd, did)


@pytest.mark.parametrize("kind", ["oserror", "cancel", "custom"])
async def test_installation_primary_survives_partial_delete_with_safe_formatted_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation_resources, caplog, kind: str
) -> None:
    # Requirement: partial rmtree failure preserves the exact primary and never logs its data.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None and run.bundle is not None
    root = Path(run.local_workspace_root)
    secret = "registered-installation-private-value"

    class StartupFailure(RuntimeError):
        pass

    primary = {
        "oserror": OSError(errno.EIO, secret, secret),
        "cancel": asyncio.CancelledError(secret),
        "custom": StartupFailure(secret),
    }[kind]
    cause = RuntimeError(secret)
    context = ValueError(secret)
    primary.__cause__ = cause
    primary.__context__ = context
    original_args = primary.args
    original_errno = getattr(primary, "errno", None)
    original_filename = getattr(primary, "filename", None)
    destructive_attempts = []

    def fail(*args, **kwargs):
        raise primary

    def incomplete(*args, **kwargs):
        destructive_attempts.append(args[-1])
        (root / "work/main/source").unlink()
        raise PermissionError(errno.EACCES, secret, secret)

    monkeypatch.setattr(local_backend, "ManagedWorkspaceContext", fail)
    monkeypatch.setattr(local_workspace, "delete", incomplete)
    with pytest.raises(type(primary)) as caught:
        await LocalRunnerBackend().prepare_run(run)
    assert caught.value is primary and primary.args == original_args
    assert getattr(primary, "errno", None) == original_errno
    assert getattr(primary, "filename", None) == original_filename
    assert primary.__cause__ is cause and primary.__context__ is context
    assert root.exists() and not (root / "work/main/source").exists()
    assert (Path(run.bundle.store_path) / "tree/main/source").read_bytes() == b"main"
    assert len(destructive_attempts) == 1
    assert any("rollback incomplete" in record.getMessage() for record in caplog.records)
    for record in caplog.records:
        assert secret not in logging.Formatter("%(levelname)s %(message)s").format(record)
        assert record.exc_info is None and record.stack_info is None


async def test_installation_logging_failure_cannot_replace_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation_resources
) -> None:
    # Requirement: a failing warning handler cannot replace startup error or expose secondary text.
    run = _run(tmp_path, monkeypatch)
    assert run.local_workspace_root is not None
    primary = OSError(errno.EIO, "private primary", "private filename")

    class BrokenSink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise asyncio.CancelledError("private secondary sink failure")

    def fail(*args, **kwargs):
        raise primary

    handler = BrokenSink()
    process_core._log.addHandler(handler)
    monkeypatch.setattr(local_backend, "ManagedWorkspaceContext", fail)
    try:
        with pytest.raises(OSError) as caught:
            await LocalRunnerBackend().prepare_run(run)
        assert caught.value is primary
        assert primary.args == (errno.EIO, "private primary")
        assert primary.filename == "private filename" and primary.__context__ is None
        assert primary.__notes__ == [
            "Managed local installation diagnostic could not be delivered."
        ]
        assert not Path(run.local_workspace_root).exists()
    finally:
        process_core._log.removeHandler(handler)
