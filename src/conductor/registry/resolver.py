"""Workflow reference resolution.

Parses user-supplied workflow references (e.g. ``qa-bot@team#v1.2.3``) and
determines whether an argument is a local file path, a configured registry
reference, or an ad-hoc GitHub registry reference.

Resolution rules (in order):
1. If the argument exists as a file on disk, treat it as a local path.
2. If it looks like a file path (has path separators or YAML extension), treat
   it as a local path — even if the file doesn't exist yet.
3. Otherwise parse as a registry reference using ``<workflow>[@<registry>][#<ref>]``
   syntax. The ``<registry>`` segment can be either:
   - A configured registry name (looked up in ``~/.conductor/registries.toml``), or
   - An ``owner/repo`` literal (contains ``/``) that is fetched ad-hoc from
     GitHub without requiring registry configuration.
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

    kind: Literal["file", "registry", "adhoc"]

    # For file refs
    path: Path | None = None

    # For registry refs (and adhoc refs)
    workflow: str | None = None
    registry_name: str | None = None
    ref: str | None = None  # Git tag / branch / SHA. None means "latest"
    registry_entry: RegistryEntry | None = None

    # For adhoc refs only — owner and repo parsed from the right side of '@'
    adhoc_owner: str | None = None
    adhoc_repo: str | None = None


def resolve_ref(ref: str) -> ResolvedRef:
    """Resolve a workflow reference string to a :class:`ResolvedRef`.

    If *ref* is an existing file path or looks like a file path (contains path
    separators or has a ``.yaml``/``.yml`` extension), a **file** ref is
    returned.  Otherwise *ref* is parsed as a registry reference using
    ``<workflow>[@<registry>][#<ref>]`` syntax, where ``@`` introduces an
    optional registry name and ``#`` introduces an optional git ref (tag,
    branch, or commit SHA). An empty registry segment (``name@#ref``) selects
    the configured default registry.

    The ``<registry>`` segment supports two forms:

    - A configured registry name (e.g. ``qa-bot@team#v1.0.0``) — looked up in
      ``~/.conductor/registries.toml``.
    - An ``owner/repo`` literal (e.g. ``qa-bot@acme/workflows#v1.0.0``) —
      detected by the presence of ``/``. Treated as an **ad-hoc** GitHub
      reference, fetched and cached without requiring registry
      pre-configuration. Returns ``kind="adhoc"``.

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

    Refs containing ``@`` are always treated as registry references (named
    or ad-hoc), regardless of whether the rest looks like a path. This
    allows the ad-hoc form ``workflow@owner/repo#ref`` (which contains ``/``
    in the registry slot) to be parsed correctly rather than being
    misclassified as a file path.
    """
    # Registry refs (named or ad-hoc) always contain '@'. Yield to the
    # registry parser so 'workflow@owner/repo#ref' is not treated as a file.
    if "@" in ref:
        return False

    if "/" in ref or "\\" in ref:
        return True

    if Path(ref).suffix.lower() in _YAML_EXTENSIONS:
        return True

    path = Path(ref)
    return path.exists() and path.is_file()


def _parse_registry_ref(raw: str) -> ResolvedRef:
    """Parse *raw* as ``<workflow>[@<registry>][#<ref>]`` and resolve.

    The ``<registry>`` segment can be:
    - A configured registry name → looked up in ``~/.conductor/registries.toml``
    - An ``owner/repo`` literal (contains ``/``) → ad-hoc GitHub reference,
      no configuration required

    Raises:
        RegistryError: On malformed syntax, missing default registry, or
            unknown registry name (named-registry form only).
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

    # Ad-hoc owner/repo form — detected by '/' in the registry segment.
    if raw_registry is not None and "/" in raw_registry:
        return _parse_adhoc_ref(workflow, raw_registry, git_ref)

    # Named-registry form (existing behavior).
    return _parse_named_registry_ref(workflow, raw_registry, git_ref)


def _parse_adhoc_ref(
    workflow: str,
    raw_registry: str,
    git_ref: str | None,
) -> ResolvedRef:
    """Parse an ad-hoc ``<workflow>@<owner>/<repo>[#<ref>]`` reference.

    No registry configuration lookup — the ``owner/repo`` literal is used
    directly to fetch from GitHub.

    Raises:
        RegistryError: If ``owner/repo`` is malformed (e.g. empty owner or
            repo, or contains additional path segments).
    """
    parts = raw_registry.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise RegistryError(
            f"Invalid ad-hoc registry source '{raw_registry}'. "
            "Expected '<owner>/<repo>' with exactly one '/'.",
            suggestion=("Use 'workflow@owner/repo[#ref]' (e.g. analysis@acme/workflows#v1.0.0)."),
        )

    owner, repo = parts
    return ResolvedRef(
        kind="adhoc",
        workflow=workflow,
        registry_name=raw_registry,
        ref=git_ref,
        adhoc_owner=owner,
        adhoc_repo=repo,
    )


def _parse_named_registry_ref(
    workflow: str,
    raw_registry: str | None,
    git_ref: str | None,
) -> ResolvedRef:
    """Parse a named ``<workflow>[@<registry>][#<ref>]`` reference.

    Looks up the registry by name in the user's configuration.

    Raises:
        RegistryError: If the registry is missing (and no default is
            configured), or the named registry doesn't exist in config.
    """
    config = load_config()

    # Determine the registry name: empty string or None → use default.
    if raw_registry is None or raw_registry == "":
        if config.default is None:
            raise RegistryError(
                "No default registry configured",
                suggestion=(
                    "Run 'conductor registry add <name> <source> --default' to "
                    "configure a default registry, or use the ad-hoc form "
                    "'workflow@owner/repo[#ref]' to reference a GitHub repo "
                    "directly."
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
                "Run 'conductor registry list' to see all registries, "
                "or use the ad-hoc form 'workflow@owner/repo[#ref]' "
                "to reference a GitHub repo directly."
            ),
        )

    return ResolvedRef(
        kind="registry",
        workflow=workflow,
        registry_name=registry_name,
        ref=git_ref,
        registry_entry=config.registries[registry_name],
    )
