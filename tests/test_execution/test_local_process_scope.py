"""Native managed scope regressions, using installed helper and real payloads."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from conductor.execution import local_process_scope as process_core
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.local import LocalRunnerBackend
from conductor.execution.local_process_scope import LocalProcessScopeController
from conductor.execution.local_scope_launcher import connect
from conductor.execution.types import CommandSpec, ManagedWorkspaceContext, ProcessLaunchSpec
from conductor.redaction import REDACTED_MARKER, RunRedactor


class ControlledTimer:
    def __init__(self, at: float, callback: Callable[[], None]) -> None:
        self.at, self.callback, self.cancelled = at, callback, False

    def cancel(self) -> None:
        self.cancelled = True


class ControlledClock:
    def __init__(self) -> None:
        self.value = 0.0
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


async def facade(wrapped: ProcessLaunchSpec, *args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *wrapped.argv,
        *args,
        env=dict(wrapped.env),
        cwd=wrapped.working_dir,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _join_scope_task_callbacks(controller: LocalProcessScopeController) -> None:
    """Join actual completion callbacks, including retirement admitted by previous owners."""
    while controller._tasks or controller.owners.tasks:
        owners = {
            *tuple(controller._tasks),
            *(entry.task for entry in tuple(controller.owners.tasks.values())),
        }
        callbacks: list[asyncio.Future[None]] = []
        for owner in owners:
            assert isinstance(owner, asyncio.Future)
            finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            owner.add_done_callback(lambda _done, finished=finished: finished.set_result(None))
            callbacks.append(finished)
        await asyncio.gather(*callbacks)


async def test_scope_callback_join_observes_done_and_transitive_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: queued product callbacks and their newly admitted owners must actually join.
    controller = LocalProcessScopeController(str(tmp_path), "callback-order")
    terminal = asyncio.Event()
    external: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    controller.owners.register(None, external)
    callbacks_seen: list[str] = []
    children = []

    async def first_owner() -> None:
        terminal.set()
        external.set_result(None)

    async def second_owner() -> None:
        return None

    def admit_child(_done) -> None:
        callbacks_seen.append("first")
        child = controller._task(second_owner())
        children.append(child)
        child.add_done_callback(lambda _child: callbacks_seen.append("second"))

    first = controller._task(first_owner())
    first.add_done_callback(admit_child)
    original_gather = asyncio.gather
    eager_iterations = 0

    def finite_gather(*owners, **kwargs):
        nonlocal eager_iterations
        if owners and all(owner.done() for owner in owners) and first in controller._tasks:
            eager_iterations += 1
            assert eager_iterations <= 3, "Join starved the queued product completion callback"
        return original_gather(*owners, **kwargs)

    monkeypatch.setattr(asyncio, "gather", finite_gather)
    try:
        await terminal.wait()
        assert first.done() and first in controller._tasks
        assert external.done() and controller.owners.tasks
        await _join_scope_task_callbacks(controller)
        assert callbacks_seen == ["first", "second"]
        assert children and all(child.done() for child in children)
        assert not controller._tasks and not controller.owners.tasks
    finally:
        # Preserve physical cleanup even if the finite starvation guard detects a regression.
        joined: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        first.add_done_callback(lambda _done: joined.set_result(None))
        await joined
        for child in children:
            completed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            child.add_done_callback(lambda _done, completed=completed: completed.set_result(None))
            await completed
        assert (await controller.close()).status == "released"
        controller._accepting.join(timeout=3)
        assert not controller._accepting.is_alive()


async def test_script_preserves_bytes_argv_environment_and_cwd(tmp_path: Path) -> None:
    # Requirement: real payload output cannot forge private proof; launch fields survive.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    try:
        result = await asyncio.wait_for(
            controller.spawn_script(
                "script",
                CommandSpec(
                    sys.executable,
                    (
                        "-c",
                        (
                            "import os,sys,json; print(json.dumps([sys.argv[1],os.getcwd(),"
                            "os.environ['TOKEN'],sys.stdin.read()])); "
                            "sys.stderr.write('DONE SCOPE_EMPTY'); sys.exit(42)"
                        ),
                        "a b; $(false)",
                    ),
                    str(tmp_path),
                    {"TOKEN": "private"},
                    stdin=b"input",
                ),
            ),
            10,
        )
        assert result.exit_code == 42
        assert json.loads(result.stdout) == ["a b; $(false)", str(tmp_path), "private", "input"]
        assert result.stderr == "DONE SCOPE_EMPTY"
        assert (await controller.await_release()).status == "released"
        (proof,) = controller.proofs
        assert proof.kernel_empty and proof.streams_drained and proof.custodian_reaped
        assert proof.events.count("DONE") == 1
    finally:
        await controller.close()


async def test_real_probe_and_main_share_family_not_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: version/main scopes preserve exact binary output; seal rejects a third.
    clock = ControlledClock()
    controller = LocalProcessScopeController(str(tmp_path), "inc", _scheduler=clock)
    owner_return = asyncio.Event()
    original_watch = controller._watch_facade

    async def watch(scope: process_core._Scope) -> None:
        await original_watch(scope)
        # Requirement: native exit signals must not stand in for owner quiescence.
        await asyncio.wait_for(owner_return.wait(), 10)

    monkeypatch.setattr(controller, "_watch_facade", watch)
    wrapped = await controller.wrap_stdio(
        "sdk", ProcessLaunchSpec((sys.executable,), dict(os.environ), str(tmp_path))
    )
    try:
        # Native SDK version probes intentionally omit options.env and options.cwd.
        probe = await asyncio.create_subprocess_exec(
            *wrapped.argv,
            "--version",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(probe.communicate(), 10)
        assert out.startswith(b"Python ") and err == b"" and probe.returncode == 0
        scope = controller._scopes[controller.proofs[0].scope_id]
        await asyncio.wait_for(scope.completed.wait(), 10)
        await asyncio.wait_for(scope.facade_dead.wait(), 10)
        assert scope.resources and not scope.episode.terminal.is_set()
        assert controller.owners.deadline("sdk") == 30
        assert not controller._invocations["sdk"].sealed
        terminal = asyncio.create_task(scope.episode.terminal.wait())
        owner_return.set()
        await asyncio.wait_for(terminal, 10)
        assert scope.episode.result is not None and scope.episode.result.status == "released"
        assert not scope.resources
        clock.advance(100)
        assert controller.owners.deadline("sdk") is None
        main = await facade(
            wrapped,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()+b'\\n'); "
            "sys.stdout.buffer.flush()",
        )
        out, err = await asyncio.wait_for(main.communicate(b"real-main"), 10)
        assert out == b"real-main\n" and err == b"" and main.returncode == 0
        assert len({proof.scope_id for proof in controller.proofs}) == 2
        controller.begin_teardown("sdk")
        rejected = await facade(wrapped, "-c", "raise AssertionError('must not run')")
        await asyncio.wait_for(rejected.communicate(), 10)
        assert rejected.returncode == 125
        assert len(controller.proofs) == 2
        assert (await controller.await_release()).status == "released"
        assert all(proof.facade_exited and proof.custodian_reaped for proof in controller.proofs)
    finally:
        owner_return.set()
        await controller.close()


async def test_grace_preserves_eof_only_final_bytes(tmp_path: Path) -> None:
    # Requirement: teardown preserves exact EOF-only bytes without text newline translation.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    wrapped = await controller.wrap_stdio(
        "sdk",
        ProcessLaunchSpec(
            (
                sys.executable,
                "-u",
                "-c",
                "import sys; sys.stdout.buffer.write(b'ready\\n'); sys.stdout.buffer.flush(); "
                "data=sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(b'final:'+data)",
            ),
            dict(os.environ),
            str(tmp_path),
        ),
    )
    try:
        process = await facade(wrapped)
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        controller.begin_teardown("sdk")
        out, err = await asyncio.wait_for(process.communicate(b"accepted"), 10)
        assert out == b"final:accepted" and err == b"" and process.returncode == 0
        assert (await controller.await_release()).status == "released"
        assert all("FORCE_STOP" not in proof.events for proof in controller.proofs)
    finally:
        await controller.close()


async def test_terminate_facade_does_not_destroy_custodian(tmp_path: Path) -> None:
    # Requirement: facade termination keeps custody through EOF, exact final bytes and proof.
    saved = tmp_path / "eof-result"
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    wrapped = await controller.wrap_stdio(
        "sdk",
        ProcessLaunchSpec(
            (
                sys.executable,
                "-u",
                "-c",
                "import sys,pathlib; sys.stdout.buffer.write(b'ready\\n'); "
                "sys.stdout.buffer.flush(); sys.stdin.buffer.read(); "
                f"pathlib.Path({str(saved)!r}).write_bytes(b'final-on-eof')",
            ),
            dict(os.environ),
            str(tmp_path),
        ),
    )
    try:
        process = await facade(wrapped)
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        controller.begin_teardown("sdk")
        process.terminate()
        await asyncio.wait_for(process.communicate(), 10)
        assert (await asyncio.wait_for(controller.await_release(), 10)).status == "released"
        assert saved.read_bytes() == b"final-on-eof"
        (proof,) = controller.proofs
        assert proof.facade_exited and proof.kernel_empty and proof.custodian_reaped
    finally:
        await controller.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux subreaper/pidfd proof")
async def test_detached_double_fork_is_reaped_before_result(tmp_path: Path) -> None:
    # Requirement: redirected setsid/double-fork descendants cannot outlive command proof.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    payload = (
        "import os,signal,sys\n"
        "r,w=os.pipe()\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        " os.close(r); os.setsid()\n"
        " if os.fork(): os._exit(0)\n"
        " null=os.open(os.devnull,os.O_RDWR)\n"
        " for fd in (0,1,2): os.dup2(null,fd)\n"
        " os.write(w,str(os.getpid()).encode()); os.close(w); signal.pause()\n"
        "os.close(w); print(os.read(r,100).decode(),flush=True); os.close(r)\n"
    )
    try:
        result = await asyncio.wait_for(
            controller.spawn_script(
                "script", CommandSpec(sys.executable, ("-c", payload), stdin=b"")
            ),
            10,
        )
        assert result.exit_code == 0
        pid = int(result.stdout)
        with pytest.raises(ProcessLookupError):
            os.pidfd_open(pid)
        assert (await controller.await_release()).status == "released"
    finally:
        await controller.close()


@pytest.mark.parametrize("failure", ["missing", "cwd"])
async def test_structured_target_start_failure(tmp_path: Path, failure: str) -> None:
    # Requirement: start failures preserve exact native metadata on this platform, never exit 127.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    command = str(tmp_path / "missing") if failure == "missing" else sys.executable
    cwd = str(tmp_path / "missing-cwd") if failure == "cwd" else str(tmp_path)
    spec = CommandSpec(command, working_dir=cwd, stdin=b"")
    try:
        native = await asyncio.wait_for(LocalRunnerBackend().run_command(spec, None), 10)
        assert native.outcome in ("command_not_found", "start_failed")
        assert native.exit_code is None and native.start_error is not None
        result = await asyncio.wait_for(controller.spawn_script("script", spec), 10)
        assert result.outcome == native.outcome
        assert result.exit_code is None and result.start_error is not None
        assert result.start_error == native.start_error
        assert "COMMAND_EXITED" not in controller.proofs[0].events
        assert (await controller.await_release()).status == "released"
    finally:
        await controller.close()


async def test_deadline_quarantines_owner_and_observes_late_failure(tmp_path: Path) -> None:
    # Requirement: total deadline never awaits cancel acknowledgment or permits late mutation.
    clock = ControlledClock()
    controller = LocalProcessScopeController(str(tmp_path), "inc", _scheduler=clock)
    ready, release = asyncio.Event(), asyncio.Event()
    observed = asyncio.Event()

    async def stubborn() -> None:
        ready.set()
        await release.wait()
        raise RuntimeError("late-private")

    owner = asyncio.create_task(stubborn())
    owner.add_done_callback(lambda _task: observed.set())
    controller.owners.register("sdk", owner)
    try:
        await asyncio.wait_for(ready.wait(), 10)
        controller.begin_teardown("sdk")
        first = controller._invocations["sdk"].deadline
        controller.begin_teardown("sdk")
        observation = asyncio.create_task(controller.owners.observe("sdk", owner))
        clock.advance(30)
        assert await asyncio.wait_for(observation, 10) is False
        assert controller._invocations["sdk"].deadline == first
        assert (await controller.await_release()).status == "unconfirmed"
        assert controller.owners.quarantined == 1
        assert not controller.owners.mutations_allowed("sdk")
        release.set()
        await asyncio.wait_for(observed.wait(), 10)
        assert controller.owners.quarantined == 0
        assert controller.owners.failures == ["late-private"]
        assert (await controller.await_release()).status == "unconfirmed"
    finally:
        release.set()
        await controller.close()


async def test_reserved_environment_rejected_before_launch(tmp_path: Path) -> None:
    # Requirement: helper-reserved environment collisions refuse before any payload.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    try:
        with pytest.raises(ExecutionSpecError):
            await controller.wrap_stdio(
                "sdk",
                ProcessLaunchSpec(
                    (sys.executable,), {"CONDUCTOR_LOCAL_SCOPE_TOKEN": "private"}, str(tmp_path)
                ),
            )
        assert controller.proofs == ()
    finally:
        await controller.close()


class DiagnosticHandler(logging.Handler):
    """Record the actual diagnostic before optionally failing its delivery."""

    def __init__(self, failure: type[BaseException] | None) -> None:
        super().__init__()
        self.failure = failure
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        if self.failure is not None:
            raise self.failure("secondary diagnostic failure")


@pytest.mark.parametrize("operation", ["descriptor", "spawn"])
@pytest.mark.parametrize("sink_failure", [None, ValueError, asyncio.CancelledError, BaseException])
async def test_fatal_infrastructure_fences_before_optional_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    sink_failure: type[BaseException] | None,
) -> None:
    # Requirement: a failing diagnostic sink cannot prevent fatal fencing or owner accounting.
    secret = "registered-infrastructure-secret"
    redactor = RunRedactor()
    redactor.register([secret])
    clock = ControlledClock()
    controller = LocalProcessScopeController(
        str(tmp_path), "inc", scrub=redactor.scrub, _scheduler=clock
    )
    descriptor = controller._descriptor
    payload = tmp_path / "payload-started"
    primary = PermissionError(errno.EACCES, "custodian launch denied", secret)
    callback_finished = asyncio.Event()
    failed_tasks: list[asyncio.Task[process_core.Json]] = []
    loop = asyncio.get_running_loop()
    late_owner: asyncio.Future[None] = loop.create_future()
    controller.owners.register("diagnostic", late_owner)
    late_finished = asyncio.Event()
    late_owner.add_done_callback(lambda _owner: late_finished.set())
    loop_errors: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    handler = DiagnosticHandler(sink_failure)
    logger = logging.getLogger(process_core.__name__)
    handlers, propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [handler], False
    command = None

    def fail() -> None:
        task = asyncio.current_task()
        assert task is not None
        failed_tasks.append(task)
        task.add_done_callback(lambda _task: callback_finished.set())
        try:
            raise OSError(errno.ENOSPC, "descriptor storage exhausted", f"/private/{secret}")
        except OSError as cause:
            raise primary from cause

    def failed_descriptor(directory: Path, role: str, **values: str) -> Path:
        if role == "custodian":
            fail()
        return descriptor(directory, role, **values)

    async def failed_spawn(*args: str, **kwargs: int | None) -> asyncio.subprocess.Process:
        fail()
        raise AssertionError("unreachable")

    try:
        if operation == "descriptor":
            monkeypatch.setattr(controller, "_descriptor", failed_descriptor)
        else:
            monkeypatch.setattr(process_core.asyncio, "create_subprocess_exec", failed_spawn)
        command = asyncio.create_task(
            controller.spawn_script(
                "diagnostic",
                CommandSpec(
                    sys.executable,
                    ("-c", f"from pathlib import Path; Path({str(payload)!r}).touch()"),
                ),
            )
        )
        await asyncio.wait_for(callback_finished.wait(), 5)
        scope = next(iter(controller._scopes.values()))
        assert handler.records
        assert controller._root.sealed and controller._invocations["diagnostic"].sealed
        assert not scope.resources and failed_tasks[0] not in controller._tasks
        assert id(failed_tasks[0]) not in process_core._quarantine.get(loop, {})
        done, _ = await asyncio.wait({command}, timeout=5)
        assert command in done, "Fatal callback did not release the spawn caller"
        result = command.result()
        assert result.outcome == "start_failed" and result.exit_code is None
        assert not payload.exists()
        assert (await asyncio.wait_for(controller.await_release(), 5)).status == "unconfirmed"
        assert clock.now() == 0
        with pytest.raises(ExecutionSpecError, match="sealed"):
            controller.check_admission()
        records = handler.records
        formatted = "\n".join(logging.Formatter().format(record) for record in records)
        assert "PermissionError" in formatted and "OSError" in formatted
        assert (
            "descriptor storage exhausted" in formatted and "custodian launch denied" in formatted
        )
        assert secret not in formatted and REDACTED_MARKER in formatted
        assert "diagnostic" in formatted and controller.proofs[0].scope_id in formatted
        assert "Traceback" in formatted and all(record.exc_info is None for record in records)
        assert not controller.owners.mutations_allowed("diagnostic")
        assert not loop_errors
        if sink_failure is not None:
            assert primary.__notes__ == [
                "Managed process infrastructure diagnostic could not be delivered."
            ]
        late_owner.set_result(None)
        await asyncio.wait_for(late_finished.wait(), 5)
        await asyncio.wait_for(_join_scope_task_callbacks(controller), 5)
        assert not controller.owners.quarantined
        assert (await controller.await_release()).status == "unconfirmed"
    finally:
        logger.handlers, logger.propagate = handlers, propagate
        handler.close()
        # Failure cleanup releases only this test's callers, without advancing its test clock.
        controller._poison("Test cleanup after injected infrastructure failure")
        if not late_owner.done():
            late_owner.set_result(None)
        try:
            if command is not None:
                await asyncio.wait_for(command, 5)
            await asyncio.wait_for(controller.close(), 5)
        finally:
            loop.set_exception_handler(previous_handler)
            controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()


@pytest.mark.parametrize("pre_unconfirmed", [False, True])
@pytest.mark.parametrize("sink_failure", [ValueError, asyncio.CancelledError, BaseException])
async def test_owner_failure_sink_preserves_native_release_and_accounting(
    tmp_path: Path, pre_unconfirmed: bool, sink_failure: type[BaseException]
) -> None:
    # Requirement: SDK failure diagnostics cannot retain joined owners or upgrade uncertainty.
    secret = "registered-owner-secret"
    redactor = RunRedactor()
    redactor.register([secret])
    clock = ControlledClock()
    controller = LocalProcessScopeController(
        str(tmp_path), "owner-diagnostic", scrub=redactor.scrub, _scheduler=clock
    )
    loop = asyncio.get_running_loop()
    owner: asyncio.Future[None] = loop.create_future()
    controller.owners.register("sdk", owner)
    finished = asyncio.Event()
    owner.add_done_callback(lambda _owner: finished.set())
    primary = RuntimeError(f"SDK close failed: {secret}")
    handler = DiagnosticHandler(sink_failure)
    logger = logging.getLogger(process_core.__name__)
    handlers, propagate = logger.handlers[:], logger.propagate
    loop_errors: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    observation = asyncio.create_task(controller.owners.observe("sdk", owner))
    try:
        # Keep raw output independent of the child's platform newline translation.
        result = await asyncio.wait_for(
            controller.spawn_script(
                "sdk",
                CommandSpec(
                    sys.executable,
                    ("-c", "import sys; sys.stdout.buffer.write(b'native\\n')"),
                ),
            ),
            10,
        )
        assert result.exit_code == 0 and result.stdout == "native\n"
        assert all(proof.kernel_empty and proof.custodian_reaped for proof in controller.proofs)
        controller.begin_teardown()
        if pre_unconfirmed:
            clock.advance(30)
            assert id(owner) in process_core._quarantine[loop]
        logger.handlers, logger.propagate = [handler], False
        owner.set_exception(primary)
        await asyncio.wait_for(finished.wait(), 5)
        assert handler.records
        assert not controller.owners.tasks
        assert id(owner) not in process_core._quarantine.get(loop, {})
        assert await asyncio.wait_for(observation, 5) is not pre_unconfirmed
        with pytest.raises(RuntimeError) as raised:
            owner.result()
        assert raised.value is primary
        assert controller.owners.failures == [f"SDK close failed: {REDACTED_MARKER}"]
        assert primary.__notes__ == ["Managed resource owner diagnostic could not be delivered."]
        assert all(record.exc_info is None for record in handler.records)
        assert secret not in logging.Formatter().format(handler.records[0])
        assert not loop_errors
        await asyncio.wait_for(_join_scope_task_callbacks(controller), 5)
        assert not controller.owners.quarantined
        expected = "unconfirmed" if pre_unconfirmed else "released"
        assert (await asyncio.wait_for(controller.await_release(), 5)).status == expected
        assert not controller.owners.mutations_allowed("sdk")
    finally:
        logger.handlers, logger.propagate = handlers, propagate
        handler.close()
        if not owner.done():
            owner.set_result(None)
        try:
            await asyncio.wait_for(observation, 5)
            await asyncio.wait_for(controller.close(), 5)
        finally:
            loop.set_exception_handler(previous_handler)
            controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()


async def test_nonfatal_infrastructure_failure_does_not_poison_or_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Requirement: expected nonfatal infrastructure errors do not become fatal diagnostics.
    controller = LocalProcessScopeController(str(tmp_path), "inc")

    async def peer_closed() -> None:
        raise BrokenPipeError("expected peer close")

    task = controller._task(peer_closed(), fatal=False)
    observed = asyncio.Event()
    task.add_done_callback(lambda _task: observed.set())
    try:
        await asyncio.wait_for(observed.wait(), 10)
        assert isinstance(task.exception(), BrokenPipeError)
        controller.check_admission()
        assert not [record for record in caplog.records if record.name == process_core.__name__]
        assert (await controller.await_release()).status == "released"
    finally:
        await controller.close()
        controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()


async def test_original_root_deadline_bounds_registry_owner(tmp_path: Path) -> None:
    # Requirement: an earlier external budget binds root owners, without a second 30s.
    clock = ControlledClock()
    clock.advance(15)
    controller = LocalProcessScopeController(str(tmp_path), "inc", _scheduler=clock)
    release = asyncio.Event()
    owner = asyncio.create_task(release.wait())
    controller.owners.register(None, owner)
    controller.begin_teardown(deadline=20)
    terminal = asyncio.create_task(controller.owners.wait_terminal())
    observation = asyncio.create_task(controller.owners.observe(None, owner))
    try:
        assert controller.owners.deadline() == 20
        controller.begin_teardown(deadline=100)
        assert controller.owners.deadline() == 20
        clock.advance(20)
        assert (await asyncio.wait_for(terminal, 10)).status == "unconfirmed"
        assert not await asyncio.wait_for(observation, 10)
        assert not controller.owners.mutations_allowed(None)
    finally:
        release.set()
        await owner
        await controller.close()


@pytest.mark.parametrize("early_failure", [False, True], ids=["force", "early-readiness-failure"])
async def test_force_driver_runs_while_native_owner_is_blocked(
    tmp_path: Path, early_failure: bool
) -> None:
    # Requirement: EOF grace expiry forces the native tree independently of SDK finally.
    # Requirement: an early readiness assertion survives cleanup with all native owners joined.
    clock = ControlledClock()
    controller = LocalProcessScopeController(str(tmp_path), "inc", _scheduler=clock)
    release = asyncio.Event()
    owner = asyncio.create_task(release.wait())
    controller.owners.register("sdk", owner)
    process = None
    scopes = ()
    readiness = b"ready\r\n" if early_failure else b"ready\n"
    expected = (
        pytest.raises(AssertionError, match="readiness bytes differ")
        if early_failure
        else contextlib.nullcontext()
    )
    with expected:
        try:
            wrapped = await controller.wrap_stdio(
                "sdk",
                ProcessLaunchSpec(
                    (
                        sys.executable,
                        "-u",
                        "-c",
                        f"import sys,threading; sys.stdout.buffer.write({readiness!r}); "
                        "sys.stdout.buffer.flush(); sys.stdin.read(); threading.Event().wait()",
                    ),
                    dict(os.environ),
                    str(tmp_path),
                ),
            )
            process = await facade(wrapped)
            assert process.stdout is not None and process.stdin is not None
            assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n", (
                "readiness bytes differ"
            )
            scope = controller._scopes[controller.proofs[0].scope_id]
            controller.begin_teardown("sdk")
            process.stdin.close()
            await asyncio.wait_for(scope.eof_received.wait(), 10)
            assert "FORCE_STOP" not in scope.events
            clock.advance(5)
            await asyncio.wait_for(scope.force_sent.wait(), 10)
            await asyncio.wait_for(process.communicate(), 10)
            await asyncio.wait_for(scope.completed.wait(), 10)
            assert scope.kernel_empty and scope.drained and scope.reaped
            assert not owner.done()
            release.set()
            await owner
            assert (await controller.await_release()).status == "released"
        finally:
            primary = sys.exception()
            release.set()
            await owner
            controller.begin_teardown()
            scopes = tuple(controller._scopes.values())
            if process is not None and process.stdin is not None:
                process.stdin.close()
            try:
                try:
                    for scope in scopes:
                        if not scope.completed.is_set():
                            await asyncio.wait_for(scope.eof_received.wait(), 10)
                    # Both the normal and early-failure paths start at zero: advance is absolute.
                    clock.advance(5)
                    if process is not None:
                        await asyncio.wait_for(process.communicate(), 10)
                    for scope in scopes:
                        await asyncio.wait_for(scope.completed.wait(), 10)
                finally:
                    # Terminal must be driven before close: await_release absorbs cancellation.
                    deadline = controller.owners.deadline()
                    if deadline is not None:
                        clock.advance(deadline)
                    await controller.close()
                    await asyncio.wait_for(_join_scope_task_callbacks(controller), 10)
                    controller._accepting.join(timeout=3)
            except BaseException as cleanup_error:
                if primary is None:
                    raise
                primary.add_note(f"Test resource cleanup failed: {cleanup_error!r}")
    assert process is not None and process.returncode is not None
    assert process.stdin is not None and process.stdin.is_closing()
    assert process.stdout is not None and process.stdout.at_eof()
    assert process.stderr is not None and process.stderr.at_eof()
    assert all(scope.kernel_empty and scope.drained and scope.reaped for scope in scopes)
    assert all(
        proof.kernel_empty and proof.streams_drained and proof.custodian_reaped
        for proof in controller.proofs
    )
    assert owner.done() and not controller.owners.tasks and not controller.owners.quarantined
    assert not controller._tasks and not controller._connections
    assert controller._closed and not controller._accepting.is_alive()


async def test_repeated_cancellation_seals_without_resetting_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: repeated caller interruptions do not cancel owners or extend cleanup.
    clock = ControlledClock()
    controller = LocalProcessScopeController(str(tmp_path), "inc", _scheduler=clock)
    release, intent = asyncio.Event(), asyncio.Event()
    owner = asyncio.create_task(release.wait())
    controller.owners.register("sdk", owner)
    original = controller.begin_teardown

    def begin(invocation_id: str | None = None, *, deadline: float | None = None) -> None:
        original(invocation_id, deadline=deadline)
        intent.set()

    monkeypatch.setattr(controller, "begin_teardown", begin)
    observation = asyncio.create_task(controller.owners.observe("sdk", owner))
    try:
        # Observe entry into the exact wait primitive before triggering cancellation.
        entered = asyncio.Event()
        original_wait = asyncio.wait

        async def subscribed(*args, **kwargs):
            entered.set()
            return await original_wait(*args, **kwargs)

        from unittest.mock import patch

        with patch("asyncio.wait", subscribed):
            await asyncio.wait_for(entered.wait(), 10)
            observation.cancel()
            await asyncio.wait_for(intent.wait(), 10)
            deadline = controller.owners.deadline("sdk")
            intent.clear()
            observation.cancel()
            await asyncio.wait_for(intent.wait(), 10)
        assert controller.owners.deadline("sdk") == deadline == 30
        assert not owner.cancelled()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(observation, 10)
        assert (await controller.await_release()).status == "released"
    finally:
        release.set()
        await owner
        await controller.close()


async def test_helper_loss_before_go_never_proves_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a dead helper is infrastructure failure, not target-not-found/empty.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    spawn = asyncio.create_subprocess_exec

    async def crash(*args, **kwargs) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        process.kill()
        await process.wait()
        return process

    try:
        with monkeypatch.context() as context:
            context.setattr(asyncio, "create_subprocess_exec", crash)
            result = await asyncio.wait_for(
                controller.spawn_script(
                    "script", CommandSpec(sys.executable, ("-c", "raise AssertionError"), stdin=b"")
                ),
                10,
            )
        assert result.outcome == "start_failed"
        assert result.start_error is not None
        assert result.start_error.kind == "os_error" and result.start_error.errno is None
        assert (await controller.await_release()).status == "unconfirmed"
        assert not controller.proofs[0].kernel_empty
    finally:
        await controller.close()


async def test_seal_between_registration_and_go_has_no_payload_result(tmp_path: Path) -> None:
    # Requirement: closing registration before GO cannot execute a payload or return its result.
    sentinel = tmp_path / "unowned"
    sentinel.write_bytes(b"keep")
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                controller.spawn_script(
                    "script",
                    CommandSpec(sys.executable, ("-c", "raise AssertionError"), stdin=b""),
                    on_dispatch=lambda: controller.begin_teardown("script"),
                ),
                10,
            )
        assert (await controller.await_release()).status == "released"
        assert sentinel.read_bytes() == b"keep"
        assert "COMMAND_STARTED" not in controller.proofs[0].events
    finally:
        await controller.close()


async def test_malformed_private_input_does_not_disable_next_launch(tmp_path: Path) -> None:
    # Requirement: malformed authenticated input cannot forge proof or kill the acceptor.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    try:

        def malformed() -> None:
            with connect({"endpoint": controller._endpoint, "nonce": controller._nonce}) as channel:
                channel.send_bytes(b'{"v":1,"event":')
                with pytest.raises(EOFError):
                    channel.recv_bytes()

        await asyncio.wait_for(asyncio.to_thread(malformed), 10)
        result = await asyncio.wait_for(
            controller.spawn_script(
                "script",
                CommandSpec(
                    sys.executable,
                    ("-c", "import sys; sys.stdout.buffer.write(b'actual\\n')"),
                    stdin=b"",
                ),
            ),
            10,
        )
        assert result.stdout == "actual\n"
        assert (await controller.await_release()).status == "released"
    finally:
        await controller.close()


async def test_old_launcher_does_not_reopen_closed_root(tmp_path: Path) -> None:
    # Requirement: a stale reusable launcher cannot reopen an incarnation after close.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    wrapped = await controller.wrap_stdio(
        "sdk", ProcessLaunchSpec((sys.executable,), dict(os.environ), str(tmp_path))
    )
    assert (await controller.close()).status == "released"
    process = await facade(wrapped, "-c", "raise AssertionError")
    await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 125
    assert controller.proofs == ()


async def test_active_eof_does_not_start_cleanup_episode(tmp_path: Path) -> None:
    # Requirement: input EOF during ordinary model execution is not a teardown intent.
    clock = ControlledClock()
    controller = LocalProcessScopeController(str(tmp_path), "inc", _scheduler=clock)
    wrapped = await controller.wrap_stdio(
        "sdk",
        ProcessLaunchSpec(
            (
                sys.executable,
                "-u",
                "-c",
                "import sys,threading; print('ready',flush=True); "
                "sys.stdin.read(); threading.Event().wait()",
            ),
            dict(os.environ),
            str(tmp_path),
        ),
    )
    try:
        process = await facade(wrapped)
        assert process.stdin is not None and process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        scope = controller._scopes[controller.proofs[0].scope_id]
        process.stdin.close()
        await asyncio.wait_for(scope.eof_received.wait(), 10)
        clock.advance(100)
        assert controller.owners.deadline("sdk") is None
        assert "FORCE_STOP" not in scope.events
        controller.begin_teardown("sdk")
        clock.advance(105)
        await asyncio.wait_for(scope.force_sent.wait(), 10)
        await asyncio.wait_for(process.communicate(), 10)
        assert (await controller.await_release()).status == "released"
    finally:
        await controller.close()


async def test_target_oserror_round_trips_real_native_fields(tmp_path: Path) -> None:
    # Requirement: non-ENOENT target start failures preserve native reconstructed causes.
    bad_cwd = tmp_path / "file-not-directory"
    bad_cwd.write_bytes(b"preserve")
    with pytest.raises(OSError) as expected:
        subprocess.run([sys.executable, "-c", ""], cwd=str(bad_cwd), capture_output=True, timeout=5)
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    try:
        result = await asyncio.wait_for(
            controller.spawn_script(
                "script",
                CommandSpec(sys.executable, ("-c", ""), working_dir=str(bad_cwd), stdin=b""),
            ),
            10,
        )
        assert result.outcome == "start_failed" and result.start_error is not None
        error = result.start_error
        reconstructed = OSError(error.errno, error.message, error.filename)
        assert type(reconstructed) is type(expected.value)
        assert str(reconstructed) == str(expected.value)
        assert reconstructed.errno == expected.value.errno
        assert reconstructed.filename == expected.value.filename
        assert (await controller.await_release()).status == "released"
    finally:
        await controller.close()


def test_uninstalled_helper_refuses_without_source_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: source import/PYTHONPATH is not a replacement for installed entrypoint.
    async def attempt() -> None:
        with pytest.raises(ExecutionSpecError):
            LocalProcessScopeController(str(tmp_path), "inc")

    monkeypatch.setattr("sysconfig.get_path", lambda _name: str(tmp_path))
    asyncio.run(attempt())


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows Job Object kernel")
async def test_windows_nonbreakaway_job_cleans_redirected_descendant(tmp_path: Path) -> None:
    # Requirement: native Windows ActiveProcesses proof includes redirected descendants.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    child = (
        "import os,threading,msvcrt; "
        "fd=msvcrt.open_osfhandle(int(os.environ['READY_HANDLE']),os.O_WRONLY); "
        "os.write(fd, b'R'); os.close(fd); "
        "threading.Event().wait()"
    )
    payload = (
        "import os,subprocess,sys,msvcrt\n"
        "r,w=os.pipe(); os.set_handle_inheritable(msvcrt.get_osfhandle(w),True)\n"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],"
        "env={**os.environ,'READY_HANDLE':str(msvcrt.get_osfhandle(w))},close_fds=False,"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "os.close(w); assert os.read(r,1)==b'R'; os.close(r); print(p.pid)\n"
    )
    try:
        result = await asyncio.wait_for(
            controller.spawn_script(
                "script", CommandSpec(sys.executable, ("-c", payload), stdin=b"")
            ),
            10,
        )
        assert result.exit_code == 0
        assert int(result.stdout) > 0
        assert (await controller.await_release()).status == "released"
        (proof,) = controller.proofs
        assert proof.kernel_empty and proof.streams_drained and proof.custodian_reaped
    finally:
        await controller.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows breakaway enforcement")
async def test_windows_job_refuses_breakaway_payload(tmp_path: Path) -> None:
    # Requirement: the assigned job does not allow descendants to break custody.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    payload = (
        "import subprocess,sys\n"
        "try:\n"
        " p=subprocess.Popen([sys.executable,'-c',''],"
        "creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB)\n"
        "except OSError as e:\n"
        " print(e.winerror)\n"
        "else:\n"
        " p.wait(); raise AssertionError('job allowed breakaway')\n"
    )
    try:
        result = await asyncio.wait_for(
            controller.spawn_script(
                "script", CommandSpec(sys.executable, ("-c", payload), stdin=b"")
            ),
            10,
        )
        assert result.exit_code == 0
        assert int(result.stdout) == 5
        assert (await controller.await_release()).status == "released"
    finally:
        await controller.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Native SIGSTOP/SIGCONT custody fault")
async def test_unresponsive_custodian_deadline_is_terminal_and_bounded(tmp_path: Path) -> None:
    # Requirement: an unresponsive custodian cannot block the driver or prove empty.
    clock = ControlledClock()
    controller = LocalProcessScopeController(str(tmp_path), "inc", _scheduler=clock)
    stopped = asyncio.Event()

    def stop_before_go() -> None:
        scope = next(iter(controller._scopes.values()))
        assert scope.process is not None
        scope.process.send_signal(signal.SIGSTOP)
        stopped.set()

    command = asyncio.create_task(
        controller.spawn_script(
            "script",
            CommandSpec(sys.executable, ("-c", ""), stdin=b""),
            on_dispatch=stop_before_go,
        )
    )
    controller.owners.register("script", command)
    try:
        await asyncio.wait_for(stopped.wait(), 10)
        scope = next(iter(controller._scopes.values()))
        assert scope.process is not None
        _pid, status = await asyncio.wait_for(
            asyncio.to_thread(os.waitpid, scope.process.pid, os.WUNTRACED), 10
        )
        assert os.WIFSTOPPED(status)
        observation = asyncio.create_task(controller.owners.observe("script", command))
        controller.begin_teardown("script")
        clock.advance(30)
        assert not await asyncio.wait_for(observation, 10)
        assert (await controller.await_release()).status == "unconfirmed"
        assert not scope.kernel_empty
        assert controller.owners.quarantined > 0
        scope.process.send_signal(signal.SIGCONT)
        await asyncio.wait_for(scope.completed.wait(), 10)
        await asyncio.wait_for(command, 10)
        assert scope.reaped and scope.kernel_empty and scope.drained
        assert (await controller.await_release()).status == "unconfirmed"
        assert not controller.owners.mutations_allowed("script")
    finally:
        for scope in controller._scopes.values():
            if scope.process is not None and scope.process.returncode is None:
                scope.process.send_signal(signal.SIGCONT)
        await controller.close()


async def test_managed_context_exposes_typed_consumer_surface(tmp_path: Path) -> None:
    # Requirement: adapters can register, observe, seal and inspect proofs without privates.
    controller = LocalProcessScopeController(str(tmp_path), "inc")
    context = ManagedWorkspaceContext(str(tmp_path), "inc", controller)
    scopes = context.process_scopes
    command = asyncio.create_task(
        scopes.spawn_script(
            "script",
            CommandSpec(
                sys.executable, ("-c", "import sys; sys.stdout.buffer.write(b'7\\n')"), stdin=b""
            ),
        )
    )
    scopes.owners.register("script", command)
    try:
        assert await asyncio.wait_for(scopes.owners.observe("script", command), 10)
        assert command.result().stdout == "7\n"
        scopes.begin_teardown(deadline=asyncio.get_running_loop().time() + 30)
        assert (await scopes.await_release()).status == "released"
        assert all(
            proof.kernel_empty and proof.streams_drained and proof.custodian_reaped
            for proof in scopes.proofs
        )
    finally:
        await controller.close()


@pytest.mark.parametrize("expire", [False, True])
async def test_release_joins_output_producer_and_late_native_result_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expire: bool
) -> None:
    # Requirement: real DONE/reap cannot release before producers and actual RESULT writes join.
    clock = ControlledClock()
    controller = LocalProcessScopeController(str(tmp_path), "quiescence", _scheduler=clock)
    output_gate, producer_waiting, writer_entered = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    writer_gate = threading.Event()
    output_tasks = []
    original_output, original_send = controller._output, process_core.send_frame
    original_gather = asyncio.gather
    loop = asyncio.get_running_loop()
    release_task = None

    async def output(scope, reader, name, captured) -> None:
        output_tasks.append(asyncio.current_task())
        await original_output(scope, reader, name, captured)
        await output_gate.wait()

    def gathered(*tasks, **kwargs):
        if len(tasks) == 2 and set(tasks) == set(output_tasks):
            producer_waiting.set()
        return original_gather(*tasks, **kwargs)

    def send(channel, frame) -> None:
        if frame["event"] == "RESULT":
            loop.call_soon_threadsafe(writer_entered.set)
            assert writer_gate.wait(10)
        original_send(channel, frame)

    monkeypatch.setattr(controller, "_output", output)
    monkeypatch.setattr(asyncio, "gather", gathered)
    monkeypatch.setattr(process_core, "send_frame", send)
    try:
        wrapped = await controller.wrap_stdio(
            "window",
            ProcessLaunchSpec(
                (
                    sys.executable,
                    "-u",
                    "-c",
                    "import sys;print('READY',flush=True);sys.stdin.read()",
                ),
                dict(os.environ),
                str(tmp_path),
            ),
        )
        process = await facade(wrapped)
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"READY\n"
        drain = asyncio.create_task(process.communicate())
        controller.owners.register("window", drain)
        controller.begin_teardown("window")
        process.terminate()
        await asyncio.wait_for(drain, 10)
        await asyncio.wait_for(producer_waiting.wait(), 10)
        scope = next(iter(controller._scopes.values()))
        control = scope.control
        assert control is not None
        await asyncio.wait_for(scope.facade_dead.wait(), 10)
        assert all(
            p.done
            and p.kernel_empty
            and p.streams_drained
            and p.custodian_reaped
            and p.facade_exited
            for p in controller.proofs
        )
        started = asyncio.Event()

        async def release():
            started.set()
            return await controller.await_release()

        release_task = asyncio.create_task(release())
        await asyncio.wait_for(started.wait(), 10)
        assert controller._root.result is None
        assert not release_task.done() and not controller._writes
        output_gate.set()
        await asyncio.wait_for(writer_entered.wait(), 10)
        writers = tuple(controller._writes)
        assert writers and any(not task.done() for task in writers)
        assert controller._root.result is None and not release_task.done()
        if expire:
            clock.advance(30)
            assert (await release_task).status == "unconfirmed"
            controller.begin_teardown(deadline=100)
            assert controller.owners.deadline() == 30
            assert controller.owners.quarantined > 0
            assert not controller.owners.mutations_allowed("window")
        joined: list[asyncio.Event] = []

        def subscribe[T](owner: asyncio.Future[T]) -> None:
            observed = asyncio.Event()
            owner.add_done_callback(lambda _done, event=observed: event.set())
            joined.append(observed)

        for owner in scope.resources:
            subscribe(owner)
        for entry in process_core._quarantine.get(loop, {}).values():
            if entry.controller is controller and isinstance(entry.owner, asyncio.Future):
                subscribe(entry.owner)
        writer_gate.set()
        await asyncio.wait_for(scope.completed.wait(), 10)
        await asyncio.wait_for(
            original_gather(*tuple(controller._tasks), return_exceptions=True), 10
        )
        for observed in joined:
            await asyncio.wait_for(observed.wait(), 10)
        # Join retirement work admitted by those actual resource completion callbacks.
        async with asyncio.timeout(10):
            await _join_scope_task_callbacks(controller)
        assert all(task.done() and isinstance(task.exception(), OSError) for task in writers)
        assert (await release_task).status == ("unconfirmed" if expire else "released")
        assert not controller._writes and not controller._send_tails
        assert controller.owners.quarantined == 0, [
            repr(entry.owner)
            for entry in process_core._quarantine.get(loop, {}).values()
            if entry.controller is controller
        ]
        assert not scope.resources
        assert control.closed
        with pytest.raises(ExecutionSpecError):
            controller._send(scope, control, {"event": "RESULT", "code": 0})
        if expire:
            assert not controller.owners.mutations_allowed("window")
    finally:
        output_gate.set()
        writer_gate.set()
        if controller._tasks:
            await asyncio.wait_for(
                original_gather(*tuple(controller._tasks), return_exceptions=True), 10
            )
        if release_task is not None:
            await asyncio.wait_for(release_task, 10)
        await controller.close()


async def test_facade_preserves_actual_cwd_and_env_per_launch(tmp_path: Path) -> None:
    # Requirement: SDK probes inherit parent cwd/env; explicit main/auth launches retain theirs.
    work = tmp_path / "work"
    work.mkdir()
    controller = LocalProcessScopeController(str(tmp_path), "cwd")
    code = (
        "import os,sys,json; print(json.dumps([os.getcwd(),"
        "os.environ.get('CORE_LAUNCH_VALUE'),sys.argv[1:]]))"
    )
    wrapped = await controller.wrap_stdio(
        "sdk",
        ProcessLaunchSpec(
            (sys.executable, "-c", code),
            {**os.environ, "CORE_LAUNCH_VALUE": "prototype"},
            str(work),
        ),
    )
    try:
        for args, cwd, env, expected in (
            (("-v",), None, None, [os.getcwd(), os.environ.get("CORE_LAUNCH_VALUE"), ["-v"]]),
            (
                ("main",),
                str(work),
                {**wrapped.env, "CORE_LAUNCH_VALUE": "main"},
                [str(work), "main", ["main"]],
            ),
            (
                ("auth",),
                str(work),
                {**wrapped.env, "CORE_LAUNCH_VALUE": "auth"},
                [str(work), "auth", ["auth"]],
            ),
        ):
            process = await asyncio.create_subprocess_exec(
                *wrapped.argv,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            out, err = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0 and not err
            assert json.loads(out) == expected
        assert (await controller.await_release()).status == "released"
        assert len({proof.scope_id for proof in controller.proofs}) == 3
    finally:
        await controller.close()


def _signal_wakeup_worker(held: bool) -> None:
    """Exercise real child SIGCHLD at the actual production clear boundary."""
    from conductor.execution.local_scope_launcher import _LinuxTree

    tree = _LinuxTree()
    tree.start(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; sys.stdout.write('READY'); sys.stdout.flush(); sys.stdin.buffer.read()",
        ],
        dict(os.environ),
        os.getcwd(),
    )
    assert tree.process is not None
    assert tree.stdin is not None and tree.stdout is not None and tree.stderr is not None
    target_stdin = tree.stdin
    assert os.read(tree.stdout.fileno(), 5) == b"READY"
    print(tree.process.pid, flush=True)
    returned_r, returned_w = os.pipe()
    original = signal.getsignal(signal.SIGCHLD)
    assert callable(original)

    def child_changed(signum, frame) -> None:
        os.write(1, b"ENTERED\n")
        original(signum, frame)
        os.write(1, b"RETURNED\n")
        os.write(returned_w, b"R")

    signal.signal(signal.SIGCHLD, child_changed)
    armed = False

    def trace(frame, event, _arg):
        nonlocal armed
        if armed or event != "line" or frame.f_code.co_name != "clear":
            return trace
        if frame.f_locals.get("self") is not tree.changed:
            return trace
        condition = getattr(tree.changed, "_cond", None)
        if condition is not None and not condition._lock.locked():
            return trace
        armed = True
        os.write(1, b"WINDOW\n")
        if held:
            target_stdin.close()
        assert os.read(0, 1) == b"G"
        return trace

    try:
        sys.settrace(trace)
        tree.changed.clear()
        sys.settrace(None)
        assert armed
        if not held:
            target_stdin.close()
            # Only the actual SIGCHLD handler can notify here: there are no pump threads.
            assert tree.changed.wait(2)
        assert os.read(returned_r, 1) == b"R"
        tree.reap()
        assert tree.code == 0 and tree.empty
        assert os.read(tree.stdout.fileno(), 1) == b""
        assert os.read(tree.stderr.fileno(), 1) == b""
        print("REAPED_DRAINED", flush=True)
    finally:
        sys.settrace(None)
        if tree.process.returncode is None:
            tree.process.kill()
            tree.process.wait()
        tree.close()
        os.close(returned_r)
        os.close(returned_w)


@pytest.mark.skipif(sys.platform != "linux", reason="Native SIGCHLD/subreaper wakeup")
@pytest.mark.parametrize("held", [False, True])
async def test_sigchld_reentry_at_clear_preserves_notification_and_reap(
    tmp_path: Path, held: bool
) -> None:
    # Requirement: real SIGCHLD during clear cannot deadlock; after-clear notification is not lost.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        __file__,
        str(tmp_path),
        "signal-overlap" if held else "signal-control",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload_handle: int | None = None
    try:
        assert process.stdout is not None and process.stdin is not None
        payload = int(await asyncio.wait_for(process.stdout.readline(), 10))
        payload_handle = os.pidfd_open(payload)
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"WINDOW\n"
        if not held:
            process.stdin.write(b"G")
            await process.stdin.drain()
        assert await asyncio.wait_for(process.stdout.readline(), 2) == b"ENTERED\n"
        assert await asyncio.wait_for(process.stdout.readline(), 2) == b"RETURNED\n"
        if held:
            process.stdin.write(b"G")
            await process.stdin.drain()
        out, err = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, err
        assert out == b"REAPED_DRAINED\n"
    finally:
        if payload_handle is not None:
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(payload_handle, signal.SIGKILL)
            os.close(payload_handle)
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _saturated_control_worker(root: Path, recovery: str) -> None:
    """Keep the real helper stopped until the external supervisor permits recovery."""
    from unittest.mock import patch

    clock = ControlledClock()
    controller = LocalProcessScopeController(str(root), "saturated", _scheduler=clock)
    filled = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_send = process_core.send_frame

    def saturated(channel, frame) -> None:
        if frame["event"] != "CONFIGURE":
            original_send(channel, frame)
            return
        scope = next(iter(controller._scopes.values()))
        assert scope.process is not None
        scope.process.send_signal(signal.SIGSTOP)
        pid, status = os.waitpid(scope.process.pid, os.WUNTRACED)
        assert os.WIFSTOPPED(status)
        body = json.dumps({"v": 1, **frame}).encode()
        remaining = memoryview(struct.pack("!i", len(body)) + body)
        with socket.socket(fileno=os.dup(channel.fileno())) as native:
            native.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            try:
                while remaining:
                    remaining = remaining[native.send(remaining, socket.MSG_DONTWAIT) :]
            except BlockingIOError:
                print(json.dumps({"pid": pid, "pending": len(remaining)}), flush=True)
                loop.call_soon_threadsafe(filled.set)
            native.sendall(remaining)

    with patch.object(process_core, "send_frame", saturated):
        command = asyncio.create_task(
            controller.spawn_script(
                "A",
                CommandSpec(
                    sys.executable, ("-c", "", *("x" * 10000 for _ in range(100))), stdin=b""
                ),
            )
        )
        controller.owners.register("A", command)
        try:
            await asyncio.wait_for(filled.wait(), 10)
            controller.begin_teardown("A")
            clock.advance(20)
            heartbeat = loop.create_future()

            def beat() -> None:
                print("HEARTBEAT", flush=True)
                heartbeat.set_result(None)

            loop.call_soon(beat)
            await heartbeat
            observation = asyncio.create_task(controller.owners.observe("A", command))
            clock.advance(30)
            assert not await asyncio.wait_for(observation, 10)
            terminal = await controller.owners.wait_terminal("A")
            assert terminal.status == "unconfirmed"
            controller.begin_teardown("A", deadline=100)
            assert controller.owners.deadline("A") == 30
            assert (await controller.await_release()).status == "unconfirmed"
            assert not controller.owners.mutations_allowed("A")
            scope = next(iter(controller._scopes.values()))
            assert not scope.kernel_empty
            joined: list[asyncio.Event] = []
            for entry in process_core._quarantine.get(loop, {}).values():
                if entry.controller is controller:
                    assert isinstance(entry.owner, asyncio.Future)
                    signal_joined = asyncio.Event()
                    entry.owner.add_done_callback(lambda _done, event=signal_joined: event.set())
                    joined.append(signal_joined)
            print("TERMINAL_UNCONFIRMED", flush=True)
            if recovery == "close":
                await controller.close()
                assert scope.force_write is not None
                with pytest.raises(OSError):
                    await asyncio.wait_for(asyncio.shield(scope.force_write), 10)
                assert controller.owners.deadline("A") == 30
                print("IPC_CLOSED", flush=True)
            # The supervisor has not resumed/killed the helper until this exact signal.
            assert await asyncio.to_thread(sys.stdin.buffer.readline) == b"RECOVERED\n"
            await asyncio.wait_for(command, 10)
            assert scope.process is not None
            await asyncio.wait_for(scope.process.wait(), 10)
            await asyncio.wait_for(
                asyncio.gather(*tuple(controller._tasks), return_exceptions=True), 10
            )
            for signal_joined in joined:
                await asyncio.wait_for(signal_joined.wait(), 10)
            write = scope.force_write
            assert write is not None and write.done()
            if recovery in ("kill", "close"):
                assert isinstance(write.exception(), OSError)
            else:
                assert write.exception() is None and scope.force_sent.is_set()
                assert "CUSTODIAN_READY" in scope.events
                assert "COMMAND_STARTED" not in scope.events
            assert not controller._writes and not controller._send_tails
            assert (await controller.close()).status == "unconfirmed"
            assert controller.owners.quarantined == 0, [
                (str(entry.owner), entry.owner.done())
                for entry in process_core._quarantine.get(loop, {}).values()
                if entry.controller is controller
            ]
            print(
                json.dumps(
                    {
                        "reaped": scope.reaped,
                        "writers_joined": True,
                        "late_send_failed": recovery in ("kill", "close"),
                    }
                ),
                flush=True,
            )
        finally:
            await controller.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Native stopped Unix control socket")
@pytest.mark.parametrize("recovery", ["resume", "kill", "close"])
async def test_saturated_control_does_not_block_terminal_driver(
    tmp_path: Path, recovery: str
) -> None:
    # Requirement: full control writes cannot block heartbeat/terminal or interleave frames.
    reference = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "",
        *("x" * 10000 for _ in range(100)),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(reference.communicate(), 10)
    assert reference.returncode == 0
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        __file__,
        str(tmp_path),
        recovery,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    handle: int | None = None
    try:
        assert worker.stdout is not None
        ready = json.loads(await asyncio.wait_for(worker.stdout.readline(), 10))
        assert ready["pending"] > 0
        handle = os.pidfd_open(ready["pid"])
        # Liveness itself is the behavior under test, with force queued before heartbeat.
        assert await asyncio.wait_for(worker.stdout.readline(), 2) == b"HEARTBEAT\n"
        assert await asyncio.wait_for(worker.stdout.readline(), 2) == b"TERMINAL_UNCONFIRMED\n"
        if recovery == "close":
            assert await asyncio.wait_for(worker.stdout.readline(), 2) == b"IPC_CLOSED\n"
        signal.pidfd_send_signal(handle, signal.SIGKILL if recovery == "kill" else signal.SIGCONT)
        output, error = await asyncio.wait_for(worker.communicate(b"RECOVERED\n"), 15)
        assert worker.returncode == 0, (output, error)
        receipt = json.loads(output)
        assert receipt["reaped"] and receipt["writers_joined"]
        assert receipt["late_send_failed"] == (recovery in ("kill", "close"))
    finally:
        if handle is not None:
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(handle, signal.SIGCONT)
            os.close(handle)
        if worker.returncode is None:
            await asyncio.wait_for(worker.communicate(b"RECOVERED\n"), 15)


async def test_admission_uses_root_gate_without_changing_owner_mutation_rights(
    tmp_path: Path,
) -> None:
    # Requirement: synchronous admission is side-effect-free and refuses sealed nonterminal roots.
    controller = LocalProcessScopeController(str(tmp_path), "admission")
    owner: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    controller.owners.register(None, owner)
    try:
        before = (tuple(controller._invocations), tuple(controller._launchers))
        controller.check_admission()
        controller.check_admission()
        assert (tuple(controller._invocations), tuple(controller._launchers)) == before
        controller.begin_teardown()
        assert controller._root.sealed and controller._root.result is None
        assert controller.owners.mutations_allowed(None)
        with pytest.raises(ExecutionSpecError, match="root is sealed"):
            controller.check_admission()
        assert (tuple(controller._invocations), tuple(controller._launchers)) == before
    finally:
        owner.set_result(None)
        assert (await controller.close()).status == "released"
        controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()


@pytest.mark.skipif(sys.platform != "linux", reason="Exact native descriptor baseline")
@pytest.mark.parametrize("wrapped", [False, True], ids=["script", "facade"])
async def test_consumed_scopes_retire_resources_in_a_live_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrapped: bool
) -> None:
    # Requirement: sequential consumed scopes release native resources without closing their root.
    controller = LocalProcessScopeController(str(tmp_path), "retirement")
    scopes = []
    original = controller._new_scope

    def created(*args):
        scope = original(*args)
        scopes.append(scope)
        return scope

    def descriptors() -> dict[int, str]:
        result = {}
        for path in Path("/proc/self/fd").iterdir():
            with contextlib.suppress(FileNotFoundError):
                result[int(path.name)] = os.readlink(path)
        return result

    monkeypatch.setattr(controller, "_new_scope", created)
    baseline = descriptors()
    threads = set(threading.enumerate())
    launches = (
        [
            await controller.wrap_stdio(
                family, ProcessLaunchSpec((sys.executable,), dict(os.environ), str(tmp_path))
            )
            for family in ("one", "two")
        ]
        if wrapped
        else []
    )
    saved_proofs = ()
    try:
        for index in range(16):
            output = f"{index}\x00" + "x" * 65536
            code = (
                f"import sys; sys.stdout.buffer.write({output.encode()!r}); "
                "sys.stderr.buffer.write(b'exact\\x00stderr')"
            )
            if wrapped:
                process = await facade(launches[index % 2], "-c", code)
                stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
                assert stdout == output.encode() and stderr == b"exact\x00stderr"
                assert process.returncode == 0
            else:
                result = await controller.spawn_script(
                    f"script-{index % 2}", CommandSpec(sys.executable, ("-c", code), stdin=b"")
                )
                assert result.stdout == output and result.stderr == "exact\x00stderr"
                assert result.exit_code == 0
            scope = scopes[-1]
            await asyncio.wait_for(scope.episode.terminal.wait(), 10)
            if controller._tasks:
                await asyncio.wait_for(
                    asyncio.gather(*tuple(controller._tasks), return_exceptions=True), 10
                )
            for thread in set(threading.enumerate()) - threads:
                thread.join(timeout=5)
                assert not thread.is_alive()
            if index == 15:
                print(
                    "RETIREMENT",
                    wrapped,
                    len(baseline),
                    len(descriptors()),
                    len(controller._connections),
                    sum(len(item.stdout) + len(item.stderr) for item in scopes),
                )
                assert descriptors() == baseline
                assert not controller._connections
                assert all(not item.stdout and not item.stderr for item in scopes)
                assert all(item.stdin is None and item.process is None for item in scopes)
                assert all(item.control is None and not item.channels for item in scopes)
                assert not controller._scopes
            assert controller.proofs[:-1] == saved_proofs
            saved_proofs = controller.proofs
            assert len(saved_proofs) == index + 1
            assert all(p.kernel_empty and p.custodian_reaped for p in saved_proofs)
            assert not controller._root.sealed
        assert len(controller._launchers) == (2 if wrapped else 0)
        await controller.force_stop(tuple(p.scope_id for p in controller.proofs))
        assert (await controller.await_release()).status == "released"
        assert controller.proofs == saved_proofs
    finally:
        await controller.close()
        controller._accepting.join(timeout=5)
        assert not controller._accepting.is_alive()


async def test_retirement_waits_for_script_consumption_and_owner_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: native proof alone cannot discard unconsumed bytes or an unjoined owner.
    controller = LocalProcessScopeController(str(tmp_path), "consumption")
    paused, consume = asyncio.Event(), asyncio.Event()
    original = asyncio.wait
    owner: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    controller.owners.register("script", owner)
    command = None

    async def wait(*args, **kwargs):
        result = await original(*args, **kwargs)
        if asyncio.current_task() is command:
            paused.set()
            await consume.wait()
        return result

    monkeypatch.setattr(asyncio, "wait", wait)
    command = asyncio.create_task(
        controller.spawn_script(
            "script",
            CommandSpec(
                sys.executable,
                ("-c", "import sys; sys.stdout.buffer.write(b'unconsumed\\n')"),
                stdin=b"",
            ),
        )
    )
    try:
        await asyncio.wait_for(paused.wait(), 10)
        scope = next(iter(controller._scopes.values()))
        assert scope.done and scope.reaped
        assert scope.stdout == b"unconsumed\n" and scope.control is not None
        assert not scope.control.closed
        consume.set()
        assert (await asyncio.wait_for(command, 10)).stdout == "unconsumed\n"
        assert scope.scope_id in controller._scopes and not scope.control.closed
        owner.set_result(None)
        await asyncio.wait_for(scope.episode.terminal.wait(), 10)
        if controller._tasks:
            await asyncio.wait_for(
                asyncio.gather(*tuple(controller._tasks), return_exceptions=True), 10
            )
        assert scope.scope_id not in controller._scopes
        assert not scope.stdout and scope.control is None
    finally:
        consume.set()
        if not owner.done():
            owner.set_result(None)
        await command
        await controller.close()


async def test_retirement_preserves_unconsumed_facade_bytes_and_reader_owner(
    tmp_path: Path,
) -> None:
    # Requirement: facade exit cannot retire channels before its registered reader consumes EOF.
    controller = LocalProcessScopeController(str(tmp_path), "reader")
    reader: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    controller.owners.register("sdk", reader)
    launch = await controller.wrap_stdio(
        "sdk",
        ProcessLaunchSpec(
            (
                sys.executable,
                "-c",
                "import sys; sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(b'not-yet-consumed\\x00'); "
                "sys.stderr.buffer.write(b'last-stderr')",
            ),
            dict(os.environ),
            str(tmp_path),
        ),
    )
    process = await facade(launch)
    assert process.stdin is not None
    process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), 10)
        scope = next(iter(controller._scopes.values()))
        await asyncio.wait_for(scope.completed.wait(), 10)
        assert scope.done and scope.reaped and scope.scope_id in controller._scopes
        assert scope.control is not None and not scope.control.closed
        assert process.stdout is not None and process.stderr is not None
        assert await process.stdout.read() == b"not-yet-consumed\x00"
        assert await process.stderr.read() == b"last-stderr"
        assert process.stdout.at_eof() and process.stderr.at_eof()
        assert scope.scope_id in controller._scopes
        reader.set_result(None)
        await asyncio.wait_for(scope.episode.terminal.wait(), 10)
        assert scope.scope_id not in controller._scopes
        assert process.stdout.at_eof() and process.stderr.at_eof()
        assert (await controller.await_release()).status == "released"
    finally:
        if not reader.done():
            reader.set_result(None)
        await controller.close()
        await process.communicate()


async def _terminal_sigint_worker(root: Path) -> None:
    """Own a controlling terminal and reap only this worker's test descendants."""
    import ctypes
    import select

    # The supervisor must also recover descendants when the unfixed custodian dies.
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0

    def descriptors() -> dict[int, str]:
        snapshot = {}
        for path in Path("/proc/self/fd").iterdir():
            with contextlib.suppress(FileNotFoundError):
                snapshot[int(path.name)] = os.readlink(path)
        return snapshot

    baseline_fds, baseline_threads = descriptors(), set(threading.enumerate())
    fifo = root / "ready"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[bytes] = loop.create_future()

    def readable() -> None:
        data = os.read(reader, 4096)
        if data:
            loop.remove_reader(reader)
            ready.set_result(data)

    loop.add_reader(reader, readable)
    payload = (
        "import json,os,signal,sys\n"
        "parent=os.getpid()\n"
        "if os.fork()==0:\n"
        " os.setsid()\n"
        " null=os.open(os.devnull,os.O_RDWR)\n"
        " for fd in (0,1,2): os.dup2(null,fd)\n"
        f" with open({str(root / 'descendant-write')!r},'wb') as output:\n"
        "  output.write(b'owned descendant'); output.flush()\n"
        f"  with open({str(fifo)!r},'w') as channel:\n"
        "   json.dump({'payload':parent,'descendant':os.getpid(),"
        "'sigint_default':signal.getsignal(signal.SIGINT)==signal.default_int_handler},channel)\n"
        "  signal.pause()\n"
        "signal.pause()\n"
    )
    controller = LocalProcessScopeController(str(root), "terminal")
    command = asyncio.create_task(
        controller.spawn_script("terminal", CommandSpec(sys.executable, ("-c", payload), stdin=b""))
    )
    controller.owners.register("terminal", command)
    handles: list[int] = []
    receipt = {}
    try:
        family = json.loads(await asyncio.wait_for(ready, 10))
        scope = next(iter(controller._scopes.values()))
        assert scope.process is not None
        for pid in (scope.process.pid, family["payload"], family["descendant"]):
            handles.append(os.pidfd_open(pid))
        receipt = {
            "parent": os.getpid(),
            "foreground": os.tcgetpgrp(0),
            "parent_group": os.getpgrp(),
            "custodian_group": os.getpgid(scope.process.pid),
            "custodian": scope.process.pid,
            "payload_group": os.getpgid(family["payload"]),
            "descendant_group": os.getpgid(family["descendant"]),
            **family,
        }
        print("READY " + json.dumps(receipt), flush=True)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            receipt["parent_cancelled"] = True
            controller.begin_teardown()
            deadline = controller._root.deadline
            command.cancel()
            try:
                await asyncio.shield(command)
            except asyncio.CancelledError:
                receipt["command_cancelled"] = True
            proof = await controller.await_release()
            controller.begin_teardown(deadline=loop.time() + 100)
            receipt.update(
                status=proof.status,
                original_deadline=deadline,
                final_deadline=controller._root.deadline,
                started=controller._root.started,
                proof_complete=all(
                    p.kernel_empty and p.streams_drained and p.custodian_reaped and p.done
                    for p in controller.proofs
                ),
                descendant_dead_before_cleanup=bool(select.select([handles[-1]], [], [], 0)[0]),
            )
    finally:
        # Stable handles never select unrelated processes, even on a failing RED run.
        kills = 0
        for handle in handles:
            if not select.select([handle], [], [], 0)[0]:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(handle, signal.SIGKILL)
                    kills += 1
        await controller.close()
        if controller._tasks:
            await asyncio.wait_for(
                asyncio.gather(*tuple(controller._tasks), return_exceptions=True), 5
            )
        if not command.done():
            command.cancel()
        await asyncio.wait({command}, timeout=5)
        for handle in handles:
            exited: asyncio.Future[None] = loop.create_future()
            loop.add_reader(handle, lambda event=exited: event.done() or event.set_result(None))
            try:
                await asyncio.wait_for(exited, 5)
            finally:
                loop.remove_reader(handle)
                os.close(handle)
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            assert pid > 0, "An owned child is still running after its pidfd reported exit"
        loop.remove_reader(reader)
        os.close(reader)
        for thread in set(threading.enumerate()) - baseline_threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert descriptors() == baseline_fds
        assert set(threading.enumerate()) == baseline_threads
        assert not controller._tasks and not controller._writes and not controller._connections
        assert not controller.owners.tasks and not controller.owners.quarantined
        assert asyncio.all_tasks() == {asyncio.current_task()}
        receipt.update(fallback_kills=kills, owned_fds=0, owned_threads=0, owned_tasks=0)
        print("RESULT " + json.dumps(receipt), flush=True)


