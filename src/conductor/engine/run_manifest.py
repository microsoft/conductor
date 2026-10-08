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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from conductor.config.environment import ProfileDefinition, ResolvedEnvironment
from conductor.config.schema import (
    AgentDef,
    ExecutableStepBase,
    RuntimeConfig,
    ScriptStepDef,
    StepSecretRef,
    WorkflowConfig,
    WorkflowDefaults,
)
from conductor.exceptions import ConfigurationError
from conductor.execution import LocalRunnerBackend, RunnerBackend
from conductor.execution.docker import DockerRunnerBackend
from conductor.providers.resolution import (
    effective_mcp_consumer_providers,
    format_claude_agent_sdk_remote_env_error,
    format_remote_mcp_stdio_only_error,
    provider_type_for_agent,
)

REMOTE_AGENT_PROVIDERS = frozenset({"copilot", "openai", "claude"})
RESERVED_AGENT_ENV_NAMES = frozenset(
    {
        "base_url",
        "api_key",
        "bearer_token",
        "github_token",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_BEARER_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }
)
LOCAL_AGENT_SCOPE_ERROR = (
    "local agent runtime inherits the control environment; scoped delivery for local "
    "agents is not built — use a remote execution profile or mcp/script scope"
)


def agent_secret_name_error(name: str) -> str | None:
    """Keep per-call MCP delivery disjoint from provider credentials."""
    if name.casefold() in {reserved.casefold() for reserved in RESERVED_AGENT_ENV_NAMES}:
        return (
            f"Agent-scope delivery.env '{name}' is reserved for provider credentials; "
            "choose a different MCP process environment variable."
        )
    return None


def agent_has_stdio_mcp(config: WorkflowConfig, agent: AgentDef) -> bool:
    """Conservatively identify MCP sources without resolving plugin contents."""
    if agent.tools == []:
        return False
    if any(server.type == "stdio" for server in config.workflow.runtime.mcp_servers.values()):
        return True
    plugins = agent.plugins if agent.plugins is not None else config.workflow.runtime.plugins
    return any(plugin.mcp for plugin in plugins)


def agent_scope_stdio_error(key: str) -> str:
    """Describe a per-call overlay that has no process to receive it."""
    return (
        f"Agent step '{key}' requests agent-scope delivery.env, but no stdio MCP server "
        "can receive env_overlay: it reaches only the spawn-env of stdio MCP processes "
        "in the remote realm, never the model SDK environment. Declare a stdio MCP server "
        "in workflow.runtime.mcp_servers or enable an MCP-shipping plugin for this agent "
        "(and enable its tools), or remove the agent-scope secret."
    )


