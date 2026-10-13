"""The resolved run manifest: a deterministic snapshot of execution resolution.

``compile_run_manifest`` resolves every executable step of the *root* workflow
configuration to a concrete runner backend through an execution environment
document and records the result in a :class:`ResolvedRunManifest`. The manifest is
**run-invariant**: it deliberately contains no run id, no timestamps, no
inputs, no CLI overrides, and no absolute paths, so two compilations of the
same inputs produce byte-identical ``model_dump(mode="json")`` output and the
manifest can act as the audit record of *how* a run was set up to execute.

**Execution-step identity.** The keys of ``ResolvedRunManifest.profiles`` are:

* ``<name>`` for top-level executable steps (agent, script, mcp, workflow)
  declared in ``config.agents`` — e.g. ``inspect``.
* ``for_each.<group_name>.agent`` for the inline agent of a for-each group —
  e.g. ``for_each.repos.agent``.

The qualified for-each key keeps two inline agents that share one step name in
different groups distinct (``for_each.first.agent`` vs
``for_each.second.agent``). The key construction lives in exactly one helper,
:func:`executable_step_identity`, shared with the runtime resolver so the
manifest and the run-time backend lookup can never drift apart. A generated
key that collides with another step's key (e.g. a top-level step literally
named ``for_each.batch.agent`` alongside a for-each group named ``batch``) is
a hard :class:`~conductor.exceptions.ConfigurationError` naming both steps —
one step silently overwriting the other's resolution would defeat the
manifest's purpose as an audit record.

**Scope boundary.** Only the root configuration is compiled into the manifest.
Nested sub-workflow steps (the file a ``type: workflow`` step points at) are
*not* included: a sub-workflow engine compiles its own view over its own
configuration when it is constructed at reach time, so a root manifest never
claims resolution authority over configuration it has not read.

**Audit posture.** Every manifest carries an :class:`AuditInfo` classifying
itself as ``non-hermetic-compatibility``: resolved profiles and backends may
depend on machine-local environment documents, so the manifest records what was
resolved (names, digests, content hashes) rather than pretending the
resolution was hermetic.

**Versioning.** Version 1 is additive: new fields may be added without a
version bump, and consumers are expected to ignore unknown keys. Defaults on
new model fields preserve reads of manifests written by earlier v1 producers.
The optional per-profile ``execution`` field follows this rule: consumers must
ignore it when unknown or ``None``. The ``script_steps`` identity list is the
exception that proves the rule: it is an in-memory aid for script-step
discrimination (see :func:`script_step_backends`), excluded from serialization
entirely so dumps stay byte-identical to pre-recording producers.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_serializer,
    field_validator,
    model_serializer,
)

from conductor.config.environment import ProfileDefinition, ResolvedEnvironment
from conductor.config.schema import (
    ExecutableStepBase,
    ScriptStepDef,
    StepSecretRef,
    WorkflowConfig,
    WorkflowDefaults,
    WorkflowStepDef,
)
from conductor.digest import canonical_json_digest
from conductor.exceptions import ConfigurationError
from conductor.execution import LocalRunnerBackend
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.types import RunnerCapabilities
from conductor.providers.resolution import (
    effective_mcp_consumer_providers,
    format_claude_agent_sdk_remote_env_error,
    format_remote_mcp_stdio_only_error,
)


class BackendCapabilityProvider(Protocol):
    def capabilities(self) -> RunnerCapabilities: ...


# Capability-introspection registry: backend name -> a backend instance asked
# only for its static ``capabilities()`` declaration. The instances are
# assumed STATELESS with respect to capabilities — the answer must not depend
# on when it is asked, because the manifest compiler asks at compile time and
# the run may ask again later. ``LocalRunnerBackend`` satisfies this today (it
# has no instance state at all); any future backend registered here must too.
BACKEND_CAPABILITY_PROVIDERS: dict[str, BackendCapabilityProvider] = {
    "local": LocalRunnerBackend(),
    # DockerRunnerBackend's constructor is I/O-free (it only stores the binary
    # name and an environment snapshot), so a module-level instance is safe to
    # keep here for static capability introspection at compile time.
    "docker": DockerRunnerBackend(),
}


class WorkflowIdentity(BaseModel):
    """Identity of the workflow being run.

    ``digest`` is ``sha256:<hex>`` over the workflow file's bytes, or ``None``
    when the workflow was built without a file on disk (programmatic
    construction) — there is nothing to hash.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None
    digest: str | None


