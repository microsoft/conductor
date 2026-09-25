"""Content-addressed store for run bundles.

A published bundle lives at ``<base>/sha256-<hex>/`` where
``<base>`` is ``$CONDUCTOR_HOME/cache/bundles/`` (or the ``~/.conductor``
fallback). The filesystem key replaces the colon in the manifest's full
``sha256:<hex>`` content digest with a hyphen so it is valid on Windows. The
directory holds:

* the staged entry tree at each entry's full store-relative
  ``logical_path`` (which already carries the ``tree/main/`` prefix — no
  additional prefix is added at write time),
* ``bundle.tar.gz`` — the deterministic archive of the entry tree plus a
  ``.bundle/manifest.json`` copy of the manifest,
* ``bundle.json`` — the serialized manifest, written **last** as the
  readiness sentinel: a reader that sees this file can trust the whole
  directory (mirroring the plugin-fetch sentinel convention).

Publishing is idempotent and self-healing. A cross-process lock serializes
validation, repair, and publication for each digest. If the final directory
already exists it is reused when valid — ``bundle.json`` parses and its
``bundle_digest`` equals the requested manifest digest (a parse-only check,
no full re-hash, per the ``plugins.fetch.is_cached`` precedent). An invalid
directory is reported through the warning sink, removed, and rebuilt.

The ephemeral :class:`~conductor.bundle.model.BundleDescriptor` is
deliberately **not** stored and not an input here (Oracle R1-O1):
``publish_bundle`` takes only the content manifest plus the entry payloads,
so provenance never leaks into the content-addressed store.
"""

from __future__ import annotations

import contextlib
import errno
import itertools
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from conductor.bundle.archive import write_bundle_archive
from conductor.bundle.model import BundleEntry, BundleManifest, serialize_manifest

logger = logging.getLogger(__name__)

_BUNDLE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
"""The manifest keeps the full content digest; filesystem keys replace ``:``."""

_ARCHIVE_NAME = "bundle.tar.gz"
_STORE_MANIFEST_COPY = Path(".bundle") / "manifest.json"
"""In-archive (and staged) copy of the manifest, byte-identical to ``bundle.json``."""

_SENTINEL_NAME = "bundle.json"
"""Readiness sentinel — written last, so its presence certifies the tree."""

_LOST_RACE_ERRNOS = (errno.ENOTEMPTY, errno.EEXIST)

_quarantine_counter = itertools.count()
"""Uniquifier for quarantine names — one publisher, many self-heals."""


def bundle_store_base() -> Path:
    """Return the base directory bundle trees are stored under.

    Uses ``$CONDUCTOR_HOME/cache/bundles/`` or ``~/.conductor/cache/bundles/``,
    following the ``plugins.fetch.get_plugin_cache_base`` idiom deliberately
    inline (not extracted into a shared helper — two callsites stay two).
    """
    home = os.environ.get("CONDUCTOR_HOME")
    base = Path(home) if home else Path.home() / ".conductor"
    return base / "cache" / "bundles"


def bundle_store_key(digest: str) -> str:
    """Return the portable filesystem key for a full bundle digest."""
    if not _BUNDLE_DIGEST_RE.fullmatch(digest):
        raise ValueError(f"bundle_digest must be a full 'sha256:<64 hex>' digest, got {digest!r}")
    return f"sha256-{digest.removeprefix('sha256:')}"


def bundle_store_path(digest: str) -> Path:
    """Return the store directory for a full bundle digest."""
    return bundle_store_base() / bundle_store_key(digest)


def _manifest_bytes(manifest: BundleManifest) -> bytes:
    serialized = serialize_manifest(manifest).replace("\r\n", "\n").replace("\r", "\n")
    return serialized.encode("utf-8")


