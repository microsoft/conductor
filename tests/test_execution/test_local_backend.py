"""Contract tests for local command execution and ScriptExecutor delegation."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import ScriptStepDef
from conductor.exceptions import ExecutionError
from conductor.execution import (
    CommandOutcome,
    CommandResult,
    CommandSpec,
    LocalRunnerBackend,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    StartError,
    StartErrorKind,
    WorkspaceIdentity,
    WorkspaceLease,
)
from conductor.execution.errors import WorkspaceAttachError
from conductor.executor.script import ScriptExecutor


async def _wait_for_file(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {path}")


class TestLocalRunnerBackend:
    """Requirements for the local implementation of ``RunnerBackend``."""

    def test_capabilities_are_local_batch(self) -> None:
        # Requirement: local execution supports batch commands in a shared workspace only.
        capabilities = LocalRunnerBackend().capabilities()
        assert capabilities.batch is True
        assert capabilities.sessions is False
        assert capabilities.shared_workspace is True
        assert capabilities.snapshots is False

    @pytest.mark.asyncio
    async def test_prepare_run_returns_local_opaque_lease(self) -> None:
        # Requirement: preparation creates a local lease keyed by the run id.
        lease = await LocalRunnerBackend().prepare_run(RunSpec(run_id="run-1"))
        assert lease.lease_id == "run-1"
        assert lease.backend == "local"
        assert len(lease.incarnation) == 12
        assert lease.location is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expect_staged", [False, True])
    async def test_attach_rejects_retained_workspace_with_staging_policy(
        self, expect_staged: bool
    ) -> None:
        # Requirement: local attach accepts the shared staging policy but never attaches storage.
        with pytest.raises(WorkspaceAttachError, match="local backend does not support"):
            await LocalRunnerBackend().attach_run(
                RunSpec("run-1"),
                WorkspaceIdentity("local", "run-1", "token"),
                expect_staged=expect_staged,
            )

    @pytest.mark.asyncio
    async def test_echo_completed_result(self) -> None:
        # Requirement: argv, stdin, stdout, exit code, resolution, and timing cross the seam.
        result = await LocalRunnerBackend().run_command(
            CommandSpec(
                command=sys.executable,
                args=("-c", "import sys; print(sys.argv[1]); print(sys.stdin.read())", "arg"),
                stdin=b"input",
            ),
            None,
        )
        assert result.outcome == "completed"
        assert result.stdout.splitlines() == ["arg", "input"]
        assert result.stderr == ""
        assert result.exit_code == 0
        assert result.resolved_command
        assert result.duration_seconds > 0

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_completed(self) -> None:
        # Requirement: command failure is data, not an infrastructure error.
        result = await LocalRunnerBackend().run_command(
            CommandSpec(command=sys.executable, args=("-c", "raise SystemExit(42)")), None
        )
        assert result.outcome == "completed"
        assert result.exit_code == 42

    @pytest.mark.asyncio
    async def test_stderr_is_captured(self) -> None:
        # Requirement: stderr is decoded and returned separately from stdout.
        result = await LocalRunnerBackend().run_command(
            CommandSpec(
                command=sys.executable,
                args=("-c", "import sys; sys.stderr.write('problem')"),
            ),
            None,
        )
        assert result.stderr == "problem"

    @pytest.mark.asyncio
    async def test_environment_and_path_override_reach_child(self, tmp_path: Path) -> None:
        # Requirement: declared env overrides win and their PATH drives command resolution.
        # On Windows, shutil.which() resolves only names carrying a PATHEXT
        # extension, so an extensionless file would stay unresolved there.
        exe_name = "backend-python.exe" if sys.platform == "win32" else "backend-python"
        executable = tmp_path / exe_name
        executable.symlink_to(sys.executable)
        result = await LocalRunnerBackend().run_command(
            CommandSpec(
                command=executable.name,
                args=("-c", "import os; print(os.environ['LOCAL_BACKEND_VALUE'])"),
                env={"PATH": str(tmp_path), "LOCAL_BACKEND_VALUE": "visible"},
            ),
            None,
        )
        assert result.resolved_command == str(executable)
        assert result.stdout.strip() == "visible"

    @pytest.mark.asyncio
    async def test_working_directory_is_honored(self, tmp_path: Path) -> None:
        # Requirement: the backend passes the rendered working directory to spawn verbatim.
        result = await LocalRunnerBackend().run_command(
            CommandSpec(
                command=sys.executable,
                args=("-c", "import os; print(os.getcwd())"),
                working_dir=str(tmp_path),
            ),
            None,
        )
        assert os.path.realpath(result.stdout.strip()) == os.path.realpath(tmp_path)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("stdin", "expected"), [(b"payload", "payload"), (b"", "")])
    async def test_stdin_payload_and_empty_pipe(self, stdin: bytes, expected: str) -> None:
        # Requirement: bytes round-trip and an explicit empty payload means immediate EOF.
        result = await LocalRunnerBackend().run_command(
            CommandSpec(
                command=sys.executable,
                args=("-c", "import sys; sys.stdout.write(sys.stdin.read())"),
                stdin=stdin,
            ),
            None,
        )
        assert result.stdout == expected

    @pytest.mark.asyncio
    async def test_none_stdin_inherits(self) -> None:
        # Requirement: omitted stdin uses no PIPE and communicate receives None.
        process = AsyncMock()
        process.communicate.return_value = (b"", b"")
        process.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=process) as spawn:
            await LocalRunnerBackend().run_command(CommandSpec(command="tool"), None)
        assert spawn.call_args.kwargs["stdin"] is None
        process.communicate.assert_awaited_once_with(input=None)

    @pytest.mark.asyncio
    async def test_timeout_returns_outcome_and_kills_process(self) -> None:
        # Requirement: timeout kills and reaps the child before returning a timed-out outcome.
        process = AsyncMock()
        process.communicate.side_effect = TimeoutError
        process.kill = MagicMock()
        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await LocalRunnerBackend().run_command(
                CommandSpec(command="tool", timeout=0.01), None
            )
        assert result.outcome == "timed_out"
        assert result.duration_seconds < 1
        process.kill.assert_called_once_with()
        process.wait.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_cancel_during_timeout_cleanup_reraises_cancellation(self) -> None:
        # Requirement: a run cancellation racing the per-command timeout
        # cleanup is absorbed by the shielded drain and then re-raised -- it
        # must never collapse into a data-shaped timed_out result.
        process = AsyncMock()
        communicate_started = asyncio.Event()
        communicate_release = asyncio.Event()

        async def blocking_communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
            communicate_started.set()
            await communicate_release.wait()
            return (b"", b"")

        async def wait_completed() -> int:
            return -9

        process.communicate = blocking_communicate
        process.wait = wait_completed
        process.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=process):
            task = asyncio.create_task(
                LocalRunnerBackend().run_command(CommandSpec(command="tool", timeout=0.05), None)
            )
            await communicate_started.wait()
            await asyncio.sleep(0.15)  # the 0.05s timeout fires; drain begins
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done(), "the shielded drain must hold the cancellation back"
            communicate_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_command_not_found_is_data_shaped(self) -> None:
        # Requirement: missing commands return the resolved name and file-not-found metadata.
        original = FileNotFoundError(errno.ENOENT, "No such file or directory", "missing")
        with patch("asyncio.create_subprocess_exec", side_effect=original):
            result = await LocalRunnerBackend().run_command(CommandSpec(command="missing"), None)
        assert result.outcome == "command_not_found"
        assert result.resolved_command == "missing"
        assert result.start_error is not None
        assert result.start_error.kind == "file_not_found"
        assert result.start_error.message == original.strerror
        assert result.start_error.errno == original.errno
        assert result.start_error.filename == original.filename

    @pytest.mark.asyncio
    async def test_os_error_is_data_shaped(self) -> None:
        # Requirement: other spawn failures return start-failed metadata rather than raising.
        original = OSError(errno.EACCES, "Permission denied", "/secret/tool")
        with patch("asyncio.create_subprocess_exec", side_effect=original):
            result = await LocalRunnerBackend().run_command(CommandSpec(command="tool"), None)
        assert result.outcome == "start_failed"
        assert result.start_error is not None
        assert result.start_error.kind == "os_error"
        assert result.start_error.message == original.strerror
        assert result.start_error.errno == original.errno
        assert result.start_error.filename == original.filename

    @pytest.mark.asyncio
    async def test_cancelled_task_kills_and_reaps_child(self) -> None:
        # Requirement: cancellation propagates only after the backend kills and reaps its child.
        process = AsyncMock()
        process.communicate.side_effect = asyncio.CancelledError
        process.kill = MagicMock()
        with (
            patch("asyncio.create_subprocess_exec", return_value=process),
            pytest.raises(asyncio.CancelledError),
        ):
            await LocalRunnerBackend().run_command(CommandSpec(command="tool"), None)
        process.kill.assert_called_once_with()
        process.wait.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_process_lookup_error_does_not_mask_cancellation(self) -> None:
        # Requirement: a kill-versus-natural-exit race never replaces CancelledError.
        process = AsyncMock()
        process.communicate.side_effect = asyncio.CancelledError
        process.kill = MagicMock(side_effect=ProcessLookupError)
        with (
            patch("asyncio.create_subprocess_exec", return_value=process),
            pytest.raises(asyncio.CancelledError),
        ):
            await LocalRunnerBackend().run_command(CommandSpec(command="tool"), None)
        process.wait.assert_awaited_once_with()

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process liveness probe")
    async def test_real_cancelled_child_is_not_left_running(self, tmp_path: Path) -> None:
        # Requirement: cancelling real local execution does not orphan the spawned process.
        pid_file = tmp_path / "pid"
        task = asyncio.create_task(
            LocalRunnerBackend().run_command(
                CommandSpec(
                    command=sys.executable,
                    args=(
                        "-c",
                        "import os,pathlib,time; "
                        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                        "time.sleep(30)",
                    ),
                ),
                None,
            )
        )
        await _wait_for_file(pid_file)
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    @pytest.mark.asyncio
    async def test_cancel_drains_flooded_pipes_and_reaps_child(self) -> None:
        # Requirement: cancelling run_command against a continuously writing
        # child still kills and reaps it promptly (PR #541 review). The
        # pre-fix handler cancelled communicate() -- pausing the pipe
        # transports -- and then awaited process.wait(), which can stay
        # blocked behind flooded pipes even after the child is dead.
        spawned: list[asyncio.subprocess.Process] = []
        spawn_completed = asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec

        async def spy_spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            process = await real_spawn(*args, **kwargs)
            spawned.append(process)
            spawn_completed.set()
            return process

        with patch("asyncio.create_subprocess_exec", spy_spawn):
            task = asyncio.create_task(
                LocalRunnerBackend().run_command(
                    CommandSpec(
                        command=sys.executable,
                        args=(
                            "-u",
                            "-c",
                            "import sys\n"
                            "while True:\n"
                            "    sys.stdout.write('x' * 65536)\n"
                            "    sys.stdout.flush()\n"
                            "    sys.stderr.write('y' * 65536)\n"
                            "    sys.stderr.flush()",
                        ),
                    ),
                    None,
                )
            )
            # A loaded CI runner may spawn slowly; cancel only once the child
            # is genuinely up, or the assertions below would fail for the
            # wrong reason.
            await asyncio.wait_for(spawn_completed.wait(), timeout=10)
            await asyncio.sleep(0.3)  # let the child fill both pipe buffers
            task.cancel()
            # asyncio.wait (not wait_for) so a wedged cleanup cannot be
            # unwedged by a second cancellation at the deadline.
            done, _pending = await asyncio.wait({task}, timeout=10)
        if task not in done:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            pytest.fail("cancelling a flooded child wedged the kill/reap path")
        assert task.cancelled()
        process = spawned[0]
        # The child is reaped (not merely signalled) ...
        assert process.returncode is not None
        # ... and the shielded drain read both pipes to EOF -- the pre-fix
        # code left the paused transports unread, so the StreamReaders never
        # observed EOF.
        assert process.stdout is not None and process.stdout.at_eof()
        assert process.stderr is not None and process.stderr.at_eof()

    @pytest.mark.asyncio
    async def test_control_environment_can_be_disabled(self) -> None:
        # Requirement: disabling inheritance yields only PYTHONUTF8 plus declared overrides.
        process = AsyncMock()
        process.communicate.return_value = (b"", b"")
        process.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=process) as spawn:
            await LocalRunnerBackend().run_command(
                CommandSpec(
                    command="./tool",
                    env={"ONLY_THIS": "yes"},
                    inherit_control_environment=False,
                ),
                None,
            )
        assert spawn.call_args.kwargs["env"] == {"PYTHONUTF8": "1", "ONLY_THIS": "yes"}

    @pytest.mark.asyncio
    async def test_diagnostics_preserve_exact_strings_and_points(self) -> None:
        # Requirement: diagnostics receive the legacy pre-spawn and post-completion strings.
        process = AsyncMock()
        process.communicate.return_value = (b"", b" warning \n")
        process.returncode = 0
        diagnostics: list[str] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            await LocalRunnerBackend().run_command(
                CommandSpec(command="./tool", args=("one", "two"), stdin=b"abc"),
                None,
                diagnostics=diagnostics.append,
            )
        assert diagnostics == [
            "  Script: ./tool one two",
            "  Script stdin: 3 bytes",
            "  Script stderr: warning",
        ]


class RecordingBackend:
    """Minimal backend fake for the ScriptExecutor contract boundary."""

    def __init__(self, result: CommandResult, *, batch: bool = True) -> None:
        self.result = result
        self.batch = batch
        self.calls: list[
            tuple[CommandSpec, WorkspaceLease | None, Callable[[str], None] | None]
        ] = []

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=self.batch,
            sessions=False,
            shared_workspace=True,
            snapshots=False,
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        return WorkspaceLease(run.run_id, "recording", "one")

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Callable[[str], None] | None = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> CommandResult:
        if on_dispatch is not None:
            on_dispatch()
        self.calls.append((spec, lease, diagnostics))
        return self.result

    async def finalize_run(
        self, lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
    ) -> None:
        del lease, outcome


class TestScriptExecutorDelegation:
    """Requirements for the executor-to-backend seam and legacy parity."""

    @pytest.mark.asyncio
    async def test_executor_passes_one_rendered_unresolved_spec(self) -> None:
        # Requirement: the executor renders once but leaves host command resolution to backend.
        backend = RecordingBackend(
            CommandResult(
                outcome="completed",
                stdout="ok",
                stderr="",
                exit_code=0,
                resolved_command="/resolved/tool",
            )
        )
        lease = WorkspaceLease("run", "recording", "one")
        agent = ScriptStepDef(
            name="delegate",
            command="{{ command }}",
            args=["{{ value }}"],
            working_dir="{{ directory }}",
            env={"STATIC": "{{ literal }}"},
            stdin="{{ payload }}",
            timeout=1,
        )
        output = await ScriptExecutor(backend).execute(
            agent,
            {
                "command": "unresolved-tool",
                "value": "rendered",
                "directory": "/workspace",
                "literal": "ignored",
                "payload": "input",
            },
            lease=lease,
        )
        assert output.stdout == "ok"
        assert output.stdin_bytes == 5
        assert len(backend.calls) == 1
        spec, actual_lease, diagnostics = backend.calls[0]
        assert spec == CommandSpec(
            command="unresolved-tool",
            args=("rendered",),
            working_dir="/workspace",
            env={"STATIC": "{{ literal }}"},
            stdin=b"input",
            timeout=1,
        )
        assert actual_lease is lease
        assert diagnostics is not None

    def test_constructor_rejects_backend_without_batch(self) -> None:
        # Requirement: ScriptExecutor refuses a backend that cannot run batch commands.
        backend = RecordingBackend(CommandResult(outcome="completed"), batch=False)
        with pytest.raises(
            ExecutionError,
            match="Script execution backend does not support batch commands",
        ):
            ScriptExecutor(backend)

    @pytest.mark.asyncio
    async def test_completed_outcome_preserves_script_output(self) -> None:
        # Requirement: completed backend data maps to the unchanged public ScriptOutput shape.
        backend = RecordingBackend(
            CommandResult(outcome="completed", stdout="out", stderr="err", exit_code=42)
        )
        output = await ScriptExecutor(backend).execute(
            ScriptStepDef(name="completed", command="tool", stdin="é", timeout=None), {}
        )
        assert output.stdout == "out"
        assert output.stderr == "err"
        assert output.exit_code == 42
        assert output.stdin_bytes == len("é".encode())

    @pytest.mark.asyncio
    async def test_timeout_message_is_byte_identical(self) -> None:
        # Requirement: timed-out backend outcomes retain the exact legacy ExecutionError text.
        backend = RecordingBackend(CommandResult(outcome="timed_out"))
        with pytest.raises(ExecutionError) as exc_info:
            await ScriptExecutor(backend).execute(
                ScriptStepDef(name="slow", command="tool", timeout=1), {}
            )
        assert str(exc_info.value) == "Script 'slow' timed out after 1s"
        assert exc_info.value.__cause__ is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("platform", "hint"),
        [
            ("linux", ""),
            (
                "win32",
                " Hint: on Windows, include the file extension (e.g. .exe) "
                "or use an absolute path.",
            ),
        ],
    )
    async def test_command_not_found_message_is_byte_identical(
        self, platform: str, hint: str
    ) -> None:
        # Requirement: command-not-found mapping preserves working-dir text and Windows hint.
        start_error = StartError(kind="file_not_found", message="not found")
        backend = RecordingBackend(
            CommandResult(
                outcome="command_not_found",
                resolved_command="missing-tool",
                start_error=start_error,
            )
        )
        with (
            patch("conductor.executor.script.sys") as mock_sys,
            pytest.raises(ExecutionError) as exc_info,
        ):
            mock_sys.platform = platform
            await ScriptExecutor(backend).execute(
                ScriptStepDef(
                    name="missing",
                    command="tool",
                    working_dir="/workspace",
                    timeout=None,
                ),
                {},
            )
        assert str(exc_info.value) == (
            "Script 'missing': command not found: 'missing-tool' "
            f"(working_dir=/workspace){hint}\n\n"
            "💡 Suggestion: Ensure 'missing-tool' is installed and on PATH"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("original", "outcome"),
        [
            (FileNotFoundError("not found"), "command_not_found"),
            (OSError(errno.EACCES, "Permission denied", "/secret/path"), "start_failed"),
        ],
    )
    async def test_execution_error_cause_round_trip(self, original: OSError, outcome: str) -> None:
        # Requirement: both spawn-failure kinds preserve outer text and cause equivalence.
        kind: StartErrorKind = (
            "file_not_found" if isinstance(original, FileNotFoundError) else "os_error"
        )
        start_error = StartError(
            kind=kind,
            message=cast(str, original.strerror) if original.errno is not None else str(original),
            errno=original.errno,
            filename=cast(str | None, original.filename),
        )
        typed_outcome = cast(CommandOutcome, outcome)
        backend = RecordingBackend(
            CommandResult(
                outcome=typed_outcome,
                resolved_command="tool",
                start_error=start_error,
            )
        )
        with pytest.raises(ExecutionError) as exc_info:
            await ScriptExecutor(backend).execute(
                ScriptStepDef(name="failure", command="tool", timeout=None), {}
            )
        cause = exc_info.value.__cause__
        assert type(cause) is type(original)
        assert str(cause) == str(original)
        assert isinstance(cause, OSError)
        assert cause.errno == original.errno
        assert cause.filename == original.filename
        # The executor appends the Windows resolution hint only on win32, so
        # the expected text must branch on the real platform (unlike the
        # sibling byte-identity test, which patches sys.platform explicitly).
        hint = (
            " Hint: on Windows, include the file extension (e.g. .exe) or use an absolute path."
            if sys.platform == "win32"
            else ""
        )
        expected = (
            f"Script 'failure': command not found: 'tool' (working_dir=cwd){hint}\n\n"
            "💡 Suggestion: Ensure 'tool' is installed and on PATH"
            if outcome == "command_not_found"
            else f"Script 'failure' failed to start: {original}"
        )
        assert str(exc_info.value) == expected

    @pytest.mark.asyncio
    async def test_legacy_module_patch_targets_reach_local_backend(self) -> None:
        # Requirement: old executor module patches still control the extracted local logic.
        process = AsyncMock()
        process.communicate.return_value = (b"", b"")
        process.returncode = 0
        with (
            patch(
                "conductor.executor.script.shutil.which",
                return_value="/patched/tool",
            ) as mock_which,
            patch("conductor.executor.script.os.path.isabs", return_value=True),
            patch("asyncio.create_subprocess_exec", return_value=process) as spawn,
        ):
            await ScriptExecutor().execute(
                ScriptStepDef(name="legacy", command="C:/tool", timeout=None), {}
            )
        assert mock_which.call_args.args == ("C:/tool",)
        assert "path" in mock_which.call_args.kwargs
        assert spawn.call_args.args[0] == "/patched/tool"
