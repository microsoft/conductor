"""Background runner for ``--web-bg`` mode.

When ``conductor run --web-bg`` or ``conductor resume --web-bg`` is used, this
module forks a detached child process that runs the workflow with ``--web``
enabled, then the parent process prints the dashboard URL and exits
immediately.

The child process is fully detached (new session on Unix, new process group on
Windows) so it outlives the parent. It auto-shuts down after the workflow
completes and all WebSocket clients disconnect (the existing ``--web`` +
``bg=True`` behavior in ``WebDashboard``).

The child's stdout/stderr/stdin are redirected to ``DEVNULL`` in the Popen
call, not suppressed via ``--silent``. This is a deliberate design choice:
``--silent`` would also set ``verbose_mode=False``, which gates provider-side
SDK event logging that ``--log-file`` writes to disk. Relying on DEVNULL
keeps the file log populated for detached children where the user has no
other way to observe runtime behavior (see issue #196).
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

# Windows process creation flags. Exposed via ``getattr`` with documented
# fallbacks so this module can be imported on POSIX (where these attributes do
# not exist on ``subprocess``) and so tests can patch ``sys.platform`` to
# ``"win32"`` from a Linux/macOS host.
_CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_CREATE_BREAKAWAY_FROM_JOB: int = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)

# Win32 ERROR_ACCESS_DENIED — the error code raised when CreateProcess is
# called with ``CREATE_BREAKAWAY_FROM_JOB`` and the parent's job object has
# ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` cleared (some hardened CI environments).
_ERROR_ACCESS_DENIED = 5


def _detachment_kwargs() -> dict[str, Any]:
    """Return Popen kwargs that detach the child from the parent's lifecycle.

    On POSIX, ``start_new_session=True`` puts the child in its own session so
    it survives the parent and any controlling terminal closing.

    On Windows, ``CREATE_NEW_PROCESS_GROUP`` gives the child its own console
    process group (no shared Ctrl+C delivery) and ``CREATE_BREAKAWAY_FROM_JOB``
    detaches the child from the parent's Windows job object. The latter is
    required when the parent shell runs inside a job with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` set (e.g. GitHub Actions runners,
    VS Code integrated terminal, JetBrains IDE terminals, the GitHub Copilot
    CLI shell tool): without breakaway, the bg child inherits the job and is
    killed when the parent exits and the job tears down.

    Returns:
        Platform-specific Popen keyword arguments. The full Popen call should
        merge these with stdio + env kwargs.
    """
    if sys.platform == "win32":
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB}
    return {"start_new_session": True}


def _is_breakaway_denied(exc: OSError) -> bool:
    """Return True if a Popen ``OSError`` was caused by the parent job forbidding breakaway.

    Windows raises ``OSError`` with ``winerror == 5`` (ERROR_ACCESS_DENIED)
    when ``CREATE_BREAKAWAY_FROM_JOB`` is passed but the parent's job object
    has ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` cleared.

    Args:
        exc: The exception raised by ``subprocess.Popen``.

    Returns:
        True only for the access-denied breakaway case; other ``OSError``
        causes (e.g. ``FileNotFoundError`` for a missing executable) return
        False so the original error can propagate.
    """
    return getattr(exc, "winerror", None) == _ERROR_ACCESS_DENIED


def _spawn_detached(cmd: list[str], env: dict[str, str]) -> subprocess.Popen[Any]:
    """Launch a fully-detached child process for ``--web-bg`` mode.

    Composes DEVNULL stdio + the supplied environment + the platform-specific
    detachment kwargs from :func:`_detachment_kwargs`, then calls
    ``subprocess.Popen``.

    On Windows, if the Popen call fails with ``ERROR_ACCESS_DENIED`` because
    the parent's job object forbids breakaway, prints a visible warning to
    ``sys.stderr`` and retries WITHOUT ``CREATE_BREAKAWAY_FROM_JOB``. In that
    environment the child may still be killed when the parent's job closes;
    the warning sets that expectation so the user does not see only the
    "Dashboard: ..." line and assume success.

    Args:
        cmd: The fully-resolved command-line argv to execute.
        env: The environment dict to pass to the child (callers prepare this
            with ``CONDUCTOR_WEB_BG`` / ``CONDUCTOR_WEB_PORT`` set).

    Returns:
        The running :class:`subprocess.Popen` handle for the detached child.

    Raises:
        OSError: Propagated from ``Popen`` for any failure other than the
            Windows breakaway-denied case (e.g. ``FileNotFoundError`` for a
            missing executable). Callers wrap this in a ``RuntimeError``.
    """
    base: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    try:
        return subprocess.Popen(cmd, **base, **_detachment_kwargs())  # noqa: S603
    except OSError as exc:
        if not _is_breakaway_denied(exc):
            raise
        sys.stderr.write(
            "warning: parent shell forbids Windows job breakaway; the "
            "background workflow may not survive shell exit. Run "
            "--web-bg from a non-job-managed shell (e.g. a regular "
            "PowerShell window) for reliable persistence.\n"
        )
        return subprocess.Popen(  # noqa: S603
            cmd,
            **base,
            creationflags=_CREATE_NEW_PROCESS_GROUP,
        )


def _bg_child_env(web_port: int) -> dict[str, str]:
    """Build the child-process environment for ``--web-bg`` mode.

    Copies the current environment and sets the two signals the detached
    child reads to enable bg-specific behavior in the web dashboard.

    Args:
        web_port: The port the child's dashboard will listen on. Recorded
            in ``CONDUCTOR_WEB_PORT`` so the child can rebind if needed.

    Returns:
        A new environment dict suitable for passing to ``subprocess.Popen``.
    """
    env = os.environ.copy()
    env["CONDUCTOR_WEB_BG"] = "1"
    env["CONDUCTOR_WEB_PORT"] = str(web_port)
    return env


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

    # Build the subprocess command. Console output is already redirected to
    # DEVNULL via the Popen ``stdout``/``stderr`` kwargs below, so the child
    # runs at default verbosity. This keeps ``verbose_log()`` and provider
    # SDK event logging active so ``--log-file`` captures a real trace when
    # enabled (see issue #196).
    cmd: list[str] = [
        sys.executable,
        "-m",
        "conductor",
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

    # Launch detached child with platform-appropriate detachment kwargs.
    try:
        proc = _spawn_detached(cmd, _bg_child_env(web_port))
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

    # Build the subprocess command. Console output is already redirected to
    # DEVNULL via the Popen ``stdout``/``stderr`` kwargs below, so the child
    # runs at default verbosity. This keeps ``verbose_log()`` and provider
    # SDK event logging active so ``--log-file`` captures a real trace when
    # enabled (see issue #196).
    cmd: list[str] = [
        sys.executable,
        "-m",
        "conductor",
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

    # Launch detached child with platform-appropriate detachment kwargs.
    try:
        proc = _spawn_detached(cmd, _bg_child_env(web_port))
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
