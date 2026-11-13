"""Managed Copilot ownership through the real SDK and installed scope helper."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from conductor.config.schema import AgentDef, ProviderSettings
from conductor.exceptions import ProviderError
from conductor.execution.local_process_scope import LocalProcessScopeController
from conductor.execution.types import ManagedWorkspaceContext
from conductor.providers.copilot import CopilotProvider, RetryConfig
from conductor.redaction import REDACTED_MARKER, RunRedactor

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Native Linux SDK custody fixture")


def agent(**kwargs: Any) -> AgentDef:
    return AgentDef(
        name="a",
        prompt="p",
        timeout_seconds=None,
        max_session_seconds=None,
        max_agent_iterations=None,
        **kwargs,
    )


class Timer:
    def __init__(self, deadline: float, callback: Callable[[], None]) -> None:
        self.deadline, self.callback, self.cancelled = deadline, callback, False

    def cancel(self) -> None:
        self.cancelled = True


class Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.timers: list[Timer] = []

    def now(self) -> float:
        return self.value

    def at(self, deadline: float, callback: Callable[[], None]) -> Timer:
        timer = Timer(deadline, callback)
        self.timers.append(timer)
        return timer

    def advance(self, value: float) -> None:
        self.value = value
        for timer in sorted(self.timers, key=lambda item: item.deadline):
            if not timer.cancelled and timer.deadline <= value:
                timer.cancelled = True
                timer.callback()


@pytest.fixture
async def wire_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    endpoint = f"/proc/{os.getpid()}/fd/{directory}/ready"
    clients: set[asyncio.Task[None]] = set()

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        clients.add(task)
        try:
            queue.put_nowait(json.loads(await reader.readline()))
        finally:
            writer.close()
            await writer.wait_closed()
            clients.remove(task)

    server = await asyncio.start_unix_server(receive, path=endpoint)
    monkeypatch.setenv("COPILOT_FIXTURE_ENDPOINT", endpoint)
    try:
        yield queue
    finally:
        server.close()
        await server.wait_closed()
        if clients:
            await asyncio.gather(*clients)
        os.close(directory)


async def method_received(queue: asyncio.Queue[dict[str, Any]], method: str) -> None:
    async with asyncio.timeout(10):
        while (await queue.get()).get("method") != method:
            pass


@pytest.fixture
async def workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ManagedWorkspaceContext]:
    """Keep every native process, config and artifact under this test root."""
    root = tmp_path / "root"
    (root / "work").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "COPILOT_HOME"):
        monkeypatch.setenv(name, str(home))
    for name in tuple(os.environ):
        if name.startswith(
            ("GH_", "GITHUB_", "OPENAI_", "COPILOT_PROVIDER_", "CONDUCTOR_LOCAL_SCOPE_")
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("COPILOT_FIXTURE_RECEIPT", str(tmp_path))
    fixture = tmp_path / "rpc-peer"
    fixture.write_text(
        f"#!{sys.executable}\n" + Path(__file__).with_name("managed_copilot_fixture.py").read_text()
    )
    fixture.chmod(0o700)
    monkeypatch.setenv("COPILOT_CLI_PATH", str(fixture))
    controller = LocalProcessScopeController(str(root), "test-incarnation")
    try:
        yield ManagedWorkspaceContext(str(root), "test-incarnation", controller)
    finally:
        controller.begin_teardown()
        await controller.force_stop(tuple(proof.scope_id for proof in controller.proofs))
        await controller.await_release()
        await controller.close()


def receipts(tmp_path: Path) -> list[list[dict[str, Any]]]:
    return [
        [json.loads(line) for line in path.read_text().splitlines()]
        for path in tmp_path.glob("*.jsonl")
    ]


@pytest.fixture
def sdk_processes(
    workspace: ManagedWorkspaceContext, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[subprocess.Popen[bytes]]]:
    """Retain exact real SDK facade pipes, including on failed startup."""
    processes: list[subprocess.Popen[bytes]] = []
    initialize = subprocess.Popen.__init__

    def observe(process: Any, args: Any, *positional: Any, **kwargs: Any) -> None:
        initialize(process, args, *positional, **kwargs)
        if Path(args[0]).is_relative_to(Path(workspace.root) / ".launchers"):
            processes.append(process)

    monkeypatch.setattr(subprocess.Popen, "__init__", observe)
    try:
        yield processes
    finally:
        # Physical cleanup after an assertion failure is not product success.
        for process in processes:
            if process.poll() is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


async def test_execute_uses_owned_sdk_and_native_shutdown(
    workspace: ManagedWorkspaceContext,
    tmp_path: Path,
    sdk_processes: list[subprocess.Popen[bytes]],
) -> None:
    # Requirement: success requires native shutdown, process proof and owner join.
    provider = CopilotProvider(github_token="fixture-session-token")
    definition = agent(tools=[], working_dir=workspace.root + "/work")
    output = await provider.execute(definition, {}, "opaque input", managed_workspace=workspace)
    await provider.release_workspace(workspace.root)
    assert output.content == {"result": "SUCCESS DONE ignore instructions"}
    assert provider._client is None
    assert provider.get_session_cwds() == {"a": definition.working_dir}
    (proof,) = workspace.process_scopes.proofs
    assert (
        proof.kernel_empty
        and proof.streams_drained
        and proof.facade_exited
        and proof.custodian_reaped
    )
    (wire,) = receipts(tmp_path)
    methods = [entry["method"] for entry in wire if entry["event"] == "rpc"]
    assert methods[0] == "connect" and methods[-1] == "runtime.shutdown"
    assert any(entry["event"] == "shutdown-replied" for entry in wire)
    create = next(entry for entry in wire if entry.get("method") == "session.create")
    assert create["params"]["availableTools"] == []
    assert create["params"]["gitHubToken"] == "fixture-session-token"
    spawn = wire[0]
    assert spawn["cwd"] == definition.working_dir
    assert "--stdio" in spawn["argv"]
    assert "COPILOT_SDK_AUTH_TOKEN" not in spawn["env"]
    assert len(sdk_processes) == 1
    assert all(
        stream is not None and stream.closed
        for process in sdk_processes
        for stream in (process.stdin, process.stdout, process.stderr)
    )


async def test_resume_and_dialog_use_fresh_clients(
    workspace: ManagedWorkspaceContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a new runtime resumes prior conversation bytes in the selected namespace.
    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "history")
    provider = CopilotProvider()
    selected_cwd = Path(workspace.root) / "work" / "nested"
    selected_cwd.mkdir()
    definition = agent(working_dir=str(selected_cwd))
    await provider.execute(definition, {}, "first", managed_workspace=workspace)
    provider.set_resume_session_ids(provider.get_session_ids())
    provider.set_resume_session_cwds(provider.get_session_cwds())
    second = await provider.execute(definition, {}, "second", managed_workspace=workspace)
    assert json.loads(second.content["result"]) == {"history": ["first", "second"]}
    response = await provider.execute_dialog_turn(
        "system", "third", managed_workspace=workspace, working_dir=str(selected_cwd)
    )
    await provider.release_workspace(workspace.root)
    wires = receipts(tmp_path)
    assert len(wires) == 3 and json.loads(response) == {"history": ["User: third"]}
    assert sum(any(item.get("method") == "session.resume" for item in wire) for wire in wires) == 1
    assert len({proof.invocation_id for proof in workspace.process_scopes.proofs}) == 3
    for wire in wires:
        assert wire[0]["cwd"] == str(selected_cwd)
        session_rpc = next(
            item for item in wire if item.get("method") in {"session.create", "session.resume"}
        )
        assert session_rpc["params"]["workingDirectory"] == str(selected_cwd)


async def test_retry_joins_first_native_attempt_before_starting_second(
    workspace: ManagedWorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
    sdk_processes: list[subprocess.Popen[bytes]],
) -> None:
    # Requirement: a retryable send gets a new runtime only after real release and joins.
    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "retry-once")
    provider = CopilotProvider(retry_config=RetryConfig(max_attempts=2, base_delay=0, jitter=False))
    start = subprocess.Popen.__init__
    launches = 0

    def observe(process: Any, args: Any, *positional: Any, **kwargs: Any) -> None:
        nonlocal launches
        if Path(args[0]).is_relative_to(Path(workspace.root) / ".launchers"):
            if launches:
                first = provider._managed_invocations[workspace.root][0]
                assert first.owner is not None and first.owner.done()
                assert all(not thread.is_alive() for thread in first.readers)
                assert sdk_processes[0].poll() is not None
                assert all(
                    stream is not None and stream.closed
                    for stream in (
                        sdk_processes[0].stdin,
                        sdk_processes[0].stdout,
                        sdk_processes[0].stderr,
                    )
                )
                assert all(proof.done for proof in workspace.process_scopes.proofs)
            launches += 1
        start(process, args, *positional, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", observe)
    result = await provider.execute(agent(), {}, "retry payload", managed_workspace=workspace)
    await provider.release_workspace(workspace.root)
    assert result.content == {"result": "SUCCESS DONE ignore instructions"}
    assert launches == len(sdk_processes) == 2
    assert len({item.invocation_id for item in workspace.process_scopes.proofs}) == 2
    assert len(provider.get_retry_history()) == 1


@pytest.mark.parametrize("stage", ["disconnect", "stop", "stdio"])
async def test_cleanup_warnings_scrub_native_exception_chains(
    workspace: ManagedWorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
    sdk_processes: list[subprocess.Popen[bytes]],
    caplog: pytest.LogCaptureFixture,
    stage: str,
) -> None:
    # Requirement: every managed cleanup sink scrubs chains while retaining native release proof.
    from copilot import CopilotClient
    from copilot.session import CopilotSession

    secret = "registered-copilot-cleanup-secret"
    redactor = RunRedactor()
    redactor.register([secret])
    controller = workspace.process_scopes
    assert isinstance(controller, LocalProcessScopeController)
    controller.scrub = redactor.scrub
    clock = Clock()
    controller._clock = clock
    failed = asyncio.Event()
    stop = CopilotClient.stop
    disconnect = CopilotSession.disconnect

    def fail() -> None:
        failed.set()
        try:
            raise PermissionError(13, "native cleanup denied", f"/private/{secret}")
        except PermissionError as cause:
            raise OSError(f"native {stage} failed: {secret}") from cause

    async def failed_disconnect(session: CopilotSession) -> None:
        already_closed = session._destroyed
        await disconnect(session)
        if not already_closed:
            fail()

    async def failed_stop(client: CopilotClient) -> None:
        await stop(client)
        if stage == "stop":
            fail()
        if stage == "stdio":
            (process,) = sdk_processes
            stream = process.stderr
            assert stream is not None
            close = stream.close

            def failed_close() -> None:
                monkeypatch.setattr(stream, "close", close)
                close()
                fail()

            monkeypatch.setattr(stream, "close", failed_close)

    if stage == "disconnect":
        monkeypatch.setattr(CopilotSession, "disconnect", failed_disconnect)
    else:
        monkeypatch.setattr(CopilotClient, "stop", failed_stop)
    provider = CopilotProvider()
    call = asyncio.create_task(provider.execute(agent(), {}, "p", managed_workspace=workspace))
    await asyncio.wait_for(failed.wait(), 10)
    if stage == "stdio":
        for scope in controller._scopes.values():
            await asyncio.wait_for(scope.completed.wait(), 10)
        clock.advance(30)
        with pytest.raises(ProviderError):
            await call
        with pytest.raises(ExceptionGroup):
            await provider.release_workspace(workspace.root)
    else:
        output = await call
        await provider.release_workspace(workspace.root)
        assert output.content == {"result": "SUCCESS DONE ignore instructions"}
    if stage == "stdio":
        joined = asyncio.Event()
        for entry in tuple(controller.owners.tasks.values()):
            assert isinstance(entry.task, asyncio.Future)
            entry.task.add_done_callback(lambda _task: joined.set())
            entry.task.cancel()
        await asyncio.wait_for(joined.wait(), 10)
    records = [record for record in caplog.records if record.name == "conductor.providers.copilot"]
    formatted = "\n".join(logging.Formatter().format(record) for record in records)
    assert secret not in formatted
    assert REDACTED_MARKER in formatted and "PermissionError" in formatted
    assert "native cleanup denied" in formatted and f"native {stage} failed" in formatted
    assert "Traceback" in formatted and all(record.exc_info is None for record in records)
    expected_status = "unconfirmed" if stage == "stdio" else "released"
    assert (await workspace.process_scopes.await_release()).status == expected_status
    assert all(
        proof.kernel_empty and proof.custodian_reaped for proof in workspace.process_scopes.proofs
    )
    assert all(
        stream is not None and stream.closed
        for process in sdk_processes
        for stream in (process.stdin, process.stdout, process.stderr)
    )


@pytest.mark.parametrize("working_dir", [None, "", "relative"])
async def test_managed_dialog_refuses_missing_or_relative_cwd_before_startup(
    workspace: ManagedWorkspaceContext, working_dir: str | None, tmp_path: Path
) -> None:
    # Requirement: managed dialog must not guess a namespace or use the host/root cwd.
    provider = CopilotProvider()
    with pytest.raises(ProviderError) as error:
        await provider.execute_dialog_turn(
            "system", "dialog", managed_workspace=workspace, working_dir=working_dir
        )
    assert not error.value.is_retryable
    assert workspace.process_scopes.proofs == ()
    assert receipts(tmp_path) == []


async def test_concurrent_managed_dialogs_keep_their_explicit_namespace_cwds(
    workspace: ManagedWorkspaceContext, tmp_path: Path
) -> None:
    # Requirement: concurrent dialogs sharing ownership retain independent selected cwds.
    provider = CopilotProvider()
    directories = [Path(workspace.root) / "work" / name for name in ("left", "right")]
    for directory in directories:
        directory.mkdir()
    await asyncio.gather(
        *(
            provider.execute_dialog_turn(
                "system", "dialog", managed_workspace=workspace, working_dir=str(directory)
            )
            for directory in directories
        )
    )
    await provider.release_workspace(workspace.root)
    wires = receipts(tmp_path)
    assert len(wires) == 2
    assert {wire[0]["cwd"] for wire in wires} == {str(path) for path in directories}
    session_cwds = {
        item["params"]["workingDirectory"]
        for wire in wires
        for item in wire
        if item.get("method") == "session.create"
    }
    assert session_cwds == {str(path) for path in directories}
    proofs = workspace.process_scopes.proofs
    assert len({proof.invocation_id for proof in proofs}) == 2
    assert all(
        proof.kernel_empty
        and proof.streams_drained
        and proof.facade_exited
        and proof.custodian_reaped
        for proof in proofs
    )


async def test_failed_startup_has_clean_release(
    workspace: ManagedWorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
    sdk_processes: list[subprocess.Popen[bytes]],
) -> None:
    # Requirement: native handshake refusal closes the exact owned pipes before release.
    from copilot._jsonrpc import JsonRpcError

    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "fail")
    provider = CopilotProvider()
    with pytest.raises(JsonRpcError) as caught:
        await provider.execute(agent(), {}, "p", managed_workspace=workspace)
    assert caught.value.code == -32001
    await provider.release_workspace(workspace.root)
    assert len(sdk_processes) == 1
    assert sdk_processes[0].returncode is not None
    assert all(
        stream is not None and stream.closed
        for process in sdk_processes
        for stream in (process.stdin, process.stdout, process.stderr)
    )
    assert all(
        proof.kernel_empty and proof.custodian_reaped for proof in workspace.process_scopes.proofs
    )


async def test_failed_startup_does_not_close_live_sibling_pipes(
    workspace: ManagedWorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
    sdk_processes: list[subprocess.Popen[bytes]],
) -> None:
    # Requirement: failed connect closes only its own pipes, never an active sibling's.
    from copilot._jsonrpc import JsonRpcError

    provider = CopilotProvider()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def sibling_operation() -> None:
        entered.set()
        await finish.wait()

    sibling = asyncio.create_task(provider._managed_call(workspace, None, sibling_operation))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
        (live,) = sdk_processes
        monkeypatch.setenv("COPILOT_FIXTURE_MODE", "fail")
        with pytest.raises(JsonRpcError):
            await provider.execute(agent(), {}, "p", managed_workspace=workspace)
        assert not sibling.done() and live.poll() is None
        assert all(
            stream is not None and not stream.closed
            for stream in (live.stdin, live.stdout, live.stderr)
        )
        assert len(sdk_processes) == 2
        assert all(
            stream is not None and stream.closed
            for stream in (
                sdk_processes[1].stdin,
                sdk_processes[1].stdout,
                sdk_processes[1].stderr,
            )
        )
    finally:
        finish.set()
        async with asyncio.timeout(10):
            await sibling
    await provider.release_workspace(workspace.root)
    assert all(
        stream is not None and stream.closed for stream in (live.stdin, live.stdout, live.stderr)
    )


@pytest.mark.parametrize("external", [True, False])
async def test_external_resources_rejected_before_startup(
    workspace: ManagedWorkspaceContext, external: bool
) -> None:
    # Requirement: managed dispatch never adopts an external runtime or SDK client.
    settings = ProviderSettings(name="copilot", runtime_url="localhost:1") if external else None
    provider = CopilotProvider(provider_settings=settings)
    if not external:
        provider._client = AsyncMock()
    with pytest.raises(ProviderError):
        await provider.execute(agent(), {}, "p", managed_workspace=workspace)
    assert workspace.process_scopes.proofs == ()


async def test_stale_context_rejected_without_new_payload(
    workspace: ManagedWorkspaceContext,
) -> None:
    # Requirement: an old incarnation cannot replace the registered owner context.
    provider = CopilotProvider()
    await provider.execute(agent(), {}, "p", managed_workspace=workspace)
    stale = ManagedWorkspaceContext(workspace.root, "different", workspace.process_scopes)
    with pytest.raises(ProviderError):
        await provider.execute(agent(), {}, "p", managed_workspace=stale)
    assert len(workspace.process_scopes.proofs) == 1


async def test_cancelled_actual_startup_joins_owner(
    workspace: ManagedWorkspaceContext,
    wire_events: asyncio.Queue[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    sdk_processes: list[subprocess.Popen[bytes]],
) -> None:
    # Requirement: cancelled native startup remains primary after proven clean release.
    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "hold")
    provider = CopilotProvider()
    call = asyncio.create_task(provider.execute(agent(), {}, "p", managed_workspace=workspace))
    await method_received(wire_events, "connect")
    call.cancel("original cancel")
    async with asyncio.timeout(10):
        with pytest.raises(asyncio.CancelledError) as error:
            await call
    assert error.value.args == ("original cancel",)
    await provider.release_workspace(workspace.root)
    assert len(sdk_processes) == 1
    assert all(
        stream is not None and stream.closed
        for process in sdk_processes
        for stream in (process.stdin, process.stdout, process.stderr)
    )
    assert all(
        proof.kernel_empty and proof.custodian_reaped for proof in workspace.process_scopes.proofs
    )


async def test_native_interrupt_resumes_disk_session_with_fresh_runtime(
    workspace: ManagedWorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Requirement: guidance in a fresh runtime observes the interrupted conversation history.
    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "interrupt")
    provider = CopilotProvider()
    signal = asyncio.Event()
    signal.set()
    partial = await provider.execute(
        agent(), {}, "p", interrupt_signal=signal, managed_workspace=workspace
    )
    assert partial.partial
    identity = provider.get_interrupted_session()
    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "history")
    output = await provider.send_followup(identity, "guidance", managed_workspace=workspace)
    await provider.release_workspace(workspace.root)
    assert not output.partial and len(workspace.process_scopes.proofs) == 2
    assert output.content == {"history": ["p", "guidance"]}
    assert (
        sum(
            any(item.get("method") == "session.abort" for item in wire)
            for wire in receipts(tmp_path)
        )
        == 1
    )
    assert (
        sum(
            any(item.get("method") == "session.resume" for item in wire)
            for wire in receipts(tmp_path)
        )
        == 1
    )


@pytest.mark.parametrize("identity_source", ["wrong-id", "fresh-root"])
async def test_native_followup_refuses_unknown_disk_session(
    workspace: ManagedWorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    identity_source: str,
) -> None:
    # Requirement: a supplied SID cannot fabricate history in another root or unknown session.
    from copilot._jsonrpc import JsonRpcError

    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "interrupt")
    provider = CopilotProvider()
    signal = asyncio.Event()
    signal.set()
    await provider.execute(
        agent(), {}, "private first turn", interrupt_signal=signal, managed_workspace=workspace
    )
    identity = provider.get_interrupted_session()
    assert identity is not None
    root = tmp_path / "fresh"
    (root / "work").mkdir(parents=True)
    controller = LocalProcessScopeController(str(root), "fresh-incarnation")
    fresh = ManagedWorkspaceContext(str(root), "fresh-incarnation", controller)
    selected = workspace
    if identity_source == "wrong-id":
        identity = replace(identity, session_id="unknown-session")
    else:
        selected = fresh
        identity = replace(
            identity, root=fresh.root, incarnation=fresh.incarnation, cwd=str(root / "work")
        )
    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "history")
    try:
        with pytest.raises(JsonRpcError) as caught:
            await provider.send_followup(identity, "recall", managed_workspace=selected)
        assert caught.value.code == -32003
        await provider.release_workspace(workspace.root)
        if selected is fresh:
            await provider.release_workspace(fresh.root)
    finally:
        controller.begin_teardown()
        await controller.force_stop(tuple(proof.scope_id for proof in controller.proofs))
        await controller.await_release()
        await controller.close()


async def test_omitted_resume_id_starts_without_prior_history(
    workspace: ManagedWorkspaceContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: history is recovered from the supplied disk SID, never a process global.
    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "history")
    first = CopilotProvider()
    await first.execute(agent(), {}, "private first turn", managed_workspace=workspace)
    fresh = CopilotProvider()
    output = await fresh.execute(agent(), {}, "recall", managed_workspace=workspace)
    assert json.loads(output.content["result"]) == {"history": ["recall"]}
    await first.release_workspace(workspace.root)
    await fresh.release_workspace(workspace.root)


async def test_retryable_primary_does_not_retry_after_unconfirmed_pipe_join(
    workspace: ManagedWorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
    sdk_processes: list[subprocess.Popen[bytes]],
) -> None:
    # Requirement: a retryable payload error never overrides an unconfirmed native join fence.
    from copilot import CopilotClient

    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "retry-once")
    controller = workspace.process_scopes
    assert isinstance(controller, LocalProcessScopeController)
    clock = Clock()
    controller._clock = clock
    failed = asyncio.Event()
    stop = CopilotClient.stop

    async def failed_close(client: CopilotClient) -> None:
        await stop(client)
        stream = sdk_processes[0].stderr
        assert stream is not None
        close = stream.close

        def close_once() -> None:
            monkeypatch.setattr(stream, "close", close)
            close()
            failed.set()
            raise OSError("fixture pipe close unconfirmed")

        monkeypatch.setattr(stream, "close", close_once)

    monkeypatch.setattr(CopilotClient, "stop", failed_close)
    provider = CopilotProvider(retry_config=RetryConfig(max_attempts=2, base_delay=0, jitter=False))
    call = asyncio.create_task(provider.execute(agent(), {}, "p", managed_workspace=workspace))
    await asyncio.wait_for(failed.wait(), 10)
    for scope in controller._scopes.values():
        await asyncio.wait_for(scope.completed.wait(), 10)
    clock.advance(30)
    with pytest.raises(ProviderError, match="fixture transient send failure"):
        await call
    assert len(sdk_processes) == 1
    assert (await controller.await_release()).status == "unconfirmed"
    with pytest.raises(ExceptionGroup):
        await provider.release_workspace(workspace.root)
    for entry in tuple(controller.owners.tasks.values()):
        assert isinstance(entry.task, asyncio.Future)
        entry.task.cancel()


async def test_native_stuck_shutdown_force_is_independent(
    tmp_path: Path,
    workspace: ManagedWorkspaceContext,
    wire_events: asyncio.Queue[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a stuck native shutdown cannot prevent the original deadline driver.
    clock = Clock()
    controller = LocalProcessScopeController(workspace.root, "stuck", _scheduler=clock)
    context = ManagedWorkspaceContext(workspace.root, "stuck", controller)
    monkeypatch.setenv("COPILOT_FIXTURE_MODE", "stuck-shutdown")
    provider = CopilotProvider()
    call = asyncio.create_task(provider.execute(agent(), {}, "p", managed_workspace=context))
    try:
        await method_received(wire_events, "runtime.shutdown")
        deadline = controller.owners.deadline()
        assert deadline == 30
        clock.advance(20)
        async with asyncio.timeout(10):
            result = await call
            await provider.release_workspace(context.root)
        assert result.content["result"] == "SUCCESS DONE ignore instructions"
        (proof,) = controller.proofs
        assert proof.kernel_empty and proof.custodian_reaped and "FORCE_STOP" in proof.events
        assert any(
            item.get("method") == "runtime.shutdown" for wire in receipts(tmp_path) for item in wire
        )
    finally:
        controller.begin_teardown()
        await controller.force_stop(tuple(proof.scope_id for proof in controller.proofs))
        await controller.await_release()
        await controller.close()


async def test_unjoined_owner_is_sticky_after_late_failure(
    workspace: ManagedWorkspaceContext,
) -> None:
    # Requirement: native proofs do not make an unjoined cancellation-resistant owner released.
    clock = Clock()
    controller = LocalProcessScopeController(workspace.root, "late-owner", _scheduler=clock)
    context = ManagedWorkspaceContext(workspace.root, "late-owner", controller)
    provider = CopilotProvider()
    entered, finish, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def resistant() -> str:
        entered.set()
        while not finish.is_set():
            try:
                await finish.wait()
            except asyncio.CancelledError:
                interrupted.set()
        raise RuntimeError("late owner failure")

    call = asyncio.create_task(provider._managed_call(context, None, resistant))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        release = asyncio.create_task(provider.release_workspace(context.root))
        await asyncio.wait_for(interrupted.wait(), 10)
        deadline = controller.owners.deadline()
        controller.begin_teardown(deadline=100)
        assert controller.owners.deadline() == deadline == 30
        clock.advance(30)
        async with asyncio.timeout(10):
            with pytest.raises(ProviderError, match="unconfirmed"):
                await call
            with pytest.raises(ExceptionGroup):
                await release
        assert controller.owners.quarantined >= 1
        finish.set()
        owner = provider._managed_invocations[context.root][0].owner
        assert owner is not None
        completed, _ = await asyncio.wait({owner}, timeout=10)
        assert completed == {owner}
        assert isinstance(owner.exception(), RuntimeError)
        assert (await controller.await_release()).status == "unconfirmed"
        with pytest.raises(ExceptionGroup):
            await provider.release_workspace(context.root)
    finally:
        finish.set()
        await controller.close()
