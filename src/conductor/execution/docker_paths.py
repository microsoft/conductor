"""Translate collected host dependencies to a Docker workspace snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
from dataclasses import replace
from pathlib import Path
from typing import Any

from conductor.exceptions import ConfigurationError
from conductor.execution.types import AgentSpec, BundleRef

_REMEDY = (
    "Declare the path under workflow.bundle.additional_roots or include the skill/plugin "
    "in the workflow bundle before running the agent in Docker."
)


def _matches_file(source: Path, logical: str, entries: dict[str, str], store_path: str) -> bool:
    if not source.is_file() or logical not in entries:
        return False
    if source.is_symlink():
        staged = Path(store_path) / logical
        return staged.is_file() and source.read_bytes() == staged.read_bytes()
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    return digest == entries[logical]


def _map_skill(
    directory: str, bundle: BundleRef, manifest: dict[str, Any], entries: dict[str, str]
) -> str:
    source = Path(os.path.abspath(directory))
    paths = {
        os.path.normcase(os.path.abspath(host)): namespace for host, namespace in bundle.agent_paths
    }
    namespace = paths.get(os.path.normcase(str(source)))
    if namespace is not None and ".." not in namespace.split("/"):
        topology = "tree/" + namespace + "/SKILL.md"
        if topology in manifest.get("skills_topology", {}).values() and _matches_file(
            source / "SKILL.md", topology, entries, bundle.store_path
        ):
            if namespace.startswith("skills/"):
                return "/workspace/" + namespace
            if namespace.startswith("plugins/"):
                plugin_name = namespace.split("/")[1]
                plugin_manifest = manifest.get("plugins_topology", {}).get(plugin_name)
                if (
                    isinstance(plugin_manifest, str)
                    and plugin_manifest.startswith(f"tree/plugins/{plugin_name}/")
                    and ".." not in plugin_manifest.split("/")
                ):
                    relative = plugin_manifest.removeprefix(f"tree/plugins/{plugin_name}/")
                    plugin_root = next(
                        (
                            Path(host)
                            for host, mapped in bundle.agent_paths
                            if mapped == f"plugins/{plugin_name}"
                        ),
                        None,
                    )
                    if plugin_root is not None and _matches_file(
                        plugin_root / relative, plugin_manifest, entries, bundle.store_path
                    ):
                        return "/workspace/" + namespace
    raise ConfigurationError(
        f"Agent skill directory {directory!r} is not present in the staged bundle topology.",
        suggestion=_REMEDY,
    )


def _map_working_dir(value: str | None, bundle: BundleRef) -> str | None:
    if value is None:
        return None
    if not os.path.isabs(value):
        parts = Path(os.path.normpath(value)).parts
        logical = posixpath.normpath(posixpath.join(bundle.root, *parts))
        if logical != bundle.root and not logical.startswith(bundle.root + "/"):
            raise ConfigurationError(
                f"Agent working_dir {value!r} escapes the staged bundle", suggestion=_REMEDY
            )
        if (Path(bundle.store_path) / "tree" / logical).is_dir():
            return "/workspace/" + logical
        raise ConfigurationError(f"Agent working_dir {value!r} is not staged", suggestion=_REMEDY)
    source = Path(os.path.abspath(value))
    if value.startswith("/workspace/"):
        logical = posixpath.normpath(value.removeprefix("/workspace/"))
        if (
            logical != ".."
            and not logical.startswith("../")
            and (Path(bundle.store_path) / "tree" / logical).is_dir()
        ):
            return "/workspace/" + logical
        raise ConfigurationError(
            f"Agent working_dir {value!r} escapes the staged bundle", suggestion=_REMEDY
        )
    for host, namespace in sorted(bundle.source_roots, key=lambda item: -len(item[0])):
        root = Path(os.path.abspath(host))
        if source.is_relative_to(root):
            relative = source.relative_to(root)
            staged = Path(bundle.store_path) / "tree" / namespace / relative
            if staged.is_dir():
                return "/workspace/" + (Path(namespace) / relative).as_posix()
    raise ConfigurationError(
        f"Agent working_dir {value!r} is outside the staged Docker bundle.",
        suggestion=_REMEDY,
    )


def map_agent_paths(spec: AgentSpec, bundle: BundleRef | None) -> AgentSpec:
    """Map host agent paths onto their published Docker workspace locations.

    Args:
        spec: Agent invocation with host-side skill and working directories.
        bundle: Published bundle containing the staged files and path mapping.

    Returns:
        A copy of the invocation with staged skill and working directories.

    Raises:
        ConfigurationError: If the bundle topology is unreadable or a required
            host component is missing from the published bundle.
    """
    if bundle is None:
        raise ConfigurationError("Docker agent realm requires a published run bundle")
    try:
        manifest = json.loads((Path(bundle.store_path) / "bundle.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not all(
            isinstance(manifest.get(name), dict) for name in ("skills_topology", "plugins_topology")
        ):
            raise ValueError("invalid bundle topology")
        entries = {entry["logical_path"]: entry["digest"] for entry in manifest["entries"]}
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise ConfigurationError("Docker agent bundle has no readable topology") from exc
    return replace(
        spec,
        skill_directories=tuple(
            _map_skill(path, bundle, manifest, entries) for path in spec.skill_directories
        ),
        working_dir=_map_working_dir(spec.working_dir, bundle),
    )
