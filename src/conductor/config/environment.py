"""Execution environment documents: loading, discovery, and resolution.

An *environment document* is a small YAML file naming the execution
profiles a workflow may select from (``execution.profile`` on executable
steps, ``workflow.defaults.execution``, or the document's own ``default``)
and the runner backend each profile maps to. Two levels exist:

* **project** — ``.conductor/environments/<name>.yaml`` at any ancestor of
  the workflow file, up to the repository root (the first ``.git`` marker).
* **user** — ``$CONDUCTOR_HOME/environments/<name>.yaml``.

Resolution is **whole-document shadowing with first match wins: there is
no merge and no inheritance between documents.** A project document named
``prod`` completely replaces a user-level document of the same name — a
profile key present only in the shadowed document is gone, not merged in.
This is the deliberate design: a partially-merged ambient set is exactly
the kind of machine-dependent state environment documents exist to make
explicit, and shadowing keeps every resolved document byte-accountable to
one file on disk (plus one digest).

Documents are loaded as **plain YAML with no ``${VAR}`` expansion**.
Secrets are expected to arrive as typed references in a later step, and
silently expanding machine-dependent environment variables inside an
environment document would be an anti-hermetic vector — the same document
would resolve to different backends on different machines with nothing in
the file to say why.

Importing this module performs no I/O: every filesystem access happens
inside an explicit function call.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from conductor.digest import canonical_json_digest
from conductor.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

WarningSink = Callable[[str], None]
"""Sink for non-fatal diagnostics, mirroring ``skills.registry.WarningSink``."""

# Execution backends compiled into this build of Conductor. Step 2 ships the
# local subprocess backend only; the registry is a module constant so later
# backends widen it in one place.
AVAILABLE_BACKEND_NAMES = frozenset({"local"})

# Environment names map directly to ``<name>.yaml``, so the charset excludes
# path separators and dots; anything outside it must be written as a path.
_ENVIRONMENT_NAME_PATTERN = re.compile(r"\A[A-Za-z0-9_-]+\Z")

# Project-level environment documents live here, relative to each walked
# ancestor of the workflow file.
_PROJECT_ENVIRONMENTS_ROOT = Path(".conductor") / "environments"

# Marker identifying a repository root, where the project walk stops. Both a
# directory (a normal clone) and a file (a linked worktree's ".git" pointer)
# count — the test is plain ``.exists()``, as in ``skills.discovery``.
_REPO_MARKER = ".git"

# Plain-YAML reader. Safe typ so an environment document can never construct
# arbitrary Python objects, and no !file tag support: documents are plain
# data. Module-level because constructing it does no I/O.
_yaml = YAML(typ="safe")


class ProfileDefinition(BaseModel):
    """One named execution profile inside an environment document."""

    model_config = ConfigDict(extra="forbid")

    backend: str
    """Runner backend implementing this profile, from ``AVAILABLE_BACKEND_NAMES``."""

    @field_validator("backend")
    @classmethod
    def _backend_must_be_available(cls, value: str) -> str:
        if value not in AVAILABLE_BACKEND_NAMES:
            available = ", ".join(sorted(AVAILABLE_BACKEND_NAMES))
            raise ValueError(
                f"execution backend '{value}' is not available in this build of "
                f"Conductor (available: {available})"
            )
        return value


class EnvironmentDocument(BaseModel):
    """Parsed execution environment document.

    Whole documents shadow same-named lower-priority ones; nothing is ever
    merged or inherited between documents — see the module docstring.
    """

    model_config = ConfigDict(extra="forbid")

    default: str | None = None
    """Profile applied to executable steps that name no profile.

    Mandatory key in the built-in document; in authored documents it may be
    omitted, but when set it must name a profile defined in ``profiles`` —
    otherwise the precedence chain would fall through to a hard error on
    every profile-less workflow.
    """

    profiles: dict[str, ProfileDefinition] = Field(min_length=1)
    """Named execution profiles, each mapping to a runner backend."""

    @model_validator(mode="after")
    def _default_names_an_existing_profile(self) -> EnvironmentDocument:
        if self.default is not None and self.default not in self.profiles:
            raise ValueError(
                f"default profile '{self.default}' is not defined in profiles "
                f"(defined: {', '.join(sorted(self.profiles))})"
            )
        return self


@dataclass(frozen=True)
class ResolvedEnvironment:
    """An environment document plus the metadata of how it was reached."""

    document: EnvironmentDocument
    """The parsed document. Whole and unmerged — see the module docstring."""

    name: str
    """Environment name: the file stem for path/project/user sources, or
    ``"local/default"`` for the built-in environment."""

    source: Literal["builtin", "path", "project", "user"]
    """Which level produced the document."""

    path: Path | None
    """Absolute-ish path to the document on disk, or ``None`` for the
    built-in environment."""

    digest: str
    """``sha256:<hex>`` of the canonical JSON serialization of the document
    (``model_dump(mode="json")`` with sorted keys), pinning the resolved
    content for the run manifest."""


def _document_digest(document: EnvironmentDocument) -> str:
    """Digest pinning an environment document's exact content.

    Delegates to :func:`conductor.digest.canonical_json_digest` — canonical
    form: JSON with sorted keys and tight separators, so the digest depends
    on the document's data alone, never on insertion order or formatting.
    Spelled ``sha256:<hex>``, matching the workflow-hash convention in
    ``engine.checkpoint``.
    """
    return canonical_json_digest(document.model_dump(mode="json"))


def load_environment_document(path: Path) -> EnvironmentDocument:
    """Load and validate one environment document.

    The file is plain YAML read with ruamel — **no ``${VAR}`` expansion**
    (see the module docstring for why silent machine-dependent expansion is
    refused here) and no ``!file`` tags.

    Args:
        path: Path to the environment document.

    Returns:
        The parsed document.

    Raises:
        ConfigurationError: If the file cannot be read, contains malformed
            YAML, or fails schema validation. The error names ``file_path``.
    """
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError(
            f"Environment document '{path}' is not valid UTF-8: {exc}",
            suggestion="Save the document as UTF-8 (re-export or re-encode the "
            "file); environment documents are read strictly as UTF-8.",
            file_path=str(path),
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            f"Failed to read environment document '{path}': {exc}",
            suggestion="Check file permissions and ensure the file is readable.",
            file_path=str(path),
        ) from exc

    try:
        data = _yaml.load(content)
    except YAMLError as exc:
        raise ConfigurationError(
            f"Invalid YAML syntax in environment document '{path}': {exc}",
            suggestion="Check the YAML syntax: indentation, colons, and quoting.",
            file_path=str(path),
        ) from exc

    try:
        return EnvironmentDocument.model_validate(data)
    except PydanticValidationError as exc:
        raise ConfigurationError(
            f"Invalid environment document in {path}: {exc}",
            suggestion="Environment documents declare 'default' and 'profiles' "
            "mapping profile names to a 'backend'.",
            file_path=str(path),
        ) from exc


def is_path_reference(value: str) -> bool:
    """Whether an ``--environment`` value denotes a filesystem path.

    Prefix-based, mirroring ``plugins.sources._is_local_path``: a value
    starting with ``~`` or ``.``, or containing ``/`` or ``\\``, is a path;
    anything else is an environment name from the ``[A-Za-z0-9_-]+``
    charset (no dots — a name maps directly to ``<name>.yaml``).
    """
    return value.startswith(("~", ".")) or "/" in value or "\\" in value


def _conductor_home() -> Path:
    """Base directory for the user level, honoring ``$CONDUCTOR_HOME``.

    Mirrors ``settings.get_settings_path``: the env var wins, falling back
    to ``~/.conductor``. Tests isolate the user level by setting
    ``CONDUCTOR_HOME``; nothing here reads the real home unless the
    variable is unset.
    """
    home = os.environ.get("CONDUCTOR_HOME")
    return Path(home) if home else Path.home() / ".conductor"


def _project_environment_dirs(workflow_dir: Path) -> list[Path]:
    """Candidate project environment directories, nearest directory first.

    Walks ``workflow_dir`` and its ancestors up to the first ``.git``
    marker (file or directory — a linked worktree stops the walk exactly
    like a clone). When no ancestor has a marker, the result collapses to
    ``workflow_dir`` alone: climbing an unversioned tree to the filesystem
    root would sweep in unrelated directories, the same collapse
    ``skills.discovery._project_roots`` performs.

    The anchor is first made absolute and normalized with
    ``os.path.abspath``: a *relative* workflow directory (e.g. ``Path('.')``
    when the CLI was invoked from a subdirectory) has no ``.parents``, so
    the ancestor walk would never reach the repository root and a real
    root-level environment document would be silently missed. ``abspath``
    rather than ``Path.resolve()`` deliberately preserves symlink aliases,
    matching the repo-wide "normpath, not resolve" convention for
    user-typed paths.
    """
    workflow_dir = Path(os.path.abspath(workflow_dir))
    ancestors: list[Path] = []
    for directory in (workflow_dir, *workflow_dir.parents):
        ancestors.append(directory)
        if (directory / _REPO_MARKER).exists():
            break
    else:
        ancestors = [workflow_dir]
    return [ancestor / _PROJECT_ENVIRONMENTS_ROOT for ancestor in ancestors]


def _user_environments_dir() -> Path:
    """Directory holding user-level environment documents."""
    return _conductor_home() / "environments"


def _warn(on_warning: WarningSink | None, message: str) -> None:
    """Report a non-fatal diagnostic to the sink, or to the logger."""
    if on_warning is not None:
        on_warning(message)
    else:
        logger.warning("%s", message)


def resolve_environment(
    name_or_path: str,
    *,
    workflow_dir: Path,
    on_warning: WarningSink | None = None,
) -> ResolvedEnvironment:
    """Resolve an environment name or path to a document.

    A path reference (see :func:`is_path_reference`) loads directly, with
    ``~`` expanded. A name resolves first-match through the project chain
    (nearest ancestor of ``workflow_dir`` with
    ``.conductor/environments/<name>.yaml`` wins, up to the repository
    root) and then through the user level under ``$CONDUCTOR_HOME``.

    Whole-document shadowing applies: the first document found completely
    replaces any same-named lower-priority document — nothing is merged
    (see the module docstring).

    Args:
        name_or_path: Environment name or path to a document.
        workflow_dir: The workflow file's directory, anchoring the walk.
        on_warning: Sink for non-fatal diagnostics (reserved for future
            lenient paths; explicit resolution fails hard instead).

    Returns:
        The resolved environment with source, path, and content digest.

    Raises:
        ConfigurationError: If the document is missing, malformed, or
            schema-invalid. A missing name lists every location searched.
    """
    value = name_or_path.strip()
    if not value:
        raise ConfigurationError("Environment name or path must be a non-empty string.")

    if is_path_reference(value):
        path = Path(value).expanduser()
        document = load_environment_document(path)
        return ResolvedEnvironment(
            document=document,
            name=path.stem,
            source="path",
            path=path,
            digest=_document_digest(document),
        )

    if _ENVIRONMENT_NAME_PATTERN.match(value) is None:
        raise ConfigurationError(
            f"Invalid environment name '{value}': names must match "
            f"{_ENVIRONMENT_NAME_PATTERN.pattern} (letters, digits, '_' and '-'; "
            "no dots or separators — a name maps to '<name>.yaml').",
            suggestion="Use a name from the charset, or pass a path to the environment "
            "document if the file name needs other characters.",
        )

    searched = [
        env_dir / f"{value}.yaml" for env_dir in _project_environment_dirs(Path(workflow_dir))
    ]
    user_candidate = _user_environments_dir() / f"{value}.yaml"
    searched.append(user_candidate)

    for candidate in searched:
        if not candidate.exists():
            continue
        document = load_environment_document(candidate)
        return ResolvedEnvironment(
            document=document,
            name=value,
            source="user" if candidate == user_candidate else "project",
            path=candidate,
            digest=_document_digest(document),
        )

    locations = "\n".join(f"  - {candidate}" for candidate in searched)
    raise ConfigurationError(
        f"Execution environment '{value}' was not found. Searched:\n{locations}",
        suggestion="Create the environment document at one of the searched locations, "
        "or pass a path to an environment document.",
    )


def builtin_local_environment() -> ResolvedEnvironment:
    """The built-in fallback environment: a single ``local`` default profile.

    The ``default`` key is mandatory, not a convenience: without it, the
    manifest precedence chain (step → workflow defaults → environment
    default) would miss on every profile-less workflow, and a workflow
    with no execution configuration at all must still compile against this
    environment. Name is ``"local/default"`` and ``path`` is ``None`` —
    there is no file behind it.
    """
    document = EnvironmentDocument(
        default="default",
        profiles={"default": ProfileDefinition(backend="local")},
    )
    return ResolvedEnvironment(
        document=document,
        name="local/default",
        source="builtin",
        path=None,
        digest=_document_digest(document),
    )


def discover_all_environments(
    workflow_dir: Path,
    *,
    on_warning: WarningSink | None = None,
) -> dict[str, ResolvedEnvironment]:
    """Collect every environment document discoverable for a workflow.

    Used by bare ``conductor validate`` to cross-check profile references
    against the ambient environment set without an explicit ``--environment``.
    Scans the same project chain and user level as :func:`resolve_environment`
    and returns the successfully parsed documents keyed by environment name,
    nearest occurrence winning on a name collision.

    Malformed documents are **never a hard error here**: each one produces a
    warning naming the file and is treated as non-resolving, so one stray
    file cannot fail validation of a workflow that never referenced it.
    (Resolving that same name explicitly through :func:`resolve_environment`
    is fatal, as explicit resolution promises a loadable document.)

    Args:
        workflow_dir: The workflow file's directory, anchoring the walk.
        on_warning: Sink for per-file malformed-document warnings.

    Returns:
        Discovered environments keyed by name, nearest source first on
        collisions. Whole documents shadow — no merging (see the module
        docstring).
    """
    discovered: dict[str, ResolvedEnvironment] = {}
    search_dirs: list[tuple[Path, Literal["project", "user"]]] = [
        (env_dir, "project") for env_dir in _project_environment_dirs(Path(workflow_dir))
    ]
    search_dirs.append((_user_environments_dir(), "user"))

    for directory, source in search_dirs:
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.yaml")):
            name = file.stem
            if name in discovered:
                continue
            try:
                document = load_environment_document(file)
            except ConfigurationError as exc:
                _warn(on_warning, f"Skipping malformed environment document {file}: {exc}")
                continue
            discovered[name] = ResolvedEnvironment(
                document=document,
                name=name,
                source=source,
                path=file,
                digest=_document_digest(document),
            )
    return discovered
