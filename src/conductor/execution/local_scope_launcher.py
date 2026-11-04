"""Installed stdio facade and independent, per-command native tree custodian.

Only private authenticated connections carry control messages. Standard streams
are payload bytes, never a source of lifecycle proof. This module is stdlib-only
so the installed helper also works with a minimal target environment.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Client, Listener
from multiprocessing.reduction import send_handle
from pathlib import Path
from types import TracebackType
from typing import Protocol
from uuid import uuid4

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]

MAX_CONTROL = 8 * 1024 * 1024
CHUNK = 65536


class Channel(Protocol):
    """Private authenticated byte channel, independent of its native handle."""

    def send_bytes(self, buf: bytes, offset: int = 0, size: int | None = None) -> None: ...
    def recv_bytes(self, maxlength: int | None = None) -> bytes: ...
    def fileno(self) -> int: ...
    def close(self) -> None: ...
    @property
    def closed(self) -> bool: ...
    def __enter__(self) -> Channel: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...


class Acceptor(Protocol):
    def accept(self) -> Channel: ...
    def close(self) -> None: ...


class CustodyError(OSError):
    """Infrastructure failure, never a target command start error."""


class NativeStream(Protocol):
    def fileno(self) -> int: ...
    def close(self) -> None: ...


def send_frame(connection: Channel, frame: dict[str, Json]) -> None:
    """Send a versioned private frame, without pickle."""
    connection.send_bytes(json.dumps({"v": 1, **frame}).encode())


def receive_frame(connection: Channel) -> dict[str, Json]:
    """Parse the private boundary once; reject malformed/versioned input."""
    value = json.loads(connection.recv_bytes(MAX_CONTROL))
    if not isinstance(value, dict) or value.get("v") != 1:
        raise ValueError("Invalid local scope protocol version")
    if not isinstance(value.get("event"), str):
        raise ValueError("Missing local scope event")
    return value


def text(frame: dict[str, Json], key: str) -> str:
    value = frame[key]
    if not isinstance(value, str):
        raise ValueError("Invalid local scope string field")
    return value


def strings(frame: dict[str, Json], key: str) -> list[str]:
    value = frame[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Invalid local scope argv")
    return [str(item) for item in value]


def environment(frame: dict[str, Json], key: str) -> dict[str, str]:
    value = frame[key]
    if not isinstance(value, dict):
        raise ValueError("Invalid local scope environment")
    result: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, str):
            raise ValueError("Invalid local scope environment entry")
        if name.startswith("CONDUCTOR_LOCAL_SCOPE_"):
            raise ValueError("Reserved local scope environment name")
        result[name] = item
    return result


def connect(descriptor: dict[str, Json], *, authenticate: bool = True) -> Channel:
    channel = Client(
        text(descriptor, "endpoint"),
        family="AF_PIPE" if sys.platform == "win32" else "AF_UNIX",
    )
    if authenticate:
        try:
            key = bytes.fromhex(text(descriptor, "nonce"))
            challenge = channel.recv_bytes(64)
            if len(challenge) != 32:
                raise ValueError("Invalid local scope authentication challenge")
            reply = os.urandom(32)
            channel.send_bytes(hmac.digest(key, b"client" + challenge, hashlib.sha256) + reply)
            if not hmac.compare_digest(
                channel.recv_bytes(32), hmac.digest(key, b"server" + reply, hashlib.sha256)
            ):
                raise ValueError("Invalid local scope authentication response")
        except (OSError, EOFError, ValueError):
            channel.close()
            raise
    return channel


def authenticate(channel: Channel, key: bytes) -> None:
    """Mutually authenticate byte channels without deserializing remote objects."""
    challenge = os.urandom(32)
    channel.send_bytes(challenge)
    response = channel.recv_bytes(64)
    if len(response) != 64 or not hmac.compare_digest(
        response[:32], hmac.digest(key, b"client" + challenge, hashlib.sha256)
    ):
        raise ValueError("Invalid local scope authentication response")
    channel.send_bytes(hmac.digest(key, b"server" + response[32:], hashlib.sha256))


def private_listener(address: str) -> Acceptor:
    """Bind a user-private Unix socket or an owner-only Windows named pipe."""
    if sys.platform != "win32":
        listener = Listener(address, family="AF_UNIX")
        os.chmod(address, 0o600)
        return listener
    # multiprocessing's default Windows DACL grants Everyone read access. The
    # native pipe factory below installs an explicit owner/system DACL instead.
    return _WindowsListener(address)


def protect(path: Path, mode: int) -> None:
    """Set private Unix mode or an explicit owner-only protected Windows DACL."""
    if sys.platform != "win32":
        path.chmod(mode)
        return
    descriptor = wintypes.LPVOID()
    convert = _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    if not convert("D:P(A;;GA;;;OW)(A;;GA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
        if not _advapi.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel.LocalFree(descriptor)


def disconnect(channel: Channel) -> None:
    """Unblock pending native I/O before releasing this owned connection."""
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel.CancelIoEx(channel.fileno(), None)
        else:
            with socket.socket(fileno=os.dup(channel.fileno())) as native:
                native.shutdown(socket.SHUT_RDWR)
    channel.close()


def _pump(source: NativeStream, target: NativeStream, finished: Callable[[], None]) -> None:
    """Drain even when the consumer has closed its reader."""
    forwarding = True
    try:
        while data := os.read(source.fileno(), CHUNK):
            if forwarding:
                try:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(target.fileno(), view) :]
                except (OSError, ValueError):
                    forwarding = False
    finally:
        finished()


def _facade(descriptor: dict[str, Json]) -> int:
    with connect(descriptor) as control:
        argv: list[Json] = list(sys.argv[1:])
        env: dict[str, Json] = dict(os.environ)
        hello: dict[str, Json] = {
            "event": "HELLO",
            "role": "facade",
            "launcher": text(descriptor, "launcher"),
            "incarnation": uuid4().hex,
            "args": argv,
            "env": env,
            "cwd": os.getcwd(),
        }
        send_frame(control, hello)
        if sys.platform == "linux":
            with contextlib.ExitStack() as stack:
                fd = os.pidfd_open(os.getpid())
                stack.callback(os.close, fd)
                send_handle(control, fd, int(text(descriptor, "controller")))
        elif sys.platform == "win32":
            import _winapi

            destination = _winapi.OpenProcess(
                _winapi.PROCESS_DUP_HANDLE, False, int(text(descriptor, "controller"))
            )
            try:
                remote = _winapi.DuplicateHandle(
                    _winapi.GetCurrentProcess(),
                    _winapi.GetCurrentProcess(),
                    destination,
                    0,
                    False,
                    _winapi.DUPLICATE_SAME_ACCESS,
                )
                try:
                    send_frame(control, {"event": "FACADE_HANDLE", "handle": remote})
                except (OSError, EOFError):
                    recovered = _winapi.DuplicateHandle(
                        destination,
                        remote,
                        _winapi.GetCurrentProcess(),
                        0,
                        False,
                        _winapi.DUPLICATE_CLOSE_SOURCE | _winapi.DUPLICATE_SAME_ACCESS,
                    )
                    _winapi.CloseHandle(recovered)
                    raise
            finally:
                _winapi.CloseHandle(destination)
        else:
            raise OSError("Managed local custody requires Linux pidfd or Windows handles")
        registered = receive_frame(control)
        if registered["event"] != "REGISTERED":
            return 125
        scope = text(registered, "scope")
        with contextlib.ExitStack() as stack:
            channels: list[Channel] = []
            for name in ("stdin", "stdout", "stderr"):
                channel = stack.enter_context(connect(descriptor))
                send_frame(
                    channel,
                    {"event": "DATA", "scope": scope, "stream": name},
                )
                channels.append(channel)

            def input_pump() -> None:
                with contextlib.suppress(BrokenPipeError, OSError):
                    while data := os.read(sys.stdin.fileno(), CHUNK):
                        channels[0].send_bytes(data)
                    channels[0].send_bytes(b"")

            def output_pump(channel: Channel, target: NativeStream) -> None:
                forwarding = True
                try:
                    while data := channel.recv_bytes(CHUNK):
                        if forwarding:
                            try:
                                view = memoryview(data)
                                while view:
                                    view = view[os.write(target.fileno(), view) :]
                            except (BrokenPipeError, OSError):
                                forwarding = False
                except (EOFError, OSError):
                    return

            threading.Thread(target=input_pump, daemon=True).start()
            outputs = [
                threading.Thread(
                    target=output_pump, args=(channels[1], sys.stdout.buffer), daemon=True
                ),
                threading.Thread(
                    target=output_pump, args=(channels[2], sys.stderr.buffer), daemon=True
                ),
            ]
            for thread in outputs:
                thread.start()
            result = receive_frame(control)
            for thread in outputs:
                thread.join()
            code = result.get("code")
            if result["event"] == "RESULT" and isinstance(code, int):
                if code < 0 and sys.platform != "win32":
                    os.kill(os.getpid(), -code)
                return code
            return 125


class _SignalWakeup:
    """Level-triggered native wakeup; Python signal handlers never take its lock."""

    def __init__(self) -> None:
        self.reader, self.writer = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._lock = threading.Lock()
        self._closed = False

    def set(self) -> None:
        with self._lock:
            if not self._closed:
                # A full pipe is already readable; the consumer will inspect all state.
                with contextlib.suppress(BlockingIOError):
                    os.write(self.writer, b"\0")

    def clear(self) -> None:
        # Clear precedes reap/state inspection. A later notification remains readable
        # across the check -> wait boundary; a consumed one is covered by that inspection.
        with contextlib.suppress(BlockingIOError):
            os.read(self.reader, CHUNK)

    def wait(self, timeout: float | None = None) -> bool:
        return bool(select.select([self.reader], [], [], timeout)[0])

    def close(self) -> None:
        with self._lock:
            self._closed = True
            os.close(self.reader)
            os.close(self.writer)


class _LinuxTree:
    """Mutable native reaping state; no PID-only success or process-group proof."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.code: int | None = None
        self.empty = False
        self.stdin: NativeStream | None = None
        self.stdout: NativeStream | None = None
        self.stderr: NativeStream | None = None
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot establish child subreaper")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise OSError("Linux pidfd support is required")
        self.changed = _SignalWakeup()
        self._previous_wakeup_fd = signal.set_wakeup_fd(
            self.changed.writer, warn_on_full_buffer=False
        )
        self._previous_sigchld = signal.signal(signal.SIGCHLD, lambda _signum, _frame: None)

    def start(self, argv: list[str], env: dict[str, str], cwd: str) -> None:
        self.process = subprocess.Popen(
            argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.stdin, self.stdout, self.stderr = (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        )

    def close(self) -> None:
        signal.set_wakeup_fd(self._previous_wakeup_fd)
        signal.signal(signal.SIGCHLD, self._previous_sigchld)
        self.changed.close()
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                stream.close()

    def reap(self) -> None:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                self.empty = True
                return
            if pid == 0:
                return
            if self.process is not None and pid == self.process.pid:
                self.code = os.waitstatus_to_exitcode(status)
                self.process.returncode = self.code

    def force(self) -> None:
        # Killing a parent reparents its descendants to this subreaper. Repeat on
        # SIGCHLD until waitpid reports ECHILD, including setsid/double forks.
        for task in Path("/proc/self/task").iterdir():
            try:
                children = (task / "children").read_text().split()
            except FileNotFoundError:
                continue
            for raw in children:
                try:
                    fd = os.pidfd_open(int(raw))
                except ProcessLookupError:
                    continue
                try:
                    with contextlib.suppress(ProcessLookupError):
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)


