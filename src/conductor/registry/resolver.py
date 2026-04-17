"""Workflow reference resolution.

Parses user-supplied workflow references (e.g. ``qa-bot@team@1.2.3``) and
determines whether an argument is a local file path or a registry reference.

Resolution rules (in order):
1. If the argument exists as a file on disk, treat it as a local path.
2. If it looks like a file path (has path separators or YAML extension), treat
   it as a local path — even if the file doesn't exist yet.
3. Otherwise parse as a registry reference using ``name[@registry][@version]``
   syntax.
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
    version: str | None = None  # None means "latest"
    registry_entry: RegistryEntry | None = None


def resolve_ref(ref: str) -> ResolvedRef:
    """Resolve a workflow reference string to a :class:`ResolvedRef`.

    If *ref* is an existing file path or looks like a file path (contains path
    separators or has a ``.yaml``/``.yml`` extension), a **file** ref is
    returned.  Otherwise *ref* is parsed as a registry reference using
    ``name[@registry][@version]`` syntax.

    Args:
        ref: The raw reference string from the CLI.

    Returns:
        A :class:`ResolvedRef` describing the resolved target.

    Raises:
        RegistryError: If the reference requires a default registry but none is
            configured, or if the named registry does not exist.
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


def _parse_registry_ref(ref: str) -> ResolvedRef:
    """Parse *ref* as ``name[@registry][@version]`` and resolve against config.

    Raises:
        RegistryError: On missing default registry or unknown registry name.
    """
    parts = ref.split("@", maxsplit=2)

    workflow = parts[0]
    raw_registry: str | None = parts[1] if len(parts) >= 2 else None
    version: str | None = parts[2] if len(parts) >= 3 else None

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
        version=version,
        registry_entry=config.registries[registry_name],
    )
