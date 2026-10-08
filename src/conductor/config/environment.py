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
Secrets arrive as typed references in the document's ``secrets`` mapping
(each mapping a secret name to a source behind the resolution seam, such as
an environment variable), and the document contains no secret values — silently
expanding machine-dependent environment variables across the entire document
would be an anti-hermetic vector.

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
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from conductor.digest import canonical_json_digest
from conductor.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

WarningSink = Callable[[str], None]
"""Sink for non-fatal diagnostics, mirroring ``skills.registry.WarningSink``."""

# Execution backends compiled into this build of Conductor.
AVAILABLE_BACKEND_NAMES = frozenset({"local", "docker", "aca"})

# Environment names map directly to ``<name>.yaml``, so the charset excludes
# path separators and dots; anything outside it must be written as a path.
_ENVIRONMENT_NAME_PATTERN = re.compile(r"\A[A-Za-z0-9_-]+\Z")

# Secret binding names map to logical identifiers, supporting letters,
# digits, underscores, dots, and hyphens.
_SECRET_NAME_PATTERN = re.compile(r"\A[A-Za-z0-9_.-]+\Z")

# Environment variable names for secret bindings must be valid shell identifiers.
_ENV_VAR_NAME_PATTERN = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# Docker user syntax pattern: <name|uid>[:<group|gid>]
_DOCKER_USER_PATTERN = re.compile(r"\A[0-9a-zA-Z_][0-9a-zA-Z_.-]*(:[0-9a-zA-Z_][0-9a-zA-Z_.-]*)?\Z")

# Size unit pattern for memory and tmpfs limits (\d+[mg])
_SIZE_PATTERN = re.compile(r"\A(\d+)([mg])\Z")

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


class SecretBindingSource(BaseModel):
    """Source configuration for resolving a secret binding.

    v1 supports the ``env`` source kind exclusively. Future vault/keychain
    sources will add optional fields to this model.
    """

    model_config = ConfigDict(extra="forbid")

    env: str | None = None
    """Environment variable name providing the secret value."""

    @field_validator("env")
    @classmethod
    def _validate_env_var_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped or _ENV_VAR_NAME_PATTERN.match(stripped) is None:
            raise ValueError(
                f"Invalid environment variable name '{value}': must be non-empty and "
                f"match {_ENV_VAR_NAME_PATTERN.pattern} (letters, digits, and underscores, "
                "not starting with a digit)."
            )
        return stripped

    @model_validator(mode="after")
    def _validate_exactly_one_source_kind(self) -> SecretBindingSource:
        set_kinds = [k for k in ("env",) if getattr(self, k) is not None]
        if len(set_kinds) != 1:
            raise ValueError("exactly one source kind must be set (supported source kinds: env)")
        return self


class SecretBinding(BaseModel):
    """A secret binding declaring its source and allowed consumer classes."""

    model_config = ConfigDict(extra="forbid")

    source: SecretBindingSource
    """Source resolving the secret value."""

    allow: list[Literal["script", "mcp"]] | None = None
    """Allowed consumer classes for this secret.

    When ``None`` (the default), all consumer classes are allowed forever.
    When set to an empty list (``[]``), the binding is explicitly fail-closed
    and accessible to no consumer classes.
    """