if sys.platform == "win32":
    import _winapi
    import msvcrt
    from ctypes import wintypes
    from multiprocessing.connection import PipeConnection

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", wintypes.LPVOID),
            ("inherit", wintypes.BOOL),
        ]

    class _WindowsListener:
        """Owner-only named pipes with the same authenticated bytes interface."""

        def __init__(self, address: str) -> None:
            self.address_value = address
            self.closed = False
            self.pending: int | None = self._new_pipe()

        def _new_pipe(self) -> int:
            descriptor = wintypes.LPVOID()
            _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.DWORD),
            ]
            if not _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "D:P(A;;GA;;;OW)(A;;GA;;;SY)", 1, ctypes.byref(descriptor), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            _kernel.CreateNamedPipeW.restype = wintypes.HANDLE
            _kernel.CreateNamedPipeW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(_SecurityAttributes),
            ]
            try:
                handle = _kernel.CreateNamedPipeW(
                    self.address_value,
                    3 | 0x40000000,
                    4 | 2 | 8,
                    255,
                    MAX_CONTROL,
                    MAX_CONTROL,
                    0,
                    ctypes.byref(attributes),
                )
            finally:
                _kernel.LocalFree(descriptor)
            if handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            return handle

        def accept(self) -> Channel:
            assert self.pending is not None
            handle = self.pending
            try:
                overlapped = _winapi.ConnectNamedPipe(handle, overlapped=True)
                _size, error = overlapped.GetOverlappedResult(True)
                if error:
                    raise ctypes.WinError(error)
            except OSError as exc:
                if exc.winerror != 535:  # ERROR_PIPE_CONNECTED is successful readiness.
                    raise
            self.pending = self._new_pipe()
            return PipeConnection(handle)

        def close(self) -> None:
            self.closed = True
            if self.pending is not None:
                _winapi.CloseHandle(self.pending)
                self.pending = None

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("basic", _BasicLimit),
            ("io", _IoCounters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("user", ctypes.c_int64),
            ("kernel", ctypes.c_int64),
            ("period_user", ctypes.c_int64),
            ("period_kernel", ctypes.c_int64),
            ("faults", wintypes.DWORD),
            ("total", wintypes.DWORD),
            ("active", wintypes.DWORD),
            ("terminated", wintypes.DWORD),
        ]

    class _CompletionPort(ctypes.Structure):
        _fields_ = [("key", ctypes.c_void_p), ("port", wintypes.HANDLE)]

    class _WindowsTree:
        """Nonbreakaway job, assigned before the suspended primary thread runs."""

        def __init__(self) -> None:
            self.changed = threading.Event()
            self.code: int | None = None
            self.empty = False
            self.process: subprocess.Popen[bytes] | None = None
            self.handle: int | None = None
            self.notifications: threading.Thread | None = None
            self.stdin: NativeStream | None = None
            self.stdout: NativeStream | None = None
            self.stderr: NativeStream | None = None
            for name in ("CreateJobObjectW", "CreateIoCompletionPort"):
                getattr(_kernel, name).restype = wintypes.HANDLE
            _kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            _kernel.CreateIoCompletionPort.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
                ctypes.c_size_t,
                wintypes.DWORD,
            ]
            _kernel.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            _kernel.QueryInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.LPVOID,
            ]
            _kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            _kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.job = _kernel.CreateJobObjectW(None, None)
            self.port = _kernel.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
            if not self.job or not self.port:
                raise ctypes.WinError(ctypes.get_last_error())
            limit = _ExtendedLimit()
            limit.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; never BREAKAWAY_OK.
            association = _CompletionPort(1, self.port)
            for kind, info in ((9, limit), (7, association)):
                if not _kernel.SetInformationJobObject(
                    self.job, kind, ctypes.byref(info), ctypes.sizeof(info)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())

        def start(self, argv: list[str], env: dict[str, str], cwd: str) -> None:
            with contextlib.ExitStack() as stack:
                child_in, parent_in = os.pipe()
                parent_out, child_out = os.pipe()
                parent_err, child_err = os.pipe()
                for fd in (child_in, parent_in, parent_out, child_out, parent_err, child_err):
                    stack.callback(os.close, fd)
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
                handles = [msvcrt.get_osfhandle(fd) for fd in (child_in, child_out, child_err)]
                for handle in handles:
                    os.set_handle_inheritable(handle, True)
                startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
                startup.lpAttributeList = {"handle_list": handles}
                process, thread, _pid, _tid = _winapi.CreateProcess(
                    None,
                    subprocess.list2cmdline(argv),
                    None,
                    None,
                    True,
                    4 | 0x400,
                    env,
                    cwd,
                    startup,
                )
                try:
                    if not _kernel.AssignProcessToJobObject(self.job, process):
                        _winapi.TerminateProcess(process, 125)
                        _winapi.WaitForSingleObject(process, _winapi.INFINITE)
                        raise CustodyError("Cannot assign suspended payload to custody job")
                    self.handle = process
                    self.stdin = os.fdopen(os.dup(parent_in), "wb", buffering=0)
                    self.stdout = os.fdopen(os.dup(parent_out), "rb", buffering=0)
                    self.stderr = os.fdopen(os.dup(parent_err), "rb", buffering=0)
                    _kernel.ResumeThread.argtypes = [wintypes.HANDLE]
                    _kernel.ResumeThread.restype = wintypes.DWORD
                    if _kernel.ResumeThread(thread) == 0xFFFFFFFF:
                        _winapi.TerminateProcess(process, 125)
                        _winapi.WaitForSingleObject(process, _winapi.INFINITE)
                        raise CustodyError("Cannot resume assigned payload")
                except OSError:
                    _winapi.CloseHandle(process)
                    raise
                finally:
                    _winapi.CloseHandle(thread)
            threading.Thread(target=self._wait, daemon=True).start()

        def _wait(self) -> None:
            assert self.handle is not None
            _winapi.WaitForSingleObject(self.handle, _winapi.INFINITE)
            self.code = _winapi.GetExitCodeProcess(self.handle)
            self.changed.set()

        def reap(self) -> None:
            accounting = _Accounting()
            if not _kernel.QueryInformationJobObject(
                self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self.empty = accounting.active == 0

        def force(self) -> None:
            if not _kernel.TerminateJobObject(self.job, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            # Notifications only wake a fresh QueryInformationJobObject proof.
            if self.notifications is None:
                self.notifications = threading.Thread(target=self._notifications, daemon=True)
                self.notifications.start()

        def _notifications(self) -> None:
            count = wintypes.DWORD()
            key = ctypes.c_size_t()
            overlapped = ctypes.c_void_p()
            _kernel.GetQueuedCompletionStatus.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_void_p),
                wintypes.DWORD,
            ]
            while _kernel.GetQueuedCompletionStatus(
                self.port,
                ctypes.byref(count),
                ctypes.byref(key),
                ctypes.byref(overlapped),
                0xFFFFFFFF,
            ):
                self.changed.set()
                if count.value == 4:  # ACTIVE_PROCESS_ZERO, still query in main.
                    return

        def close(self) -> None:
            for stream in (self.stdin, self.stdout, self.stderr):
                if stream is not None:
                    stream.close()
            for handle in (self.handle, self.job, self.port):
                if handle:
                    _winapi.CloseHandle(handle)


