"""Deterministic tar.gz writer for run bundle trees.

:func:`write_bundle_archive` serializes a staged bundle tree into a
byte-for-byte reproducible ``.tar.gz``: entries are added in POSIX-path sort
order, every ``TarInfo`` carries fixed ownership (``uid=gid=0``,
``uname=gname=""``) and ``mtime=0``, and the gzip stream pins ``mtime=0``.
Two builds of the same staged tree therefore produce identical bytes — the
guardrail test pins exactly that.

The archive contains only regular files and symlinks; directory entries are
omitted (a tarball of the file set is sufficient and keeps the member list
canonical). Regular-file modes come from the caller's manifest-derived
executable path set rather than host filesystem mode bits. Symlink members
are recorded as ``tarfile.SYMTYPE`` with the link target as ``linkname`` and
mode ``0o777`` — POSIX-first semantics: on a platform where a checkout
materialized a symlink as a regular file, it is archived as a regular file,
exactly as the staged tree presents it.

The bundle manifest travels inside the archive as ``.bundle/manifest.json``.
This function does **not** add it: the caller (``bundle.store.publish_bundle``)
stages a copy of the ``bundle.json`` content at ``.bundle/manifest.json``
inside the staged tree *before* calling here, so the archived copy is
byte-identical to the readiness sentinel written to the store afterwards.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import stat
import tarfile
from collections.abc import Collection
from pathlib import Path

_READ_CHUNK_SIZE = 64 * 1024


def write_bundle_archive(
    staged_tree: Path,
    out: Path,
    *,
    executable_paths: Collection[str],
) -> str:
    """Write the staged bundle tree at ``staged_tree`` as a tar.gz at ``out``.

    The archive is deterministic: members are ordered by their POSIX arcname,
    ownership and mtimes are pinned, and the gzip header carries no filename
    or timestamp. Rebuilding an identical staged tree yields identical bytes
    (and therefore an identical returned digest).

    Args:
        staged_tree: Root of the staged bundle tree. Only regular files and
            symlinks are archived; directory entries are omitted. The caller
            stages ``.bundle/manifest.json`` inside this tree beforehand when
            the archive should carry the manifest.
        out: Destination file. Written atomically-enough for the store's
            purposes: the file is created only by this call, and the store
            renames it into the staged tree afterwards.
        executable_paths: POSIX relative paths that receive mode ``0o755``.
            All other regular files receive ``0o644``. The store supplies
            this from the bundle manifest rather than host filesystem modes.

    Returns:
        ``"sha256:<hex>"`` — the digest of the archive bytes as written.
    """
    entries = sorted(
        (path for path in staged_tree.rglob("*")),
        key=lambda path: path.relative_to(staged_tree).as_posix(),
    )
    # Exact pattern required for reproducibility: an anonymous GzipFile
    # (filename="") over a raw file object, mtime=0 on both layers.
    with (
        open(out, "wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in entries:
            _add_entry(tar, staged_tree, path, executable_paths)
    digest = hashlib.sha256()
    with open(out, "rb") as written:
        for chunk in iter(lambda: written.read(_READ_CHUNK_SIZE), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _add_entry(
    tar: tarfile.TarFile,
    staged_tree: Path,
    path: Path,
    executable_paths: Collection[str],
) -> None:
    """Add one staged path to the archive, files as data and symlinks as links."""
    arcname = path.relative_to(staged_tree).as_posix()
    info = tarfile.TarInfo(arcname)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        info.type = tarfile.SYMTYPE
        info.linkname = os.readlink(path)
        info.mode = 0o777
        info.size = 0
        tar.addfile(info)
    elif stat.S_ISREG(st.st_mode):
        info.mode = 0o755 if arcname in executable_paths else 0o644
        info.size = st.st_size
        with path.open("rb") as source:
            tar.addfile(info, source)
    # Directories (and anything else) are omitted: the archive holds the
    # file/symlink set only.