def publish_bundle(
    manifest: BundleManifest,
    files: Mapping[str, bytes],
    links: Mapping[str, str],
    *,
    on_warning: Callable[[str], None] | None = None,
) -> Path:
    """Publish a bundle into the store and return its final directory.

    The directory is named after the portable form of ``manifest.bundle_digest`` and is
    considered complete once ``bundle.json`` exists — it is always written
    last, after the entry tree, the archive, and the staged
    ``.bundle/manifest.json`` copy inside it.

    Args:
        manifest: The content manifest. Its ``bundle_digest`` names the
            store directory; its entries provide the executable-bit flags
            (a payload path missing from the manifest, or of the wrong
            ``kind``, is a caller bug and raises ``ValueError``).
        files: Entry payloads by full store-relative ``logical_path``
            (already carrying the ``tree/main/`` prefix). Written with mode
            ``0o755`` when the manifest entry is executable, else ``0o644``.
        links: Symlink entries by ``logical_path`` → normalized link
            target, materialized with POSIX-first ``os.symlink`` semantics
            (no fallback: on platforms where a checkout materialized a
            symlink as a regular file, it arrives here as a file).
        on_warning: Optional sink for non-fatal diagnostics (an invalid
            pre-existing directory being rebuilt). Defaults to the module
            logger's ``warning``.

    Returns:
        The final store directory — either the pre-existing valid one or
        the freshly published one.

    Raises:
        ValueError: If ``manifest.bundle_digest`` is not a full
            ``sha256:<64 hex>`` digest, or a payload path is not declared
            in the manifest with the matching kind.
    """
    digest = manifest.bundle_digest
    key = bundle_store_key(digest)
    warn = on_warning if on_warning is not None else logger.warning

    base = bundle_store_base()
    base.mkdir(parents=True, exist_ok=True)
    final = base / key

    with _digest_lock(base / f".{key}.lock"):
        if final.is_dir():
            if _is_valid_store_dir(final, digest):
                return final
            warn(
                f"Invalid bundle store directory {final} "
                "(unparseable or digest-mismatched bundle.json); removing and rebuilding."
            )
            _quarantine_and_remove(final, key, digest)

        entries_by_path = {entry.logical_path: entry for entry in manifest.entries}
        executable_paths = {
            entry.logical_path
            for entry in manifest.entries
            if entry.kind == "file" and entry.executable
        }
        staging = Path(tempfile.mkdtemp(dir=base))
        # The archive is written next to the staging dir, never inside it: the
        # archive walk must not see the archive being written.
        archive_tmp = base / f".{staging.name}.tar.gz"
        try:
            _stage_tree(staging, manifest, files, links, entries_by_path)
            _ = write_bundle_archive(staging, archive_tmp, executable_paths=executable_paths)
            os.replace(archive_tmp, staging / _ARCHIVE_NAME)
            # Sentinel last: only a directory whose bundle.json carries the
            # requested digest is ever handed to a caller. Writing bytes avoids
            # platform newline translation and matches the archived copy.
            (staging / _SENTINEL_NAME).write_bytes(_manifest_bytes(manifest))
            _publish(staging, final, digest)
        finally:
            _ = shutil.rmtree(staging, ignore_errors=True)
            with contextlib.suppress(OSError):
                archive_tmp.unlink()
    return final


