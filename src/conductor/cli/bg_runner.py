"""Background runner for ``--web-bg`` mode.

When ``conductor run --web-bg`` is used, this module forks a detached child
process that runs the workflow with ``--web`` enabled, then the parent process
prints the dashboard URL and exits immediately.

The child process is fully detached (new session on Unix, new process group on
Windows) so it outlives the parent. It auto-shuts down after the workflow
completes and all WebSocket clients disconnect (the existing ``--web`` +
``bg=True`` behavior in ``WebDashboard``).
"""

from __future__ import annotations

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


def launch_background(
    *,
    workflow_path: Path,
    inputs: dict[str, Any],
    provider_override: str | None = None,
    skip_gates: bool = False,
    log_file: Path | None = None,
    no_interactive: bool = True,
    web_port: int = 0,
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

    # Wait for the web server to start
    if not _wait_for_server(web_port, timeout=15.0):
        # Check if the process already died
        retcode = proc.poll()
        if retcode is not None:
            raise RuntimeError(
                f"Background process exited immediately with code {retcode}. "
                f"Check logs or run without --web-bg for details."
            )
        raise RuntimeError(
            f"Dashboard did not start within 15 seconds on port {web_port}. "
            f"The background process (PID {proc.pid}) may still be starting."
        )

    # Write PID file so `conductor stop` can find this process
    from conductor.cli.pid import write_pid_file

    write_pid_file(proc.pid, web_port, workflow_path)

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
