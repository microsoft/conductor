"""Native managed scope regressions, using installed helper and real payloads."""

from __future__ import annotations

import asyncio
import contextlib
import json
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


async def test_force_driver_runs_while_native_owner_is_blocked(tmp_path: Path) -> None:
    # Requirement: EOF grace expiry forces the native tree independently of SDK finally.
    clock = ControlledClock()
    controller = LocalProcessScopeController(str(tmp_path), "inc", _scheduler=clock)
    release = asyncio.Event()
    owner = asyncio.create_task(release.wait())
    controller.owners.register("sdk", owner)
    wrapped = await controller.wrap_stdio(
        "sdk",
        ProcessLaunchSpec(
            (
                sys.executable,
                "-u",
                "-c",
                "import sys,threading; print('ready',flush=True); sys.stdin.read(); "
                "threading.Event().wait()",
            ),
            dict(os.environ),
            str(tmp_path),
        ),
    )
    try:
        process = await facade(wrapped)
        assert process.stdout is not None and process.stdin is not None
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
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
        release.set()
        await owner
        await controller.close()


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
                "script", CommandSpec(sys.executable, ("-c", "print('actual')"), stdin=b"")
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
        scopes.spawn_script("script", CommandSpec(sys.executable, ("-c", "print(7)"), stdin=b""))
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
        assert all(task.done() and isinstance(task.exception(), OSError) for task in writers)
        assert (await release_task).status == ("unconfirmed" if expire else "released")
        assert not controller._writes and not controller._send_tails
        assert controller.owners.quarantined == 0
        assert not scope.resources
        assert scope.control is not None
        with pytest.raises(ExecutionSpecError):
            controller._send(scope, scope.control, {"event": "RESULT", "code": 0})
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


if __name__ == "__main__":
    if sys.argv[2] in ("signal-overlap", "signal-control"):
        _signal_wakeup_worker(sys.argv[2] == "signal-overlap")
    else:
        asyncio.run(_saturated_control_worker(Path(sys.argv[1]), sys.argv[2]))
