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

**Two-stage readiness contract** (issue #410): a bg launch is considered
"finalized" only after two separate probes, not one. Stage one
(:func:`_wait_for_server`) is a plain TCP connect — it proves a process
is listening on the port, nothing more. Stage two
(:func:`_wait_for_workflow_start`) polls ``GET /api/info`` until the
payload carries a ``started_at`` key, which only appears once the
child's engine has actually emitted ``workflow_started`` — proving the
workflow itself began rather than just the dashboard's HTTP server. Both
stages check ``proc.poll()`` on every iteration so a child that exits
early (e.g. a ``ConfigurationError`` from a bad workflow) is reported in
about a second instead of after the full timeout. The stage-two wait
defaults to 30s and is tunable via ``CONDUCTOR_WEB_BG_START_TIMEOUT``
(``0`` disables it, restoring pre-#410 behavior); passing that deadline
with the child still alive is not treated as a failure — the URL is
still printed and the PID file is still left in place, since the
workflow may simply be slow to start (plugin fetch, MCP server startup,
provider connection).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from io import IOBase
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default wall-clock budget for the stage-two "did the workflow actually
# start" probe (issue #410), and the env var that overrides it. See the
# module docstring's "Two-stage readiness contract" section.
_START_TIMEOUT_DEFAULT = 30.0
_START_TIMEOUT_ENV = "CONDUCTOR_WEB_BG_START_TIMEOUT"

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

_RUN_ID_PATTERN_LOCAL = re.compile(r"[0-9a-f]{8}")


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
        run_id: 8-hex-character run id that ties this bg launch to its
            ``.events.jsonl`` peer via ``CONDUCTOR_RUN_ID``. On resume, this
            is adopted from the checkpoint's ``run_id`` when resolvable (see
            ``_checkpoint_run_id``), so the PID file, ``/api/info``, the
            events JSONL, and the capture-log filenames all agree on one id
            instead of the launcher minting an id that correlates with
            nothing.
        workflow_started: Whether the launcher observed the workflow actually
            start (via ``GET /api/info`` reporting a ``workflow_started``
            event — see ``_wait_for_workflow_start``) before its wait
            deadline. ``True`` means only that the launcher did **not**
            observe a failure to start — it is the default because the
            stage-two probe can be disabled entirely via
            ``CONDUCTOR_WEB_BG_START_TIMEOUT=0``, in which case nothing
            contradicts it. ``False`` means the deadline passed with the
            child still alive but not yet reporting a start; callers should
            surface this as a "still initializing" note rather than a
            failure — see issue #410.
        still_running: Whether the child process was still alive when the
            launcher finished waiting. ``True`` in the common case (the
            workflow is a genuine long-running background run). ``False``
            when the child completed (exit code 0) inside the launcher's
            wait window — either before the port opened or during the
            stage-two workflow-start probe. This is deliberately a separate
            field from ``workflow_started``: a clean sub-second run makes
            both ``True``, but callers must not report a URL/"running in
            background" for a process that has already exited (issue #410)
            — printing that message unconditionally on ``workflow_started``
            alone would reintroduce a narrower form of the same false-success
            bug this PR fixes.

    Invariants (enforced in ``__post_init__``):
        * ``run_id`` is exactly 8 lowercase hex characters.
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
    workflow_started: bool = True
    still_running: bool = True

    def __post_init__(self) -> None:
        if not _RUN_ID_PATTERN_LOCAL.fullmatch(self.run_id):
            raise ValueError(
                f"BackgroundLaunch.run_id must be 8 lowercase hex chars, got: {self.run_id!r}"
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


def _wait_for_server(
    port: int, timeout: float = 15.0, *, proc: subprocess.Popen[Any] | None = None
) -> bool:
    """Wait until the web server is accepting connections on *port*.

    Args:
        port: The TCP port to check.
        timeout: Maximum seconds to wait.
        proc: When given, checked with ``proc.poll()`` on every iteration so
            a dead child is detected in well under *timeout* instead of
            waiting out the full socket-connect deadline. Keyword-only so
            the many existing ``patch(..., "_wait_for_server")`` call sites
            (which invoke this positionally as ``(port, timeout)``) are
            unaffected by the added parameter.

    Returns:
        True if the server became reachable within *timeout*, False if the
        timeout elapsed or (when *proc* is given) the child exited first.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
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
    workflow_ref: Path, *, run_id: str | None = None
) -> tuple[str, Path, Path, IOBase, IOBase]:
    """Create the bg child's stderr/stdout log files and return open handles.

    Opens two log files in ``$TMPDIR/conductor/`` whose names match the
    convention used by ``EventLogSubscriber`` (timestamp + run id) so all
    three artefacts of a single bg run group together by filename.

    The caller is responsible for closing the returned handles once
    ``subprocess.Popen`` has returned (the child has its own inherited OS
    handles by that point).

    Args:
        workflow_ref: The workflow file (or checkpoint) used to derive the
            ``<name>`` segment of the filename.
        run_id: When given and it matches ``_RUN_ID_PATTERN_LOCAL`` (8
            lowercase hex chars), reuse it instead of minting a fresh one —
            used on resume so the whole launch adopts the checkpoint's run
            id. Otherwise a fresh ``secrets.token_hex(4)`` id is generated.

    Returns:
        Tuple of ``(run_id, stderr_path, stdout_path, stderr_handle,
        stdout_handle)``.

    Raises:
        OSError: If the log directory cannot be created or the files
            cannot be opened. The caller is expected to surface this as a
            ``RuntimeError`` with context.
    """
    resolved_run_id = (
        run_id if run_id is not None and _RUN_ID_PATTERN_LOCAL.fullmatch(run_id) else None
    ) or secrets.token_hex(4)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = _sanitize_name(workflow_ref.stem) if workflow_ref.stem else "workflow"
    log_dir = Path(tempfile.gettempdir()) / "conductor"
    log_dir.mkdir(parents=True, exist_ok=True)

    stderr_path = log_dir / f"conductor-{base}-{ts}-{resolved_run_id}.bg.stderr.log"
    stdout_path = log_dir / f"conductor-{base}-{ts}-{resolved_run_id}.bg.stdout.log"
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
    return resolved_run_id, stderr_path, stdout_path, stderr_handle, stdout_handle


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


class StartProbe(str, Enum):
    """Result of :func:`_wait_for_workflow_start`'s stage-two readiness probe.

    Mirrors the ``Liveness`` / ``Identity`` enum pattern in ``cli/pid.py`` and
    ``cli/app.py`` — a bool would collapse genuinely different outcomes that
    callers must react to differently.

    Attributes:
        STARTED: ``/api/info`` reported a ``started_at`` key — the child's
            engine has emitted ``workflow_started``.
        CHILD_EXITED: ``proc.poll()`` returned non-``None`` before the
            workflow was observed to start. The caller must still check the
            return code: a clean ``0`` exit (a sub-second workflow that
            finished within the wait window) is not a failure.
        PORT_CONFLICT: ``/api/info`` reported an ``int`` ``pid`` that does
            not match ``proc.pid`` — the port is answered by a foreign
            process, not our child.
        TIMED_OUT: The wait deadline passed with the child alive and no
            ``workflow_started`` observed yet.
    """

    STARTED = "started"
    CHILD_EXITED = "child_exited"
    PORT_CONFLICT = "port_conflict"
    TIMED_OUT = "timed_out"


def _resolve_start_timeout() -> float:
    """Resolve the stage-two start-wait deadline from the environment.

    Returns:
        ``CONDUCTOR_WEB_BG_START_TIMEOUT`` parsed as a float when it is a
        valid non-negative number (``0`` disables the probe entirely, which
        restores pre-#410 behavior). An unset, unparseable, or negative value
        falls back to :data:`_START_TIMEOUT_DEFAULT`, logging a warning for
        the latter two cases so a typo'd env var doesn't silently do nothing.
    """
    raw = os.environ.get(_START_TIMEOUT_ENV)
    if raw is None:
        return _START_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a valid number; using the default %.0fs.",
            _START_TIMEOUT_ENV,
            raw,
            _START_TIMEOUT_DEFAULT,
        )
        return _START_TIMEOUT_DEFAULT
    if value < 0:
        logger.warning(
            "%s=%r must not be negative; using the default %.0fs.",
            _START_TIMEOUT_ENV,
            raw,
            _START_TIMEOUT_DEFAULT,
        )
        return _START_TIMEOUT_DEFAULT
    return value


def _probe_workflow_info(port: int) -> dict[str, Any] | None:
    """Fetch ``GET /api/info`` from the dashboard on *port*, once.

    This is the same identity endpoint ``conductor stop`` polls (see
    ``cli/app.py::_confirm_identity``), so this adds no new endpoint and no
    new dependency — ``httpx`` is already a hard dependency, imported lazily
    inside functions elsewhere in this codebase.

    Args:
        port: The TCP port the dashboard should be listening on.

    Returns:
        The parsed JSON body when the request succeeds, returns a 2xx status,
        and decodes to a dict. ``None`` on a connection/timeout/HTTP-status
        failure or a non-dict body (logged at debug — a single failed probe
        is expected while the child is still starting up and not actionable
        on its own). An exception outside that expected set (a bug in this
        function, or a body large enough to raise on decode) is logged at
        *warning* instead of being folded into the same "not ready yet"
        bucket — a genuinely broken probe must not be indistinguishable from
        normal startup latency for the caller's full wait window.
    """
    import httpx

    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/api/info", timeout=1.0)
        resp.raise_for_status()
        info = resp.json()
    except httpx.HTTPError as exc:
        # Connection refused/reset, timeout, or a non-2xx status (including a
        # foreign, non-Conductor process answering the port) — all expected
        # "not ready yet" outcomes for a dashboard that hasn't bound the port
        # or started serving yet.
        logger.debug("Workflow-start probe on port %s failed: %s", port, exc)
        return None
    except ValueError as exc:
        # ``.json()`` raises a ``JSONDecodeError`` (a ``ValueError`` subclass)
        # on a non-JSON body — plausible from the same foreign-process case
        # above. Still just "not ready yet" from this caller's perspective.
        logger.debug("Workflow-start probe on port %s returned unparseable JSON: %s", port, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - see docstring: log loudly, don't hide a real bug
        logger.warning(
            "Workflow-start probe on port %s failed with an unexpected %s: %s",
            port,
            type(exc).__name__,
            exc,
        )
        return None
    return info if isinstance(info, dict) else None


def _wait_for_workflow_start(
    port: int, proc: subprocess.Popen[Any], *, timeout: float
) -> StartProbe:
    """Poll ``/api/info`` until the workflow reports having started.

    Args:
        port: The TCP port the dashboard is listening on.
        proc: The detached child process, checked with ``proc.poll()`` on
            every iteration so a dead child is detected immediately rather
            than after the full timeout.
        timeout: Maximum seconds to wait.

    Returns:
        :class:`StartProbe` describing why the wait ended.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return StartProbe.CHILD_EXITED

        info = _probe_workflow_info(port)
        if info is not None:
            reported_pid = info.get("pid")
            if isinstance(reported_pid, int) and reported_pid != proc.pid:
                return StartProbe.PORT_CONFLICT
            # Key presence, not truthiness: ``started_at`` is
            # ``event.get("timestamp", 0)`` server-side and could
            # legitimately be ``0``. That key only exists on the
            # ``workflow_started`` branch of the endpoint.
            if "started_at" in info:
                return StartProbe.STARTED

        time.sleep(0.3)
    return StartProbe.TIMED_OUT


def _tail_log(path: Path, max_lines: int = 20, max_chars: int = 2000) -> str:
    """Return a bounded tail of a captured bg log file for error messages.

    Never raises — a diagnostics helper must not be the thing that breaks
    the launch. Returns ``""`` when the file is missing or unreadable.

    Args:
        path: Path to the captured stderr/stdout log file.
        max_lines: Maximum number of trailing lines to include.
        max_chars: Maximum total characters to include (applied after the
            line limit, in case a single line is enormous).

    Returns:
        A header plus the tail of the file's contents, or ``""`` on failure.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()[-max_lines:]
    tail = "\n".join(lines)
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    if not tail:
        return ""
    return f"\n--- last {len(lines)} line(s) of {path} ---\n{tail}"


def _finalize_background_launch(
    proc: subprocess.Popen[Any],
    web_port: int,
    pid_workflow_ref: Path,
    stderr_log: Path,
    *,
    run_id: str,
    stdout_log: Path,
) -> bool:
    """Wait for the dashboard to come up, write the PID file, then confirm the workflow started.

    On any failure (server didn't start, child died early, PID write raised),
    the still-running child is terminated to avoid orphaned processes holding
    the dashboard port without a discoverable PID file. The stderr log path
    (with a bounded tail of its contents) is included in the RuntimeError so
    callers can point users at the captured crash output.

    The PID file records ``run_id`` and both capture-log paths so a bg run
    can be correlated to its events JSONL (via ``run_id``) and its captured
    stderr/stdout after the launching terminal is gone. It is written as
    soon as the port opens — before the stage-two workflow-start wait —
    because that wait can legitimately run up to 30s, and a run invisible to
    ``conductor status``/``conductor stop`` for that whole window is worse
    than a briefly-premature entry (issue #410). If the child then turns out
    to be dead, the entry is removed via ``remove_pid_file_at``, which
    re-reads the file and refuses to unlink one that no longer describes our
    PID.

    Args:
        proc: The detached child process.
        web_port: The TCP port the child should be listening on.
        pid_workflow_ref: Path used to derive the PID file name and recorded
            inside it for ``conductor stop`` to display.
        stderr_log: Path to the file capturing the child's stderr. Included
            in failure messages so users know where to look.
        run_id: The run id shared with the child via ``CONDUCTOR_RUN_ID``,
            recorded in the PID file.
        stdout_log: Path to the file capturing the child's stdout, recorded
            in the PID file.

    Returns:
        ``True`` if the workflow was observed to start (or the stage-two
        probe is disabled, or the child exited cleanly within the wait
        window). ``False`` if the stage-two wait deadline passed with the
        child still alive and not yet reporting a start — not a failure,
        just "still initializing".

    Raises:
        RuntimeError: If the child died early (with a non-zero exit code),
            the dashboard didn't start within the timeout, the PID file
            could not be written, the child died before the workflow started
            (non-zero exit), or a foreign process already holds the port.
    """
    if not _wait_for_server(web_port, timeout=15.0, proc=proc):
        retcode = proc.poll()
        if retcode is not None:
            if retcode == 0:
                # A sub-second run that finished before the socket became
                # reachable is not a failure, and there's no live process to
                # track — no PID file to write.
                return True
            raise RuntimeError(
                f"Background process exited immediately with code {retcode}. "
                f"See child stderr log: {stderr_log}{_tail_log(stderr_log)}"
            )
        _terminate_child(proc)
        raise RuntimeError(
            f"Dashboard did not start within 15 seconds on port {web_port}. "
            f"The background process was terminated. "
            f"See child stderr log: {stderr_log}{_tail_log(stderr_log)}"
        )

    from conductor.cli.pid import remove_pid_file_at, write_pid_file

    try:
        pid_path = write_pid_file(
            proc.pid,
            web_port,
            pid_workflow_ref,
            run_id=run_id,
            stderr_log=str(stderr_log),
            stdout_log=str(stdout_log),
        )
    except Exception as exc:
        _terminate_child(proc)
        raise RuntimeError(
            f"Failed to write PID file for background process: {exc}. "
            f"See child stderr log: {stderr_log}"
        ) from exc

    start_timeout = _resolve_start_timeout()
    if start_timeout == 0:
        return True

    probe = _wait_for_workflow_start(web_port, proc, timeout=start_timeout)

    if probe is StartProbe.STARTED:
        return True

    if probe is StartProbe.TIMED_OUT:
        logger.info(
            "Workflow on port %s has not reported starting after %.0fs; "
            "leaving it running. Set %s to tune this wait.",
            web_port,
            start_timeout,
            _START_TIMEOUT_ENV,
        )
        return False

    if probe is StartProbe.CHILD_EXITED:
        retcode = proc.poll()
        if retcode == 0:
            # Completed inside the window; the child already removed its
            # own PID file.
            return True
        remove_pid_file_at(pid_path, proc.pid)
        raise RuntimeError(
            "Background process exited before the workflow started "
            f"(code {retcode}). See child stderr log: "
            f"{stderr_log}{_tail_log(stderr_log)}"
        )

    # probe is StartProbe.PORT_CONFLICT
    _terminate_child(proc)
    remove_pid_file_at(pid_path, proc.pid)
    info = _probe_workflow_info(web_port)
    foreign_pid = info.get("pid") if info else "unknown"
    raise RuntimeError(
        f"Port {web_port} is already in use by another process (PID {foreign_pid}). "
        "Choose a different port with --web-port."
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
    run_id: str | None = None,
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
        pid_workflow_ref: Workflow or checkpoint path used both as the source
            of the log filename stem and as the PID file's recorded reference.
        run_id: Optional run id to adopt for this launch (e.g. resolved from a
            checkpoint on resume) instead of minting a fresh one. Passed
            through to :func:`_open_bg_log_files`, which falls back to a
            fresh id if this doesn't match the expected 8-hex-char shape.

    Returns:
        ``BackgroundLaunch`` describing the live launch.

    Raises:
        RuntimeError: If the log files cannot be created, the child fails to
            start, or the dashboard doesn't become reachable.
    """
    try:
        run_id, stderr_path, stdout_path, stderr_handle, stdout_handle = _open_bg_log_files(
            pid_workflow_ref, run_id=run_id
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

        workflow_started = _finalize_background_launch(
            proc,
            web_port,
            pid_workflow_ref,
            stderr_path,
            run_id=run_id,
            stdout_log=stdout_path,
        )
    finally:
        # The child has its own duplicated OS handles by now (or never got
        # them, if Popen raised) — either way the parent's Python file
        # objects can be released without affecting the child.
        _close_quietly(stderr_handle, stdout_handle)

    # ``_finalize_background_launch`` returns bare ``True`` for a clean
    # (exit code 0) completion it observed *during* its own wait, in
    # addition to the common "genuinely still running" case — see its
    # docstring. ``proc`` is still in hand here, so re-check it directly
    # rather than widening that function's return type: this is what lets
    # ``BackgroundLaunch.still_running`` distinguish the two without callers
    # printing a live dashboard URL for an already-exited process (#410).
    still_running = proc.poll() is None

    return BackgroundLaunch(
        url=f"http://127.0.0.1:{web_port}",
        stderr_log=stderr_path,
        stdout_log=stdout_path,
        run_id=run_id,
        workflow_started=workflow_started,
        still_running=still_running,
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

    if print_loaded_instructions:
        cmd.append("--print-loaded-instructions")

    return _spawn_bg_child(cmd=cmd, web_port=web_port, pid_workflow_ref=workflow_path)


def _checkpoint_run_id(workflow_path: Path | None, checkpoint_path: Path | None) -> str | None:
    """Resolve the run id the resumed child will adopt from its checkpoint.

    Mirrors the checkpoint-resolution precedence in
    ``cli/run.py::resume_workflow_async`` (explicit ``checkpoint_path`` first,
    else the latest checkpoint for ``workflow_path``) so the launcher and the
    child agree on which checkpoint is in play, and therefore on its
    ``run_id``. That id is what ``EventLogSubscriber`` adopts via
    ``existing_run_id`` whenever the original JSONL still exists, so using it
    here too is what keeps the PID file, ``/api/info``, and the events log
    agreeing with each other.

    A launcher must never fail a launch over checkpoint parsing — the child
    will surface the real error — so any failure here is swallowed and
    ``None`` is returned, which falls back to a fresh id. The except clause
    is deliberately broad (rather than naming only ``CheckpointManager``'s
    documented exceptions) because a hand-edited or corrupted checkpoint can
    fail in shapes ``CheckpointManager`` doesn't itself guard against — e.g.
    a top-level JSON value that isn't an object (``AttributeError`` from
    ``.get()``) or a ``run_id`` field that parsed as a non-string JSON value
    (``AttributeError`` from ``.lower()`` below). Every such failure is
    logged at warning level so the fallback leaves a forensic trail instead
    of silently reusing a fresh id with no explanation (this module's own
    stated philosophy — see the module docstring).

    Args:
        workflow_path: Optional workflow YAML path, used to find the latest
            checkpoint when ``checkpoint_path`` is not given.
        checkpoint_path: Optional explicit checkpoint path.

    Returns:
        The checkpoint's ``run_id`` lowercased, when it is 8 hex chars
        (matching ``BackgroundLaunch``'s invariant). ``None`` otherwise, or
        when no checkpoint could be resolved/loaded.
    """
    from conductor.engine.checkpoint import CheckpointManager

    try:
        resolved_path = checkpoint_path
        if resolved_path is None and workflow_path is not None:
            resolved_path = CheckpointManager.find_latest_checkpoint(workflow_path)
        if resolved_path is None:
            return None
        cp = CheckpointManager.load_checkpoint(resolved_path)
        # ``CheckpointData.run_id`` is a ``str`` type hint, not an enforced
        # constraint — a hand-edited or malformed checkpoint could carry a
        # non-string value here, which would otherwise raise a bare
        # ``AttributeError`` from ``.lower()``.
        candidate = str(cp.run_id or "").lower()
    except Exception as exc:  # must never fail a launch over checkpoint parsing; see docstring
        logger.warning(
            "Could not adopt run_id from checkpoint %s (%s: %s); minting a "
            "fresh id instead. If the resumed child adopts the checkpoint's "
            "real run_id from its own events JSONL, the PID file may not "
            "correlate with it.",
            resolved_path,
            type(exc).__name__,
            exc,
        )
        return None

    if _RUN_ID_PATTERN_LOCAL.fullmatch(candidate):
        return candidate
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
) -> BackgroundLaunch:
    """Fork a detached child process resuming the workflow with a web dashboard.

    The child executes ``conductor resume <workflow|--from path> --web ...``
    with all the caller-supplied options. ``--no-interactive`` is always
    appended since the detached child has no TTY. The parent waits briefly
    for the web server to become reachable, then returns the dashboard URL
    and the path to the child's captured stderr log.

    Either ``workflow_path`` or ``checkpoint_path`` (or both) must be
    provided — at least one is required by the resume command.

    The launch adopts the checkpoint's ``run_id`` (when it resolves to 8 hex
    chars via :func:`_checkpoint_run_id`) instead of minting a fresh one, so
    the PID file, ``/api/info``, the events JSONL, and the capture-log
    filenames all agree on one id — matching what the resumed child's own
    ``EventLogSubscriber`` adopts when the original JSONL still exists.

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

    if provider_override:
        cmd.extend(["--provider", provider_override])

    if skip_gates:
        cmd.append("--skip-gates")

    if log_file:
        cmd.extend(["--log-file", str(log_file)])

    # Use workflow_path if available, otherwise fall back to checkpoint_path
    # for the PID file name, log file naming, and recorded reference. The
    # early guard at the top of this function already rejected the case
    # where both are None; the ``or`` here picks the first non-None.
    pid_workflow_ref: Path = workflow_path or checkpoint_path  # type: ignore[assignment]

    # Adopt the checkpoint's run id (when resolvable) so the whole launch —
    # PID file, bg capture-log filenames, ``CONDUCTOR_RUN_ID`` — agrees with
    # the id the resumed child's ``EventLogSubscriber`` will itself adopt.
    # Falls back to a fresh id via ``_spawn_bg_child``/``_open_bg_log_files``
    # when the checkpoint has no usable id.
    resumed_run_id = _checkpoint_run_id(workflow_path, checkpoint_path)

    return _spawn_bg_child(
        cmd=cmd, web_port=web_port, pid_workflow_ref=pid_workflow_ref, run_id=resumed_run_id
    )


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