class DockerResources(BaseModel):
    """Resource limits for container execution.

    All limits are optional: an unset limit means no daemon-level ceiling
    is configured (the container shares host resources with other processes).
    """

    model_config = ConfigDict(extra="forbid")

    cpu: float | None = Field(default=None, ge=0.1, le=64.0)
    """CPU limit in cores (e.g. 0.5, 2.0). Bounded between 0.1 and 64 cores."""

    memory: str | None = None
    """Memory limit formatted as '<number>[m|g]' (e.g. '512m', '2g').

    Bounded between 16 MiB (16m) and 65,536 MiB (64g).
    """

    pids: int | None = Field(default=None, ge=16, le=65536)
    """Process limit. Bounded between 16 and 65536."""

    @field_validator("memory")
    @classmethod
    def _validate_memory(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip().lower()
        match = _SIZE_PATTERN.match(stripped)
        if not match:
            raise ValueError(
                f"Invalid memory limit '{value}': must match pattern \\d+[mg] (e.g. '512m', '2g')"
            )
        num = int(match.group(1))
        unit = match.group(2)
        mib = num * 1024 if unit == "g" else num
        if mib < 16 or mib > 65536:
            raise ValueError(
                f"Memory limit '{value}' out of bounds: must be between 16m and 64g (65536m)"
            )
        return stripped


class DockerProfileOptions(BaseModel):
    """Configuration options for a Docker execution profile."""

    model_config = ConfigDict(extra="forbid")

    image: str
    """Container image reference (tag or digest)."""

    runner_image: str | None = None
    """OCI reference of the agent-realm runtime image (an image with the conductor runner
    installed); when set, the profile is agent-capable; script steps keep using ``image``.
    """

    platform: Literal["linux/amd64", "linux/arm64"] | None = None
    """Target platform for the container image (None = auto-resolved by Docker)."""

    network: Literal["none", "bridge", "host"] | None = None
    """Container network mode (None = daemon default)."""

    user: str | None = None
    """Container user in docker syntax '<name|uid>[:<group|gid>]' (None = image USER)."""

    init: bool = False
    """Whether to use a container init process (docker --init)."""

    read_only: bool = False
    """Whether to mount the container's root filesystem as read-only."""

    cap_drop_all: bool = False
    """Whether to drop all Linux capabilities (docker --cap-drop=ALL)."""

    no_new_privileges: bool = False
    """Whether to prevent the container from gaining additional privileges."""

    tmpfs: bool | str = False
    """Tmpfs mount configuration: False = disabled, True = /tmp, or size string (e.g. '1g')."""

    resources: DockerResources = Field(default_factory=DockerResources)
    """Resource constraints for container execution."""

    @field_validator("image")
    @classmethod
    def _validate_image(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or bool(re.search(r"\s", stripped)):
            raise ValueError("image must be a non-empty string without whitespace")
        return stripped

    @field_validator("runner_image")
    @classmethod
    def _validate_runner_image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped or bool(re.search(r"\s", stripped)):
            raise ValueError("runner_image must be a non-empty string without whitespace")
        return stripped

    @field_validator("user")
    @classmethod
    def _validate_user(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped or _DOCKER_USER_PATTERN.match(stripped) is None:
            raise ValueError(
                f"Invalid user '{value}': must match docker user syntax <name|uid>[:<group|gid>] "
                f"and pattern {_DOCKER_USER_PATTERN.pattern}"
            )
        return stripped

    @field_validator("tmpfs", mode="before")
    @classmethod
    def _validate_tmpfs(cls, value: Any) -> bool | str:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            stripped = value.strip().lower()
            match = _SIZE_PATTERN.match(stripped)
            if not match:
                raise ValueError(
                    f"Invalid tmpfs size '{value}': must be a boolean or match pattern \\d+[mg] "
                    "(e.g. '512m', '1g')"
                )
            num = int(match.group(1))
            unit = match.group(2)
            mib = num * 1024 if unit == "g" else num
            if mib < 1 or mib > 16384:
                raise ValueError(
                    f"tmpfs size '{value}' out of bounds: must be between 1m and 16g (16384m)"
                )
            return stripped
        raise ValueError("tmpfs must be a boolean or size string (e.g. '1g', '512m')")


class AcaProfileOptions(BaseModel):
    """Connection and session policy for an ACA execution profile."""

    model_config = ConfigDict(extra="forbid")

    pool_endpoint: str
    api_version: str | None = None
    identifier_scope: Literal["workflow", "agent", "item", "none"] = "agent"
    egress: Literal["enabled", "disabled"] | None = None
    lifecycle: Literal["timed", "on_container_exit"] | None = None
    auth: Literal["azure_default"] = "azure_default"

    @field_validator("pool_endpoint")
    @classmethod
    def _validate_pool_endpoint(cls, value: str) -> str:
        endpoint = value.strip()
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError(
                "pool_endpoint must be an https:// URL with a hostname and no query or fragment"
            )
        return endpoint


class ProfileDefinition(BaseModel):
    """One named execution profile inside an environment document."""

    model_config = ConfigDict(extra="forbid")

    backend: str
    """Runner backend implementing this profile, from ``AVAILABLE_BACKEND_NAMES``."""

    inherit_control_environment: bool | None = None
    """Whether commands run under this profile inherit the host control process's environment.

    When ``None`` (the default), the runner backend's default applies (the local
    subprocess backend defaults to effective ``True``, while future remote backends
    default to effective ``False``).
    """

    docker: DockerProfileOptions | None = None
    """Docker execution options, required if and only if backend is 'docker'."""

    aca: AcaProfileOptions | None = None
    """ACA execution options, required if and only if backend is 'aca'."""

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

    @model_validator(mode="after")
    def _validate_docker_options(self) -> ProfileDefinition:
        if self.backend == "docker" and self.docker is None:
            raise ValueError(
                "profile with backend 'docker' requires a 'docker' configuration block"
            )
        if self.backend != "docker" and self.docker is not None:
            raise ValueError(
                f"profile with backend '{self.backend}' cannot specify a "
                "'docker' configuration block"
            )
        if self.backend == "aca" and self.aca is None:
            raise ValueError("profile with backend 'aca' requires an 'aca' configuration block")
        if self.backend != "aca" and self.aca is not None:
            raise ValueError(
                f"profile with backend '{self.backend}' cannot specify an 'aca' configuration block"
            )
        return self

    def model_dump(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        dump = super().model_dump(*args, **kwargs)
        if dump.get("inherit_control_environment") is None:
            dump.pop("inherit_control_environment", None)
        if dump.get("docker") is None:
            dump.pop("docker", None)
        if dump.get("aca") is None:
            dump.pop("aca", None)
        return dump


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

    secrets: dict[str, SecretBinding] | None = None
    """Named secret bindings declaring source and access policies."""

    @field_validator("secrets")
    @classmethod
    def _validate_secret_names(
        cls, value: dict[str, SecretBinding] | None
    ) -> dict[str, SecretBinding] | None:
        if value is None:
            return None
        for name in value:
            if _SECRET_NAME_PATTERN.match(name) is None:
                raise ValueError(
                    f"Invalid secret binding name '{name}': names must match "
                    f"{_SECRET_NAME_PATTERN.pattern} (letters, digits, '_', '.', and '-')."
                )
        return value

    @model_validator(mode="after")
    def _default_names_an_existing_profile(self) -> EnvironmentDocument:
        if self.default is not None and self.default not in self.profiles:
            raise ValueError(
                f"default profile '{self.default}' is not defined in profiles "
                f"(defined: {', '.join(sorted(self.profiles))})"
            )
        return self

    def model_dump(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        dump = super().model_dump(*args, **kwargs)
        if dump.get("secrets") is None:
            dump.pop("secrets", None)
        for p in dump.get("profiles", {}).values():
            if isinstance(p, dict):
                if p.get("inherit_control_environment") is None:
                    p.pop("inherit_control_environment", None)
                if p.get("docker") is None:
                    p.pop("docker", None)
                elif isinstance(p["docker"], dict) and p["docker"].get("runner_image") is None:
                    p["docker"].pop("runner_image", None)
                if p.get("aca") is None:
                    p.pop("aca", None)
        return dump


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
    dump = document.model_dump(mode="json")
    if dump.get("secrets") is None:
        dump.pop("secrets", None)
    for p in dump.get("profiles", {}).values():
        if isinstance(p, dict):
            if p.get("inherit_control_environment") is None:
                p.pop("inherit_control_environment", None)
            if p.get("docker") is None:
                p.pop("docker", None)
            elif isinstance(p["docker"], dict) and p["docker"].get("runner_image") is None:
                p["docker"].pop("runner_image", None)
            if p.get("aca") is None:
                p.pop("aca", None)
    return canonical_json_digest(dump)


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
