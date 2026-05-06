"""Background runner for ``--web-bg`` mode.

When ``conductor run --web-bg`` or ``conductor resume --web-bg`` is used, this
module forks a detached child process that runs the workflow with ``--web``
enabled, then the parent process prints the dashboard URL and exits
immediately.

The child process is fully detached (new session on Unix, new process group on
Windows) so it outlives the parent. It auto-shuts down after the workflow
completes and all WebSocket clients disconnect (the existing ``--web`` +
``bg=True`` behavior in ``WebDashboard``).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _find_free_port() -> int:
    """Find an available TCP port on localhost.

    Returns:
        An available port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 15.0) -> bool:
    """Wait until the web server is accepting connections on *port*.

    Args:
        port: The TCP port to check.
        timeout: Maximum seconds to wait.

    Returns:
        True if the server became reachable within *timeout*, False otherwise.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _terminate_child(proc: subprocess.Popen[Any]) -> None:
    """Best-effort terminate a still-running child process.

    Used to avoid orphaned background workflows when post-launch validation
    (server reachability, PID file write) fails. Any errors raised while
    terminating are swallowed so the original failure surfaces to the caller.

    Args:
        proc: The subprocess.Popen handle to terminate.
    """
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2.0)
    except Exception:  # noqa: BLE001 - cleanup must not raise
        pass


def _finalize_background_launch(
    proc: subprocess.Popen[Any],
    web_port: int,
    pid_workflow_ref: Path,
) -> None:
    """Wait for the dashboard to come up and write the PID file.

    On any failure (server didn't start, child died early, PID write raised),
    the still-running child is terminated to avoid orphaned processes holding
    the dashboard port without a discoverable PID file.

    Args:
        proc: The detached child process.
        web_port: The TCP port the child should be listening on.
        pid_workflow_ref: Path used to derive the PID file name and recorded
            inside it for ``conductor stop`` to display.

    Raises:
        RuntimeError: If the child died early, the dashboard didn't start
            within the timeout, or the PID file could not be written.
    """
    if not _wait_for_server(web_port, timeout=15.0):
        retcode = proc.poll()
        if retcode is not None:
            raise RuntimeError(
                f"Background process exited immediately with code {retcode}. "
                f"Check logs or run without --web-bg for details."
            )
        _terminate_child(proc)
        raise RuntimeError(
            f"Dashboard did not start within 15 seconds on port {web_port}. "
            f"The background process was terminated."
        )

    from conductor.cli.pid import write_pid_file

    try:
        write_pid_file(proc.pid, web_port, pid_workflow_ref)
    except Exception as exc:
        _terminate_child(proc)
        raise RuntimeError(f"Failed to write PID file for background process: {exc}") from exc


def launch_background(
    *,
    workflow_path: Path,
    inputs: dict[str, Any],
    provider_override: str | None = None,
    skip_gates: bool = False,
    log_file: Path | None = None,
    no_interactive: bool = True,
    web_port: int = 0,
    metadata: dict[str, str] | None = None,
    workspace_instructions: bool = False,
    cli_instructions: list[str] | None = None,
) -> str:
    """Fork a detached child process running the workflow with a web dashboard.

    The child executes ``conductor run <workflow> --web --web-port <port>``
    with all the caller-supplied options. The parent waits briefly for the
    web server to become reachable, then returns the dashboard URL.

    Args:
        workflow_path: Path to the workflow YAML file.
        inputs: Workflow input key=value pairs.
        provider_override: Optional provider name override.
        skip_gates: Whether to auto-select first option at human gates.
        log_file: Optional log file path.
        no_interactive: Whether to disable interactive mode (always True for bg).
        web_port: Desired port (0 = auto-select).
        metadata: Optional CLI metadata key=value pairs.
        workspace_instructions: Whether to auto-discover workspace instruction files.
        cli_instructions: Optional list of instruction file paths.

    Returns:
        The dashboard URL (e.g. ``http://127.0.0.1:8080``).

    Raises:
        RuntimeError: If the child process fails to start or the server
            doesn't become reachable within the timeout.
    """
    # Resolve port early so we know what URL to return
    if web_port == 0:
        web_port = _find_free_port()

    # Build the subprocess command
    cmd: list[str] = [
        sys.executable,
        "-m",
        "conductor",
        "--silent",  # suppress CLI output in the background process
        "run",
        str(workflow_path),
        "--web",
        "--web-port",
        str(web_port),
        "--no-interactive",
    ]

    # Forward inputs
    for key, value in inputs.items():
        cmd.extend(["--input", f"{key}={_serialize_value(value)}"])

    # Forward metadata
    if metadata:
        for key, value in metadata.items():
            cmd.extend(["--metadata", f"{key}={_serialize_value(value)}"])

    if provider_override:
        cmd.extend(["--provider", provider_override])

    if skip_gates:
        cmd.append("--skip-gates")

    if log_file:
        cmd.extend(["--log-file", str(log_file)])

    if workspace_instructions:
        cmd.append("--workspace-instructions")

    if cli_instructions:
        for instr_path in cli_instructions:
            cmd.extend(["--instructions", instr_path])

    # Launch detached child
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }

    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:
        # Windows: CREATE_NEW_PROCESS_GROUP for detachment
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    # Set environment variables to signal bg mode to the child
    env = os.environ.copy()
    env["CONDUCTOR_WEB_BG"] = "1"
    env["CONDUCTOR_WEB_PORT"] = str(web_port)
    kwargs["env"] = env

    try:
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
    except Exception as exc:
        raise RuntimeError(f"Failed to start background process: {exc}") from exc

    _finalize_background_launch(proc, web_port, workflow_path)

    return f"http://127.0.0.1:{web_port}"


def launch_background_resume(
    *,
    workflow_path: Path | None,
    checkpoint_path: Path | None,
    provider_override: str | None = None,
    skip_gates: bool = False,
    log_file: Path | None = None,
    web_port: int = 0,
    metadata: dict[str, str] | None = None,
) -> str:
    """Fork a detached child process resuming the workflow with a web dashboard.

    The child executes ``conductor resume <workflow|--from path> --web ...``
    with all the caller-supplied options. ``--no-interactive`` is always
    appended since the detached child has no TTY. The parent waits briefly
    for the web server to become reachable, then returns the dashboard URL.

    Either ``workflow_path`` or ``checkpoint_path`` (or both) must be
    provided — at least one is required by the resume command.

    Args:
        workflow_path: Optional path to the workflow YAML file. Used to find
            the latest checkpoint when ``checkpoint_path`` is not given.
        checkpoint_path: Optional explicit path to a checkpoint file.
        provider_override: Optional provider name override.
        skip_gates: Whether to auto-select first option at human gates.
        log_file: Optional log file path.
        web_port: Desired port (0 = auto-select).
        metadata: Optional CLI metadata key=value pairs.

    Returns:
        The dashboard URL (e.g. ``http://127.0.0.1:8080``).

    Raises:
        ValueError: If neither ``workflow_path`` nor ``checkpoint_path`` is
            provided.
        RuntimeError: If the child process fails to start or the server
            doesn't become reachable within the timeout.
    """
    if workflow_path is None and checkpoint_path is None:
        raise ValueError(
            "launch_background_resume requires either workflow_path or checkpoint_path"
        )

    # Resolve port early so we know what URL to return
    if web_port == 0:
        web_port = _find_free_port()

    # Build the subprocess command
    cmd: list[str] = [
        sys.executable,
        "-m",
        "conductor",
        "--silent",  # suppress CLI output in the background process
        "resume",
    ]

    if workflow_path is not None:
        cmd.append(str(workflow_path))

    if checkpoint_path is not None:
        cmd.extend(["--from", str(checkpoint_path)])

    cmd.extend(
        [
            "--web",
            "--web-port",
            str(web_port),
            "--no-interactive",
        ]
    )

    # Forward metadata
    if metadata:
        for key, value in metadata.items():
            cmd.extend(["--metadata", f"{key}={_serialize_value(value)}"])

    if provider_override:
        cmd.extend(["--provider", provider_override])

    if skip_gates:
        cmd.append("--skip-gates")

    if log_file:
        cmd.extend(["--log-file", str(log_file)])

    # Launch detached child
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }

    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:
        # Windows: CREATE_NEW_PROCESS_GROUP for detachment
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    # Set environment variables to signal bg mode to the child
    env = os.environ.copy()
    env["CONDUCTOR_WEB_BG"] = "1"
    env["CONDUCTOR_WEB_PORT"] = str(web_port)
    kwargs["env"] = env

    try:
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
    except Exception as exc:
        raise RuntimeError(f"Failed to start background process: {exc}") from exc

    # Use workflow_path if available, otherwise fall back to checkpoint_path
    # for the PID file name and recorded reference.
    pid_workflow_ref = workflow_path if workflow_path is not None else checkpoint_path
    if pid_workflow_ref is None:  # pragma: no cover - guarded above
        _terminate_child(proc)
        raise ValueError(
            "launch_background_resume requires either workflow_path or checkpoint_path"
        )

    _finalize_background_launch(proc, web_port, pid_workflow_ref)

    return f"http://127.0.0.1:{web_port}"


def _serialize_value(value: Any) -> str:
    """Serialize a value for passing as a CLI --input argument.

    Args:
        value: The value to serialize.

    Returns:
        String representation suitable for ``key=value`` CLI format.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value)
