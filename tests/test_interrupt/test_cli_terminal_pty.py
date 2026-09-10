"""Real-PTY coverage for the outer ``conductor run`` terminal boundary."""

from __future__ import annotations

import contextlib
import os
import pty
import signal
import sys
import termios
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_interrupt.test_listener_pty import _replace_stdin_with_pty


def _write_wait_workflow(path: Path) -> Path:
    workflow = path / "wait.yaml"
    workflow.write_text(
        "workflow:\n"
        "  name: tty-cleanup\n"
        "  entry_point: wait\n"
        "agents:\n"
        "  - name: wait\n"
        "    type: wait\n"
        "    duration: 1ms\n"
        "    routes:\n"
        "      - to: $end\n"
    )
    return workflow


@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["success", "failure"])
async def test_run_restores_after_provider_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    """Requirement: provider teardown cannot bypass final TTY restoration."""
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    with _replace_stdin_with_pty():
        baseline = termios.tcgetattr(0)
        engine = MagicMock()
        engine.run = AsyncMock(return_value={})
        engine.config.workflow.cost.show_summary = False
        registry = AsyncMock()
        registry.__aenter__ = AsyncMock(return_value=registry)

        async def alter_terminal(*_args: object) -> None:
            import tty

            tty.setcbreak(0)
            if cleanup_fails:
                raise RuntimeError("provider cleanup failed")

        registry.__aexit__ = AsyncMock(side_effect=alter_terminal)
        expected_error = (
            pytest.raises(RuntimeError, match="provider cleanup failed")
            if cleanup_fails
            else contextlib.nullcontext()
        )
        with (
            patch("conductor.cli.run.ProviderRegistry", return_value=registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=engine),
            expected_error,
        ):
            await run_workflow_async(_write_wait_workflow(tmp_path), {})

        assert termios.tcgetattr(0) == baseline


async def test_non_interactive_run_does_not_reapply_retired_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement: a later non-interactive run must not reuse an old TTY baseline."""
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    workflow = _write_wait_workflow(tmp_path)
    with _replace_stdin_with_pty():
        engine = MagicMock()
        engine.run = AsyncMock(return_value={})
        engine.config.workflow.cost.show_summary = False
        registry = AsyncMock()
        registry.__aenter__ = AsyncMock(return_value=registry)
        registry.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("conductor.cli.run.ProviderRegistry", return_value=registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=engine),
        ):
            await run_workflow_async(workflow, {})

            import tty

            tty.setcbreak(0)
            expected = termios.tcgetattr(0)
            await run_workflow_async(workflow, {}, no_interactive=True)

        assert termios.tcgetattr(0) == expected


@pytest.mark.parametrize("interrupt", [False, True], ids=["normal-exit", "ctrl-c"])
def test_cli_run_restores_terminal(interrupt: bool) -> None:
    """Requirement: the real CLI restores exact TTY attrs on exit and Ctrl+C."""
    command = [
        sys.executable,
        "-m",
        "conductor.cli.app",
        "--silent",
        "run",
        str(Path("examples/wait-smoke.yaml").resolve()),
    ]
    if interrupt:
        command.extend(["--input", "middle_duration_ms=30000"])

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execv(command[0], command)

    baseline = termios.tcgetattr(master_fd)
    interrupted = False
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if interrupt and not interrupted and time.monotonic() > deadline - 19:
                os.write(master_fd, b"\x03")
                interrupted = True
            completed_pid, status = os.waitpid(pid, os.WNOHANG)
            if completed_pid:
                assert os.waitstatus_to_exitcode(status) == 0
                break
            time.sleep(0.02)
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("conductor run did not exit before the PTY test deadline")

        assert termios.tcgetattr(master_fd) == baseline
    finally:
        with contextlib.suppress(OSError, ChildProcessError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
        os.close(master_fd)
