"""Validate OCI RunBundle layers and publish them into the ordinary bundle store."""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import zlib
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import IO, Final, NamedTuple, assert_never

from conductor.bundle import store
from conductor.bundle.errors import BundleError
from conductor.bundle.model import BundleEntry, BundleManifest
from conductor.bundle.oci import (
    MAX_INNER_MANIFEST_BYTES,
    MAX_LAYER_BYTES,
    MAX_PATH_BYTES,
    MAX_TAR_BYTES,
    OciConfig,
    _load,
    _manifest_input,
    encode_oci_config,
)

_MANIFEST: Final = store._STORE_MANIFEST_COPY.as_posix()
_CHUNK: Final = 64 * 1024
_BLOCK: Final = tarfile.BLOCKSIZE
_DEVICE: Final = re.compile(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]", re.I)
_INVALID: Final = re.compile(r'[\x00-\x1f<>:"\\|?*]')


class BundleImportError(BundleError):
    """An unsafe layer, corrupt CAS entry or failed filesystem operation."""


class _Tree(NamedTuple):
    entries: dict[str, BundleEntry]
    directories: set[str]
    directory_links: set[str]


def _components(path: str, *, link: bool = False) -> list[str]:
    parts = path.split("/")
    if len(path.encode("utf-8")) > MAX_PATH_BYTES or not parts[0]:
        raise BundleImportError(f"Invalid bundle path: {path!r}")
    for part in parts:
        if link and part in ("", ".", ".."):
            continue
        if (
            not part
            or part.endswith((".", " "))
            or _INVALID.search(part)
            or _DEVICE.fullmatch(part.partition(".")[0].rstrip(" "))
        ):
            raise BundleImportError(f"Non-portable bundle path: {path!r}")
    return parts


def _layout(manifest: BundleManifest) -> _Tree:
    entries = {entry.logical_path: entry for entry in manifest.entries}
    directories: set[str] = set()
    links: dict[str, list[str]] = {}
    for path, entry in entries.items():
        parts = _components(path)
        if len(parts) < 2 or parts[0] != "tree":
            raise BundleImportError(f"Bundle payload is outside tree/: {path!r}")
        directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
        if (target := entry.link_target) is not None:
            if entry.size or entry.executable or _digest(target.encode("utf-8")) != entry.digest:
                raise BundleImportError(f"Invalid manifest symlink: {path!r}")
            links[path] = _components(target, link=True)
    nodes = directories | entries.keys()
    if directories & entries.keys() or len({p.casefold() for p in nodes}) != (
        len(directories) + len(entries)
    ):
        raise BundleImportError("Colliding bundle paths")
    resolved: dict[str, str] = {}
    for path in links:
        _resolve_link(path, links, nodes, directories, resolved)
    # Recursive walks follow both ordinary children and aliases to resolved directories.
    graph: TopologicalSorter[str] = TopologicalSorter()
    for directory in sorted(directories - {"tree"}):
        graph.add(directory.rpartition("/")[0], directory)
    directory_links: set[str] = set()
    for path, target in resolved.items():
        if target in directories:
            graph.add(path.rpartition("/")[0], target)
            directory_links.add(path)
    try:
        graph.prepare()
    except CycleError as exc:
        raise BundleImportError("Cycle in bundle directory topology") from exc
    return _Tree(entries, directories, directory_links)


def _resolve_link(
    path: str,
    links: dict[str, list[str]],
    nodes: set[str],
    directories: set[str],
    resolved: dict[str, str],
) -> None:
    """Resolve POSIX link walks against declared nodes, without filesystem access."""
    if path in resolved:
        return
    stack: list[tuple[str, Iterator[str]]] = [(path, iter(links[path]))]
    active = {path}
    current = path.rpartition("/")[0]
    while stack:
        owner, parts = stack[-1]
        part = next(parts, None)
        if part is None:
            resolved[owner] = current
            active.remove(owner)
            stack.pop()
            continue
        if current not in directories or (part == ".." and current == "tree"):
            raise BundleImportError(f"Invalid traversal in bundle symlink: {path!r}")
        if part == "..":
            current = current.rpartition("/")[0]
        elif part not in ("", "."):
            current = f"{current}/{part}"
            if current not in nodes or current in active:
                raise BundleImportError(f"Missing target or cycle in bundle symlink: {path!r}")
            if current in resolved:
                current = resolved[current]
            elif current in links:
                active.add(current)
                stack.append((current, iter(links[current])))
                current = current.rpartition("/")[0]


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _file_digest(data: IO[bytes]) -> str:
    hashed = hashlib.sha256()
    while chunk := data.read(_CHUNK):
        hashed.update(chunk)
    return f"sha256:{hashed.hexdigest()}"