@contextmanager
def _digest_lock(path: Path) -> Generator[None]:
    """Hold an exclusive cross-process lock for one bundle digest."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        if sys.platform == "win32":
            import msvcrt

            with contextlib.suppress(OSError):
                os.write(fd, b"\0")
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _quarantine_and_remove(final: Path, key: str, digest: str) -> None:
    """Move an invalid final directory aside, then remove the quarantine.

    Rename-aside instead of check-and-delete: another publisher may be
    mid-publish into ``final`` (it always contains *some* directory between
    its rename and the next), and rmtree-ing that directory in place would
    delete a healthy, nearly-complete tree. The invalid content is renamed
    to a unique quarantine name inside the base dir — same filesystem, so
    the rename is atomic — and removed there. A rename failure because
    ``final`` vanished or became valid meanwhile is re-validated and
    tolerated; anything else propagates.
    """
    base = final.parent
    quarantine = base / f".{key}.quarantine-{os.getpid()}-{next(_quarantine_counter)}"
    try:
        os.rename(final, quarantine)
    except FileNotFoundError:
        return  # Another publisher removed it; nothing left to quarantine.
    except OSError:
        if final.is_dir() and _is_valid_store_dir(final, digest):
            return  # Became valid concurrently — reuse it.
        raise
    _ = shutil.rmtree(quarantine, ignore_errors=True)


def _contained_target(staging_root: Path, logical_path: str) -> Path:
    """Resolve ``staging_root / logical_path`` and refuse to leave the tree.

    Defense in depth behind :class:`~conductor.bundle.model.BundleEntry`'s
    validator: a manifest built by hand (``model_construct``) or produced by
    a future schema change could carry a ``..`` or absolute logical path,
    and a public store API must never write outside its own temp tree —
    ``target.parent.mkdir`` for such a path would happily create directories
    outside the staging dir before any write failed.
    """
    target = staging_root / logical_path
    resolved = target.resolve(strict=False)
    if resolved != staging_root and staging_root not in resolved.parents:
        raise ValueError(
            f"logical path {logical_path!r} resolves outside the staging tree ({resolved})"
        )
    return target


def _stage_tree(
    staging: Path,
    manifest: BundleManifest,
    files: Mapping[str, bytes],
    links: Mapping[str, str],
    entries_by_path: Mapping[str, BundleEntry],
) -> None:
    """Materialize the entry tree plus the in-archive manifest copy in staging."""
    staging_root = staging.resolve()
    for logical_path, data in files.items():
        entry = entries_by_path.get(logical_path)
        if entry is None or entry.kind != "file":
            raise ValueError(
                f"payload path {logical_path!r} is not a declared file entry of the bundle manifest"
            )
        target = _contained_target(staging_root, logical_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o755 if entry.executable else 0o644)
    for logical_path, link_target in links.items():
        entry = entries_by_path.get(logical_path)
        if entry is None or entry.kind != "symlink":
            raise ValueError(
                f"link path {logical_path!r} is not a declared symlink entry of the bundle manifest"
            )
        target = _contained_target(staging_root, logical_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = os.symlink(link_target, target)
    manifest_copy = staging / _STORE_MANIFEST_COPY
    manifest_copy.parent.mkdir(parents=True, exist_ok=True)
    # Byte-identical to the bundle.json sentinel written after the archive,
    # so the archived .bundle/manifest.json matches the stored manifest.
    manifest_copy.write_bytes(_manifest_bytes(manifest))


def _is_valid_store_dir(final: Path, digest: str) -> bool:
    """Whether an existing store directory is complete and content-matching.

    Parse-only check, deliberately without a full re-hash of the tree (the
    ``plugins.fetch.is_cached`` precedent): the directory name *is* the
    content digest, so validating ``bundle.json``'s digest field against it
    is sufficient. Anything unreadable or unparseable reads as invalid.
    """
    try:
        payload: object = json.loads((final / _SENTINEL_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    stored_digest = payload.get("bundle_digest")
    return isinstance(stored_digest, str) and stored_digest == digest


def _publish(staging: Path, final: Path, digest: str) -> None:
    """Move the completed staging tree into place, tolerating a lost race.

    Two publishers of the same digest may finish staging concurrently. The
    loser discards its copy rather than overwriting: the digest names the
    content, so the winner's tree is already the right one — provided it
    validates, which is checked here (the sentinel was written before the
    rename, so a present ``final`` directory is expected to be complete).
    Only the lost-race errnos are swallowed; EACCES/ENOSPC propagate, since
    those must not be reported as a successful publish. On Windows,
    replacing an existing directory raises WinError 5 (surfaced as EACCES)
    rather than ENOTEMPTY, so it is named explicitly.
    """
    try:
        os.replace(staging, final)
    except OSError as exc:
        lost_race = exc.errno in _LOST_RACE_ERRNOS or (
            sys.platform == "win32" and getattr(exc, "winerror", None) == 5
        )
        if lost_race and final.is_dir() and _is_valid_store_dir(final, digest):
            return
        raise
