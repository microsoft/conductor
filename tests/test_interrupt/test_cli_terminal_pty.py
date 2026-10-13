"""Real-PTY coverage for the outer ``conductor run`` terminal boundary."""

from __future__ import annotations

import sys

import pytest

if sys.platform == "win32":
    # Module-level skip must run BEFORE importing pty/termios (and the PTY
    # helper module), which do not exist on Windows — otherwise collection
    # fails instead of skipping.
    pytest.skip("PTY tests are Unix-only", allow_module_level=True)

import contextlib  # noqa: E402
import os  # noqa: E402
import pty  # noqa: E402
import signal  # noqa: E402
import termios  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from tests.test_interrupt.test_listener_pty import _replace_stdin_with_pty  # noqa: E402


def _without_pendin(attrs: list) -> list:
    """Drop ``PENDIN`` from ``lflag``: BSD/macOS kernels set it themselves when
    ICANON is re-enabled, until pending input is read, so it is not a setting
    the restore path controls."""
    attrs = list(attrs)
    attrs[3] &= ~getattr(termios, "PENDIN", 0)
    return attrs


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

        assert _without_pendin(termios.tcgetattr(0)) == _without_pendin(baseline)


@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["success", "failure"])
async def test_run_outcome_survives_failed_final_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    """Requirement: a termios.error from the final TTY restore never overwrites
    the workflow's own result or exception (issue #290)."""
    from conductor.cli.run import run_workflow_async

    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    with _replace_stdin_with_pty():
        engine = MagicMock()
        engine.config.workflow.cost.show_summary = False
        registry = AsyncMock()
        registry.__aenter__ = AsyncMock(return_value=registry)

        async def run_then_arm_restore_failure(_inputs: dict) -> dict:
            # Once the engine is done, every tcsetattr — listener stop() and
            # the outermost baseline restore — fails with a real termios.error.
            monkeypatch.setattr(
                termios, "tcsetattr", MagicMock(side_effect=termios.error("terminal gone"))
            )
            return {}

        engine.run = AsyncMock(side_effect=run_then_arm_restore_failure)

        async def maybe_fail_cleanup(*_args: object) -> None:
            if cleanup_fails:
                raise RuntimeError("provider cleanup failed")

        registry.__aexit__ = AsyncMock(side_effect=maybe_fail_cleanup)
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
            result = await run_workflow_async(_write_wait_workflow(tmp_path), {})

        if not cleanup_fails:
            assert result == {}


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
def test_cli_run_restores_terminal(tmp_path: Path, interrupt: bool) -> None:
    """Requirement: the real CLI restores exact TTY attrs on exit and Ctrl+C.

    The exec'd child must not see the developer's real HOME/tmp dirs: the
    parent process's monkeypatches (``tempfile.gettempdir()``, ``pid_dir()``,
    ``runs_dir()``) do not survive an exec, so without an explicit isolated
    environment the child's startup retention sweep would prune real event
    logs and legacy PID records.
    """
    home_dir = tmp_path / "home"
    tmp_dir = tmp_path / "tmp"
    home_dir.mkdir()
    tmp_dir.mkdir()
    env = dict(
        os.environ,
        HOME=str(home_dir),
        TMPDIR=str(tmp_dir),
        TMP=str(tmp_dir),
        TEMP=str(tmp_dir),
        CONDUCTOR_HOME=str(tmp_path / "conductor-home"),
    )
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
        os.execve(command[0], command, env)
        os._exit(127)

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