class EnvironmentIdentity(BaseModel):
    """Identity of the execution environment the profiles resolved against."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    source: Literal["builtin", "path", "project", "user"]
    digest: str


class ManifestExecutionSpec(BaseModel):
    """Normalized requested container configuration recorded for audit.

    An image tag records the requested reference but does not identify the
    bytes selected by the daemon. An image digest identifies those bytes. All
    fields remain present in serialized output, including explicit nulls, so
    an audit consumer sees a stable key set for every recorded execution.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str
    platform: str | None = None
    network: str | None = None
    user: str | None = None
    init: bool = False
    read_only: bool = False
    cap_drop_all: bool = False
    no_new_privileges: bool = False
    tmpfs: bool | str = False
    cpu: float | None = None
    memory: str | None = None
    pids: int | None = None


class ResolvedStepProfile(BaseModel):
    """One executable step's resolved execution profile and backend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str
    backend: str
    inherit_control_environment: bool = True
    execution: ManifestExecutionSpec | None = None

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        dump = super().model_dump(*args, **kwargs)
        if dump.get("execution") is None:
            dump.pop("execution", None)
        return dump


class ResolvedSecretUse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    consumer: str
    ref: str
    scope: Literal["script", "mcp"]
    delivery_kind: Literal["env", "header"]
    delivery_name: str


class AuditInfo(BaseModel):
    """Self-classification of the manifest's hermetic posture.

    ``hermetic`` is a literal ``False``: resolution depends on machine-local
    environment documents, so the manifest records resolved names and digests
    instead of claiming hermetic reproducibility. ``classification`` is the
    stable label downstream tooling keys on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hermetic: Literal[False]
    classification: Literal["non-hermetic-compatibility"]