# Capability-introspection registry: backend name -> a backend instance asked
# only for its static ``capabilities()`` declaration. The instances are
# assumed STATELESS with respect to capabilities — the answer must not depend
# on when it is asked, because the manifest compiler asks at compile time and
# the run may ask again later. ``LocalRunnerBackend`` satisfies this today (it
# has no instance state at all); any future backend registered here must too.
BACKEND_CAPABILITY_PROVIDERS: dict[str, RunnerBackend] = {
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
    """One step's placement; realm_image records the requested image reference,
    not the bytes selected by the daemon when the reference is a mutable tag."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str
    backend: str
    inherit_control_environment: bool = True
    execution: ManifestExecutionSpec | None = None
    realm_image: str | None = None
    inner_provider: str | None = None

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        dump = super().model_dump(*args, **kwargs)
        if dump.get("execution") is None:
            dump.pop("execution", None)
        for name in ("realm_image", "inner_provider"):
            if dump.get(name) is None:
                dump.pop(name, None)
        return dump


class ResolvedSecretUse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    consumer: str
    ref: str
    scope: Literal["script", "mcp", "agent"]
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
    conductor_version: str
    audit: AuditInfo
    script_steps: tuple[str, ...] = Field(default=(), exclude=True)
    script_steps_known: bool = Field(default=False, exclude=True)
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

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        dump = super().model_dump(*args, **kwargs)
        for profile in dump.get("profiles", {}).values():
            if isinstance(profile, dict):
                for name in ("execution", "realm_image", "inner_provider"):
                    if profile.get(name) is None:
                        profile.pop(name, None)
        return dump


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

    A newly compiled run records script identities, including an empty set
    for agent-only workflows. Its mixed-script warning never includes agent
    backends. A reloaded legacy manifest has no identities, so it retains
    the old over-inclusive fallback rather than hiding an earlier warning.
    The presence bit and identities are both excluded from serialized dumps.
    """
    if manifest.script_steps_known:
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


def _require_agent_backend_capability(
    key: str,
    backend_name: str,
    definition: ProfileDefinition,
    *,
    agent: AgentDef,
    runtime: RuntimeConfig,
) -> None:
    """Require an agent-capable backend and a complete runtime profile."""
    provider = BACKEND_CAPABILITY_PROVIDERS.get(backend_name)
    if provider is None or not provider.capabilities().agent:
        raise ConfigurationError(
            f"Agent step '{key}' resolves to backend '{backend_name}' without agent capability.",
            suggestion="Choose an agent-capable execution backend.",
        )
    effective_provider = provider_type_for_agent(agent, runtime.provider.name)
    if backend_name != "local" and effective_provider not in REMOTE_AGENT_PROVIDERS:
        raise ConfigurationError(
            f"Agent step '{key}' resolves to remote backend '{backend_name}' with provider "
            f"'{effective_provider}'; supported inner providers: copilot, openai, claude. "
            "Other providers require a follow-up implementation."
        )
    if backend_name == "docker" and (
        definition.docker is None or not definition.docker.runner_image
    ):
        raise ConfigurationError(
            f"Agent step '{key}' resolves to Docker without docker.runner_image.",
            suggestion="Set docker.runner_image to an image containing the Conductor runner.",
        )
    if backend_name == "aca":
        if agent.sandbox is not None and agent.sandbox.identifier_scope is not None:
            raise ConfigurationError(
                f"Agent step '{key}' cannot set sandbox.identifier_scope on an ACA profile; "
                "set it on the profile's aca block."
            )
        skills = agent.skills if agent.skills is not None else runtime.skills
        plugins = agent.plugins if agent.plugins is not None else runtime.plugins
        discovery = agent.skills is None and runtime.skill_discovery.is_enabled
        if skills or plugins or discovery:
            raise ConfigurationError(
                f"Agent step '{key}' resolves to ACA, which cannot stage skills or plugins.",
                suggestion="Use a Docker agent profile for staged skills/plugins, or opt out "
                "with skills: [] and plugins: [] on this agent.",
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
    scope: Literal["script", "mcp", "agent"],
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
    backend_name: str,
    config: WorkflowConfig,
) -> list[ResolvedSecretUse]:
    secrets = step.execution.secrets if step.execution is not None else []
    if not secrets:
        return []
    uses: list[ResolvedSecretUse] = []
    for secret in secrets:
        if isinstance(step, AgentDef):
            if secret.scope != "agent":
                raise ConfigurationError(
                    f"Agent step '{key}' cannot consume secret '{secret.ref}' with scope "
                    f"'{secret.scope}'; this position requires scope 'agent'."
                )
            if secret.delivery.env is not None and (
                error := agent_secret_name_error(secret.delivery.env)
            ):
                raise ConfigurationError(error)
            if backend_name == "local":
                raise ConfigurationError(f"Agent step '{key}': {LOCAL_AGENT_SCOPE_ERROR}.")
            if secret.delivery.env is None:
                raise ConfigurationError(
                    f"Agent step '{key}' requires delivery.env for secret '{secret.ref}': "
                    "env_overlay reaches only the spawn-env of stdio MCP processes in the realm "
                    "per call, never the model SDK environment."
                )
            if not agent_has_stdio_mcp(config, step):
                raise ConfigurationError(agent_scope_stdio_error(key))
            _require_known_secret_ref(secret, consumer=key, environment=environment)
            uses.append(_resolved_secret_use(key, secret, "agent"))
            continue
        if not isinstance(step, ScriptStepDef):
            raise ConfigurationError(
                f"Step '{key}' does not support secret delivery; use an agent or script step."
            )
        if secret.scope == "agent":
            raise ConfigurationError(
                f"Script step '{key}' cannot consume secret '{secret.ref}' with scope "
                "'agent'; this position requires scope 'script'."
            )
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
    _check_delivery_collisions(
        key,
        literal_env=step.env if isinstance(step, ScriptStepDef) else (),
        literal_headers=(),
        secrets=secrets,
    )
    return uses


def _compile_mcp_secret_uses(
    config: WorkflowConfig,
    environment: ResolvedEnvironment,
    profiles: Mapping[str, ResolvedStepProfile],
) -> list[ResolvedSecretUse]:
    uses: list[ResolvedSecretUse] = []
    consumers = effective_mcp_consumer_providers(config)
    local_consumer = any(
        isinstance(step, AgentDef)
        and step.tools != []
        and provider_type_for_agent(step, config.workflow.runtime.provider.name) != "hermes"
        and profiles[key].backend == "local"
        for key, step in _iter_executable_steps(config)
    )
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
                if secret.delivery.env is not None and (
                    error := agent_secret_name_error(secret.delivery.env)
                ):
                    raise ConfigurationError(error)
                if local_consumer:
                    raise ConfigurationError(
                        f"MCP server '{server_name}': {LOCAL_AGENT_SCOPE_ERROR}."
                    )
                if server.type != "stdio" or secret.delivery.env is None:
                    raise ConfigurationError(
                        f"MCP server '{server_name}' requires stdio and delivery.env for "
                        f"agent-scope secret '{secret.ref}': env_overlay reaches the spawn-env "
                        "of in-realm stdio MCP processes per call, never the model SDK "
                        "environment."
                    )
                _require_known_secret_ref(secret, consumer=consumer, environment=environment)
                uses.append(_resolved_secret_use(consumer, secret, "agent"))
                continue
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
    agent_server_secrets = [
        secret
        for server in config.workflow.runtime.mcp_servers.values()
        for secret in server.secrets
        if secret.scope == "agent"
    ]
    literal_mcp_env = {
        name
        for server in config.workflow.runtime.mcp_servers.values()
        if server.type == "stdio"
        for name in server.env
    }
    for key, step in _iter_executable_steps(config):
        if (
            isinstance(step, AgentDef)
            and step.tools != []
            and provider_type_for_agent(step, config.workflow.runtime.provider.name) != "hermes"
        ):
            step_secrets = (
                [secret for secret in step.execution.secrets if secret.scope == "agent"]
                if step.execution is not None
                else []
            )
            if step_secrets or agent_server_secrets:
                _check_delivery_collisions(
                    key,
                    literal_env=literal_mcp_env,
                    literal_headers=(),
                    secrets=[*step_secrets, *agent_server_secrets],
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
    profiles: dict[str, ResolvedStepProfile] = {}
    script_keys: list[str] = []
    secrets: list[ResolvedSecretUse] = []
    for key, step in _iter_executable_steps(config):
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
        if isinstance(step, ScriptStepDef):
            _require_script_backend_capability(key, backend_name)
            execution = resolve_execution_spec(definition)
        elif isinstance(step, AgentDef):
            _require_agent_backend_capability(
                key, backend_name, definition, agent=step, runtime=config.workflow.runtime
            )
            execution = resolve_execution_spec(definition)
            if execution is not None:
                assert definition.docker is not None and definition.docker.runner_image is not None
                execution = execution.model_copy(update={"image": definition.docker.runner_image})
        else:
            if backend_name != "local":
                raise ConfigurationError(
                    f"Step '{key}' resolves to backend '{backend_name}', but this step "
                    "requires the local backend.",
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
            realm_image=(
                definition.docker.runner_image
                if isinstance(step, AgentDef) and backend_name == "docker" and definition.docker
                else None
            ),
            inner_provider=(
                provider_type_for_agent(step, config.workflow.runtime.provider.name)
                if isinstance(step, AgentDef) and backend_name != "local"
                else None
            ),
        )
        if isinstance(step, ScriptStepDef):
            script_keys.append(key)
        secrets.extend(_compile_step_secret_uses(key, step, environment, backend_name, config))

    secrets.extend(_compile_mcp_secret_uses(config, environment, profiles))

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
        conductor_version=_conductor_version(),
        audit=AuditInfo(hermetic=False, classification="non-hermetic-compatibility"),
        script_steps=tuple(script_keys),
        script_steps_known=True,
    )
