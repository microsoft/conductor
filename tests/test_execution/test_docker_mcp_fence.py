"""Preparation owners and sticky admission under deterministic daemon races."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace

import pytest

from conductor.execution.docker import DockerRunnerBackend, _LeaseState
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import (
    McpServerSpec,
    ResolvedExecutionSpec,
    RunSpec,
    WorkspaceIdentity,
    WorkspaceLease,
)

from .test_docker_mcp import FakeRealm


@pytest.mark.parametrize("attach", [False, True])
async def test_preparation_attempt_precedes_acquire_and_abort_is_idempotent(
    fake_realm: FakeRealm, mcp_run: RunSpec, monkeypatch: pytest.MonkeyPatch, attach: bool
) -> None:
    # Requirement: even failed prepare/attach has a reachable owner before daemon acquisition.
    backend, log, store = fake_realm
    attempts: list[tuple[WorkspaceLease, Callable[[float], Awaitable[None]]]] = []

    def register(lease: WorkspaceLease, release: Callable[[float], Awaitable[None]]) -> None:
        assert not log.exists()
        attempts.append((lease, release))

    async def refuse_volume(_lease: WorkspaceLease, _state: _LeaseState) -> bool:
        raise ExecutionSpecError("volume verification refused")

    monkeypatch.setattr(backend, "_ensure_volume", refuse_volume)
    run = replace(mcp_run, on_prepare_attempt=register)
    with pytest.raises(ExecutionSpecError, match="volume verification"):
        if attach:
            await backend.attach_run(run, WorkspaceIdentity("docker", run.run_id, "existing"))
        else:
            await backend.prepare_run(run)
    assert len(attempts) == 1
    lease, abort = attempts[0]
    state = json.loads(store.read_text())
    guard = next(iter(state["containers"].values()))
    assert guard["Volume"] is None and guard["State"]["Status"] == "created"
    deadline = asyncio.get_running_loop().time() + 5
    await abort(deadline)
    await abort(deadline + 100)
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert rows.count(["rm", "-f", guard["Id"]]) == 1
    assert not any(row[0] == "start" for row in rows)
    assert json.loads(store.read_text())["containers"] == {}
    mcp = backend._lease_states[backend._lease_key(lease)].mcp
    assert mcp is not None and mcp.deadline == deadline


@pytest.mark.parametrize("expired", [False, True])
async def test_late_acquire_after_new_controller_release_never_touches_workspace(
    fake_realm: FakeRealm,
    mcp_run: RunSpec,
    monkeypatch: pytest.MonkeyPatch,
    expired: bool,
) -> None:
    # Requirement: sealed late acquisition cannot inspect; its root owner retains cleanup custody.
    backend, log, store = fake_realm
    original = backend._run_docker
    entered, release = asyncio.Event(), asyncio.Event()
    attempts: list[tuple[WorkspaceLease, Callable[[float], Awaitable[None]]]] = []
    sealed = False

    def admission() -> None:
        if sealed:
            raise ExecutionSpecError("sealed")

    async def delayed_create(
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        timeout: float | None = 300.0,
        diagnostics: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        if argv[0] == "create":
            entered.set()
            await release.wait()
        return await original(
            argv, stdin_bytes=stdin_bytes, timeout=timeout, diagnostics=diagnostics
        )

    monkeypatch.setattr(backend, "_run_docker", delayed_create)
    run = replace(
        mcp_run,
        check_mcp_admission=admission,
        on_prepare_attempt=lambda lease, abort: attempts.append((lease, abort)),
    )
    preparation = asyncio.create_task(backend.prepare_run(run))
    await asyncio.wait_for(entered.wait(), 5)
    assert len(attempts) == 1
    preparation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await preparation
    sealed = True
    deadline = asyncio.get_running_loop().time() + (-1 if expired else 30)
    if expired:
        with pytest.raises(ExecutionSpecError, match="unconfirmed"):
            await attempts[0][1](deadline)
    second = DockerRunnerBackend("docker")
    new_lease = await second.prepare_run(mcp_run)
    await second.finalize_run(new_lease, "succeeded")
    before = len(log.read_text().splitlines())
    release.set()
    owned = backend._lease_states[backend._lease_key(attempts[0][0])].mcp
    assert owned is not None and owned.preparation is not None
    with pytest.raises(ExecutionSpecError, match="sealed"):
        await asyncio.wait_for(owned.preparation, 5)
    late = [json.loads(line)["argv"] for line in log.read_text().splitlines()[before:]]
    assert [row[0] for row in late] == ["create"]
    state = json.loads(store.read_text())
    assert state["volumes"] == {} and len(state["containers"]) == 1
    guard = state["containers"][owned.guard.name]
    assert owned.guard.attempted and owned.guard.identifier is None
    assert all(guard["Config"]["Labels"].get(k) == v for k, v in owned.guard.labels.items())
    if expired:
        with pytest.raises(ExecutionSpecError, match="unconfirmed"):
            await attempts[0][1](deadline + 100)
        assert owned.uncertain and owned.guard.uncertain
        assert json.loads(store.read_text()) == state
        assert len(log.read_text().splitlines()) == before + 1
    else:
        await attempts[0][1](deadline)
        await attempts[0][1](deadline + 100)
        rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()[before + 1 :]]
        assert rows[0] == ["inspect", "--format", "{{json .}}", owned.guard.name]
        assert rows.count(["rm", "-f", guard["Id"]]) == 1
        assert ["rm", "-f", owned.guard.name] not in rows
        assert owned.guard.identifier == guard["Id"] and owned.guard.released
        assert json.loads(store.read_text())["containers"] == {}
    assert owned.deadline == deadline


async def test_sealed_staging_cannot_copy_after_image_await(
    fake_realm: FakeRealm,
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: shielded staging rechecks sticky admission immediately after awaited work.
    backend, log, _store = fake_realm
    sealed = False

    def admission() -> None:
        if sealed:
            raise ExecutionSpecError("sealed")

    lease = await backend.prepare_run(replace(mcp_run, check_mcp_admission=admission))
    original = backend._ensure_image
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_image(
        execution: ResolvedExecutionSpec, diagnostics: Callable[[str], None] | None
    ) -> None:
        await original(execution, diagnostics)
        entered.set()
        await release.wait()

    monkeypatch.setattr(backend, "_ensure_image", held_image)
    opening = asyncio.create_task(backend.open_mcp_stdio(mcp_spec, lease))
    await asyncio.wait_for(entered.wait(), 5)
    sealed = True
    release.set()
    with pytest.raises(ExecutionSpecError, match="sealed"):
        await opening
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert not any(row[0] == "cp" for row in rows)
    await backend.finalize_run(lease, "failed")


async def test_replacement_after_inspection_removes_only_original_id(
    fake_realm: FakeRealm,
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a name recycled after ownership inspection cannot redirect immutable-ID rm.
    backend, _log, store = fake_realm
    original = backend._run_docker
    target_name = ""
    armed = False

    async def replace_after_inspect(
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        timeout: float | None = 300.0,
        diagnostics: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        result = await original(
            argv, stdin_bytes=stdin_bytes, timeout=timeout, diagnostics=diagnostics
        )
        if armed and argv[0] == "inspect" and argv[-1] == target_name:
            state = json.loads(store.read_text())
            owned = state["containers"].pop(target_name)
            state["containers"]["renamed-original"] = owned
            state["containers"][target_name] = {
                "Id": "b" * 64,
                "Name": "/" + target_name,
                "Config": {"Labels": {}},
                "State": {"Status": "running"},
            }
            store.write_text(json.dumps(state))
        return result

    monkeypatch.setattr(backend, "_run_docker", replace_after_inspect)
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    target_name = target.identity
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    await target.validate_started()
    armed = True
    await target.aclose()
    state = json.loads(store.read_text())
    assert state["containers"][target_name]["Id"] == "b" * 64
    assert "renamed-original" not in state["containers"]


async def test_repeated_interruptions_share_one_target_cleanup(
    fake_realm: FakeRealm,
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: repeated cancellation cannot abandon or duplicate an owned cleanup.
    backend, log, store = fake_realm
    original = backend._run_docker
    entered, release = asyncio.Event(), asyncio.Event()
    identifier = ""

    async def held_remove(
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        timeout: float | None = 300.0,
        diagnostics: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        if tuple(argv) == ("rm", "-f", identifier):
            entered.set()
            await release.wait()
        return await original(
            argv, stdin_bytes=stdin_bytes, timeout=timeout, diagnostics=diagnostics
        )

    monkeypatch.setattr(backend, "_run_docker", held_remove)
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    await target.validate_started()
    identifier = json.loads(store.read_text())["containers"][target.identity]["Id"]
    closing = asyncio.create_task(target.aclose())
    await asyncio.wait_for(entered.wait(), 5)
    closing.cancel()
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await target.aclose()
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert rows.count(["rm", "-f", identifier]) == 1
    await backend.finalize_run(lease, "succeeded")
