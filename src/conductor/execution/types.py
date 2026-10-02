"""Data-shaped contract types for the runner backend seam.

These types define the transport-neutral contract between Conductor's
executors and pluggable execution backends (local process, and in future
remote realms). They are frozen dataclasses — not Pydantic models — and the
package deliberately imports nothing from Conductor itself, so the seam stays
a pure leaf that any layer may depend on.

Raw exception objects never appear in this contract: start failures travel as
the serializable :class:`StartError`, which the executor uses to reconstruct
the original exception for ``raise ... from ...`` chaining.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CommandOutcome = Literal["completed", "command_not_found", "start_failed", "timed_out"]
"""Outcome of a single command execution.

``completed`` includes command failure: a non-zero exit code is a normal
result that routes branch on, not an error. Task cancellation is NOT an
outcome — it propagates as ``asyncio.CancelledError`` after the backend kills
and reaps the child process, and never masquerades as a ``CommandResult``.
"""

RunOutcome = Literal["succeeded", "failed", "cancelled"]
"""Terminal outcome of a whole run, handed to the backend's ``finalize_run``
so realm cleanup can distinguish success from failure from cancellation."""

StartErrorKind = Literal["file_not_found", "os_error"]
"""Kind of a start failure, mirroring the two ``OSError`` branches the
executor chains today: a missing resolved command vs. any other OS error."""


@dataclass(frozen=True)
class BundleRef:
    """Reference to a published content-addressed run bundle.

    Attributes:
        digest: The full bundle content digest (e.g. ``"sha256:<hex>"``).
        store_path: Absolute host path to the published bundle directory in
            the bundle store. Backends derive sub-trees (e.g. ``tree/main``
            and ``tree/roots/``) from this root path.
    """

    digest: str
    store_path: str


@dataclass(frozen=True)
class ResolvedExecutionSpec:
    """Resolved container execution specification for execution backends.

    This carries only resolved immutable values needed to configure the
    execution container (e.g. image, platform, resource constraints, security
    settings), never profile names or backend-specific interpretation text.

    Values of ``None`` or ``False`` indicate that the corresponding platform
    flag is omitted, preserving platform-native daemon defaults rather than
    substituting Conductor-level defaults.

    Attributes:
        image: Container image reference as authored (tag or digest).
        platform: Optional target platform (e.g. ``"linux/amd64"``).
            ``None`` omits the platform flag for daemon auto-resolution.
        network: Optional network mode (e.g. ``"none"``, ``"bridge"``,
            ``"host"``). ``None`` omits the network flag.
        user: Optional container user specification (``"<name|uid>[:<group|gid>]"``).
            ``None`` omits the user flag to use the image default USER.
        init: When true, run an init inside the container to forward signals
            and reap processes (``--init``). ``False`` omits the flag.
        read_only: When true, mount the container's root filesystem as read-only
            (``--read-only``). ``False`` omits the flag.
        cap_drop_all: When true, drop all Linux capabilities (``--cap-drop ALL``).
            ``False`` omits the flag.
        no_new_privileges: When true, disable gaining additional privileges
            (``--security-opt no-new-privileges``). ``False`` omits the flag.
        tmpfs: Tmpfs mount configuration. ``False`` omits the mount; ``True``
            mounts tmpfs at ``/tmp``; a size string (e.g. ``"1g"``) mounts tmpfs
            at ``/tmp`` with the specified size limit.
        cpu: Optional CPU limit in fractions of a core (e.g. ``1.5``).
            ``None`` omits CPU limits.
        memory: Optional memory limit string (e.g. ``"512m"``, ``"2g"``).
            ``None`` omits memory limits.
        pids: Optional maximum number of processes/threads.
            ``None`` omits PID limits.
    """

    image: str
    platform: str | None = None
    network: str | None = None
    user: str | None = None
    init: bool = False
    read_only: bool = False
    cap_drop_all: bool = False
    no_new_privileges: bool = False
    tmpfs: bool | str = False
    cpu: float | None = None
    memory: str | None = None
    pids: int | None = None


@dataclass(frozen=True)
class CommandSpec:
    """A single command to execute, fully rendered by the caller.

    Attributes:
        command: The rendered command name or path. NOT host-resolved here —
            resolution against PATH happens inside the backend's realm.
        args: Positional arguments, verbatim.
        working_dir: Working directory passed to the spawn call verbatim;
            ``None`` means inherit the caller's current directory.
        env: Declared environment overrides only — never a pre-expanded host
            environment.
        inherit_control_environment: When true, the backend merges these
            overrides on top of its control environment; when false, the
            command runs on a minimal environment plus ``env``.
        stdin: Bytes fed to the command's stdin. ``None`` means inherit the
            caller's stdin; ``b""`` means immediate EOF.
        timeout: Per-command wall-clock timeout in seconds, or ``None`` for
            no timeout.
        execution: Resolved execution specification for containerized execution
            realms (e.g. Docker), or ``None`` for default execution. The
            manifest guarantees backend/payload consistency; local backend
            ignores this field.
        name: Optional step identity for realm-side labels/names. Local
            backend ignores this field.
    """

    command: str
    args: tuple[str, ...] = ()
    working_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    inherit_control_environment: bool = True
    stdin: bytes | None = None
    timeout: float | None = None
    execution: ResolvedExecutionSpec | None = None
    name: str | None = None


@dataclass(frozen=True)
class StartError:
    """Serializable, transport-neutral description of a command start failure.

    When a backend captures the original exception ``e``, the fields are
    filled as follows: ``kind`` is ``"file_not_found"`` for
    ``FileNotFoundError`` and ``"os_error"`` otherwise; ``message`` is
    ``e.strerror`` when ``e.errno`` is set (a structured errno means the OS
    already formatted the text, and ``strerror`` is the clean half needed to
    rebuild it) and ``str(e)`` otherwise; ``errno`` is ``e.errno``;
    ``filename`` is ``e.filename``.

    The executor reconstructs the original exception for
    ``raise ... from ...`` chaining with::

        cause_type = FileNotFoundError if kind == "file_not_found" else OSError
        cause = (
            cause_type(errno, message, filename)
            if errno is not None
            else cause_type(message)
        )

    Reconstructed and original exceptions match on ``type``/``str``/
    ``errno``/``filename`` (CPython's ``OSError(errno, ...)`` constructor
    auto-selects the matching built-in subclass, e.g. ``PermissionError``
    for errno 13). The only residual divergence is the type identity of a
    custom ``OSError`` subclass, which is accepted: raw exception objects
    never travel in this contract.
    """

    kind: StartErrorKind
    message: str
    errno: int | None = None
    filename: str | None = None


@dataclass(frozen=True)
class CommandResult:
    """The data-shaped result of one command execution.

    A backend returns this for every command/infra/timeout outcome and never
    raises for them; cancellation is the only escape and propagates as
    ``asyncio.CancelledError``.

    Attributes:
        outcome: Which of the four command outcomes occurred.
        stdout: Decoded standard output (``utf-8``, ``errors="replace"``).
        stderr: Decoded standard error (``utf-8``, ``errors="replace"``).
        exit_code: The process exit code; only set when ``outcome`` is
            ``"completed"``.
        resolved_command: The command after the backend's in-realm
            resolution (e.g. PATH lookup); used by diagnostics and error
            messages.
        start_error: Data-shaped start-failure details; set exactly when
            ``outcome`` is ``"command_not_found"`` or ``"start_failed"``.
        duration_seconds: Wall-clock duration of the command.
    """

    outcome: CommandOutcome
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    resolved_command: str = ""
    start_error: StartError | None = None
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class WorkspaceLease:
    """Opaque, backend-owned handle to a run's workspace.

    Executors pass it through to ``run_command`` without reading its fields;
    only the backend that issued it interprets the contents.

    Attributes:
        lease_id: Stable identifier of the lease (e.g. the run id).
        backend: Name of the issuing backend.
        incarnation: A short token the backend can use to detect stale
            handles after a restart.
        location: Optional realm-specific location hint (e.g. a path or
            resource URL), meaningful only to the issuing backend.
    """

    lease_id: str
    backend: str
    incarnation: str
    location: str | None = None


@dataclass(frozen=True)
class RunSpec:
    """What a backend needs to know to prepare a run's workspace.

    Attributes:
        run_id: The workflow run's identifier; also the natural lease key.
        workflow_name: Optional workflow name, for realm-side labeling.
        bundle: Optional bundle reference containing the published run bundle
            for containerized execution realms, or ``None``.
    """

    run_id: str
    workflow_name: str | None = None
    bundle: BundleRef | None = None


@dataclass(frozen=True)
class RunnerCapabilities:
    """Static capability declaration of an execution backend.

    Attributes:
        batch: The backend can run plain commands (the only typed operation
            in this contract).
        sessions: The backend offers interactive sessions (reserved for a
            later contract revision; nothing in the seam consumes it yet).
        shared_workspace: Commands run against a workspace shared with the
            caller (a lease may be threaded through).
        snapshots: The backend can snapshot and restore workspaces
            (reserved for a later contract revision).
    """

    batch: bool
    sessions: bool
    shared_workspace: bool
    snapshots: bool