@pytest.mark.skipif(
    sys.platform != "linux", reason="Native Linux controlling PTY and pidfd custody"
)
async def test_terminal_sigint_cancels_parent_without_destroying_custody(tmp_path: Path) -> None:
    # Requirement: terminal Ctrl+C cancels the parent; custody reaps detached setsid children.
    import pty

    master, slave = pty.openpty()
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        __file__,
        str(tmp_path),
        "terminal-sigint",
        stdin=slave,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    os.close(slave)
    try:
        assert worker.stdout is not None
        ready = await asyncio.wait_for(worker.stdout.readline(), 10)
        assert ready.startswith(b"READY "), ready
        family = json.loads(ready.removeprefix(b"READY "))
        assert family["parent"] == worker.pid
        assert family["foreground"] == family["parent_group"] == worker.pid
        assert family["parent_group"] != os.getpgrp()
        os.write(master, b"\x03")
        output, error = await asyncio.wait_for(worker.communicate(), 15)
        assert worker.returncode == 0, (output, error)
        print(ready.decode().strip(), output.decode().strip())
        result = json.loads(output.removeprefix(b"RESULT "))
        assert result["parent_cancelled"] and result["command_cancelled"]
        assert result["status"] == "released", (result, error.decode())
        assert result["proof_complete"] and result["descendant_dead_before_cleanup"]
        assert result["custodian_group"] == result["payload_group"] == result["custodian"]
        assert result["custodian_group"] != result["parent_group"]
        assert result["descendant_group"] == result["descendant"]
        assert result["sigint_default"]
        assert result["original_deadline"] == result["final_deadline"]
        assert result["original_deadline"] - result["started"] == 30
        assert result["fallback_kills"] == 0
        assert result["owned_fds"] == result["owned_threads"] == result["owned_tasks"] == 0
    finally:
        if worker.returncode is None:
            os.write(master, b"\x03")
            await asyncio.wait_for(worker.communicate(), 15)
        os.close(master)


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux default payload SIGINT")
async def test_managed_payload_keeps_default_sigint_behavior(tmp_path: Path) -> None:
    # Requirement: isolating custody must not make payloads inherit SIG_IGN.
    controller = LocalProcessScopeController(str(tmp_path), "payload-signal")
    try:
        result = await controller.spawn_script(
            "payload",
            CommandSpec(
                sys.executable,
                (
                    "-c",
                    "import os,signal; "
                    "assert signal.getsignal(signal.SIGINT)==signal.default_int_handler; "
                    "os.kill(os.getpid(),signal.SIGINT)",
                ),
                stdin=b"",
            ),
        )
        assert result.exit_code == -signal.SIGINT
        assert (await controller.await_release()).status == "released"
        assert controller.proofs[0].kernel_empty and controller.proofs[0].custodian_reaped
    finally:
        await controller.close()


