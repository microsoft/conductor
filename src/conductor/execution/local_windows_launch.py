"""Declared-context Windows image selection and limited literal batch invocation."""

from __future__ import annotations

import ctypes
import errno
import ntpath
import os
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import replace
from functools import cache

from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import ProcessLaunchSpec, StartError

_SELECTORS = ("PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT")
_EXTENSIONS = (".com", ".exe", ".bat", ".cmd")
_RESERVED = "CONDUCTOR_LOCAL_SCOPE_"


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


@cache
def _ordinal_library() -> ctypes.CDLL:
    if sys.platform != "win32":
        raise ExecutionSpecError("Windows ordinal environment comparison requires Windows")
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CompareStringOrdinal.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.CompareStringOrdinal.restype = ctypes.c_int
    return library


def _same_name(first: str, second: str) -> bool:
    if first.isascii() and second.isascii():
        return first.upper() == second.upper()
    if sys.platform != "win32":
        raise ExecutionSpecError("Windows ordinal environment comparison requires Windows")
    # Python casefold expands characters that Windows ordinal identity keeps distinct.
    result = _ordinal_library().CompareStringOrdinal(
        first, _utf16_length(first), second, _utf16_length(second), True
    )
    if result == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    return result == 2


def merge_windows_environment(
    base: Mapping[str, str],
    overlay: Mapping[str, str],
    *,
    canonical_names: Collection[str] = (),
) -> dict[str, str]:
    """Copy Windows variables, rejecting ambiguous aliases before applying the overlay."""
    canonical = (*_SELECTORS, *canonical_names)
    result: dict[str, str] = {}
    for source in (base, overlay):
        normalized: dict[str, str] = {}
        for key, value in source.items():
            if _same_name(key[: len(_RESERVED)], _RESERVED):
                raise ExecutionSpecError("Reserved local scope environment name")
            name = next((name for name in canonical if _same_name(key, name)), key)
            previous = next((old for old in normalized if _same_name(name, old)), None)
            if previous is not None and normalized[previous] != value:
                raise ExecutionSpecError("Conflicting Windows environment aliases")
            normalized[previous if previous is not None else name] = value
        for name, value in normalized.items():
            previous = next((old for old in result if _same_name(name, old)), None)
            if previous is not None:
                result.pop(previous)
            result[name] = value
    return result


def _absolute(path: str) -> bool:
    drive, tail = ntpath.splitdrive(path)
    return bool(drive) and (tail.startswith(("\\", "/")) or drive.startswith(("\\\\", "//")))


def _qualified(path: str, cwd: str) -> str:
    if _absolute(path):
        return ntpath.normpath(path)
    drive, tail = ntpath.splitdrive(path)
    if not path or drive or tail.startswith(("\\", "/")):
        raise ExecutionSpecError("Windows launch requires an unambiguous path")
    return ntpath.normpath(ntpath.join(cwd, path))


def _drive_absolute(path: str) -> bool:
    drive, tail = ntpath.splitdrive(path)
    return (
        len(drive) == 2
        and drive[0].isascii()
        and drive[0].isalpha()
        and drive[1] == ":"
        and tail.startswith(("\\", "/"))
    )


def _interpreter(env: Mapping[str, str]) -> str:
    if "COMSPEC" in env:
        path = env["COMSPEC"]
    else:
        root = env.get("SYSTEMROOT", "")
        if not _drive_absolute(root) or '"' in root or "\0" in root:
            raise ExecutionSpecError(
                "Batch launch requires declared absolute SystemRoot or ComSpec"
            )
        path = ntpath.join(root, "System32", "cmd.exe")
    if not _drive_absolute(path) or '"' in path or "\0" in path:
        raise ExecutionSpecError("Batch launch requires an unquoted drive-absolute ComSpec")
    return ntpath.normpath(path)


def _batch_argument(value: str, position: int) -> str:
    if any(
        char in '"%!^&|<>()*?' or unicodedata.category(char) in ("Cc", "Cs", "Zl", "Zp")
        for char in value
    ):
        raise ExecutionSpecError(f"Unsupported literal batch character in argument {position}")
    if value.endswith("\\"):
        if not all(char in "._-/\\:" or unicodedata.category(char)[0] in "LMN" for char in value):
            raise ExecutionSpecError(
                f"Batch argument {position} needs quoting before a trailing backslash"
            )
        return value
    return f'"{value}"'


