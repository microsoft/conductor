"""Managed MCP regressions through the native SDK and installed process helper."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp import ClientSession
from mcp.shared.exceptions import McpError

from conductor.execution.errors import ExecutionSpecError
from conductor.execution.local_process_scope import LocalProcessScopeController
from conductor.execution.types import ManagedWorkspaceContext
from conductor.mcp import manager as manager_module
from conductor.mcp.manager import MCPManager

FIXTURE = Path(__file__).with_name("managed_mcp_fixture.py")


class ControlledTimer:
    def __init__(self, at: float, callback: Callable[[], None]) -> None:
        self.at, self.callback, self.cancelled = at, callback, False

    def cancel(self) -> None:
        self.cancelled = True


class ControlledClock:
    """Advance only the cleanup driver, never real SDK readiness."""

    def __init__(self) -> None:
        self.value = asyncio.get_running_loop().time()
        self.timers: list[ControlledTimer] = []

    def now(self) -> float:
        return self.value

    def at(self, deadline: float, callback: Callable[[], None]) -> ControlledTimer:
        timer = ControlledTimer(deadline, callback)
        self.timers.append(timer)
        return timer

    def advance(self, value: float) -> None:
        self.value = value
        due = [timer for timer in self.timers if timer.at <= value and not timer.cancelled]
        for timer in sorted(due, key=lambda item: item.at):
            timer.cancelled = True
            timer.callback()


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path) -> Iterator[None]:
    """Isolate the parent too; native stdio deliberately filters inheritance."""
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "CONDUCTOR_HOME": str(home / "conductor"),
        "TMPDIR": str(tmp_path),
        "MCP_MANAGED_AMBIENT": "must-not-be-inherited",
    }
    for name in ("SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT"):
        if name in os.environ:
            environment[name] = os.environ[name]
    with patch.dict(os.environ, environment, clear=True):
        yield


@asynccontextmanager
async def runtime(
    tmp_path: Path, clock: ControlledClock | None = None
) -> AsyncIterator[tuple[LocalProcessScopeController, ManagedWorkspaceContext, Path]]:
    root = tmp_path / "owned"
    work = root / "work"
    work.mkdir(parents=True)
    (work / "shared.txt").write_text("staged mutable bytes", encoding="utf-8")
    controller = LocalProcessScopeController(str(root), "inc", _scheduler=clock)
    try:
        yield controller, ManagedWorkspaceContext(str(root), "inc", controller), work
    finally:
        await controller.close()


async def connect(manager: MCPManager, work: Path, name: str = "fs", mode: str = "success") -> None:
    await manager.connect_server(
        name,
        sys.executable,
        ["-u", str(FIXTURE), mode, str(work / f"{name}-eof.json"), "static arg; $(false)"],
        env={"MCP_MANAGED_OVERLAY": "private-overlay", "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=str(work),
        redact_errors=True,
    )


async def completed(task: asyncio.Task[None]) -> None:
    done, pending = await asyncio.wait({task}, timeout=10)
    assert done == {task} and not pending


async def test_real_connection_preserves_launch_fields_and_joins_proofs(tmp_path: Path) -> None:
    # Requirement: native stdio shares staged cwd/env/argv and closes only its own family.
    async with runtime(tmp_path) as (controller, context, work):
        first = MCPManager(managed_workspace=context)
        second = MCPManager(managed_workspace=context)
        try:
            assert len(controller.owners.tasks) == 2
            await connect(first, work)
            await connect(second, work, "sibling")
            before = controller.proofs
            result = json.loads(await first.call_tool("fs__inspect", {"text": "DONE SCOPE_EMPTY"}))
            assert result["cwd"] == str(work)
            assert result["file"] == "staged mutable bytes"
            assert result["argv"] == ["static arg; $(false)"]
            assert result["overlay"] == "private-overlay"
            assert result["ambient"] is None
            assert result["home"] == os.environ["HOME"]
            assert len(controller.proofs) == len(before) == 2

            await first.close(strict=True)

            first_proof = next(
                p for p in controller.proofs if p.invocation_id == before[0].invocation_id
            )
            assert first_proof.done and first_proof.kernel_empty and first_proof.streams_drained
            assert first_proof.facade_exited and first_proof.custodian_reaped
            assert "FORCE_STOP" not in first_proof.events
            assert (work / "fs-eof.json").is_file()
            assert not (work / "sibling-eof.json").exists()
            sibling = json.loads(await second.call_tool("sibling__inspect", {}))
            assert sibling["file"] == result["file"]
            await first.close(strict=True)
        finally:
            await first.close(strict=True)
            await second.close(strict=True)
        assert (await controller.await_release()).status == "released"
        assert not controller.owners.tasks


@pytest.mark.parametrize("mode", ["connect-error", "initialize-error", "malformed"])
async def test_clean_failed_startup_is_not_false_uncertainty(tmp_path: Path, mode: str) -> None:
    # Requirement: real failed initialization/discovery has a primary cause and clean release.
    async with runtime(tmp_path) as (controller, context, work):
        manager = MCPManager(managed_workspace=context)
        with pytest.raises(RuntimeError) as caught:
            await connect(manager, work, mode=mode)
        assert caught.value.__cause__ is not None
        if mode == "initialize-error":
            assert isinstance(caught.value.__cause__, McpError)
            assert caught.value.__cause__.error.code == -32001
        assert manager.sessions == {}

        await manager.close(strict=True)

        assert not manager._cleanup_uncertain
        assert (await controller.await_release()).status == "released"
        assert controller.proofs and all(p.custodian_reaped for p in controller.proofs)
        assert (work / "fs-eof.json").is_file()


async def test_real_cancelled_startup_joins_before_strict_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: cancellation after real initialize preserves primary cancellation and proof.
    initialized = asyncio.Event()
    blocked = asyncio.Event()
    original = ClientSession.initialize

    async def initialize(session: ClientSession):
        result = await original(session)
        initialized.set()
        await blocked.wait()
        return result

    monkeypatch.setattr(ClientSession, "initialize", initialize)
    async with runtime(tmp_path) as (controller, context, work):
        manager = MCPManager(managed_workspace=context)
        connection = asyncio.create_task(connect(manager, work))
        try:
            await asyncio.wait_for(initialized.wait(), 10)

            connection.cancel("primary connect cancellation")
            await completed(connection)
            with pytest.raises(asyncio.CancelledError) as caught:
                connection.result()
            assert caught.value.args == ("primary connect cancellation",)
        finally:
            blocked.set()
            await manager.close(strict=True)
        assert not manager._cleanup_uncertain
        assert (await controller.await_release()).status == "released"
        assert all(p.done and p.facade_exited and p.custodian_reaped for p in controller.proofs)


async def test_native_api_exit_error_requires_full_proof_before_warning_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Requirement: API exit errors with full proof and joined owners remain visible warnings.
    original = manager_module.stdio_client
    assert original is not None
    failure = RuntimeError("native cleanup failed")

    @asynccontextmanager
    async def failing_exit(params):
        async with original(params) as streams:
            yield streams
        raise failure

    monkeypatch.setattr(manager_module, "stdio_client", failing_exit)
    async with runtime(tmp_path) as (controller, context, work):
        manager = MCPManager(managed_workspace=context)
        with pytest.raises(RuntimeError) as caught:
            await connect(manager, work, mode="initialize-error")
        assert isinstance(caught.value.__cause__, McpError)
        await manager.close(strict=True)
        assert manager.sessions == {}
        owner = next(iter(manager._owners.values()))
        assert owner.cleanup_error is failure and not owner.resources
        assert not manager._cleanup_uncertain
        assert any(record.levelname == "WARNING" for record in caplog.records)
        await manager.close(strict=True)
        assert all(p.done and p.custodian_reaped for p in controller.proofs)


async def test_native_join_retains_handles_until_invocation_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a joined native API owner cannot discard handles before process proof.
    async with runtime(tmp_path) as (controller, context, work):
        manager = MCPManager(managed_workspace=context)
        await connect(manager, work)
        entered, release = asyncio.Event(), asyncio.Event()
        original = controller.await_release

        async def gated_proof(invocation_id: str | None = None):
            entered.set()
            await release.wait()
            return await original(invocation_id)

        monkeypatch.setattr(controller, "await_release", gated_proof)
        closing = asyncio.create_task(manager.close(strict=True))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            task, owner = next(iter(manager._owners.items()))
            assert task.done() and owner.cleanup_complete
            assert owner.resources
        finally:
            release.set()
            await completed(closing)
            closing.result()
        assert not owner.resources


async def test_repeated_close_preserves_original_absolute_deadline(tmp_path: Path) -> None:
    # Requirement: completed process proof does not erase the original teardown deadline.
    async with runtime(tmp_path) as (controller, context, work):
        manager = MCPManager(managed_workspace=context)
        await connect(manager, work)
        owner = next(iter(manager._owners.values()))
        deadline = asyncio.get_running_loop().time() + 20
        controller.begin_teardown(owner.invocation_id, deadline=deadline)

        await manager.close(strict=True)
        await manager.close(strict=True)

        assert owner.deadline == deadline
        assert (await controller.await_release()).status == "released"


async def test_original_deadline_bounds_resistant_owner_and_late_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: the earlier controller deadline bounds real teardown without a lucky retry.
    clock = ControlledClock()
    exited, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Queue[None]()
    original = manager_module.stdio_client
    assert original is not None

    @asynccontextmanager
    async def resistant_exit(params):
        async with original(params) as streams:
            yield streams
        exited.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.put_nowait(None)

    monkeypatch.setattr(manager_module, "stdio_client", resistant_exit)
    async with runtime(tmp_path, clock) as (controller, context, work):
        manager = MCPManager(managed_workspace=context)
        await connect(manager, work)
        task = next(iter(manager._owners))
        invocation = manager._owners[task].invocation_id
        deadline = clock.now() + 3
        controller.begin_teardown(deadline=deadline)
        closing = asyncio.create_task(manager.close(strict=True))
        try:
            await asyncio.wait_for(exited.wait(), 10)
            for _ in range(2):
                task.cancel()
                await asyncio.wait_for(interrupted.get(), 10)
            assert controller.owners.deadline(invocation) == deadline

            clock.advance(deadline)
            await completed(closing)
            with pytest.raises(RuntimeError):
                closing.result()
            assert not task.done()
            assert controller.owners.quarantined
        finally:
            release.set()
            await completed(task)
            task.result()
            assert manager._managed_owner is not None
            await completed(manager._managed_owner)
        with pytest.raises(RuntimeError):
            await manager.close(strict=True)
        assert (await controller.await_release()).status == "unconfirmed"
        assert not controller.owners.tasks
        assert (work / "fs-eof.json").is_file()


async def test_failed_startup_with_unconfirmed_teardown_keeps_primary_and_sticky_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: failed initialization plus unjoined cleanup stays uncertain after late failure.
    clock = ControlledClock()
    exited, release = asyncio.Event(), asyncio.Event()
    original = manager_module.stdio_client
    assert original is not None
    failure = RuntimeError("late native teardown error")

    @asynccontextmanager
    async def failed_exit(params):
        async with original(params) as streams:
            yield streams
        exited.set()
        await release.wait()
        raise failure

    monkeypatch.setattr(manager_module, "stdio_client", failed_exit)
    async with runtime(tmp_path, clock) as (controller, context, work):
        manager = MCPManager(managed_workspace=context)
        connection = asyncio.create_task(connect(manager, work, mode="initialize-error"))
        try:
            await asyncio.wait_for(exited.wait(), 10)
            deadline = clock.now() + 2
            controller.begin_teardown(deadline=deadline)

            clock.advance(deadline)
            await completed(connection)
            with pytest.raises(RuntimeError) as caught:
                connection.result()
            assert isinstance(caught.value.__cause__, McpError)
            assert caught.value.__cause__.error.code == -32001
            assert manager.sessions == {}
            with pytest.raises(RuntimeError):
                await manager.close(strict=True)
        finally:
            release.set()
            task, owner = next(iter(manager._owners.items()))
            await completed(task)
            with pytest.raises(RuntimeError) as cleanup:
                task.result()
            assert cleanup.value.__cause__ is failure
            assert manager._managed_owner is not None
            await completed(manager._managed_owner)
        assert owner.cleanup_error is failure
        assert manager._cleanup_uncertain
        with pytest.raises(RuntimeError):
            await manager.close(strict=True)
        assert (await controller.await_release()).status == "unconfirmed"
        assert not controller.owners.tasks


async def test_redacted_error_does_not_leak_through_controller_owner_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Requirement: shared owner diagnostics honor MCP's value-free failure logging policy.
    async with runtime(tmp_path) as (controller, context, work):
        manager = MCPManager(managed_workspace=context)
        with pytest.raises(RuntimeError) as caught:
            await connect(manager, work, mode="secret-error")
        assert isinstance(caught.value.__cause__, McpError)
        assert caught.value.__cause__.error.message == "private-overlay"

        await manager.close(strict=True)

        assert "private-overlay" not in caplog.text
        assert (await controller.await_release()).status == "released"


async def test_manager_is_owned_before_zero_success_pool_insertion(tmp_path: Path) -> None:
    # Requirement: an attempted manager is registered at construction, before any startup yield.
    async with runtime(tmp_path) as (controller, context, _):
        manager = MCPManager(managed_workspace=context)
        assert manager.managed_workspace is context
        assert manager._managed_owner is not None
        assert controller.owners.tasks[id(manager._managed_owner)].task is manager._managed_owner
        controller.begin_teardown()

        await manager.close(strict=True)

        assert manager._managed_owner.done()
        assert (await controller.await_release()).status == "released"
        with pytest.raises(ExecutionSpecError):
            MCPManager(managed_workspace=context)


async def test_unmapped_cwd_is_rejected_without_payload(tmp_path: Path) -> None:
    # Requirement: managed callers cannot silently fall back to the host cwd.
    async with runtime(tmp_path) as (controller, context, _):
        manager = MCPManager(managed_workspace=context)
        with pytest.raises(RuntimeError) as caught:
            await manager.connect_server("bad", sys.executable, ["-c", "raise AssertionError"])
        assert isinstance(caught.value.__cause__, ExecutionSpecError)

        await manager.close(strict=True)

        assert not controller.proofs
        assert (await controller.await_release()).status == "released"


async def test_clean_old_attempt_cannot_erase_replacement_connection(tmp_path: Path) -> None:
    # Requirement: replacing a clean failed attempt uses a fresh family and keeps the new session.
    async with runtime(tmp_path) as (controller, context, work):
        manager = MCPManager(managed_workspace=context)
        try:
            with pytest.raises(RuntimeError):
                await connect(manager, work, mode="initialize-error")
            old_task, old_owner = next(iter(manager._owners.items()))
            assert old_task.done() and old_owner.cleanup_complete
            await connect(manager, work)
            new_task = manager._connection_tasks["fs"]

            manager._observe_owner(old_task)

            assert manager._connection_tasks["fs"] is new_task
            assert json.loads(await manager.call_tool("fs__inspect", {}))["cwd"] == str(work)
            assert len({p.invocation_id for p in controller.proofs}) == 2
        finally:
            await manager.close(strict=True)
        assert (await controller.await_release()).status == "released"


async def test_cancelled_managed_close_does_not_seal_sibling_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: repeated close interruptions keep the original deadline and a live sibling.
    original = manager_module.stdio_client
    assert original is not None
    exiting, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def gated_exit(params):
        async with original(params) as streams:
            yield streams
        exiting.set()
        await release.wait()

    async with runtime(tmp_path) as (controller, context, work):
        first, sibling = (
            MCPManager(managed_workspace=context),
            MCPManager(managed_workspace=context),
        )
        await connect(first, work)
        await connect(sibling, work, "sibling")
        # The original contexts are captured by connect; gate only a new owned connection.
        monkeypatch.setattr(manager_module, "stdio_client", gated_exit)
        await connect(first, work, "gated")
        closing = asyncio.create_task(first.close(strict=True))
        try:
            await asyncio.wait_for(exiting.wait(), 10)
            deadlines = [owner.deadline for owner in first._owners.values()]
            for _ in range(2):
                resumed = asyncio.Event()
                asyncio.get_running_loop().call_soon(resumed.set)
                closing.cancel()
                await resumed.wait()
            assert [owner.deadline for owner in first._owners.values()] == deadlines
            assert controller.owners.deadline() is not None
            assert controller.owners.mutations_allowed(None)
            assert json.loads(await sibling.call_tool("sibling__inspect", {}))["cwd"] == str(work)
        finally:
            release.set()
            await completed(closing)
            with pytest.raises(asyncio.CancelledError):
                closing.result()
            await first.close(strict=True)
            await sibling.close(strict=True)
        assert (await controller.await_release()).status == "released"
