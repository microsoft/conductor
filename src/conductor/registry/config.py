"""Registry configuration management.

Handles loading, saving, and modifying the registries config file
stored at ``~/.conductor/registries.toml`` (or under ``$CONDUCTOR_HOME``).
"""

from __future__ import annotations

import os
import re
import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, model_validator

from conductor.registry.errors import RegistryError

_GITHUB_SOURCE_RE = re.compile(r"^[a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+$")

CONFIG_FILENAME = "registries.toml"


class RegistryType(StrEnum):
    """Supported registry backend types."""

    github = "github"
    path = "path"


class RegistryEntry(BaseModel):
    """A single registry definition."""

    type: RegistryType
    """Backend type — ``github`` or ``path``."""

    source: str
    """Location of workflows. ``owner/repo`` for github, filesystem path for path."""


class RegistriesConfig(BaseModel):
    """Top-level registries configuration."""

    default: str | None = None
    """Name of the default registry, must be a key in ``registries``."""

    registries: dict[str, RegistryEntry] = {}
    """Mapping of registry name to its entry."""

    @model_validator(mode="after")
    def _validate_default(self) -> RegistriesConfig:
        """Ensure ``default`` references an existing registry."""
        if self.default is not None and self.default not in self.registries:
            raise ValueError(f"default registry '{self.default}' is not defined in registries")
        return self


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_config_path() -> Path:
    """Return the path to the registries config file.

    Respects the ``CONDUCTOR_HOME`` environment variable. Falls back to
    ``~/.conductor``.
    """
    home = os.environ.get("CONDUCTOR_HOME")
    base = Path(home) if home else Path.home() / ".conductor"
    return base / CONFIG_FILENAME


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_config() -> RegistriesConfig:
    """Load the registries configuration from disk.

    Returns:
        Parsed ``RegistriesConfig``. An empty config is returned when the
        file does not exist.

    Raises:
        RegistryError: If the file exists but contains malformed TOML or
            invalid data.
    """
    path = get_config_path()
    if not path.exists():
        return RegistriesConfig()

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(
            f"Failed to parse {path}: {exc}",
            suggestion="Check the TOML syntax in your registries config file.",
            file_path=str(path),
        ) from exc

    try:
        return RegistriesConfig.model_validate(raw)
    except Exception as exc:
        raise RegistryError(
            f"Invalid registry config in {path}: {exc}",
            suggestion="Verify the structure of your registries config file.",
            file_path=str(path),
        ) from exc


def save_config(config: RegistriesConfig) -> None:
    """Atomically write the registries config to disk.

    Creates parent directories as needed. Writes to a temporary file in the
    same directory then renames to ensure atomicity.

    Args:
        config: The configuration to persist.
    """
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    content = _format_toml(config)

    tmp_path = path.with_suffix(".toml.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        # Clean up the temp file on failure
        tmp_path.unlink(missing_ok=True)
        raise


def _format_toml(config: RegistriesConfig) -> str:
    """Format a ``RegistriesConfig`` as TOML text."""
    lines: list[str] = []

    if config.default is not None:
        lines.append(f'default = "{config.default}"')

    for name, entry in config.registries.items():
        lines.append("")
        lines.append(f"[registries.{name}]")
        lines.append(f'type = "{entry.type}"')
        lines.append(f'source = "{entry.source}"')

    # Ensure trailing newline
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def _infer_type(source: str) -> RegistryType:
    """Infer registry type from the source string."""
    if _GITHUB_SOURCE_RE.match(source):
        return RegistryType.github
    return RegistryType.path


def add_registry(
    name: str,
    source: str,
    registry_type: RegistryType | None = None,
    set_default: bool = False,
) -> RegistriesConfig:
    """Add a new registry entry and persist the config.

    Args:
        name: Unique name for the registry.
        source: Location — ``owner/repo`` for github, filesystem path for path.
        registry_type: Explicit type; auto-inferred from *source* when ``None``.
        set_default: If ``True``, make this the default registry.

    Returns:
        The updated ``RegistriesConfig``.

    Raises:
        RegistryError: If a registry with *name* already exists.
    """
    config = load_config()

    if name in config.registries:
        raise RegistryError(
            f"Registry '{name}' already exists",
            suggestion=f"Use a different name or remove '{name}' first.",
        )

    resolved_type = registry_type if registry_type is not None else _infer_type(source)

    config.registries[name] = RegistryEntry(type=resolved_type, source=source)
    if set_default:
        config.default = name

    save_config(config)
    return config


def remove_registry(name: str) -> RegistriesConfig:
    """Remove a registry entry and persist the config.

    If the removed registry was the default, the default is cleared.

    Args:
        name: Name of the registry to remove.

    Returns:
        The updated ``RegistriesConfig``.

    Raises:
        RegistryError: If no registry with *name* exists.
    """
    config = load_config()

    if name not in config.registries:
        raise RegistryError(
            f"Registry '{name}' not found",
            suggestion="Run 'conductor registry list' to see available registries.",
        )

    del config.registries[name]
    if config.default == name:
        config.default = None

    save_config(config)
    return config


def get_registry(name: str) -> RegistryEntry:
    """Look up a single registry by name.

    Args:
        name: Name of the registry.

    Returns:
        The matching ``RegistryEntry``.

    Raises:
        RegistryError: If no registry with *name* exists.
    """
    config = load_config()

    if name not in config.registries:
        raise RegistryError(
            f"Registry '{name}' not found",
            suggestion="Run 'conductor registry list' to see available registries.",
        )

    return config.registries[name]