def windows_create_process_arguments(prepared: ProcessLaunchSpec) -> tuple[str | None, str]:
    """Encode an already selected image without filesystem or ambient-context lookup."""
    if not prepared.argv or not _absolute(prepared.argv[0]):
        raise ExecutionSpecError("Windows launch requires a fully qualified selected target")
    if not _absolute(prepared.working_dir):
        raise ExecutionSpecError("Windows launch requires a fully qualified working directory")
    env = merge_windows_environment(prepared.env, {})
    extension = ntpath.splitext(prepared.argv[0])[1].lower()
    if extension not in _EXTENSIONS:
        raise ExecutionSpecError("Unsupported Windows target type; use a native executable")
    if extension in (".exe", ".com"):
        return None, subprocess.list2cmdline(prepared.argv)
    if not all(_drive_absolute(path) for path in (prepared.argv[0], prepared.working_dir)):
        raise ExecutionSpecError("Automatic batch launch requires drive-absolute target and cwd")
    interpreter = _interpreter(env)
    body = " ".join(_batch_argument(value, index) for index, value in enumerate(prepared.argv))
    command_line = f'"{interpreter}" /d /e:on /v:off /s /c "{body}"'
    if _utf16_length(command_line) + 1 > 8191:
        raise ExecutionSpecError("Batch command line exceeds the 8191 UTF-16 unit limit")
    if any(_utf16_length(f"{name}={value}") + 1 > 8191 for name, value in env.items()):
        raise ExecutionSpecError("Batch environment entry exceeds the 8191 UTF-16 unit limit")
    return interpreter, command_line


def _resolve(launch: ProcessLaunchSpec) -> str:
    command = launch.argv[0]
    extension = ntpath.splitext(command)[1].lower()
    if extension and extension not in _EXTENSIONS:
        raise ExecutionSpecError("Unsupported Windows target type; use a native executable")
    qualified = bool(ntpath.dirname(command) or ntpath.splitdrive(command)[0])
    if qualified:
        command = _qualified(command, launch.working_dir)
        if extension:
            # Explicit native paths reach CreateProcess even when missing or inaccessible.
            return command
        directories = [""]
    else:
        directories = []
        for entry in launch.env.get("PATH", "").split(";"):
            if not entry:
                continue
            if '"' in entry:
                if not (entry.startswith('"') and entry.endswith('"') and entry.count('"') == 2):
                    raise ExecutionSpecError("Unsupported quoting in Windows PATH entry")
                entry = entry[1:-1]
                if not entry:
                    continue
            directories.append(_qualified(entry, launch.working_dir))
    suffixes = [""] if extension else []
    if not extension:
        for suffix in launch.env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";"):
            if suffix.lower() in _EXTENSIONS and suffix.lower() not in (
                old.lower() for old in suffixes
            ):
                suffixes.append(suffix)
    for directory in directories:
        for suffix in suffixes:
            candidate = ntpath.join(directory, command + suffix)
            try:
                info = os.stat(candidate)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if stat.S_ISREG(info.st_mode):
                return candidate
    if sys.platform == "win32":
        error = ctypes.WinError(2)
        # Match CPython's native CreateProcess formatting only for this synthetic miss.
        message = error.strerror
        while message and (message[-1] <= " " or message[-1] == "."):
            message = message[:-1]
        error.strerror = message
        raise error
    # Portable policy tests use the host's missing-file data, never a simulated Win32 result.
    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))


def prepare_windows_launch(
    launch: ProcessLaunchSpec,
) -> tuple[ProcessLaunchSpec, StartError | None]:
    """Resolve the final script or HELLO context, retaining the last selected target on refusal."""
    prepared = replace(launch, env=dict(launch.env))
    try:
        prepared = replace(prepared, env=merge_windows_environment(launch.env, {}))
        if not launch.argv or not launch.argv[0]:
            raise ExecutionSpecError("Windows launch requires a nonempty command")
        if not _absolute(launch.working_dir):
            raise ExecutionSpecError("Windows launch requires a fully qualified working directory")
        target = _resolve(prepared)
        prepared = replace(prepared, argv=(target, *launch.argv[1:]))
        interpreter, _command_line = windows_create_process_arguments(prepared)
        if interpreter is not None:
            with open(target, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ExecutionSpecError("Batch target must be a readable regular file")
            if not stat.S_ISREG(os.stat(interpreter).st_mode):
                raise ExecutionSpecError("Batch interpreter must be a regular image")
    except ExecutionSpecError as exc:
        return prepared, StartError("os_error", str(exc))
    except OSError as exc:
        return prepared, StartError(
            "file_not_found" if isinstance(exc, FileNotFoundError) else "os_error",
            exc.strerror if exc.strerror is not None else str(exc),
            exc.errno,
            exc.filename,
        )
    return prepared, None
