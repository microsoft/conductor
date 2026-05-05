"""Workflow reference resolution.

Parses user-supplied workflow references (e.g. ``qa-bot@team#v1.2.3``) and
determines whether an argument is a local file path or a registry reference.

Resolution rules (in order):
1. If the argument exists as a file on disk, treat it as a local path.
2. If it looks like a file path (has path separators or YAML extension), treat
   it as a local path — even if the file doesn't exist yet.
3. Otherwise parse as a registry reference using ``<workflow>[@<registry>][#<ref>]``
   syntax, where ``<ref>`` is a git tag, branch, or commit SHA.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from conductor.registry.config import RegistryEntry, load_config
from conductor.registry.errors import RegistryError

_YAML_EXTENSIONS = {".yaml", ".yml"}


@dataclass(frozen=True)
class ResolvedRef:
    """A resolved workflow reference."""

    kind: Literal["file", "registry"]

    # For file refs
    path: Path | None = None

    # For registry refs
    workflow: str | None = None
    registry_name: str | None = None
    ref: str | None = None  # Git tag / branch / SHA. None means "latest"
    registry_entry: RegistryEntry | None = None


def resolve_ref(ref: str) -> ResolvedRef:
    """Resolve a workflow reference string to a :class:`ResolvedRef`.

    If *ref* is an existing file path or looks like a file path (contains path
    separators or has a ``.yaml``/``.yml`` extension), a **file** ref is
    returned.  Otherwise *ref* is parsed as a registry reference using
    ``<workflow>[@<registry>][#<ref>]`` syntax, where ``@`` introduces an
    optional registry name and ``#`` introduces an optional git ref (tag,
    branch, or commit SHA). An empty registry segment (``name@#ref``) selects
    the configured default registry.

    Args:
        ref: The raw reference string from the CLI.

    Returns:
        A :class:`ResolvedRef` describing the resolved target.

    Raises:
        RegistryError: If the reference is malformed, requires a default
            registry but none is configured, or names an unknown registry.
    """
    if _looks_like_file_path(ref):
        return ResolvedRef(kind="file", path=Path(ref))

    return _parse_registry_ref(ref)


def _looks_like_file_path(ref: str) -> bool:
    """Heuristic: is *ref* a file path rather than a registry reference?

    Returns ``True`` when any of the following hold:

    * The ref exists as a file on disk.
    * The ref contains a path separator (``/`` or ``\\``).
    * The ref ends with ``.yaml`` or ``.yml``.
    """
    if "/" in ref or "\\" in ref:
        return True

    if Path(ref).suffix.lower() in _YAML_EXTENSIONS:
        return True

    path = Path(ref)
    return path.exists() and path.is_file()


def _parse_registry_ref(raw: str) -> ResolvedRef:
    """Parse *raw* as ``<workflow>[@<registry>][#<ref>]`` and resolve against config.

    Raises:
        RegistryError: On malformed syntax, missing default registry, or
            unknown registry name.
    """
    # Split on '#' first — the right side (if any) is the git ref.
    hash_parts = raw.split("#")
    if len(hash_parts) > 2:
        raise RegistryError(
            "Workflow ref may contain at most one '#'",
            suggestion="Use '<workflow>[@<registry>][#<ref>]' (e.g. qa-bot@team#v1.0.0).",
        )

    left = hash_parts[0]
    git_ref: str | None
    if len(hash_parts) == 2:
        git_ref = hash_parts[1]
        if git_ref == "":
            raise RegistryError(
                "Ref cannot be empty after '#'",
                suggestion="Provide a tag, branch, or commit SHA after '#' (e.g. qa-bot#v1.0.0).",
            )
    else:
        git_ref = None

    # Split the left side on '@' — at most one '@' is allowed.
    at_parts = left.split("@")
    if len(at_parts) > 2:
        raise RegistryError(
            "Workflow ref may contain at most one '@' "
            "(use '#' for refs, e.g. name@registry#v1.0.0)",
            suggestion="Use '<workflow>[@<registry>][#<ref>]' syntax.",
        )

    workflow = at_parts[0]
    if workflow == "":
        raise RegistryError(
            "Workflow name is required",
            suggestion="Provide a workflow name (e.g. qa-bot, qa-bot@team#v1.0.0).",
        )

    raw_registry: str | None = at_parts[1] if len(at_parts) == 2 else None

    config = load_config()

    # Determine the registry name: empty string or None → use default.
    if raw_registry is None or raw_registry == "":
        if config.default is None:
            raise RegistryError(
                "No default registry configured",
                suggestion=(
                    "Run 'conductor registry add <name> <source> --default' to "
                    "configure a default registry."
                ),
            )
        registry_name = config.default
    else:
        registry_name = raw_registry

    if registry_name not in config.registries:
        available = ", ".join(sorted(config.registries)) or "(none)"
        raise RegistryError(
            f"Registry '{registry_name}' not found",
            suggestion=(
                f"Available registries: {available}. "
                "Run 'conductor registry list' to see all registries."
            ),
        )

    return ResolvedRef(
        kind="registry",
        workflow=workflow,
        registry_name=registry_name,
        ref=git_ref,
        registry_entry=config.registries[registry_name],
    )
