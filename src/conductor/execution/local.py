"""Local-process implementation of the runner backend contract."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from collections.abc import Callable
from typing import cast
from uuid import uuid4

from conductor.execution.errors import ExecutionSpecError, WorkspaceAttachError
from conductor.execution.types import (
    CommandResult,
    CommandSpec,
    RunnerCapabilities,
    RunOutcome,
    RunSpec,
    StartError,
    WorkspaceIdentity,
    WorkspaceLease,
)


async def _kill_and_reap(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> bool:
    """Kill the child and finish draining it before returning.

    The shielded ``communicate_task`` is never cancelled here, so the pipe
    transports keep reading to EOF — a killed child that left full pipe
    buffers behind cannot wedge the reap. The subsequent ``process.wait()``
    runs as its own shielded task for the same reason: a second cancellation
    racing the cleanup is absorbed and reported (return value) instead of
    interrupting it, and the caller re-raises ``CancelledError`` afterward.

    Args:
        process: The spawned child process.
        communicate_task: The still-running ``Process.communicate()`` task.

    Returns:
        True when a racing cancellation was absorbed during cleanup and must
        be re-raised by the caller, False otherwise.
    """
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    absorbed_cancel = False
    while not communicate_task.done():
        try:
            await asyncio.shield(communicate_task)
        except asyncio.CancelledError:
            absorbed_cancel = True
        except Exception:
            # The drain itself failed; the child is already dead and the
            # explicit wait below still reaps it.
            break
    if communicate_task.done() and not communicate_task.cancelled():
        # Mark a finished drain's outcome as retrieved -- a failed one would
        # otherwise surface as "Task exception was never retrieved".
        communicate_task.exception()
    wait_task = asyncio.ensure_future(process.wait())
    while not wait_task.done():
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            absorbed_cancel = True
        except Exception:
            break
    if wait_task.done() and not wait_task.cancelled():
        wait_task.exception()
    return absorbed_cancel


class LocalRunnerBackend:
    """Run commands as child processes on the local machine."""

    def capabilities(self) -> RunnerCapabilities:
        """Declare local batch execution with a shared workspace."""
        return RunnerCapabilities(
            batch=True,
            sessions=False,
            shared_workspace=True,
            snapshots=False,
        )

    async def prepare_run(self, run: RunSpec) -> WorkspaceLease:
        """Create an opaque local workspace handle for a run."""
        if run.workspace_persistence is not None:
            raise ExecutionSpecError("local backend does not support retained workspaces")
        return WorkspaceLease(
            lease_id=run.run_id,
            backend="local",
            incarnation=uuid4().hex[:12],
            location=None,
        )

    async def attach_run(
        self, run: RunSpec, identity: WorkspaceIdentity, *, expect_staged: bool = False
    ) -> WorkspaceLease:
        """Reject attach without mutating anything; local workspaces cannot be retained."""
        del run, identity, expect_staged
        raise WorkspaceAttachError("local backend does not support retained workspaces")

    async def finalize_run(
        self, lease: WorkspaceLease, outcome: RunOutcome, *, retain: bool = False
    ) -> None:
        """Finalize a local run.

        Local execution owns no run-scoped resources, so ``retain`` is ignored.
        Remote backends use this lifecycle point to clean up their execution realm.
        """
        del lease, outcome, retain

    async def run_command(
        self,
        spec: CommandSpec,
        lease: WorkspaceLease | None,
        *,
        diagnostics: Callable[[str], None] | None = None,
        on_dispatch: Callable[[], None] | None = None,
    ) -> CommandResult:
        """Run one command locally and return its data-shaped outcome."""
        del lease
        started_at = time.monotonic()

        # Build environment (merge os.environ + declared overrides).
        # Always set PYTHONUTF8=1 so child Python processes use UTF-8 encoding
        # instead of the system default (cp1252 on Windows), preventing garbled
        # Unicode characters in script output.
        if spec.inherit_control_environment:
            env = {**os.environ, "PYTHONUTF8": "1", **spec.env}
        else:
            env = {"PYTHONUTF8": "1", **spec.env}

        # Resolve bare command names and absolute paths against PATH so that a
        # bare name (e.g. "python") finds the executable the shell would, and a
        # path missing an extension resolves correctly. Resolution uses the
        # subprocess's own ``PATH`` (``env`` may override it via ``spec.env``),
        # so the resolved binary matches the one the child would have executed.
        # Relative paths containing a separator are left untouched so they keep
        # resolving against ``working_dir``. Resolution is non-destructive: when
        # ``which`` cannot resolve the command we fall back to the rendered value.
        resolved_command = spec.command
        has_separator = os.sep in resolved_command or (
            os.altsep is not None and os.altsep in resolved_command
        )
        if os.path.isabs(resolved_command) or not has_separator:
            resolved_command = (
                shutil.which(resolved_command, path=env.get("PATH")) or resolved_command
            )

        if diagnostics is not None:
            diagnostics(f"  Script: {resolved_command} {' '.join(spec.args)}")
            if spec.stdin is not None:
                diagnostics(f"  Script stdin: {len(spec.stdin)} bytes")

        try:
            if on_dispatch is not None:
                on_dispatch()
            process = await asyncio.create_subprocess_exec(
                resolved_command,
                *spec.args,
                stdin=asyncio.subprocess.PIPE if spec.stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.working_dir,
                env=env,
            )
        except FileNotFoundError as exc:
            return CommandResult(
                outcome="command_not_found",
                resolved_command=resolved_command,
                start_error=StartError(
                    kind="file_not_found",
                    message=cast(str, exc.strerror) if exc.errno is not None else str(exc),
                    errno=exc.errno,
                    filename=cast(str | None, exc.filename),
                ),
                duration_seconds=time.monotonic() - started_at,
            )
        except OSError as exc:
            return CommandResult(
                outcome="start_failed",
                resolved_command=resolved_command,
                start_error=StartError(
                    kind="os_error",
                    message=cast(str, exc.strerror) if exc.errno is not None else str(exc),
                    errno=exc.errno,
                    filename=cast(str | None, exc.filename),
                ),
                duration_seconds=time.monotonic() - started_at,
            )

        # ``communicate`` runs as its own task, shielded from the wait_for
        # timeout and from cancellation of this coroutine, so pipe drainage
        # never stops mid-flight: killing a child whose output filled the
        # pipe buffers reaps reliably only while the parent keeps reading to
        # EOF. Cancelling communicate() itself pauses the pipe transports, and
        # a bare ``process.wait()`` after ``kill()`` can then stay blocked even
        # though the child is already dead (observed on the Windows CI fleet
        # with a continuously-writing child, returncode already -9).
        communicate_task = asyncio.create_task(process.communicate(input=spec.stdin))
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=spec.timeout
            )
        except TimeoutError:
            if await _kill_and_reap(process, communicate_task):
                # A run cancellation raced the timeout cleanup; it must not be
                # swallowed into a data-shaped outcome.
                raise asyncio.CancelledError from None
            return CommandResult(
                outcome="timed_out",
                resolved_command=resolved_command,
                duration_seconds=time.monotonic() - started_at,
            )
        except asyncio.CancelledError:
            await _kill_and_reap(process, communicate_task)
            raise

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        if stderr_text and diagnostics is not None:
            diagnostics(f"  Script stderr: {stderr_text.strip()}")

        # ``returncode`` is guaranteed non-None after ``communicate``.
        assert process.returncode is not None
        return CommandResult(
            outcome="completed",
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=process.returncode,
            resolved_command=resolved_command,
            duration_seconds=time.monotonic() - started_at,
        )
