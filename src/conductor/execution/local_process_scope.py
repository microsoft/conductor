"""Workspace-scoped native custody, independent deadlines and bounded owners.

The controller runs on its caller's asyncio loop. Blocking private IPC uses
daemon threads, never the loop's executor: an uncooperative owner must not make
the loop's executor shutdown an unbounded second teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import select
import shutil
import stat
import sys
import sysconfig
import threading
import time
import traceback
import weakref
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from multiprocessing.reduction import recv_handle
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from conductor.execution.errors import ExecutionSpecError
from conductor.execution.local_windows_launch import (
    merge_windows_environment,
    prepare_windows_launch,
)
from conductor.execution.types import (
    CommandResult,
    CommandSpec,
    ProcessLaunchSpec,
    ProcessProof,
    ShutdownResult,
    StartError,
)

from .local_scope_launcher import (
    CHUNK,
    Acceptor,
    Channel,
    Json,
    authenticate,
    connect,
    disconnect,
    environment,
    private_listener,
    protect,
    receive_frame,
    send_frame,
    strings,
    text,
)

_log = logging.getLogger(__name__)

type _InstallationStage = Literal[
    "preflight",
    "active-fence",
    "ipc-directory",
    "directory-fd",
    "listener-allocation",
    "listener-permissions",
    "thread-construction",
    "thread-start",
    "backend-assembly",
    "registry-commit",
    "admission-commit",
]


def _installation_warning(primary: BaseException, message: str) -> None:
    """Report only caller-supplied fixed diagnostics, preserving the primary on sink failure."""
    try:
        _log.warning(message)
    except BaseException:
        # This installation boundary must also preserve cancellation from a failed sink.
        BaseException.add_note(
            primary, "Managed local installation diagnostic could not be delivered."
        )


@dataclass(slots=True)
class _ControllerInstallation:
    """Own startup acquisitions until backend commit, independently of its primary exception."""

    stage: _InstallationStage = "preflight"
    directory: Path | None = None
    directory_identity: tuple[int, int] | None = None
    directory_pending: bool = False
    fd: int | None = None
    fd_pending: bool = False
    listener: Acceptor | None = None
    listener_pending: bool = False
    thread: threading.Thread | None = None
    start_attempted: bool = False
    entered: threading.Event = field(default_factory=threading.Event)
    gate: threading.Event = field(default_factory=threading.Event)
    aborted: bool = False
    commit_started: bool = False
    committed: bool = False
    deadline: float | None = None
    outcome: Literal["confirmed", "unconfirmed"] | None = None

    def receive_listener(self, listener: Acceptor) -> None:
        """Take the concrete resource before the factory can fail at chmod."""
        self.listener = listener
        self.listener_pending = False
        self.stage = "listener-permissions"

    def reject_listener_transfer(self) -> None:
        """Keep an interrupted transfer unknown even when factory disposal was attempted."""
        self.listener_pending = True

    def bootstrap(self, accept: Callable[[], None]) -> None:
        """Keep native acceptance and HELLO readers behind the registry commit."""
        self.entered.set()
        self.gate.wait()
        if self.committed and not self.aborted:
            accept()

    def commit(self) -> None:
        """Irreversibly transfer admission only after all backend mappings are published."""
        self.stage = "admission-commit"
        self.commit_started = True
        self.committed = True
        self.gate.set()

    def abort(self, primary: BaseException) -> Literal["confirmed", "unconfirmed"]:
        """Attempt concrete disposal once, within one absolute IPC-derived join budget."""
        if self.outcome is not None:
            return self.outcome
        self.aborted = True
        if self.commit_started:
            self.outcome = "unconfirmed"
            return self.outcome
        limit = time.monotonic() + 1
        self.deadline = min(self.deadline, limit) if self.deadline is not None else limit
        confirmed = not (self.directory_pending or self.fd_pending or self.listener_pending)
        try:
            self.gate.set()
        except BaseException:
            confirmed = False
            _installation_warning(primary, "Managed local installation abort gate failed.")
        if self.start_attempted:
            assert self.thread is not None
            try:
                self.thread.join(max(0, self.deadline - time.monotonic()))
                if self.thread.is_alive() or not self.entered.is_set():
                    confirmed = False
            except BaseException:
                # A failed start may still have started a native thread; absence is not proof.
                confirmed = False
                _installation_warning(
                    primary, "Managed local installation thread join unconfirmed."
                )
        listener_closed = not self.listener_pending
        if self.listener is not None and not self.listener_pending:
            try:
                self.listener.close()
            except BaseException:
                listener_closed = False
                confirmed = False
                _installation_warning(
                    primary, "Managed local installation listener close unconfirmed."
                )
        fd_closed = not self.fd_pending and listener_closed
        if self.fd is not None and fd_closed:
            try:
                os.close(self.fd)
            except BaseException:
                fd_closed = False
                confirmed = False
                _installation_warning(
                    primary, "Managed local installation directory fd close unconfirmed."
                )
        if self.directory is not None and fd_closed and not self.directory_pending:
            try:
                info = self.directory.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino) != self.directory_identity
                ):
                    raise ExecutionSpecError("Installation IPC directory identity changed")
                self.directory.rmdir()
            except BaseException:
                confirmed = False
                _installation_warning(primary, "Managed local installation IPC directory retained.")
        self.outcome = "confirmed" if confirmed else "unconfirmed"
        return self.outcome


# Direct constructor failures have no backend lifetime to retain unknown native acquisitions.
_failed_controller_installations: list[_ControllerInstallation] = []


class Timer(Protocol):
    def cancel(self) -> None: ...


class Scheduler(Protocol):
    """Private clock seam; test clocks trigger callbacks with explicit events."""

    def now(self) -> float: ...
    def at(self, deadline: float, callback: Callable[[], None]) -> Timer: ...


class _LoopScheduler:
    def now(self) -> float:
        return asyncio.get_running_loop().time()

    def at(self, deadline: float, callback: Callable[[], None]) -> Timer:
        return asyncio.get_running_loop().call_at(deadline, callback)


@dataclass
class _Episode:
    """Mutable, monotonic teardown state; terminal results never reopen."""

    sealed: bool = False
    started: float | None = None
    deadline: float | None = None
    result: ShutdownResult | None = None
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    timers: list[Timer] = field(default_factory=list)


@dataclass
class _Scope:
    """Mutable private process accounting, registered before GO."""

    scope_id: str
    invocation: str
    incarnation: str
    launch: ProcessLaunchSpec | None = field(repr=False)
    episode: _Episode = field(default_factory=_Episode)
    process: asyncio.subprocess.Process | None = None
    control: Channel | None = field(default=None, repr=False)
    facade: Channel | None = field(default=None, repr=False)
    facade_handle: int | None = None
    facade_exited: bool = True
    channels: dict[str, Channel] = field(default_factory=dict, repr=False)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    data_ready: asyncio.Event = field(default_factory=asyncio.Event)
    facade_dead: asyncio.Event = field(default_factory=asyncio.Event)
    rejected: bool = False
    eof: float | None = None
    events: list[str] = field(default_factory=list)
    kernel_empty: bool = False
    drained: bool = False
    reaped: bool = False
    done: bool = False
    code: int | None = None
    error: StartError | None = None
    stdout: bytearray = field(default_factory=bytearray, repr=False)
    stderr: bytearray = field(default_factory=bytearray, repr=False)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    resources: set[asyncio.Task[Json]] = field(default_factory=set, repr=False)
    dispatched: asyncio.Event = field(default_factory=asyncio.Event)
    on_dispatch: Callable[[], None] | None = None
    stdin: bytes | None = field(default=None, repr=False)
    eof_received: asyncio.Event = field(default_factory=asyncio.Event)
    force_sent: asyncio.Event = field(default_factory=asyncio.Event)
    force_write: asyncio.Task[Json] | None = field(default=None, repr=False)
    consumed: bool = False
    retiring: bool = False

    @property
    def proof(self) -> ProcessProof:
        return ProcessProof(
            self.scope_id,
            self.invocation,
            self.incarnation,
            self.kernel_empty,
            self.drained,
            self.facade_exited,
            self.reaped,
            self.done,
            tuple(self.events),
        )


@dataclass(frozen=True, slots=True)
class _RetiredScope:
    """Immutable audit and terminal accounting without native or launch resources."""

    proof: ProcessProof
    result: ShutdownResult
    deadline: float | None
    code: int | None
    error: StartError | None


@dataclass(frozen=True)
class _Quarantined:
    controller: LocalProcessScopeController
    invocation: str | None
    owner: _Owner
    scrub: Callable[[str], str] = field(repr=False)


class _Owner(Protocol):
    def done(self) -> bool: ...
    def cancelled(self) -> bool: ...
    def exception(self) -> BaseException | None: ...


@dataclass(frozen=True)
class _OwnerEntry:
    task: _Owner
    invocation: str | None


_quarantine: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, _Quarantined]] = (
    weakref.WeakKeyDictionary()
)


def _thread_call[T](function: Callable[[], T]) -> asyncio.Future[T]:
    """Observe a blocking OS operation without making cancellation a join."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def deliver(value: T) -> None:
        if not future.done():
            future.set_result(value)

    def fail(error: BaseException) -> None:
        if not future.done():
            future.set_exception(error)

    def run() -> None:
        try:
            value = function()
        except (OSError, EOFError, ValueError, KeyError, TypeError) as error:
            # Connection's partial-write loop can see handle=None during IPC abort.
            if not loop.is_closed():
                loop.call_soon_threadsafe(fail, error)
        else:
            if not loop.is_closed():
                loop.call_soon_threadsafe(deliver, value)

    threading.Thread(target=run, daemon=True).start()
    return future