if __name__ == "__main__":
    if sys.argv[2] in ("signal-overlap", "signal-control"):
        _signal_wakeup_worker(sys.argv[2] == "signal-overlap")
    elif sys.argv[2] == "terminal-sigint":
        import fcntl
        import termios

        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(0, os.getpgrp())
        asyncio.run(_terminal_sigint_worker(Path(sys.argv[1])))
    else:
        asyncio.run(_saturated_control_worker(Path(sys.argv[1]), sys.argv[2]))


def _installation_descriptor_identities() -> dict[int, tuple[int, int]]:
    result = {}
    for path in list(Path("/proc/self/fd").iterdir()):
        fd = int(path.name)
        try:
            info = os.fstat(fd)
        except OSError as error:
            assert error.errno == errno.EBADF
            continue
        result[fd] = (info.st_dev, info.st_ino)
    return result


def test_controller_preflight_requires_actual_running_loop_before_allocation(
    tmp_path: Path,
) -> None:
    # Requirement: direct callers outside a running loop allocate no controller resources.
    root = tmp_path / "not-created"
    with pytest.raises(RuntimeError, match="no running event loop"):
        LocalProcessScopeController(str(root), "no-loop")
    assert not root.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux procfs FD/thread identity")
async def test_direct_constructor_chmod_failure_closes_actual_listener_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: default construction owns permissions-failure cleanup without a backend.
    before = _installation_descriptor_identities()
    sentinel = tmp_path / "user-data"
    sentinel.write_bytes(b"keep")
    original_factory = process_core.private_listener
    original_chmod = os.chmod
    captured = []
    primary = PermissionError(errno.EACCES, "permissions primary", "private filename")

    def factory(address, *, register, transfer_failed):
        owner = getattr(register, "__self__", None)
        assert isinstance(owner, process_core._ControllerInstallation)
        captured.append(owner)
        return original_factory(address, register=register, transfer_failed=transfer_failed)

    def denied(path, *args, **kwargs):
        if str(path).startswith("/proc/") and str(path).endswith("/control"):
            raise primary
        return original_chmod(path, *args, **kwargs)

    monkeypatch.setattr(process_core, "private_listener", factory)
    monkeypatch.setattr(os, "chmod", denied)
    with pytest.raises(PermissionError) as caught:
        LocalProcessScopeController(str(tmp_path), "direct-failure")
    assert caught.value is primary and primary.__traceback__ is not None
    assert captured[0].outcome == "confirmed"
    assert captured[0].directory is not None and not captured[0].directory.exists()
    assert _installation_descriptor_identities() == before
    assert sentinel.read_bytes() == b"keep"


@pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux procfs FD/thread identity")
async def test_uncommitted_bootstrap_thread_never_enters_native_accept(tmp_path: Path) -> None:
    # Requirement: real startup threads wait for commit and abort without accepting or HELLO.
    before = _installation_descriptor_identities()
    owner = process_core._ControllerInstallation()
    controller = LocalProcessScopeController(str(tmp_path), "bootstrap", _installation=owner)
    primary = RuntimeError("abort uncommitted controller")
    try:
        assert owner.entered.wait(1) and not owner.gate.is_set()
        assert owner.thread is not None and owner.thread.ident is not None
        assert owner.thread.native_id is not None
        frame = sys._current_frames()[owner.thread.ident]
        frames = []
        while frame is not None:
            frames.append(frame.f_code.co_name)
            frame = frame.f_back
        assert "bootstrap" in frames and "_accept" not in frames
        assert not controller._connections and not controller._tasks and not controller.proofs
    finally:
        controller._closed = True
        with connect({"endpoint": controller._endpoint}, authenticate=False):
            pass
        assert owner.abort(primary) == "confirmed"
    assert owner.thread is not None and not owner.thread.is_alive()
    assert not Path(f"/proc/self/task/{owner.thread.native_id}").exists()
    assert _installation_descriptor_identities() == before