def _custodian(descriptor: dict[str, Json]) -> int:
    tree = _WindowsTree() if sys.platform == "win32" else _LinuxTree()
    with connect(descriptor) as control:
        lock = threading.Lock()
        lost = threading.Event()
        forced = threading.Event()
        input_closed = threading.Event()
        drained = [threading.Event(), threading.Event()]
        deadline: float | None = None

        def emit(event: str, **values: Json) -> None:
            with lock, contextlib.suppress(BrokenPipeError, OSError):
                send_frame(control, {"event": event, **values})

        emit("CUSTODIAN_CONNECTED", scope=text(descriptor, "scope"))
        configuration = receive_frame(control)
        if configuration["event"] != "CONFIGURE":
            raise ValueError("Missing private launch configuration")
        argv = strings(configuration, "argv")
        env = environment(configuration, "env")
        cwd = text(configuration, "cwd")
        emit("CUSTODIAN_READY")
        command = receive_frame(control)
        if command["event"] == "REJECTED":
            tree.reap()
            if not tree.empty:
                raise CustodyError("Rejected helper has unexpected children")
            emit("SCOPE_EMPTY")
            emit("STREAMS_DRAINED")
            emit("DONE")
            tree.close()
            return 0
        if command["event"] != "GO":
            raise ValueError("Invalid registration gate")

        def watch_control() -> None:
            try:
                while True:
                    frame = receive_frame(control)
                    if frame["event"] != "FORCE_STOP":
                        raise ValueError("Invalid custodian control event")
                    forced.set()
                    tree.changed.set()
            except (EOFError, OSError, ValueError):
                lost.set()
                tree.changed.set()

        threading.Thread(target=watch_control, daemon=True).start()
        try:
            tree.start(argv, env, cwd)
        except CustodyError:
            tree.close()
            emit("HELPER_FAILED", phase="assignment", message="Native custody setup failed")
            return 125
        except OSError as exc:
            emit(
                "PAYLOAD_START_FAILED",
                resolved_command=argv[0],
                kind="file_not_found" if isinstance(exc, FileNotFoundError) else "os_error",
                message=exc.strerror if exc.errno is not None else str(exc),
                errno=exc.errno,
                filename=exc.filename,
            )
            tree.reap()
            if not tree.empty:
                raise CustodyError("Start failure left an unproved native scope") from exc
            emit("SCOPE_EMPTY")
            emit("STREAMS_DRAINED")
            emit("DONE")
            tree.close()
            return 0
        target_in, target_out, target_err = tree.stdin, tree.stdout, tree.stderr
        assert target_in is not None and target_out is not None and target_err is not None
        emit("COMMAND_STARTED")

        def end_input() -> None:
            target_in.close()
            input_closed.set()
            emit("INPUT_CLOSED")
            tree.changed.set()

        threading.Thread(
            target=_pump, args=(sys.stdin.buffer, target_in, end_input), daemon=True
        ).start()
        for source, target, event in (
            (target_out, sys.stdout.buffer, drained[0]),
            (target_err, sys.stderr.buffer, drained[1]),
        ):

            def finished(event: threading.Event = event) -> None:
                event.set()
                tree.changed.set()

            threading.Thread(target=_pump, args=(source, target, finished), daemon=True).start()
        announced = False
        cleaning = False
        grace: float | None = None
        while True:
            tree.changed.clear()
            tree.reap()
            now = time.monotonic()
            if tree.code is not None and not announced:
                emit("COMMAND_EXITED", code=tree.code)
                announced = True
                cleaning = True
                deadline = min(deadline or now + 30, now + 30)
            if lost.is_set() and deadline is None:
                deadline = now + 30
                grace = now + (5 if input_closed.is_set() else 20)
            if lost.is_set() and input_closed.is_set():
                grace = min(grace or now + 5, now + 5)
            if forced.is_set() or (grace is not None and now >= grace):
                cleaning = True
                deadline = min(deadline or now + 30, now + 30)
            if cleaning:
                tree.force()
                tree.reap()
            if announced and tree.empty and all(event.is_set() for event in drained):
                emit("SCOPE_EMPTY")
                emit("STREAMS_DRAINED")
                emit("DONE")
                tree.close()
                return 0
            if deadline is not None and now >= deadline:
                emit("HELPER_FAILED", phase="cleanup", message="Native cleanup deadline")
                tree.close()
                return 125
            next_wake = min(
                value
                for value in (deadline, grace if not cleaning else None, now + 86400)
                if value is not None
            )
            tree.changed.wait(max(0, next_wake - time.monotonic()))


def main() -> int:
    """Resolve immutable role metadata relative to the invoked installed copy."""
    try:
        if sys.platform == "win32":
            import msvcrt

            for fd in (0, 1, 2):
                msvcrt.setmode(fd, os.O_BINARY)
        descriptor = json.loads(
            Path(sys.argv[0]).with_name("descriptor.json").read_text(encoding="utf-8")
        )
        if not isinstance(descriptor, dict) or descriptor.get("v") != 1:
            raise ValueError("Invalid local scope descriptor")
        match descriptor.get("role"):
            case "facade":
                return _facade(descriptor)
            case "custodian":
                return _custodian(descriptor)
            case _:
                raise ValueError("Invalid local scope role")
    except (OSError, EOFError, ValueError, KeyError):
        # Never echo private descriptors, credentials, target argv or tokens.
        sys.stderr.write("conductor-local-scope: infrastructure failure\n")
        return 125