class OwnerRegistry:
    """Resource-owner accounting and bounded observation, not orchestration."""

    def __init__(self, controller: LocalProcessScopeController) -> None:
        self.controller = controller
        self.tasks: dict[int, _OwnerEntry] = {}
        self._known: weakref.WeakKeyDictionary[_Owner, str | None] = weakref.WeakKeyDictionary()
        self.failures: list[str] = []

    def register[T](self, invocation_id: str | None, owner: asyncio.Future[T]) -> None:
        """Register immediately after task creation, before yielding to spawn."""
        self.controller._open(invocation_id)
        if id(owner) in self.tasks:
            if self.tasks[id(owner)].invocation != invocation_id:
                raise ExecutionSpecError("Owner already belongs to another invocation")
            return
        self.tasks[id(owner)] = _OwnerEntry(owner, invocation_id)
        self._known[owner] = invocation_id
        owner.add_done_callback(self._finished)

    def _finished(self, owner: _Owner) -> None:
        if not owner.cancelled() and (error := owner.exception()) is not None:
            message = self.controller.scrub(str(error))
            self.failures.append(message)
            _log.warning("Managed resource owner failed: %s", message)
        self.tasks.pop(id(owner), None)
        _quarantine.get(self.controller._loop, {}).pop(id(owner), None)
        self.controller._check()

    def mutations_allowed(self, invocation_id: str | None) -> bool:
        """Owners must check this before checkpoint/terminal-state mutation."""
        episode = self.controller._episode(invocation_id)
        return not (self.controller._root.result is not None or episode.result is not None)

    def deadline(self, invocation_id: str | None = None) -> float | None:
        episodes = [self.controller._root]
        episodes.extend(
            episode
            for invocation, episode in self.controller._invocations.items()
            if invocation_id is None or invocation_id == invocation
        )
        episodes.extend(
            scope.episode
            for scope in self.controller._scopes.values()
            if invocation_id is None or invocation_id == scope.invocation
        )
        deadlines = [
            episode.deadline
            for episode in episodes
            if episode.deadline is not None
            and (episode.result is None or episode.result.status == "unconfirmed")
        ]
        deadlines.extend(
            record.deadline
            for record in self.controller._history.values()
            if record is not None
            and record.deadline is not None
            and record.result.status == "unconfirmed"
            and (invocation_id is None or invocation_id == record.proof.invocation_id)
        )
        return min(deadlines) if deadlines else None

    async def wait_terminal(self, invocation_id: str | None = None) -> ShutdownResult:
        episode = self.controller._episode(invocation_id)
        await episode.terminal.wait()
        assert episode.result is not None
        return episode.result

    async def observe[T](self, invocation_id: str | None, owner: asyncio.Future[T]) -> bool:
        """Return completion, or False on uncertainty; never await cancel ack.

        The caller retrieves owner.result() only after True (including its native
        exception). Cancellation seals first, continues bounded observation, and
        is re-raised without cancelling or losing the resource owner.
        """
        if owner not in self._known or self._known[owner] != invocation_id:
            raise ExecutionSpecError("Register the resource owner before observation")
        episode = self.controller._episode(invocation_id)
        terminal = asyncio.create_task(episode.terminal.wait())
        cancelled = False
        try:
            while not owner.done() and not episode.terminal.is_set():
                try:
                    await asyncio.wait({owner, terminal}, return_when=asyncio.FIRST_COMPLETED)
                except asyncio.CancelledError:
                    cancelled = True
                    self.controller.begin_teardown(invocation_id)
            if cancelled:
                raise asyncio.CancelledError
            return owner.done() and (
                episode.result is None or episode.result.status != "unconfirmed"
            )
        finally:
            terminal.cancel()

    @property
    def quarantined(self) -> int:
        """Number of still-running owners in this root's per-loop quarantine."""
        entries = _quarantine.get(self.controller._loop, {})
        return sum(entry.controller is self.controller for entry in entries.values())

    def _quarantine(self) -> None:
        entries = _quarantine.setdefault(self.controller._loop, {})
        for key, entry in self.tasks.items():
            if not entry.task.done() and key not in entries:
                entries[key] = _Quarantined(
                    self.controller, entry.invocation, entry.task, self.controller.scrub
                )


