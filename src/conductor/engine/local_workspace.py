"""Translate already-rendered directory fields into a live managed local tree."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from conductor.exceptions import ConfigurationError
from conductor.execution.types import LocalWorkspaceBinding


class LocalWorkspacePathError(ConfigurationError):
    """A path refusal with a machine-readable, path-free reason.

    Neither rendered values nor underlying OS exceptions are exposed: either
    may contain secret-derived paths before the caller's redaction boundary.
    """

    def __init__(self, reason: str) -> None:
        self.reason: str = reason
        super().__init__(
            f"Managed local directory refused ({reason}).",
            suggestion="Use a declared staged directory in the live managed workspace.",
        )


def _namespace(value: str) -> PurePosixPath:
    """Parse a tree-relative namespace without accepting native path aliases."""
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or ":" in value
        or "\0" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise LocalWorkspacePathError("invalid_namespace")
    return path


def _host_path(value: str, *, windows: bool) -> PurePosixPath | PureWindowsPath:
    """Normalize host spelling lexically, without touching the source tree."""
    if not value.strip() or "\0" in value:
        raise LocalWorkspacePathError("invalid_path")
    if windows:
        path = PureWindowsPath(value)
        if value.startswith(("\\\\?\\", "\\\\.\\")):
            raise LocalWorkspacePathError("invalid_windows_path")
        for part in path.parts[1:] if path.anchor else path.parts:
            if part not in (".", "..") and (
                part.endswith((" ", "."))
                or any(char in part for char in ':<>"|?*')
                or any(ord(char) < 32 for char in part)
                or re.fullmatch(
                    r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9\u00b9\u00b2\u00b3]",
                    part.partition(".")[0].rstrip(" "),
                    re.IGNORECASE,
                )
            ):
                raise LocalWorkspacePathError("invalid_windows_path")
        if (path.drive or path.root) and not path.is_absolute():
            raise LocalWorkspacePathError("ambiguous_windows_path")
        return PureWindowsPath(ntpath.normpath(value))
    return PurePosixPath(posixpath.normpath(value))


def _logical_directory(
    rendered: str | None,
    roots: tuple[tuple[str, str], ...],
    namespace: str,
    *,
    windows: bool,
) -> PurePosixPath:
    """Map native host components to a tree-relative directory, without I/O."""
    base = _namespace(namespace)
    if rendered is None:
        return base
    path = _host_path(rendered, windows=windows)
    if not path.is_absolute():
        parts: list[str] = []
        for part in path.parts:
            if part == "..":
                if not parts:
                    raise LocalWorkspacePathError("namespace_escape")
                _ = parts.pop()
            elif part != ".":
                parts.append(part)
        return base.joinpath(*parts)

    mappings: dict[PurePosixPath | PureWindowsPath, PurePosixPath] = {}
    for host, logical in roots:
        root = _host_path(host, windows=windows)
        target = _namespace(logical)
        if not root.is_absolute():
            raise LocalWorkspacePathError("invalid_source_root")
        if root in mappings and mappings[root] != target:
            raise LocalWorkspacePathError("source_root_collision")
        mappings[root] = target
    for root in sorted(mappings, key=lambda item: len(item.parts), reverse=True):
        if path.is_relative_to(root):
            return mappings[root].joinpath(*path.relative_to(root).parts)
    raise LocalWorkspacePathError("undeclared_absolute_path")


@dataclass(frozen=True, slots=True)
class LocalWorkspacePathMapper:
    """Map rendered cwd/settings fields without rendering or granting access.

    ``workflow_namespace`` is the current workflow's tree-relative directory,
    not necessarily the root workflow's ``BundleRef.root``. Source and agent
    path tables are translation provenance, never a dependency selection.
    Ordinary argv, environment and prompt strings must not pass through here.
    """

    binding: LocalWorkspaceBinding = field(repr=False)
    workflow_namespace: str | None = None

    def __call__(self, rendered: str | None) -> str:
        """Provide the ``Callable[[str | None], str]`` cwd integration seam."""
        return self.map_directory(rendered)

    def map_directory(self, rendered: str | None) -> str:
        """Map one already-rendered cwd or explicit settings directory.

        No settings tiers, filesystem grants or discovery are enabled here.
        Remaining template-looking text is literal path data, never evaluated.
        """
        logical = _logical_directory(
            rendered,
            self.binding.bundle.source_roots + self.binding.bundle.agent_paths,
            self.workflow_namespace
            if self.workflow_namespace is not None
            else self.binding.bundle.root,
            windows=os.name == "nt",
        )
        return self._live_directory(logical)

    def map_selected_directory(self, namespace: str) -> str:
        """Map an exact per-agent selected dependency namespace, not a host path.

        The caller supplies ``CollectedSkill.directory``, its ``owner_root``,
        or a selected plugin root from the frozen per-agent topology. All are
        relative to full ``tree/`` (without that prefix). The caller must retain
        the selection key and authored switches; union ``agent_paths`` and the
        content manifest are not authorization. No host rediscovery occurs.
        """
        return self._live_directory(_namespace(namespace))

    def _live_directory(self, logical: PurePosixPath) -> str:
        """Check the mutable tree on every call, including resolved containment."""
        if os.name == "nt":
            _ = _host_path(logical.as_posix(), windows=True)
        work = Path(self.binding.work_root)
        if not work.is_absolute():
            raise LocalWorkspacePathError("invalid_work_root")
        try:
            if work.is_symlink():
                raise LocalWorkspacePathError("work_root_symlink")
            root = work.resolve(strict=True)
            candidate = root.joinpath(*logical.parts).resolve(strict=True)
            if not candidate.is_relative_to(root):
                raise LocalWorkspacePathError("symlink_escape")
            if not stat.S_ISDIR(candidate.stat().st_mode):
                raise LocalWorkspacePathError("not_directory")
        except (OSError, ValueError, RuntimeError):
            raise LocalWorkspacePathError("unavailable_directory") from None
        return str(candidate)