def _parse_manifest(raw: bytes) -> BundleManifest:
    parsed = _manifest_input(_load(raw, MAX_INNER_MANIFEST_BYTES))
    return BundleManifest.model_validate(parsed, strict=True)


@contextmanager
def _read_archive(layer: Path, manifest: BundleManifest, tree: _Tree) -> Generator[tarfile.TarFile]:
    with layer.open("rb") as source, tempfile.TemporaryFile() as decoded:
        if os.fstat(source.fileno()).st_size > MAX_LAYER_BYTES:
            raise BundleImportError("Compressed bundle layer exceeds its byte limit")
        with gzip.GzipFile(fileobj=source) as compressed:
            total = 0
            while chunk := compressed.read(_CHUNK):
                total += len(chunk)
                if total > MAX_TAR_BYTES:
                    raise BundleImportError("Decoded bundle tar exceeds its byte limit")
                decoded.write(chunk)
        decoded.seek(0)
        with tarfile.open(fileobj=decoded, mode="r:") as tar:
            seen: set[str] = set()
            end = 0
            for member in tar:
                name = member.name
                if name in seen or (name != _MANIFEST and name not in tree.entries):
                    raise BundleImportError(f"Unexpected or duplicate bundle member: {name!r}")
                sparse_pax = any(k.startswith("GNU.sparse.") for k in member.pax_headers)
                if member.issparse() or sparse_pax:
                    raise BundleImportError(f"Sparse bundle member: {name!r}")
                seen.add(name)
                _check_member(tar, member, tree.entries.get(name) or manifest)
                end = tar.offset
            if seen != tree.entries.keys() | {_MANIFEST}:
                raise BundleImportError("Missing bundle archive entries")
            # tarfile may stop at the first zero block or silently at a bad header.
            decoded.seek(end)
            if total % _BLOCK or decoded.read(2 * _BLOCK) != bytes(2 * _BLOCK):
                raise BundleImportError("Invalid bundle tar terminator")
            while chunk := decoded.read(_CHUNK):
                if chunk != bytes(len(chunk)):
                    raise BundleImportError("Nonzero data after bundle tar terminator")
            yield tar


def _check_member(
    tar: tarfile.TarFile, member: tarfile.TarInfo, expected: BundleManifest | BundleEntry
) -> None:
    name = member.name
    regular = member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
    if isinstance(expected, BundleManifest):
        if regular and 0 <= member.size <= MAX_INNER_MANIFEST_BYTES:
            with _member_file(tar, member) as data:
                if _parse_manifest(data.read()) == expected:
                    return
    else:
        match expected.kind:
            case "file":
                executable = bool(member.mode & 0o111)
                if regular and member.size == expected.size and executable == expected.executable:
                    with _member_file(tar, member) as data:
                        if _file_digest(data) == expected.digest:
                            return
            case "symlink":
                if member.issym() and member.size == 0 and member.linkname == expected.link_target:
                    return
            case unreachable:
                assert_never(unreachable)
    raise BundleImportError(f"Archive member does not match manifest: {name!r}")


def _member_file(tar: tarfile.TarFile, member: tarfile.TarInfo) -> IO[bytes]:
    data = tar.extractfile(member)
    if data is None:
        raise BundleImportError(f"No file payload for {member.name!r}")
    return data


def _plain(path: Path, *, directory: bool = False) -> os.stat_result:
    info = store._plain_stat(path)
    kind_matches = (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode)
    if not kind_matches or (not directory and info.st_nlink != 1):
        raise BundleImportError(f"Unsafe bundle store node: {path}")
    return info