class LocalProcessScopeController:
    """Mutable custody registry for exactly one workspace incarnation.

    Construct on the running loop before creating invocation owners. The
    independent deadline callbacks are armed synchronously by begin_teardown;
    SDK code need not reach a finally block for force/deadline to execute.
    """

    def __init__(
        self,
        root: str,
        incarnation: str,
        *,
        scrub: Callable[[str], str] = str,
        _scheduler: Scheduler | None = None,
        _installation: _ControllerInstallation | None = None,
    ) -> None:
        self.root = Path(root)
        self.incarnation = incarnation
        self.scrub = scrub
        self._loop, self._helper = self.preflight()
        self._clock = _scheduler or _LoopScheduler()
        self._root = _Episode()
        self._invocations: dict[str, _Episode] = {}
        self._launchers: dict[str, tuple[str, ProcessLaunchSpec]] = {}
        self._scopes: dict[str, _Scope] = {}
        self._history: dict[str, _RetiredScope | None] = {}
        self._processes: weakref.WeakKeyDictionary[asyncio.subprocess.Process, str] = (
            weakref.WeakKeyDictionary()
        )
        self._tasks: set[asyncio.Task[Json]] = set()
        self._writes: dict[asyncio.Task[Json], str] = {}
        self._send_tails: dict[Channel, threading.Event] = {}
        self._closed = False
        self._connections: set[Channel] = set()
        self._connections_lock = threading.Lock()
        self._accept_finished = threading.Event()
        self._owners = OwnerRegistry(self)
        self._nonce = os.urandom(32).hex()
        installation = _installation if _installation is not None else _ControllerInstallation()
        try:
            # Avoid Unix sockaddr path limits even for deeply nested workspace roots.
            if sys.platform == "win32":
                self._endpoint = "\\\\.\\pipe\\conductor-scope-" + uuid4().hex
            else:
                ipc = self.root / (".scope-ipc-" + uuid4().hex)
                installation.stage = "ipc-directory"
                installation.directory_pending = True
                ipc.mkdir(mode=0o700)
                installation.directory = ipc
                info = ipc.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_dev <= 0 or info.st_ino <= 0:
                    raise ExecutionSpecError("Installation IPC directory identity is unavailable")
                installation.directory_identity = (info.st_dev, info.st_ino)
                installation.directory_pending = False
                installation.stage = "directory-fd"
                installation.fd_pending = True
                self._ipc_fd = os.open(ipc, os.O_RDONLY | os.O_DIRECTORY)
                installation.fd = self._ipc_fd
                installation.fd_pending = False
                self._endpoint = f"/proc/{os.getpid()}/fd/{self._ipc_fd}/control"
            installation.stage = "listener-allocation"
            installation.listener_pending = True
            self._listener = private_listener(
                self._endpoint,
                register=installation.receive_listener,
                transfer_failed=installation.reject_listener_transfer,
            )
            installation.stage = "thread-construction"
            self._accepting = threading.Thread(
                target=installation.bootstrap, args=(self._accept,), daemon=True
            )
            installation.thread = self._accepting
            installation.stage = "thread-start"
            installation.start_attempted = True
            self._accepting.start()
            if _installation is None:
                installation.commit()
        except BaseException as primary:
            if _installation is None and installation.abort(primary) == "unconfirmed":
                _failed_controller_installations.append(installation)
                _installation_warning(
                    primary,
                    "Managed local controller installation cleanup unconfirmed; "
                    "resources retained.",
                )
            raise

    @staticmethod
    def preflight() -> tuple[asyncio.AbstractEventLoop, Path]:
        """Require the actual loop, installed executable and supported platform before staging."""
        loop = asyncio.get_running_loop()
        helper = Path(sysconfig.get_path("scripts")) / (
            "conductor-local-scope.exe" if sys.platform == "win32" else "conductor-local-scope"
        )
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise ExecutionSpecError(
                "Installed conductor-local-scope is required in this Python environment; "
                "install conductor-cli into the active environment before managed local execution"
            )
        if sys.platform not in ("linux", "win32"):
            raise ExecutionSpecError("Managed local custody requires Linux or Windows")
        return loop, helper

    def _task(
        self,
        coroutine: Coroutine[None, None, Json],
        *,
        fatal: bool = True,
        scope: _Scope | None = None,
    ) -> asyncio.Task[Json]:
        if scope is not None and scope.completed.is_set():
            coroutine.close()
            raise ExecutionSpecError("Process scope resource registration is closed")
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        if scope is not None:
            scope.resources.add(task)

        def finished(completed: asyncio.Task[Json]) -> None:
            self._tasks.discard(completed)
            _quarantine.get(self._loop, {}).pop(id(completed), None)
            error = None if completed.cancelled() else completed.exception()
            if error is not None and fatal:
                context = (
                    f"scope={scope.scope_id}, invocation={scope.invocation}"
                    if scope is not None
                    else "root scope"
                )
                _log.warning(
                    "Managed process infrastructure failure: %s",
                    self.scrub(f"{context}\n{''.join(traceback.format_exception(error))}"),
                )
                self._poison("Managed process infrastructure failure")
            if scope is not None:
                if completed.cancelled():
                    self._poison("Process scope resource was cancelled before quiescence")
                scope.resources.discard(completed)
            self._check()

        task.add_done_callback(finished)
        if self._root.result is not None and self._root.result.status == "unconfirmed":
            _quarantine.setdefault(self._loop, {})[id(task)] = _Quarantined(
                self, "@process", task, self.scrub
            )
        return task

    def _send(self, scope: _Scope, channel: Channel, frame: dict[str, Json]) -> asyncio.Task[Json]:
        """Own a FIFO daemon write through actual completion, not caller cancellation."""
        if scope.completed.is_set():
            raise ExecutionSpecError("Process scope resource registration is closed")
        previous = self._send_tails.get(channel)
        finished = threading.Event()
        self._send_tails[channel] = finished

        def send() -> None:
            if previous is not None:
                previous.wait()
            try:
                send_frame(channel, frame)
            except (OSError, EOFError, ValueError, TypeError):
                # A failed partial frame cannot be followed by another valid frame.
                self._disconnect(channel)
                raise
            finally:
                finished.set()

        operation = _thread_call(send)

        async def observe() -> None:
            await asyncio.shield(operation)

        writer = self._task(observe(), fatal=False, scope=scope)
        self._writes[writer] = scope.invocation

        def written(task: asyncio.Task[Json]) -> None:
            self._writes.pop(task)
            if self._send_tails.get(channel) is finished:
                self._send_tails.pop(channel)
            if not task.cancelled() and task.exception() is not None:
                if channel is scope.facade:
                    scope.events.append("FACADE_WRITE_CLOSED")
                else:
                    scope.events.append("CONTROL_WRITE_FAILED")
                    _log.warning("Managed control channel write failed")
                    if not (scope.done and scope.reaped and scope.facade_exited):
                        self._poison("Managed process infrastructure failure")
            elif not task.cancelled() and frame["event"] == "FORCE_STOP":
                scope.events.append("FORCE_STOP")
                scope.force_sent.set()
            self._check()

        writer.add_done_callback(written)
        return writer

    @property
    def owners(self) -> OwnerRegistry:
        return self._owners

    def _episode(self, invocation: str | None) -> _Episode:
        return self._root if invocation is None else self._invocations[invocation]

    def _open(self, invocation: str | None) -> _Episode:
        if self._root.sealed:
            raise ExecutionSpecError("Managed process root is sealed")
        if invocation is None:
            return self._root
        episode = self._invocations.setdefault(invocation, _Episode())
        if episode.sealed:
            raise ExecutionSpecError("Managed process invocation is sealed")
        return episode

    def check_admission(self) -> None:
        """Apply the existing root admission gate without creating an invocation."""
        self._open(None)

    def _descriptor(self, directory: Path, role: str, **values: str) -> Path:
        directory.mkdir(mode=0o700, parents=True)
        protect(directory, 0o700)
        path = directory / self._helper.name
        shutil.copyfile(self._helper, path)
        protect(path, 0o500)
        descriptor: dict[str, Json] = {
            "v": 1,
            "role": role,
            "root": str(self.root),
            "root_incarnation": self.incarnation,
            "controller": str(os.getpid()),
            "endpoint": self._endpoint,
            "nonce": self._nonce,
            **values,
        }
        metadata = directory / "descriptor.json"
        with metadata.open("x", encoding="utf-8") as stream:
            json.dump(descriptor, stream)
        protect(metadata, 0o400)
        return path

    def _admit_launch(self, invocation_id: str, launch: ProcessLaunchSpec) -> None:
        self._open(invocation_id)
        if not launch.argv or any(key.startswith("CONDUCTOR_LOCAL_SCOPE_") for key in launch.env):
            raise ExecutionSpecError("Empty argv or reserved local scope environment name")

    def register_launcher(self, invocation_id: str, launch: ProcessLaunchSpec) -> str:
        self._admit_launch(invocation_id, launch)
        launcher = uuid4().hex
        self._launchers[launcher] = (invocation_id, launch)
        return launcher

    def register_process(self, invocation_id: str, process: asyncio.subprocess.Process) -> str:
        """Accept only controller-created custodians; arbitrary PIDs are not proof."""
        self._open(invocation_id)
        scope_id = self._processes.get(process)
        if scope_id is not None:
            record = self._history[scope_id]
            owner = (
                record.proof.invocation_id
                if record is not None
                else self._scopes[scope_id].invocation
            )
            if owner == invocation_id:
                return scope_id
        raise ExecutionSpecError("A managed process must be created by its custodian")

    async def wrap_stdio(self, invocation_id: str, launch: ProcessLaunchSpec) -> ProcessLaunchSpec:
        launcher = self.register_launcher(invocation_id, launch)
        path = self._descriptor(
            self.root / ".launchers" / launcher,
            "facade",
            launcher=launcher,
            invocation=invocation_id,
            cwd=launch.working_dir,
            executable=launch.argv[0],
        )
        return ProcessLaunchSpec((str(path),), dict(launch.env), launch.working_dir)

    @property
    def proofs(self) -> tuple[ProcessProof, ...]:
        return tuple(
            record.proof if record is not None else self._scopes[scope_id].proof
            for scope_id, record in self._history.items()
        )

    def _accept(self) -> None:
        try:
            while not self._closed:
                try:
                    connection = self._listener.accept()
                except (OSError, EOFError):
                    return
                if self._closed:
                    connection.close()
                    return
                with self._connections_lock:
                    self._connections.add(connection)
                threading.Thread(target=self._hello, args=(connection,), daemon=True).start()
        finally:
            self._accept_finished.set()

    def _hello(self, connection: Channel) -> None:
        handle: int | None = None
        attempted_launch = False
        try:
            key = bytes.fromhex(self._nonce)
            authenticate(connection, key)
            frame = receive_frame(connection)
            if frame["event"] == "HELLO":
                attempted_launch = text(frame, "launcher") in self._launchers
                if sys.platform == "win32":
                    transfer = receive_frame(connection)
                    value = transfer.get("handle")
                    if transfer["event"] != "FACADE_HANDLE" or not isinstance(value, int):
                        raise ValueError("Invalid facade handle transfer")
                    handle = value
                else:
                    handle = recv_handle(connection)
            if self._loop.is_closed():
                self._disconnect(connection)
                return
            self._loop.call_soon_threadsafe(self._received, connection, frame, handle)
        except (OSError, EOFError, ValueError, KeyError):
            self._disconnect(connection)
            if handle is not None:
                self._close_handle(handle)
            if attempted_launch and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(
                    self._poison, "Facade stable exit handle was not established"
                )

    def _received(self, connection: Channel, frame: dict[str, Json], handle: int | None) -> None:
        try:
            match frame["event"]:
                case "HELLO":
                    launcher = text(frame, "launcher")
                    invocation, original = self._launchers[launcher]
                    self._open(invocation)
                    launch = ProcessLaunchSpec(
                        (*original.argv, *strings(frame, "args")),
                        environment(frame, "env"),
                        text(frame, "cwd"),
                    )
                    scope = self._new_scope(invocation, launch, text(frame, "incarnation"))
                    scope.facade = connection
                    scope.facade_handle = handle
                    scope.facade_exited = False
                    self._send(scope, connection, {"event": "REGISTERED", "scope": scope.scope_id})
                    self._task(self._watch_facade(scope), scope=scope)
                    self._task(self._run(scope), scope=scope)
                case "DATA":
                    scope = self._scopes[text(frame, "scope")]
                    name = text(frame, "stream")
                    if name not in ("stdin", "stdout", "stderr") or name in scope.channels:
                        raise ValueError("Invalid raw stream registration")
                    scope.channels[name] = connection
                    if len(scope.channels) == 3:
                        scope.data_ready.set()
                case "CUSTODIAN_CONNECTED":
                    scope = self._scopes[text(frame, "scope")]
                    if scope.control is not None:
                        raise ValueError("Duplicate custodian registration")
                    scope.control = connection
                    scope.ready.set()
                case "CLOSE":
                    self._disconnect(connection)
                case _:
                    raise ValueError("Invalid registration")
        except (KeyError, ValueError, OSError, ExecutionSpecError):
            self._disconnect(connection)
            if handle is not None:
                self._close_handle(handle)

    def _new_scope(self, invocation: str, launch: ProcessLaunchSpec, incarnation: str) -> _Scope:
        self._open(invocation)
        scope = _Scope(uuid4().hex, invocation, incarnation, launch)
        self._scopes[scope.scope_id] = scope
        self._history[scope.scope_id] = None
        return scope

    @staticmethod
    def _close_handle(handle: int) -> None:
        if sys.platform == "win32":
            import _winapi

            _winapi.CloseHandle(handle)
        else:
            os.close(handle)

    @staticmethod
    def _handle_exited(handle: int, *, wait: bool) -> bool:
        if sys.platform == "win32":
            import _winapi

            return (
                _winapi.WaitForSingleObject(handle, _winapi.INFINITE if wait else 0)
                == _winapi.WAIT_OBJECT_0
            )
        return bool(select.select([handle], [], [], None if wait else 0)[0])

    async def _watch_facade(self, scope: _Scope) -> None:
        assert scope.facade_handle is not None
        handle = scope.facade_handle
        await _thread_call(lambda: self._handle_exited(handle, wait=True))
        scope.facade_exited = True
        scope.facade_dead.set()
        self._close_handle(scope.facade_handle)
        scope.facade_handle = None
        # The private input channel reaches EOF only after bytes already accepted
        # from the facade. Its input pump, not PID observation, closes payload stdin.
        self._start_episode(scope.episode, lambda: self._expire("Process cleanup deadline"))
        self._arm_force(scope)
        self._check()

    async def _run(self, scope: _Scope) -> None:
        path = self._descriptor(
            self.root / ".scopes" / scope.scope_id, "custodian", scope=scope.scope_id
        )
        scope.process = await asyncio.create_subprocess_exec(
            str(path),
            stdin=asyncio.subprocess.PIPE
            if (scope.facade is not None or scope.stdin is not None)
            else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Terminal SIGINT must not end custody before controlled teardown and reaping.
            start_new_session=sys.platform == "linux",
        )
        self._processes[scope.process] = scope.scope_id
        reaped = self._task(self._reap(scope), scope=scope)
        ready = self._task(scope.ready.wait())
        done, _ = await asyncio.wait({ready, reaped}, return_when=asyncio.FIRST_COMPLETED)
        if ready not in done or scope.control is None:
            ready.cancel()
            self._poison("Custodian exited before registration")
            return
        control = scope.control
        launch = scope.launch
        assert launch is not None
        if sys.platform == "win32" and scope.error is None:

            def prepare(
                target: ProcessLaunchSpec = launch,
            ) -> tuple[ProcessLaunchSpec, StartError | None] | Exception:
                try:
                    return prepare_windows_launch(target)
                except Exception as error:
                    # Transport unexpected exceptions past _thread_call's narrow I/O catches.
                    # Re-raise on the owned task; they are infrastructure failures, not refusals.
                    return error

            preparation = await _thread_call(prepare)
            if isinstance(preparation, Exception):
                raise preparation
            launch, scope.error = preparation
            scope.launch = launch
        argv: list[Json] = list(launch.argv)
        env: dict[str, Json] = dict(launch.env)
        configuration: dict[str, Json] = {
            "event": "CONFIGURE",
            "argv": argv,
            "env": env,
            "cwd": launch.working_dir,
        }
        await asyncio.shield(self._send(scope, control, configuration))
        acknowledgement = await _thread_call(lambda: receive_frame(control))
        if acknowledgement["event"] != "CUSTODIAN_READY":
            raise ValueError("Custodian did not establish native custody")
        scope.events.append("CUSTODIAN_READY")
        if scope.facade is not None:
            data_ready = asyncio.create_task(scope.data_ready.wait())
            facade_dead = asyncio.create_task(scope.facade_dead.wait())
            await asyncio.wait({data_ready, facade_dead}, return_when=asyncio.FIRST_COMPLETED)
            data_ready.cancel()
            facade_dead.cancel()
        try:
            self._open(scope.invocation)
            if scope.facade_exited and scope.facade is not None:
                raise ExecutionSpecError("Facade exited before GO")
            if scope.facade_handle is not None and self._handle_exited(
                scope.facade_handle, wait=False
            ):
                raise ExecutionSpecError("Facade exited before GO")
            if scope.error is None:
                if scope.on_dispatch is not None:
                    scope.on_dispatch()
                self._open(scope.invocation)
                await asyncio.shield(self._send(scope, control, {"event": "GO"}))
        except (ExecutionSpecError, OSError):
            scope.rejected = True
        else:
            if scope.error is None:
                scope.dispatched.set()
        if not scope.dispatched.is_set():
            if scope.error is not None:
                _log.warning("Managed Windows launch refused: %s", self.scrub(scope.error.message))
                self._start_episode(scope.episode, lambda: self._expire("Process cleanup deadline"))
            with contextlib.suppress(OSError):
                await asyncio.shield(self._send(scope, control, {"event": "REJECTED"}))
        assert scope.process.stdout is not None and scope.process.stderr is not None
        outputs = [
            self._task(
                self._output(scope, scope.process.stdout, "stdout", scope.stdout), scope=scope
            ),
            self._task(
                self._output(scope, scope.process.stderr, "stderr", scope.stderr), scope=scope
            ),
        ]
        if scope.facade is not None and scope.dispatched.is_set():
            self._task(self._input(scope), scope=scope)
        elif scope.stdin is not None and scope.dispatched.is_set():
            self._task(self._script_input(scope), scope=scope)
        try:
            while True:
                frame = await _thread_call(lambda: receive_frame(control))
                event = text(frame, "event")
                scope.events.append(event)
                match event:
                    case "COMMAND_STARTED":
                        if (
                            scope.events.count(event) != 1
                            or scope.rejected
                            or (scope.code is not None or scope.error is not None)
                        ):
                            raise ValueError("Duplicate payload")
                    case "INPUT_CLOSED":
                        scope.eof = self._clock.now()
                        scope.eof_received.set()
                        self._arm_force(scope)
                    case "COMMAND_EXITED":
                        code = frame.get("code")
                        if (
                            not isinstance(code, int)
                            or scope.error is not None
                            or ("COMMAND_STARTED" not in scope.events or scope.code is not None)
                        ):
                            raise ValueError("Invalid command exit")
                        scope.code = code
                        self._start_episode(
                            scope.episode, lambda: self._expire("Process cleanup deadline")
                        )
                    case "PAYLOAD_START_FAILED":
                        kind = text(frame, "kind")
                        if kind not in ("file_not_found", "os_error") or (
                            "COMMAND_STARTED" in scope.events or scope.error is not None
                        ):
                            raise ValueError("Invalid start failure")
                        number, filename = frame.get("errno"), frame.get("filename")
                        if number is not None and not isinstance(number, int):
                            raise ValueError("Invalid start errno")
                        if filename is not None and not isinstance(filename, str):
                            raise ValueError("Invalid start filename")
                        scope.error = StartError(
                            "file_not_found" if kind == "file_not_found" else "os_error",
                            text(frame, "message"),
                            number,
                            filename,
                        )
                        self._start_episode(
                            scope.episode, lambda: self._expire("Process cleanup deadline")
                        )
                    case "SCOPE_EMPTY":
                        if scope.kernel_empty or (
                            scope.code is None and scope.error is None and not scope.rejected
                        ):
                            raise ValueError("Invalid native empty proof order")
                        scope.kernel_empty = True
                    case "STREAMS_DRAINED":
                        if scope.drained:
                            raise ValueError("Duplicate drain proof")
                        scope.drained = True
                    case "DONE":
                        if not (scope.kernel_empty and scope.drained) or (
                            scope.code is None and scope.error is None and not scope.rejected
                        ):
                            raise ValueError("Incomplete native proof")
                        scope.done = True
                        break
                    case _:
                        raise ValueError("Custodian infrastructure failure")
        except (OSError, EOFError, ValueError, KeyError):
            self._poison("Custodian control lost or invalid proof")
        await reaped
        await asyncio.gather(*outputs)
        facade = scope.facade
        if facade is not None:
            with contextlib.suppress(OSError):
                await asyncio.shield(
                    self._send(
                        scope,
                        facade,
                        {
                            "event": "RESULT" if scope.done and not scope.rejected else "REJECTED",
                            "code": scope.code if scope.code is not None else 125,
                        },
                    )
                )
            await _thread_call(lambda: self._disconnect(facade))
            # Facade exit additionally proves its DATA output pumps consumed every frame.
            scope.consumed = True
        scope.completed.set()
        self._check()

    async def _reap(self, scope: _Scope) -> None:
        assert scope.process is not None
        await scope.process.wait()
        scope.reaped = True
        if scope.process.returncode != 0:
            self._poison("Custodian exited without successful native cleanup")

    async def _output(
        self, scope: _Scope, reader: asyncio.StreamReader, name: str, captured: bytearray
    ) -> None:
        while data := await reader.read(CHUNK):
            if scope.facade is None:
                captured.extend(data)
            elif not scope.facade_exited and name in scope.channels:
                with contextlib.suppress(OSError, EOFError):
                    await _thread_call(lambda: scope.channels[name].send_bytes(data))
        if scope.facade is not None and name in scope.channels:
            with contextlib.suppress(OSError):
                await _thread_call(lambda: scope.channels[name].send_bytes(b""))

    async def _input(self, scope: _Scope) -> None:
        assert scope.process is not None and scope.process.stdin is not None
        try:
            while data := await _thread_call(lambda: scope.channels["stdin"].recv_bytes(CHUNK)):
                scope.process.stdin.write(data)
                await scope.process.stdin.drain()
        except (OSError, EOFError, ConnectionError):
            pass
        finally:
            scope.process.stdin.close()

    async def _script_input(self, scope: _Scope) -> None:
        assert scope.process is not None and scope.process.stdin is not None
        assert scope.stdin is not None
        try:
            scope.process.stdin.write(scope.stdin)
            await scope.process.stdin.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            scope.process.stdin.close()

    def _start_episode(
        self, episode: _Episode, expire: Callable[[], None], deadline: float | None = None
    ) -> None:
        if episode.result is not None:
            return
        now = self._clock.now()
        candidate = min(now + 30, deadline) if deadline is not None else now + 30
        if episode.deadline is not None and episode.deadline <= candidate:
            return
        if episode.started is None:
            episode.started = now
        episode.deadline = candidate
        for timer in episode.timers:
            timer.cancel()
        episode.timers = [self._clock.at(candidate, expire)]

    def _arm_force(self, scope: _Scope) -> None:
        starts = [
            episode.started
            for episode in (scope.episode, self._invocations[scope.invocation], self._root)
            if episode.started is not None
        ]
        if not starts or scope.done:
            return
        start = min(starts)
        grace = min(start + 20, max(start, scope.eof) + 5) if scope.eof is not None else start + 20
        deadline = self.owners.deadline(scope.invocation)
        if deadline is not None:
            grace = min(grace, deadline - 10)

        def force() -> None:
            self._task(self.force_stop((scope.scope_id,)))

        scope.episode.timers.append(self._clock.at(grace, force))

    def begin_teardown(
        self, invocation_id: str | None = None, *, deadline: float | None = None
    ) -> None:
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("A finite absolute monotonic deadline is required")
        episode = (
            self._root
            if invocation_id is None
            else self._invocations.setdefault(invocation_id, _Episode())
        )
        episode.sealed = True
        previous = self.owners.deadline(invocation_id)
        if previous is not None:
            deadline = min(previous, deadline) if deadline is not None else previous
        self._start_episode(episode, lambda: self._expire("Managed cleanup deadline"), deadline)
        if invocation_id is None:
            for invocation in self._invocations:
                self.begin_teardown(invocation, deadline=episode.deadline)
        for scope in self._scopes.values():
            if invocation_id is None or scope.invocation == invocation_id:
                self._arm_force(scope)
        self._check()

    async def force_stop(self, scope_ids: tuple[str, ...]) -> None:
        """Queue force requests without waiting for an unresponsive peer to read."""
        for scope_id in scope_ids:
            if self._history[scope_id] is not None:
                continue
            scope = self._scopes[scope_id]
            if (
                scope.control is not None
                and not scope.done
                and not scope.completed.is_set()
                and scope.force_write is None
            ):
                scope.force_write = self._send(scope, scope.control, {"event": "FORCE_STOP"})

    def _expire(self, reason: str) -> None:
        self._poison(reason)

    def _poison(self, reason: str) -> None:
        if self._root.result is not None:
            return
        self._root.sealed = True
        self._terminal(self._root, ShutdownResult("unconfirmed", (reason,)))
        for episode in self._invocations.values():
            episode.sealed = True
            if episode.result is None:
                self._terminal(episode, ShutdownResult("unconfirmed", (reason,)))
        for scope in self._scopes.values():
            self._terminal(scope.episode, ShutdownResult("unconfirmed", (reason,)))
        self.owners._quarantine()
        entries = _quarantine.setdefault(self._loop, {})
        for task in tuple(self._tasks):
            if not task.done():
                entries[id(task)] = _Quarantined(self, "@process", task, self.scrub)
        if self._scopes:
            self._task(self.force_stop(tuple(self._scopes)))

    @staticmethod
    def _terminal(episode: _Episode, result: ShutdownResult) -> None:
        if episode.result is None:
            episode.result = result
            for timer in episode.timers:
                timer.cancel()
            episode.terminal.set()

    def _check(self) -> None:
        for scope in self._scopes.values():
            if (
                scope.done
                and scope.reaped
                and scope.facade_exited
                and scope.completed.is_set()
                and not scope.resources
                and scope.consumed
                and not scope.retiring
                and not any(
                    not entry.task.done() and entry.invocation == scope.invocation
                    for entry in self.owners.tasks.values()
                )
                and scope.invocation not in self._writes.values()
            ):
                # Root aggregators can await child release; the root barrier joins them separately.
                scope.retiring = True
                self._task(self._retire(scope))
        for invocation, episode in self._invocations.items():
            if (
                episode.sealed
                and episode.result is None
                and all(
                    (
                        result := record.result
                        if record is not None
                        else self._scopes[scope_id].episode.result
                    )
                    is not None
                    and result.status == "released"
                    for scope_id, record in self._history.items()
                    if (
                        record.proof.invocation_id
                        if record is not None
                        else self._scopes[scope_id].invocation
                    )
                    == invocation
                )
                and all(
                    entry.task.done()
                    for entry in self.owners.tasks.values()
                    if entry.invocation == invocation
                )
                and all(task.done() for task, owner in self._writes.items() if owner == invocation)
            ):
                self._terminal(episode, ShutdownResult("released"))
            if (
                episode.sealed
                and episode.result is not None
                and not any(scope.invocation == invocation for scope in self._scopes.values())
                and not any(
                    not entry.task.done() and entry.invocation == invocation
                    for entry in self.owners.tasks.values()
                )
            ):
                for launcher, (owner, _launch) in tuple(self._launchers.items()):
                    if owner == invocation:
                        del self._launchers[launcher]
        if (
            self._root.sealed
            and self._root.result is None
            and all(
                episode.result is not None and episode.result.status == "released"
                for episode in self._invocations.values()
            )
            and all(entry.task.done() for entry in self.owners.tasks.values())
            and all(task.done() for task in self._writes)
        ):
            self._terminal(self._root, ShutdownResult("released"))

    async def _retire(self, scope: _Scope) -> None:
        """Close only joined scope endpoints, then retain its immutable audit result."""
        channels = tuple(
            {
                channel
                for channel in (scope.control, scope.facade, *scope.channels.values())
                if channel is not None
            }
        )

        def close_channels() -> None:
            for channel in channels:
                self._disconnect(channel)

        await _thread_call(close_channels)
        self._terminal(scope.episode, ShutdownResult("released"))
        assert scope.episode.result is not None
        self._history[scope.scope_id] = _RetiredScope(
            scope.proof, scope.episode.result, scope.episode.deadline, scope.code, scope.error
        )
        del self._scopes[scope.scope_id]
        scope.process = scope.control = scope.facade = scope.launch = None
        scope.channels.clear()
        scope.stdout.clear()
        scope.stderr.clear()
        scope.stdin = scope.on_dispatch = scope.force_write = None

    async def await_release(self, invocation_id: str | None = None) -> ShutdownResult:
        """Observe the first teardown deadline, absorbing repeated cancellation."""
        self.begin_teardown(invocation_id)
        episode = self._root if invocation_id is None else self._invocations[invocation_id]
        terminal = asyncio.create_task(episode.terminal.wait())
        cancelled = False
        try:
            while not episode.terminal.is_set():
                try:
                    await asyncio.wait({terminal})
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            assert episode.result is not None
            return episode.result
        finally:
            terminal.cancel()

    async def spawn_script(
        self,
        invocation_id: str,
        spec: CommandSpec,
        *,
        on_dispatch: Callable[[], None] | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        env = {
            **(os.environ if spec.inherit_control_environment else {}),
            "PYTHONUTF8": "1",
            **spec.env,
        }
        preparation_error: StartError | None = None
        if sys.platform == "win32":
            try:
                inherited = merge_windows_environment(
                    os.environ if spec.inherit_control_environment else {},
                    {"PYTHONUTF8": "1"},
                    canonical_names=("PYTHONUTF8",),
                )
                env = merge_windows_environment(
                    inherited, spec.env, canonical_names=("PYTHONUTF8",)
                )
            except ExecutionSpecError as exc:
                preparation_error = StartError("os_error", str(exc))
        command = spec.command
        if sys.platform != "win32" and (
            os.path.isabs(command)
            or not any(separator and separator in command for separator in (os.sep, os.altsep))
        ):
            command = shutil.which(command, path=env.get("PATH")) or command
        launch = ProcessLaunchSpec((command, *spec.args), env, spec.working_dir or os.getcwd())
        self._admit_launch(invocation_id, launch)
        scope = self._new_scope(invocation_id, launch, uuid4().hex)
        scope.error = preparation_error
        scope.on_dispatch, scope.stdin = on_dispatch, spec.stdin
        self._task(self._run(scope), scope=scope)
        completed = asyncio.create_task(scope.completed.wait())
        terminal = asyncio.create_task(self._invocations[invocation_id].terminal.wait())
        cancelled = False
        timed_out = False
        try:
            try:
                done, _ = await asyncio.wait(
                    {completed, terminal},
                    timeout=spec.timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                timed_out = not done
            except asyncio.CancelledError:
                cancelled = True
            if cancelled or timed_out:
                self.begin_teardown(invocation_id)
                await self.force_stop((scope.scope_id,))
                while not completed.done() and not terminal.done():
                    try:
                        await asyncio.wait(
                            {completed, terminal}, return_when=asyncio.FIRST_COMPLETED
                        )
                    except asyncio.CancelledError:
                        cancelled = True
            if cancelled:
                raise asyncio.CancelledError
            if scope.rejected:
                raise asyncio.CancelledError
            assert scope.launch is not None
            command = scope.launch.argv[0]
            duration = time.monotonic() - started
            if timed_out:
                return CommandResult(
                    "timed_out", resolved_command=command, duration_seconds=duration
                )
            if scope.error is not None:
                return CommandResult(
                    "command_not_found" if scope.error.kind == "file_not_found" else "start_failed",
                    resolved_command=command,
                    start_error=scope.error,
                    duration_seconds=duration,
                )
            if scope.code is not None:
                return CommandResult(
                    "completed",
                    scope.stdout.decode("utf-8", "replace"),
                    scope.stderr.decode("utf-8", "replace"),
                    scope.code,
                    command,
                    duration_seconds=duration,
                )
            return CommandResult(
                "start_failed",
                resolved_command=command,
                start_error=StartError("os_error", "Managed process infrastructure failure"),
                duration_seconds=duration,
            )
        finally:
            scope.consumed = True
            self._check()
            completed.cancel()
            terminal.cancel()

    async def close(self) -> ShutdownResult:
        """Seal, observe bounded release, then close only controller-owned IPC."""
        result = await self.await_release()
        if self._closed:
            return result
        self._closed = True

        async def close_io() -> None:
            await _thread_call(self._close_ipc)

        await asyncio.shield(self._task(close_io()))
        return result

    def _close_ipc(self) -> None:
        """Wake/close owned transports off-loop, including blocked senders/readers."""
        with connect({"endpoint": self._endpoint, "nonce": self._nonce}, authenticate=False):
            pass
        if not self._accept_finished.wait(1):
            raise ExecutionSpecError("Local scope acceptor did not close")
        self._listener.close()
        if sys.platform == "linux":
            os.close(self._ipc_fd)
        with self._connections_lock:
            connections = tuple(self._connections)
            self._connections.clear()
        for connection in connections:
            self._disconnect(connection)

    def _disconnect(self, connection: Channel) -> None:
        """Serialize native close against writers that fail while IPC is closing."""
        with self._connections_lock:
            if not connection.closed:
                disconnect(connection)
            self._connections.discard(connection)