class WorkspacePolicy(BaseModel):
    """Run-global workspace mode and retention policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    persistence: str


class ResolvedRunManifest(BaseModel):
    """The compiled, run-invariant execution manifest for one workflow run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    workflow: WorkflowIdentity
    environment: EnvironmentIdentity
    profiles: Mapping[str, ResolvedStepProfile]
    """Resolved profile per executable step, keyed by
    :func:`executable_step_identity`. Stored as a defensively copied,
    read-only mapping: the manifest is the run's audit record, so nothing
    with a handle to it may mutate execution resolution after compilation.
    Serialization still yields a plain ``dict`` (see the field serializer)."""
    secrets: tuple[ResolvedSecretUse, ...] = ()
    workspace: WorkspacePolicy | None = None
    conductor_version: str
    audit: AuditInfo
    script_steps: tuple[str, ...] = Field(default=(), exclude=True)
    """Identities (``profiles`` keys) of the steps the compiler resolved as
    :class:`ScriptStepDef`, captured during compilation so
    :func:`script_step_backends` can restrict mixed-backend detection to
    script steps. An in-memory aid, never part of the audit dump:
    ``exclude=True`` keeps serialized manifests byte-identical to
    pre-recording v1 producers, at the cost that a manifest revalidated from
    its own dump carries no identities (the helper's documented fallback
    covers that case)."""

    @field_validator("profiles", mode="after")
    @classmethod
    def _freeze_profiles(
        cls, value: Mapping[str, ResolvedStepProfile]
    ) -> Mapping[str, ResolvedStepProfile]:
        """Copy into a read-only mapping so ``frozen=True`` reaches the data.

        A shallow copy suffices: the values are themselves frozen models.
        The copy happens even when the caller already passed an immutable
        mapping — the manifest must never alias storage it does not own.
        """
        return MappingProxyType(dict(value))

    @field_serializer("profiles")
    def _serialize_profiles(
        self, value: Mapping[str, ResolvedStepProfile]
    ) -> dict[str, ResolvedStepProfile]:
        """Serialize the read-only mapping as a plain JSON object.

        ``MappingProxyType`` is otherwise opaque to Pydantic's serializer,
        which would raise ``PydanticSerializationError`` on
        ``model_dump(mode="json")``. Insertion order is preserved, keeping
        the manifest's byte-deterministic dump contract.
        """
        return dict(value)

    @model_serializer(mode="wrap")
    def _serialize_workspace(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        dump = handler(self)
        if dump.get("workspace") is None:
            dump.pop("workspace", None)
        return dump

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        dump = super().model_dump(*args, **kwargs)
        if dump.get("workspace") is None:
            dump.pop("workspace", None)
        for profile in dump.get("profiles", {}).values():
            if isinstance(profile, dict) and profile.get("execution") is None:
                profile.pop("execution", None)
        return dump


def manifest_semantic_digest(manifest: ResolvedRunManifest) -> str:
    """Digest workflow, environment, profiles, secret references and workspace policy.

    This does not prove the dynamic sub-workflow closure or image bytes selected by a tag.
    """
    return canonical_json_digest(
        manifest.model_dump(mode="json", exclude={"conductor_version", "audit"})
    )


def executable_step_identity(name: str, *, for_each_group: str | None = None) -> str:
    """Manifest key for one executable step: bare name or the qualified
    for-each form.

    This is the single definition of the key shape, shared between the
    manifest compiler and the runtime resolver — both must agree, or a run
    would look up backends under keys the manifest never wrote.
    """
    if for_each_group is not None:
        return f"for_each.{for_each_group}.agent"
    return name


def _conductor_version() -> str:
    """Return the installed conductor-cli version.

    Mirrors ``WorkflowEngine._conductor_version``: ``conductor.__version__``
    is itself sourced from ``importlib.metadata``, with ``"unknown"`` as the
    fallback when the distribution metadata cannot be read.
    """
    try:
        from conductor import __version__

        return __version__
    except Exception:
        return "unknown"


def _iter_executable_steps(config: WorkflowConfig) -> list[tuple[str, ExecutableStepBase]]:
    """Collect ``(identity_key, step)`` for every executable step of the root config.

    Top-level executable steps key by their bare ``name``; inline for-each
    agents key by the qualified form (see :func:`executable_step_identity`).
    Engine-local steps (set, wait, terminate, human_gate, questions) are not
    executable and are skipped.

    A generated key that two distinct steps claim (e.g. a top-level step
    literally named ``for_each.batch.agent`` alongside a for-each group
    named ``batch``) is a hard error naming both declarations — the
    alternative, one step silently overwriting the other's entry, would
    make the manifest report one step's profile for another step and stop
    recording the overwritten step as executable at all.
    """
    steps: list[tuple[str, ExecutableStepBase]] = []
    seen: dict[str, str] = {}

    def _claim(key: str, provenance: str) -> None:
        prior = seen.get(key)
        if prior is not None:
            suggestion = "Rename one of them so every executable step has a unique identity."
            if (
                key.startswith("for_each.")
                and key.endswith(".agent")
                and ("top-level" in prior or "top-level" in provenance)
            ):
                # The generated for-each form is the one collision an author
                # cannot see from their own step names alone — call it out.
                suggestion += (
                    " A top-level step named 'for_each.<group>.agent' always "
                    "collides with that group's inline agent."
                )
            raise ConfigurationError(
                f"Executable step identity '{key}' is claimed by both {prior} "
                f"and {provenance}. Rename one of them.",
                suggestion=suggestion,
            )
        seen[key] = provenance

    for step in config.agents:
        if isinstance(step, ExecutableStepBase):
            key = executable_step_identity(step.name)
            _claim(key, f"top-level executable step '{step.name}'")
            steps.append((key, step))
    for group in config.for_each:
        agent = group.agent
        if isinstance(agent, ExecutableStepBase):
            key = executable_step_identity(agent.name, for_each_group=group.name)
            _claim(
                key,
                f"inline executable step '{agent.name}' in for-each group '{group.name}'",
            )
            steps.append((key, agent))
    return steps


def effective_profile_name(
    step: ExecutableStepBase,
    key: str,
    workflow_defaults: WorkflowDefaults,
    environment: ResolvedEnvironment,
) -> str:
    """Resolve the profile name for one step through the precedence chain.

    Chain, first hit wins:

    1. ``step.execution.profile``
    2. ``workflow_defaults.execution.profile``
    3. ``environment.document.default``

    A complete miss is a configuration error naming the step and the full
    chain, so the author sees every level that failed rather than guessing.
    """
    step_profile = step.execution.profile if step.execution is not None else None
    if step_profile is not None:
        return step_profile

    defaults = workflow_defaults.execution
    workflow_profile = defaults.profile if defaults is not None else None
    if workflow_profile is not None:
        return workflow_profile

    if environment.document.default is not None:
        return environment.document.default

    raise ConfigurationError(
        f"Step '{key}' names no execution profile and none can be resolved. "
        "Precedence chain exhausted: "
        "step 'execution.profile' is unset, "
        "'workflow.defaults.execution.profile' is unset, "
        f"and environment '{environment.name}' defines no 'default' profile.",
        suggestion="Set 'execution.profile' on the step, add "
        "'workflow.defaults.execution.profile', or give the environment "
        "document a 'default' profile.",
    )


def resolve_execution_spec(definition: ProfileDefinition) -> ManifestExecutionSpec | None:
    """Copy a profile's normalized requested container configuration.

    Environment schema validation has already normalized every value. This
    function deliberately performs no daemon lookup or further interpretation.
    """
    if definition.backend != "docker":
        return None
    docker = definition.docker
    assert docker is not None
    resources = docker.resources
    return ManifestExecutionSpec(
        image=docker.image,
        platform=docker.platform,
        network=docker.network,
        user=docker.user,
        init=docker.init,
        read_only=docker.read_only,
        cap_drop_all=docker.cap_drop_all,
        no_new_privileges=docker.no_new_privileges,
        tmpfs=docker.tmpfs,
        cpu=resources.cpu,
        memory=resources.memory,
        pids=resources.pids,
    )


def script_step_backends(manifest: ResolvedRunManifest) -> frozenset[str]:
    """Return the distinct backends the run's script steps resolve to.

    Only profiles whose step identity the compiler recorded in
    ``ResolvedRunManifest.script_steps`` count. Agent, workflow, and MCP steps
    are compile-time pinned to ``local`` (a non-local backend there is a
    reserved error), so reading backends off *every* profile would pollute
    mixed-backend detection with a backend no script step uses — the dominant
    shape, an agent-plus-Docker-script workflow, would always look "mixed".

    Legacy fallback: an empty ``script_steps`` means either a genuinely
    script-free compilation or a manifest without recorded identities — a v1
    payload written by an earlier producer, or any manifest revalidated from
    its serialized dump (``script_steps`` is never serialized). The two are
    indistinguishable, and the second cannot afford a wrong "no": this
    helper's only consumers are the mixed-backend run warning and the bundle
    docker-presence gate, where silently dropping a diagnostic the run used
    to emit is worse than the old over-inclusive answer. An empty set
    therefore answers as the pre-recording implementation did — the backends
    of ALL profiles. That fallback is exact for the script-free case anyway:
    non-script steps pin to ``local``, so no Docker backend can appear in a
    manifest that compiled no script steps.
    """
    if manifest.script_steps:
        return frozenset(manifest.profiles[key].backend for key in manifest.script_steps)
    return frozenset(profile.backend for profile in manifest.profiles.values())


def _require_script_backend_capability(key: str, backend_name: str) -> None:
    """Enforce that a script step's backend can run plain commands.

    ``ScriptStepDef`` delegates to the backend's batch execution, so a backend
    without ``capabilities().batch`` cannot serve the step and compilation
    fails fast with a ``ConfigurationError`` naming the step, the backend, and
    the missing capability.
    """
    provider = BACKEND_CAPABILITY_PROVIDERS.get(backend_name)
    if provider is None or not provider.capabilities().batch:
        raise ConfigurationError(
            f"Script step '{key}' resolves to backend '{backend_name}', which "
            "does not declare the 'batch' capability required to run commands.",
            suggestion="Map the step's execution profile to a backend that "
            "supports batch execution in this build of Conductor.",
        )


def _require_known_secret_ref(
    secret: StepSecretRef,
    *,
    consumer: str,
    environment: ResolvedEnvironment,
) -> None:
    bindings = environment.document.secrets or {}
    if secret.ref in bindings:
        return
    available = ", ".join(sorted(bindings)) or "none"
    raise ConfigurationError(
        f"Secret reference '{secret.ref}' used by '{consumer}' is not defined "
        f"in environment '{environment.name}' (available secrets: {available}).",
        suggestion="Declare the secret in the environment document, or use one "
        "of the available secret names.",
    )


def _resolved_secret_use(
    consumer: str,
    secret: StepSecretRef,
    scope: Literal["script", "mcp"],
) -> ResolvedSecretUse:
    if secret.delivery.env is not None:
        return ResolvedSecretUse(
            consumer=consumer,
            ref=secret.ref,
            scope=scope,
            delivery_kind="env",
            delivery_name=secret.delivery.env,
        )
    assert secret.delivery.header is not None
    return ResolvedSecretUse(
        consumer=consumer,
        ref=secret.ref,
        scope=scope,
        delivery_kind="header",
        delivery_name=secret.delivery.header,
    )


def _check_delivery_collisions(
    consumer: str,
    *,
    literal_env: Iterable[str],
    literal_headers: Iterable[str],
    secrets: Iterable[StepSecretRef],
) -> None:
    """Reject delivery-name collisions within one consumer at compile time.

    Mirrors ``config.validator._delivery_collision_errors`` with identical
    wording, so the same mistake reads the same whether it surfaces at
    ``conductor validate`` or at manifest compilation. Running here matters
    because ``conductor run`` never invokes the semantic validator: without
    it, two secret references collapsing onto one delivery name silently
    let the last credential win in the delivery dicts, and a case-only
    literal collision goes unnoticed on Windows, where environment variable
    names are case-insensitive.
    """
    env_names = {name.casefold() if sys.platform == "win32" else name for name in literal_env}
    header_names = {name.casefold() for name in literal_headers}
    for secret in secrets:
        delivery = secret.delivery
        if delivery.env is not None:
            env_key = delivery.env.casefold() if sys.platform == "win32" else delivery.env
            if env_key in env_names:
                raise ConfigurationError(
                    f"Secret delivery name '{delivery.env}' collides within consumer "
                    f"'{consumer}' (environment variable names must be unique)."
                )
            env_names.add(env_key)
        else:
            assert delivery.header is not None
            header_key = delivery.header.casefold()
            if header_key in header_names:
                raise ConfigurationError(
                    f"Secret delivery name '{delivery.header}' collides within consumer "
                    f"'{consumer}' (HTTP header names are case-insensitive and must be unique)."
                )
            header_names.add(header_key)


def _compile_step_secret_uses(
    key: str,
    step: ExecutableStepBase,
    environment: ResolvedEnvironment,
) -> list[ResolvedSecretUse]:
    secrets = step.execution.secrets if step.execution is not None else []
    if not secrets:
        return []
    if not isinstance(step, ScriptStepDef) or any(secret.scope == "agent" for secret in secrets):
        raise ConfigurationError(
            f"Step '{key}' requests secret delivery, but agent-scope delivery is reserved "
            "until agent execution realms (step 7).",
            suggestion="Remove the secret reference until agent execution realms are available.",
        )

    uses: list[ResolvedSecretUse] = []
    for secret in secrets:
        if secret.delivery.header is not None:
            raise ConfigurationError(
                f"Script step '{key}' requests header delivery for secret '{secret.ref}', "
                "but header delivery is MCP-only.",
                suggestion="Use delivery.env for script steps.",
            )
        if secret.scope != "script":
            raise ConfigurationError(
                f"Script step '{key}' cannot consume secret '{secret.ref}' with scope "
                f"'{secret.scope}'; this position requires scope 'script'.",
                suggestion="Set the secret scope to 'script' or move it to an MCP server.",
            )
        _require_known_secret_ref(secret, consumer=key, environment=environment)
        uses.append(_resolved_secret_use(key, secret, "script"))
    # Non-script steps and agent-scope refs raised above, so the literal
    # ``env`` keys to check against are the script step's own.
    assert isinstance(step, ScriptStepDef)
    _check_delivery_collisions(
        key,
        literal_env=step.env,
        literal_headers=(),
        secrets=secrets,
    )
    return uses


def _compile_mcp_secret_uses(
    config: WorkflowConfig,
    environment: ResolvedEnvironment,
) -> list[ResolvedSecretUse]:
    uses: list[ResolvedSecretUse] = []
    consumers = effective_mcp_consumer_providers(config)
    for server_name in sorted(config.workflow.runtime.mcp_servers):
        server = config.workflow.runtime.mcp_servers[server_name]
        consumer = f"mcp:{server_name}"
        if server.type in ("http", "sse"):
            stdio_only = consumers & {"claude", "openai"}
            if stdio_only:
                raise ConfigurationError(
                    format_remote_mcp_stdio_only_error(
                        server_name,
                        server.type,
                        stdio_only,
                    ),
                    suggestion="Use a stdio MCP server or override every consuming agent to a "
                    "provider that supports remote MCP transports.",
                )
        for secret in server.secrets:
            if secret.scope == "agent":
                raise ConfigurationError(
                    f"MCP server '{server_name}' requests agent-scope delivery, but agent-scope "
                    "delivery is reserved until agent execution realms (step 7).",
                    suggestion="Use scope 'mcp' for MCP server secret delivery.",
                )
            if secret.scope != "mcp":
                raise ConfigurationError(
                    f"MCP server '{server_name}' cannot consume secret '{secret.ref}' with scope "
                    f"'{secret.scope}'; this position requires scope 'mcp'.",
                    suggestion="Set the secret scope to 'mcp' or move it to a script step.",
                )
            if secret.delivery.header is not None and server.type == "stdio":
                raise ConfigurationError(
                    f"MCP server '{server_name}' uses transport 'stdio', which cannot deliver "
                    f"secret '{secret.ref}' through an HTTP header.",
                    suggestion="Use delivery.env for stdio, or an HTTP/SSE transport for headers.",
                )
            if (
                secret.delivery.env is not None
                and server.type in ("http", "sse")
                and "claude-agent-sdk" in consumers
            ):
                raise ConfigurationError(
                    format_claude_agent_sdk_remote_env_error(
                        server_name,
                        server.type,
                        secret.ref,
                    ),
                    suggestion="Use delivery.header (e.g. header: Authorization) for remote "
                    "servers on the claude-agent-sdk provider.",
                )
            _require_known_secret_ref(secret, consumer=consumer, environment=environment)
            uses.append(_resolved_secret_use(consumer, secret, "mcp"))
        _check_delivery_collisions(
            consumer,
            literal_env=server.env,
            literal_headers=server.headers,
            secrets=server.secrets,
        )
    return uses


def compile_run_manifest(
    config: WorkflowConfig,
    *,
    workflow_path: Path | None,
    environment: ResolvedEnvironment,
) -> ResolvedRunManifest:
    """Compile the run-invariant execution manifest for a workflow configuration.

    Pure function: the same inputs always produce the same manifest. Each
    executable step of the root config is resolved through the precedence
    chain (see :func:`effective_profile_name`) to a profile defined in
    ``environment.document.profiles``; script steps are additionally checked
    against the backend's batch capability (see
    :func:`_require_script_backend_capability`).

    Args:
        config: The parsed root workflow configuration.
        workflow_path: Path to the workflow file, or ``None`` when the config
            was built without one (digest becomes ``None``).
        environment: The resolved execution environment the profiles resolve
            against.

    Returns:
        The compiled manifest. ``model_dump(mode="json")`` is byte-identical
        across calls with equal inputs.

    Raises:
        ConfigurationError: If any step's profile cannot be resolved through
            the chain, names an undefined profile, or (for script steps)
            resolves to a backend without batch capability.
    """
    workspace = config.workflow.workspace
    if workspace is not None and workspace.mode == "isolated":
        raise ConfigurationError(
            "workspace.mode: isolated is reserved until the isolated workspace follow-up."
        )
    defaults_restart = config.workflow.defaults.restart
    if defaults_restart is not None and defaults_restart.mode == "reuse":
        raise ConfigurationError(
            "workflow.defaults.restart.mode: reuse is reserved until the restart follow-up."
        )

    if workflow_path is not None:
        for key, step in _iter_executable_steps(config):
            if not isinstance(step, WorkflowStepDef):
                continue
            child_path = workflow_path.parent / step.workflow
            if not child_path.is_file():
                continue  # Registry and dynamic references are resolved at reach time.
            from conductor.config.loader import load_config

            try:
                child = load_config(child_path)
            except ConfigurationError:
                # Broken children still fail at reach time, preserving subworkflow_failed events.
                continue
            if child.workflow.workspace is not None:
                raise ConfigurationError(
                    f"Sub-workflow '{key}' declares workflow.workspace; workspace policy is "
                    "inherited from the root workflow and cannot be overridden."
                )

    profiles: dict[str, ResolvedStepProfile] = {}
    script_keys: list[str] = []
    secrets: list[ResolvedSecretUse] = []
    for key, step in _iter_executable_steps(config):
        if step.restart is not None and step.restart.mode == "reuse":
            raise ConfigurationError(
                f"Step '{key}' restart.mode: reuse is reserved until the restart follow-up."
            )
        profile_name = effective_profile_name(
            step,
            key,
            config.workflow.defaults,
            environment,
        )
        definition = environment.document.profiles.get(profile_name)
        if definition is None:
            available = ", ".join(sorted(environment.document.profiles))
            raise ConfigurationError(
                f"Step '{key}' names execution profile '{profile_name}', which "
                f"is not defined in environment '{environment.name}' "
                f"(defined profiles: {available}).",
                suggestion="Define the profile in the environment document, or "
                "point the step at one of the defined profiles.",
            )
        backend_name = definition.backend
        if workspace is not None and workspace.persistence != "ephemeral":
            provider = BACKEND_CAPABILITY_PROVIDERS.get(backend_name)
            if provider is not None and not provider.capabilities().retained_workspace:
                raise ConfigurationError(
                    f"Step '{key}': {backend_name} backend does not support retained workspaces; "
                    "use docker profiles for every executable step or drop persistence; "
                    "local retained workspace arrives in a later delivery. Until the agent "
                    "realm follow-up, retained policy is available only for docker script "
                    "workflows."
                )
        if isinstance(step, ScriptStepDef):
            _require_script_backend_capability(key, backend_name)
            execution = resolve_execution_spec(definition)
        else:
            if backend_name != "local":
                raise ConfigurationError(
                    f"Step '{key}' resolves to backend '{backend_name}', but backend "
                    f"'{backend_name}' is available for script steps only; agent execution "
                    "realms arrive in step 7.",
                    suggestion="Move the step to a local profile or remove execution.profile.",
                )
            execution = None
        inherit_control_environment = (
            definition.inherit_control_environment
            if definition.inherit_control_environment is not None
            else backend_name == "local"
        )
        profiles[key] = ResolvedStepProfile(
            profile=profile_name,
            backend=backend_name,
            inherit_control_environment=inherit_control_environment,
            execution=execution,
        )
        if isinstance(step, ScriptStepDef):
            script_keys.append(key)
        secrets.extend(_compile_step_secret_uses(key, step, environment))

    secrets.extend(_compile_mcp_secret_uses(config, environment))

    digest: str | None = None
    if workflow_path is not None:
        digest = f"sha256:{hashlib.sha256(workflow_path.read_bytes()).hexdigest()}"

    return ResolvedRunManifest(
        version=1,
        workflow=WorkflowIdentity(name=config.workflow.name, digest=digest),
        environment=EnvironmentIdentity(
            name=environment.name,
            source=environment.source,
            digest=environment.digest,
        ),
        profiles=profiles,
        secrets=tuple(secrets),
        workspace=(
            WorkspacePolicy(mode=workspace.mode, persistence=workspace.persistence)
            if workspace is not None
            else None
        ),
        conductor_version=_conductor_version(),
        audit=AuditInfo(hermetic=False, classification="non-hermetic-compatibility"),
        script_steps=tuple(script_keys),
    )