def _verify(root: Path, manifest: BundleManifest, tree: _Tree) -> None:
    _plain(root, directory=True)
    _plain(root / ".bundle", directory=True)
    for name in (store._SENTINEL_NAME, _MANIFEST):
        path = root / name
        _plain(path)
        with path.open("rb") as data:
            if _parse_manifest(data.read(MAX_INNER_MANIFEST_BYTES + 1)) != manifest:
                raise BundleImportError(f"Bundle store manifest mismatch: {path}")
    pending = ["tree"]
    seen: set[str] = set()
    while pending:
        directory = pending.pop()
        _plain(root / directory, directory=True)
        for child in (root / directory).iterdir():
            path = f"{directory}/{child.name}"
            if path in tree.directories:
                pending.append(path)
            elif (entry := tree.entries.get(path)) is not None:
                _verify_entry(child, entry, directory_link=path in tree.directory_links)
            else:
                raise BundleImportError(f"Unexpected bundle store entry: {path!r}")
            seen.add(path)
    if seen != (tree.directories | tree.entries.keys()) - {"tree"}:
        raise BundleImportError("Missing bundle store entries")
    archive = root / store._ARCHIVE_NAME
    _plain(archive)
    with _read_archive(archive, manifest, tree):
        return


def _verify_entry(path: Path, entry: BundleEntry, *, directory_link: bool) -> None:
    match entry.kind:
        case "file":
            info = _plain(path)
            mode_matches = sys.platform == "win32" or bool(info.st_mode & 0o111) == entry.executable
            if info.st_size == entry.size and mode_matches:
                with path.open("rb") as data:
                    if _file_digest(data) == entry.digest:
                        return
        case "symlink":
            # readlink inspects the node itself even when lstat could traverse a reparse point.
            if os.readlink(path).split(os.sep) != (entry.link_target or "").split("/"):
                raise BundleImportError(f"Bundle store link target mismatch: {path}")
            info = path.lstat()
            if sys.platform == "win32" and (
                info.st_reparse_tag != stat.IO_REPARSE_TAG_SYMLINK
                or bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY) != directory_link
            ):
                raise BundleImportError(f"Bundle store link type mismatch: {path}")
            if stat.S_ISLNK(info.st_mode):
                return
        case unreachable:
            assert_never(unreachable)
    raise BundleImportError(f"Bundle store entry mismatch: {path}")


@contextmanager
def _diagnose() -> Generator[None]:
    try:
        yield
    except (
        OSError,
        ValueError,
        tarfile.TarError,
        EOFError,
        zlib.error,
        RecursionError,
        OverflowError,
    ) as exc:
        raise BundleImportError(f"Cannot verify or publish RunBundle: {exc}") from exc


def import_bundle(layer: Path, config: OciConfig) -> Path:
    """Check a layer against a parsed OCI config, then publish or verify its CAS entry."""
    with _diagnose():
        manifest = config.manifest
        tree = _layout(manifest)
        with _read_archive(layer, manifest, tree) as tar:

            def stage(root: Path) -> None:
                for member in tar.getmembers():
                    if member.name in tree.entries and member.isreg():
                        path = root / member.name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with _member_file(tar, member) as source, path.open("xb") as target:
                            shutil.copyfileobj(source, target)
                        path.chmod(0o755 if tree.entries[member.name].executable else 0o644)
                for name, entry in tree.entries.items():
                    if (link := entry.link_target) is not None:
                        path = root / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        # Win32 keeps '/' verbatim in relative symlink targets.
                        path.symlink_to(
                            link.replace("/", os.sep),
                            target_is_directory=name in tree.directory_links,
                        )

            return store._publish_import(
                manifest, stage, lambda root: _verify(root, manifest, tree)
            )


def verify_bundle(digest: str) -> BundleManifest:
    """Verify the local CAS read-only, without a registry or the original OCI layer."""
    with _diagnose():
        root = store._checked_store_base() / store.bundle_store_key(digest)
        _plain(root, directory=True)
        sentinel = root / store._SENTINEL_NAME
        _plain(sentinel)
        with sentinel.open("rb") as data:
            manifest = _parse_manifest(data.read(MAX_INNER_MANIFEST_BYTES + 1))
        if manifest.bundle_digest != digest:
            raise BundleImportError("Bundle store digest mismatch")
        encode_oci_config(manifest)  # Recompute B and apply the same resource limits as online.
        _verify(root, manifest, _layout(manifest))
        return manifest
