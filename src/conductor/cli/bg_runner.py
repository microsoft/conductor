"""Background runner for ``--web-bg`` mode.

When ``conductor run --web-bg`` or ``conductor resume --web-bg`` is used, this
module forks a detached child process that runs the workflow with ``--web``
enabled, then the parent process prints the dashboard URL and exits
immediately.

The child process is fully detached (new session on Unix, new process group on
Windows) so it outlives the parent. It auto-shuts down after the workflow
completes and all WebSocket clients disconnect (the existing ``--web`` +
``bg=True`` behavior in ``WebDashboard``).

The child's stdout and stderr are redirected into log files under
``$TMPDIR/conductor/`` (not ``DEVNULL``) so a silent crash — an uncaught
Python exception, a ``faulthandler`` dump, or anything else the child
would normally write to ``sys.stderr`` — leaves a forensic trail. See
issue #116 for context.

This is also why we deliberately do NOT pass ``--silent`` to the child
even though no human is watching its console: ``--silent`` would also
set ``verbose_mode=False``, which gates provider-side SDK event logging
that ``--log-file`` writes to disk. Leaving verbosity at the default and
capturing the stream to a file keeps both the log files and any
``--log-file`` trace populated for detached children (see issue #196).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from io import IOBase
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

# NOTE: run-id format validation here defers to
# ``conductor.fleet.records.is_valid_run_id`` (imported lazily inside the
# functions that need it, to avoid a hard import cycle at module load time)
# rather than a hand-rolled hex-only regex. A resumed child reuses a
# checkpoint's ``run_id`` verbatim -- ``EventLogSubscriber``'s
# ``existing_path``/``existing_run_id`` branch performs no format check of
# its own -- and the only thing that actually gates whether that id can be
# used is whether the child's own ``write_run_record`` call accepts it as a
# filename component. A local hex-only copy of that rule (this module used
# to keep one) can reject a checkpoint ``run_id`` the child would happily
# reuse, causing the parent to poll for a freshly generated id the resumed
# child never writes its run record under (see ``_peek_resume_run_id``'s
# docstring).


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


def _spawn_detached(
    cmd: list[str],
    env: dict[str, str],
    *,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    stdin: Any = subprocess.DEVNULL,
) -> subprocess.Popen[Any]:
    """Launch a fully-detached child process for ``--web-bg`` mode.

    Composes the supplied stdio + environment + the platform-specific
    detachment kwargs from :func:`_detachment_kwargs`, then calls
    ``subprocess.Popen``. The default stdio is ``DEVNULL`` for all three
    streams; callers that need to capture the child's stderr/stdout
    (for diagnostics — see issue #116) can pass open file handles via
    the ``stdout`` / ``stderr`` kwargs.

    On Windows, if the Popen call fails with ``ERROR_ACCESS_DENIED`` because
    the parent's job object forbids breakaway, prints a visible warning to
    ``sys.stderr`` and retries WITHOUT ``CREATE_BREAKAWAY_FROM_JOB``. In that
    environment the child may still be killed when the parent's job closes;
    the warning sets that expectation so the user does not see only the
    "Dashboard: ..." line and assume success.

    Args:
        cmd: The fully-resolved command-line argv to execute.
        env: The environment dict to pass to the child (callers prepare this
            via :func:`_build_bg_env` with ``CONDUCTOR_WEB_BG`` /
            ``CONDUCTOR_WEB_PORT`` and the bg-diagnostics vars set).
        stdout: Popen ``stdout`` argument; defaults to ``DEVNULL``. Pass
            an open file handle to capture the child's stdout.
        stderr: Popen ``stderr`` argument; defaults to ``DEVNULL``. Pass
            an open file handle to capture the child's stderr.
        stdin: Popen ``stdin`` argument; defaults to ``DEVNULL``.

    Returns:
        The running :class:`subprocess.Popen` handle for the detached child.

    Raises:
        OSError: Propagated from ``Popen`` for any failure other than the
            Windows breakaway-denied case (e.g. ``FileNotFoundError`` for a
            missing executable). Callers wrap this in a ``RuntimeError``.
    """
    base: dict[str, Any] = {
        "stdout": stdout,
        "stderr": stderr,
        "stdin": stdin,
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


@dataclass(frozen=True, slots=True)
class BackgroundLaunch:
    """Result of launching a ``--web-bg`` child process.

    Attributes:
        url: The dashboard URL (e.g. ``http://127.0.0.1:8080``).
        stderr_log: Path to the file capturing the child's stderr — the
            first place to look when a bg run misbehaves silently.
        stdout_log: Path to the file capturing the child's stdout.
        run_id: The run id that ties this bg launch to its ``.events.jsonl``
            peer via ``CONDUCTOR_RUN_ID``. Normally exactly 8 lowercase hex
            characters (a fresh ``secrets.token_hex(4)``), but a resumed
            launch may force-carry a checkpoint's original run id instead
            (see ``_peek_resume_run_id``), which is validated against the
            same path-safe contract the fleet run-record store itself uses.

    Invariants (enforced in ``__post_init__``):
        * ``run_id`` is a valid fleet run id -- see
          ``conductor.fleet.records.is_valid_run_id``, the same contract
          the child's own ``write_run_record`` call enforces.
        * ``url`` is a localhost URL (``http://127.0.0.1:<port>``).
        * ``run_id`` appears in both ``stderr_log.name`` and
          ``stdout_log.name`` — this is what lets the bg log files and
          the child's ``.events.jsonl`` correlate by filename. See
          ``_open_bg_log_files``.
    """

    url: str
    stderr_log: Path
    stdout_log: Path
    run_id: str

    def __post_init__(self) -> None:
        from conductor.fleet.records import is_valid_run_id

        if not is_valid_run_id(self.run_id):
            raise ValueError(
                f"BackgroundLaunch.run_id must be a valid run id, got: {self.run_id!r}"
            )
        if not self.url.startswith("http://127.0.0.1:"):
            raise ValueError(f"BackgroundLaunch.url must be a localhost URL, got: {self.url!r}")
        if self.run_id not in self.stderr_log.name:
            raise ValueError(
                f"BackgroundLaunch.run_id {self.run_id!r} not embedded in "
                f"stderr_log filename {self.stderr_log.name!r}"
            )
        if self.run_id not in self.stdout_log.name:
            raise ValueError(
                f"BackgroundLaunch.run_id {self.run_id!r} not embedded in "
                f"stdout_log filename {self.stdout_log.name!r}"
            )


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
    (server reachability, the child's run record appearing) fails. Any
    errors raised while terminating are swallowed so the original failure
    surfaces to the caller.

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


# Filename pattern used by ``conductor.engine.event_log.EventLogSubscriber``
# for the events JSONL file. The bg stderr/stdout log files share the same
# ``<ts>-<runid>`` infix so all three artefacts for a single bg run sort
# next to each other in ``$TMPDIR/conductor/``.
_LOG_FILENAME_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_name(name: str) -> str:
    """Strip path-unsafe characters out of a workflow filename stem."""
    cleaned = _LOG_FILENAME_SAFE_NAME.sub("-", name).strip("-")
    return cleaned or "workflow"


def _open_bg_log_files(
    workflow_ref: Path, *, forced_run_id: str | None = None
) -> tuple[str, Path, Path, IOBase, IOBase]:
    """Create the bg child's stderr/stdout log files and return open handles.

    Generates a fresh 8-hex-character run id (unless ``forced_run_id`` is
    given) and opens two log files in ``$TMPDIR/conductor/`` whose names
    match the convention used by ``EventLogSubscriber`` (timestamp + run
    id) so all three artefacts of a single bg run group together by
    filename.

    The caller is responsible for closing the returned handles once
    ``subprocess.Popen`` has returned (the child has its own inherited OS
    handles by that point).

    Args:
        workflow_ref: The workflow file (or checkpoint) used to derive the
            ``<name>`` segment of the filename.
        forced_run_id: When given, use this run id instead of generating a
            random one. Used by ``launch_background_resume`` when the
            child is expected to reuse a checkpoint's original ``run_id``
            (see ``_peek_resume_run_id``) — the parent must use the exact
            same id for the bg log filenames, ``CONDUCTOR_RUN_ID``, and
            the run-record poll key, or the D2 launch gate polls for an id
            the child never writes.

    Returns:
        Tuple of ``(run_id, stderr_path, stdout_path, stderr_handle,
        stdout_handle)``.

    Raises:
        OSError: If the log directory cannot be created or the files
            cannot be opened. The caller is expected to surface this as a
            ``RuntimeError`` with context.
    """
    run_id = forced_run_id or secrets.token_hex(4)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = _sanitize_name(workflow_ref.stem) if workflow_ref.stem else "workflow"
    log_dir = Path(tempfile.gettempdir()) / "conductor"
    log_dir.mkdir(parents=True, exist_ok=True)

    stderr_path = log_dir / f"conductor-{base}-{ts}-{run_id}.bg.stderr.log"
    stdout_path = log_dir / f"conductor-{base}-{ts}-{run_id}.bg.stdout.log"
    # Line-buffered text mode so a tail of the file shows fresh output as
    # the child writes it. ``errors="replace"`` keeps the file readable
    # even if the child emits invalid UTF-8 (e.g. raw bytes from a
    # mis-encoded subprocess).
    stderr_handle = open(  # noqa: SIM115 - caller closes after Popen
        stderr_path, "w", encoding="utf-8", errors="replace", buffering=1
    )
    stdout_handle = open(  # noqa: SIM115 - caller closes after Popen
        stdout_path, "w", encoding="utf-8", errors="replace", buffering=1
    )
    return run_id, stderr_path, stdout_path, stderr_handle, stdout_handle


def _close_quietly(*handles: IOBase) -> None:
    """Close file handles, logging close errors to stderr but never raising.

    The parent's stdout/stderr file handles for the bg child should be
    released as soon as ``Popen`` returns (the child has its own duplicated
    OS handles by then), but ``handle.close()`` can still raise — most
    notably ``OSError`` from a buffer flush on a disk-full filesystem, or
    a Windows ``PermissionError`` if antivirus is scanning the file. The
    captured-log promise (#116) would be quietly broken in those cases,
    so print a warning to the parent's real stderr instead of suppressing
    silently. Never raise — callers run this in cleanup paths where
    propagating would mask an earlier, more relevant exception.
    """
    for h in handles:
        try:
            h.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            name = getattr(h, "name", "<unknown handle>")
            print(
                f"conductor: WARNING: failed to close bg log handle {name}: {exc}",
                file=sys.stderr,
            )


def _finalize_background_launch(
    proc: subprocess.Popen[Any],
    web_port: int,
    run_id: str,
    stderr_log: Path,
) -> None:
    """Wait for the dashboard to come up, then wait for the child's run record.

    On any failure (server didn't start, child died early, the child never
    wrote its run record within the timeout), the still-running child is
    terminated to avoid orphaned processes holding the dashboard port
    without a discoverable record. The stderr log path is included in the
    ``RuntimeError`` so callers can point users at the captured crash
    output.

    Per D2 (the child owns the write in every mode — see
    ``docs/projects/fleet-manager/fleet-manager.design.md``), the detached
    child is responsible for writing its own fleet run record (keyed by
    ``run_id``) once it starts executing. This function is the parent-side
    gate: it polls :func:`conductor.fleet.records.read_run_record` rather
    than writing a PID/record file itself, so there is exactly one writer
    (and hence exactly one record) per background run.

    The polled record must match this launch on three fields before the
    gate accepts it as readiness -- a record merely found under the right
    ``run_id`` is not proof by itself that *this* launch's child is up:

    * ``pid`` must equal the spawned child's ``proc.pid`` (e.g. rules out
      a stale record left behind under the same key by an unrelated
      process, or -- in the forced-resume-run-id case -- a leftover record
      from the *original*, now-dead run that the resumed child hasn't yet
      overwritten).
    * ``mode`` must be ``"bg"`` (rules out a record some other, differently
      launched process happens to have written under a colliding key).
    * ``port`` must equal ``web_port`` (rules out advertising an unrelated
      service's record when the requested port was occupied and the child
      fell back to a different, or no, port).

    A failure while reading the record itself (as opposed to the record
    simply not existing yet, which :func:`conductor.fleet.records.read_run_record`
    already reports as ``None``) also terminates the child and raises,
    rather than letting the exception escape uncontained and orphan the
    background process.

    Args:
        proc: The detached child process.
        web_port: The TCP port the child should be listening on.
        run_id: The run id shared with the child via ``CONDUCTOR_RUN_ID``,
            used to look up its run record once written.
        stderr_log: Path to the file capturing the child's stderr. Included
            in failure messages so users know where to look.

    Raises:
        RuntimeError: If the child died early, the dashboard didn't start
            within the timeout, the child never wrote a matching run record
            within the timeout, or reading the run record itself failed.
    """
    if not _wait_for_server(web_port, timeout=15.0):
        retcode = proc.poll()
        if retcode is not None:
            raise RuntimeError(
                f"Background process exited immediately with code {retcode}. "
                f"See child stderr log: {stderr_log}"
            )
        _terminate_child(proc)
        raise RuntimeError(
            f"Dashboard did not start within 15 seconds on port {web_port}. "
            f"The background process was terminated. "
            f"See child stderr log: {stderr_log}"
        )

    from conductor.fleet.records import read_run_record

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        retcode = proc.poll()
        if retcode is not None:
            raise RuntimeError(
                f"Background process exited immediately with code {retcode}. "
                f"See child stderr log: {stderr_log}"
            )
        try:
            record = read_run_record(run_id)
        except Exception as exc:
            _terminate_child(proc)
            raise RuntimeError(
                f"Failed to read background process's run record "
                f"(run_id={run_id}): {exc}. The background process was "
                f"terminated. See child stderr log: {stderr_log}"
            ) from exc
        if (
            record is not None
            and record.pid == proc.pid
            and record.mode == "bg"
            and record.port == web_port
        ):
            return
        time.sleep(0.2)

    _terminate_child(proc)
    raise RuntimeError(
        f"Background process did not report a run record within 15 seconds "
        f"(run_id={run_id}). The background process was terminated. "
        f"See child stderr log: {stderr_log}"
    )


def _build_bg_env(
    run_id: str,
    web_port: int,
    stderr_log: Path,
    stdout_log: Path,
) -> dict[str, str]:
    """Compose the child's environment with the bg-diagnostics env vars.

    Args:
        run_id: 8-hex-character run id shared with ``EventLogSubscriber`` so
            the events JSONL and bg log files use the same id in filenames
            and ``workflow_started`` system metadata.
        web_port: The TCP port the child should listen on.
        stderr_log: Path to the child's captured stderr log file.
        stdout_log: Path to the child's captured stdout log file.

    Returns:
        The new environment dict for ``subprocess.Popen``.
    """
    env = os.environ.copy()
    env["CONDUCTOR_WEB_BG"] = "1"
    env["CONDUCTOR_WEB_PORT"] = str(web_port)
    env["CONDUCTOR_RUN_ID"] = run_id
    env["CONDUCTOR_BG_STDERR_LOG"] = str(stderr_log)
    env["CONDUCTOR_BG_STDOUT_LOG"] = str(stdout_log)
    return env


def _spawn_bg_child(
    *,
    cmd: list[str],
    web_port: int,
    pid_workflow_ref: Path,
    forced_run_id: str | None = None,
) -> BackgroundLaunch:
    """Open the bg log files, spawn the detached child, and finalize the launch.

    Shared tail of both ``launch_background`` and ``launch_background_resume``.
    Keeping these steps in a single place is what guarantees the two paths
    cannot drift apart on the detachment flags, the stderr/stdout redirect,
    or the env-var contract that ``EventLogSubscriber`` and
    ``WorkflowEngine._build_system_metadata`` depend on. See issue #116.

    Args:
        cmd: Fully assembled subprocess command (already includes ``--silent``,
            the subcommand, ``--web``, ``--no-interactive``, etc.).
        web_port: The TCP port the child should listen on.
        pid_workflow_ref: Workflow or checkpoint path used as the source of
            the bg log filename stem (see ``_open_bg_log_files``).
        forced_run_id: When given, use this run id instead of generating a
            random one — see ``_peek_resume_run_id``. Passed through to
            ``_open_bg_log_files`` so the bg log filenames, the
            ``CONDUCTOR_RUN_ID`` env var, and the run-record poll key in
            ``_finalize_background_launch`` all agree on one id.

    Returns:
        ``BackgroundLaunch`` describing the live launch.

    Raises:
        RuntimeError: If the log files cannot be created, the child fails to
            start, or the dashboard doesn't become reachable.
    """
    try:
        run_id, stderr_path, stdout_path, stderr_handle, stdout_handle = _open_bg_log_files(
            pid_workflow_ref, forced_run_id=forced_run_id
        )
    except OSError as exc:
        raise RuntimeError(f"Failed to create background log files: {exc}") from exc

    # Spawn the detached child via the shared helper so the platform-
    # appropriate detachment kwargs — including Windows job breakaway and
    # the access-denied fallback — apply uniformly. Pass the log file
    # handles as stdio overrides so the child's output is captured (see
    # issue #116) instead of dropped on the floor.
    try:
        try:
            proc = _spawn_detached(
                cmd,
                _build_bg_env(run_id, web_port, stderr_path, stdout_path),
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to start background process: {exc}. See child stderr log: {stderr_path}"
            ) from exc

        _finalize_background_launch(proc, web_port, run_id, stderr_path)
    finally:
        # The child has its own duplicated OS handles by now (or never got
        # them, if Popen raised) — either way the parent's Python file
        # objects can be released without affecting the child.
        _close_quietly(stderr_handle, stdout_handle)

    return BackgroundLaunch(
        url=f"http://127.0.0.1:{web_port}",
        stderr_log=stderr_path,
        stdout_log=stdout_path,
        run_id=run_id,
    )


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
    print_loaded_instructions: bool = False,
) -> BackgroundLaunch:
    """Fork a detached child process running the workflow with a web dashboard.

    The child executes ``conductor run <workflow> --web --web-port <port>``
    with all the caller-supplied options. The parent waits briefly for the
    web server to become reachable, then returns the dashboard URL and the
    path to the child's captured stderr log.

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
        print_loaded_instructions: Whether to forward ``--print-loaded-instructions``
            to the background child. Output goes to the child's captured stderr
            log, not to the parent's TTY.

    Returns:
        A ``BackgroundLaunch`` describing the launch (dashboard URL,
        captured stderr/stdout log paths, run id).

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

    # Forward inputs -- via the hidden, strictly-typed --input-json flag
    # (not --input's public, ambiguous heuristic), since these values are
    # already declared-type-coerced and must round-trip verbatim (see
    # ``_serialize_input_value``/``cli/run.py::coerce_typed_value``).
    for key, value in inputs.items():
        cmd.extend(["--input-json", f"{key}={_serialize_input_value(value)}"])

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

    if print_loaded_instructions:
        cmd.append("--print-loaded-instructions")

    return _spawn_bg_child(cmd=cmd, web_port=web_port, pid_workflow_ref=workflow_path)


def _peek_resume_run_id(workflow_path: Path | None, checkpoint_path: Path | None) -> str | None:
    """Best-effort peek at the checkpoint the child will resume, to predict its ``run_id``.

    ``resume_workflow_async`` reuses the checkpoint's original ``run_id``
    (rather than generating a fresh one, or honoring ``CONDUCTOR_RUN_ID``)
    whenever the checkpoint's ``event_log_path`` still points at a real
    file — see ``EventLogSubscriber.__init__``'s
    ``existing_path``/``existing_run_id`` branch. The parent must mirror
    that exact same decision so ``_finalize_background_launch`` polls
    ``read_run_record`` for the id the child will actually write under
    (D2); polling a freshly generated id here would time out and
    terminate a perfectly healthy resumed run whenever the original log
    file survived.

    Best-effort: any failure to locate or parse the checkpoint returns
    ``None`` (falling back to a freshly generated id, the pre-existing
    behavior) rather than raising — the child's own checkpoint resolution
    is authoritative and reports a real error if something is actually
    wrong; this peek only affects which id the *parent* polls for.

    Args:
        workflow_path: Optional workflow YAML path (mirrors the resume
            command's positional arg).
        checkpoint_path: Optional explicit checkpoint path (mirrors
            ``--from``).

    Returns:
        The checkpoint's ``run_id`` if the child will reuse it (and it is
        a valid fleet run id -- see
        ``conductor.fleet.records.is_valid_run_id``, the same contract the
        child's own ``write_run_record`` call enforces -- and hence also
        satisfies ``BackgroundLaunch``'s invariant), else ``None``.
    """
    from conductor.engine.checkpoint import CheckpointManager
    from conductor.fleet.records import is_valid_run_id

    try:
        if checkpoint_path is not None:
            cp = CheckpointManager.load_checkpoint(checkpoint_path)
        elif workflow_path is not None:
            latest = CheckpointManager.find_latest_checkpoint(workflow_path)
            if latest is None:
                return None
            cp = CheckpointManager.load_checkpoint(latest)
        else:
            return None
    except Exception:
        return None

    if not cp.run_id or not cp.event_log_path:
        return None
    if not is_valid_run_id(cp.run_id):
        return None
    candidate = Path(cp.event_log_path)
    if candidate.exists() and candidate.is_file():
        return cp.run_id
    return None


def launch_background_resume(
    *,
    workflow_path: Path | None,
    checkpoint_path: Path | None,
    provider_override: str | None = None,
    skip_gates: bool = False,
    log_file: Path | None = None,
    web_port: int = 0,
    metadata: dict[str, str] | None = None,
    guidance: list[str] | None = None,
) -> BackgroundLaunch:
    """Fork a detached child process resuming the workflow with a web dashboard.

    The child executes ``conductor resume <workflow|--from path> --web ...``
    with all the caller-supplied options. ``--no-interactive`` is always
    appended since the detached child has no TTY. The parent waits briefly
    for the web server to become reachable, then returns the dashboard URL
    and the path to the child's captured stderr log.

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
        guidance: Optional mid-run guidance text(s) to apply before the
            resumed agent runs. Forwarded as repeated ``--guidance`` flags.

    Returns:
        A ``BackgroundLaunch`` describing the launch (dashboard URL,
        captured stderr/stdout log paths, run id).

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

    # Forward guidance
    if guidance:
        for text in guidance:
            cmd.extend(["--guidance", text])

    if provider_override:
        cmd.extend(["--provider", provider_override])

    if skip_gates:
        cmd.append("--skip-gates")

    if log_file:
        cmd.extend(["--log-file", str(log_file)])

    # Use workflow_path if available, otherwise fall back to checkpoint_path
    # for the bg log filename stem. The early guard at the top of this
    # function already rejected the case where both are None; the ``or``
    # here picks the first non-None.
    pid_workflow_ref: Path = workflow_path or checkpoint_path  # type: ignore[assignment]

    # Peek the checkpoint the child is about to resume so the parent polls
    # for the *same* run_id the child will actually write its run record
    # under (see ``_peek_resume_run_id``) — a resumed run whose original
    # event log survived reuses that log's run_id rather than a freshly
    # generated one, and generating our own here would poll the wrong key.
    forced_run_id = _peek_resume_run_id(workflow_path, checkpoint_path)

    return _spawn_bg_child(
        cmd=cmd,
        web_port=web_port,
        pid_workflow_ref=pid_workflow_ref,
        forced_run_id=forced_run_id,
    )


def _serialize_value(value: Any) -> str:
    """Serialize a value for passing as a CLI --metadata argument.

    Args:
        value: The value to serialize.

    Returns:
        String representation suitable for ``key=value`` CLI format.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _serialize_input_value(value: Any) -> str:
    """Serialize an already-typed input value for the ``--input-json`` boundary.

    Unlike :func:`_serialize_value`, this **always** JSON-encodes -- including
    plain strings -- and is decoded on the other side by
    ``cli/run.py::coerce_typed_value`` (a strict ``json.loads``) via the
    hidden ``--input-json`` flag.

    The pairing matters because values reaching here are already coerced to
    their **declared** types (``fleet/launch.py::_coerce_input`` maps the
    New Run form's fields onto the workflow's ``input:`` schema, returning a
    ``string``-typed value verbatim), and nothing downstream restores that
    typing: the engine's ``_apply_input_defaults`` only fills in *missing*
    inputs, so whatever the child's CLI parse produces is final. Routing
    these through the public ``--input`` flag instead would hand them to
    ``coerce_value``, whose command-line heuristic re-guesses a bare
    ``true``/``42``/``[1,2]`` into a bool/int/list -- silently discarding the
    declared ``string`` type the form was careful to preserve.

    Args:
        value: The already-coerced value to serialize.

    Returns:
        A JSON-encoded string representation suitable for ``key=value`` CLI
        format.
    """
    return json.dumps(value)
