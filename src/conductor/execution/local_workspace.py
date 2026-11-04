"""Owned mutable local trees, with verify-only identity and fail-closed disposal."""

from __future__ import annotations

import contextlib
import ctypes
import json
import ntpath
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal
from uuid import UUID

from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import BundleRef, RunSpec, WorkspaceLease


@dataclass(frozen=True, slots=True)
class LocalIdentity:
    """Marker data outside the mutable tree; file identity survives publication."""

    version: int
    backend: str
    run_id: str
    lease_id: str
    incarnation: str
    bundle_digest: str
    readiness: str
    runtime_state: Literal["idle", "active", "uncertain"]
    st_dev: int
    st_ino: int


def _directory(path: Path) -> os.stat_result:
    """Inspect a directory itself, never a symlink or Windows reparse point."""
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ExecutionSpecError(f"Managed workspace requires a non-link directory: {path}")
    if info.st_dev <= 0 or info.st_ino <= 0:
        raise ExecutionSpecError(f"Filesystem has no stable positive file identity: {path}")
    again = path.lstat()
    if (info.st_dev, info.st_ino) != (again.st_dev, again.st_ino):
        raise ExecutionSpecError(f"Directory identity changed while verifying: {path}")
    return info


def _components(path: Path, *, create: bool = False) -> None:
    """Check each absolute component before traversing or creating its child."""
    current = Path(path.anchor)
    _ = _directory(current)
    for part in path.parts[1:]:
        current = current / part
        if create:
            with contextlib.suppress(FileExistsError):
                current.mkdir(mode=0o700)
        _ = _directory(current)


def expected_root(run: RunSpec) -> Path:
    """Require the configured home layout, not checkpoint location authority."""
    if (
        not run.run_id
        or run.run_id in (".", "..")
        or any(value in run.run_id for value in ("/", "\\", "\0", ":"))
    ):
        raise ExecutionSpecError("Managed local run_id must be one filesystem component")
    home = Path(os.environ.get("CONDUCTOR_HOME", str(Path.home() / ".conductor")))
    home = Path(os.path.abspath(home))
    expected = home / "workspaces" / run.run_id / "local"
    if run.local_workspace_root is None or Path(run.local_workspace_root) != expected:
        raise ExecutionSpecError("Managed local root must be the expected CONDUCTOR_HOME workspace")
    if not Path(run.local_workspace_root).is_absolute():
        raise ExecutionSpecError("Managed local root must be absolute")
    if sys.platform == "win32":
        _windows_name(run.run_id)
    return expected


def _windows_name(name: str) -> None:
    """Reject names whose Windows representation changes the collected topology."""
    stem = name.split(".", 1)[0].upper()
    if (
        name.endswith((" ", "."))
        or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
        or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        or stem in {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789"}
    ):
        raise ExecutionSpecError(f"Bundle name cannot be represented faithfully on Windows: {name}")


def _link_target(link: Path, root: Path) -> Path:
    """Validate a complete link chain lexically without reading external targets."""
    pending = list(link.relative_to(root).parts)
    resolved: list[str] = []
    seen: set[tuple[str, ...]] = set()
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not resolved:
                raise ExecutionSpecError(f"Bundle symlink escapes the CAS tree: {link}")
            _ = resolved.pop()
            continue
        candidate = root.joinpath(*resolved, part)
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            resolved.append(part)
            continue
        if stat.S_ISLNK(info.st_mode):
            key = (*resolved, part)
            if key in seen:
                raise ExecutionSpecError(f"Bundle contains a cyclic symlink: {link}")
            seen.add(key)
            target = os.readlink(candidate)
            if os.path.isabs(target) or ntpath.isabs(target) or ntpath.splitdrive(target)[0]:
                raise ExecutionSpecError(f"Bundle symlink must be self-contained: {link}")
            pending = list(Path(target).parts) + pending
        else:
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise ExecutionSpecError(
                    f"Bundle contains a junction or reparse point: {candidate}"
                )
            resolved.append(part)
    return root.joinpath(*resolved)


def _copy_tree(source: Path, destination: Path, root: Path) -> None:
    """Copy bytes, never hardlink or flatten symlinks, and keep the copy writable."""
    _ = _directory(source)
    destination.mkdir(mode=0o700)
    names: set[str] = set()
    with os.scandir(source) as entries:
        for entry in entries:
            if sys.platform == "win32":
                _windows_name(entry.name)
                folded = entry.name.casefold()
                if folded in names:
                    raise ExecutionSpecError("Bundle contains Windows case-colliding names")
                names.add(folded)
            src, dst = Path(entry.path), destination / entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                target = _link_target(src, root)
                try:
                    directory = stat.S_ISDIR(target.lstat().st_mode)
                except FileNotFoundError:
                    directory = False
                os.symlink(os.readlink(src), dst, target_is_directory=directory)
            elif getattr(info, "st_file_attributes", 0) & 0x400:
                raise ExecutionSpecError(f"Bundle contains a junction or reparse point: {src}")
            elif stat.S_ISDIR(info.st_mode):
                _copy_tree(src, dst, root)
            elif stat.S_ISREG(info.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                with os.fdopen(os.open(src, flags), "rb") as reader, dst.open("xb") as writer:
                    opened = os.fstat(reader.fileno())
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ExecutionSpecError("CAS entry identity changed during copying")
                    shutil.copyfileobj(reader, writer)
                dst.chmod(stat.S_IMODE(info.st_mode) | stat.S_IRUSR | stat.S_IWUSR)
            else:
                raise ExecutionSpecError(f"Bundle contains an unsupported filesystem entry: {src}")


def _publish(temporary: Path, root: Path) -> None:
    """Atomically publish without replacing even an existing empty directory."""
    if sys.platform == "win32":
        os.rename(temporary, root)
    elif sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise ExecutionSpecError(
                "Atomic no-replace workspace publication is unavailable"
            ) from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(temporary), -100, os.fsencode(root), 1) != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number), str(root))
    else:
        raise ExecutionSpecError("Managed local workspaces require Linux or Windows")


def write_identity(root: Path, identity: LocalIdentity) -> None:
    """Replace the marker atomically only inside the verified directory."""
    info = _directory(root)
    if (info.st_dev, info.st_ino) != (identity.st_dev, identity.st_ino):
        raise ExecutionSpecError("Workspace identity drift prevents marker update")
    descriptor, name = tempfile.mkstemp(prefix=".identity-", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(identity), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        current = _directory(root)
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise ExecutionSpecError("Workspace identity changed during marker publication")
        os.replace(temporary, root / "identity.json")
    finally:
        temporary.unlink(missing_ok=True)


def stage(run: RunSpec, lease: WorkspaceLease, bundle: BundleRef) -> LocalIdentity:
    """Copy the entire immutable CAS namespace and publish its identity last."""
    if sys.platform not in ("linux", "win32"):
        raise ExecutionSpecError("Managed local workspaces require Linux or Windows")
    root = expected_root(run)
    _components(root.parent, create=True)
    if os.path.lexists(root):
        raise ExecutionSpecError(
            "Managed workspace already exists; attach requires verified identity"
        )
    source = Path(bundle.store_path) / "tree"
    _components(source)
    temporary = Path(tempfile.mkdtemp(prefix=".local-", dir=root.parent))
    owned = _directory(temporary)
    try:
        _copy_tree(source, temporary / "work", source)
        info = _directory(temporary)
        identity = LocalIdentity(
            1,
            "local",
            run.run_id,
            lease.lease_id,
            lease.incarnation,
            bundle.digest,
            "ready",
            "idle",
            info.st_dev,
            info.st_ino,
        )
        write_identity(temporary, identity)
        _components(root.parent)
        _publish(temporary, root)
        return identity
    finally:
        if os.path.lexists(temporary):
            current = _directory(temporary)
            if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
                raise ExecutionSpecError("Temporary workspace identity drift; refusing cleanup")
            shutil.rmtree(temporary)


def verify(
    run: RunSpec,
    lease: WorkspaceLease,
    bundle: BundleRef,
    *,
    idle: bool = True,
    previous: LocalIdentity | None = None,
) -> LocalIdentity:
    """Parse identity at the filesystem boundary and verify every ownership field."""
    root = expected_root(run)
    _components(root)
    info = _directory(root)
    marker = root / "identity.json"
    metadata = marker.lstat()
    if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise ExecutionSpecError("Workspace identity marker is not a regular non-link file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(marker, flags), encoding="utf-8") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ExecutionSpecError("Workspace marker changed while opening it")
        data: dict[str, str | int] = json.load(stream)
    try:
        if not isinstance(data, dict) or set(data) != set(LocalIdentity.__dataclass_fields__):
            raise ExecutionSpecError("Workspace marker fields do not match version 1")
        string_fields = (
            "backend",
            "run_id",
            "lease_id",
            "incarnation",
            "bundle_digest",
            "readiness",
        )
        strings: list[str] = []
        for key in string_fields:
            value = data[key]
            if not isinstance(value, str):
                raise ExecutionSpecError(f"Invalid workspace identity field: {key}")
            strings.append(value)
        numbers: list[int] = []
        for key in ("version", "st_dev", "st_ino"):
            value = data[key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ExecutionSpecError(f"Invalid workspace identity field: {key}")
            numbers.append(value)
        state = data["runtime_state"]
        if state not in ("idle", "active", "uncertain"):
            raise ExecutionSpecError("Invalid workspace runtime state")
        assert state == "idle" or state == "active" or state == "uncertain"
        identity = LocalIdentity(
            numbers[0],
            strings[0],
            strings[1],
            strings[2],
            strings[3],
            strings[4],
            strings[5],
            state,
            numbers[1],
            numbers[2],
        )
        _ = UUID(identity.incarnation)
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise ExecutionSpecError("Invalid managed workspace identity marker") from exc
    expected = LocalIdentity(
        1,
        "local",
        run.run_id,
        lease.lease_id,
        lease.incarnation,
        bundle.digest,
        "ready",
        identity.runtime_state,
        info.st_dev,
        info.st_ino,
    )
    if (
        identity != expected
        or lease.backend != "local"
        or lease.location != str(root)
        or identity.runtime_state not in ("idle", "active", "uncertain")
        or (idle and identity.runtime_state != "idle")
        or (previous is not None and identity != previous)
    ):
        raise ExecutionSpecError(
            "Managed workspace identity, readiness or runtime state does not match"
        )
    _ = _directory(root / "work")
    current = _directory(root)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise ExecutionSpecError("Workspace changed during verification")
    return identity


def set_state(
    run: RunSpec,
    lease: WorkspaceLease,
    bundle: BundleRef,
    previous: LocalIdentity,
    state: Literal["idle", "active", "uncertain"],
) -> LocalIdentity:
    """Require unchanged ownership before atomically publishing a lifecycle transition."""
    _ = verify(run, lease, bundle, idle=False, previous=previous)
    updated = replace(previous, runtime_state=state)
    write_identity(expected_root(run), updated)
    return updated


def delete(run: RunSpec, lease: WorkspaceLease, bundle: BundleRef, identity: LocalIdentity) -> None:
    """Remove only the freshly verified idle tree, without following its links."""
    _ = verify(run, lease, bundle, previous=identity)
    shutil.rmtree(expected_root(run))
