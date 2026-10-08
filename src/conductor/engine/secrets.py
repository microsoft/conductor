"""Secret resolution and per-config secret delivery indexes.

This module is the resolution seam between the secret *bindings* declared in
an execution environment document (see ``conductor.config.environment``) and
the workflow steps and MCP servers that consume them. It owns:

* :class:`ResolvedSecret` — one resolved value wrapped so plaintext never
  appears in ``repr`` or logs.
* :class:`SecretValueCache` — the only run-scoped secret state. It resolves
  references against the environment document, enforces each binding's
  ``allow`` consumer-class policy on every call (including cache hits), reads
  values from their declared source, and registers every value into the
  run's :class:`~conductor.redaction.RunRedactor` so downstream event
  streams, checkpoints, and logs are scrubbed.
* :class:`SecretUseIndex` / :func:`index_config` — a per-config, immutable
  projection of which executable steps and MCP servers use which secret
  references. An index stores references, delivery kinds, and delivery names
  only — never plaintext; values are resolved on demand through a read-only
  link back to the cache. The root engine and every sub-workflow child get
  their own index over one shared cache; a child index never mutates the
  root's.

Security invariants:

* Secret values are never logged, serialized, or embedded in exception
  messages — errors name the reference, the source variable, and the
  consumer, nothing more.
* The built-in environment declares no secrets: any secret reference under
  it is a configuration error demanding an authored environment document.

Scope-vs-position enforcement (whether a given use-site may carry a given
``scope``) is deliberately out of scope here — the resolver trusts references
already checked by the validator/manifest layer, while still enforcing the
binding's ``allow`` policy itself on every resolution.

No file or network access happens here; importing this module performs no
resolution and no other side effects.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from pydantic import SecretStr

from conductor.config.environment import ResolvedEnvironment
from conductor.config.schema import StepSecretRef, WorkflowConfig
from conductor.engine.run_manifest import _iter_executable_steps
from conductor.exceptions import ConfigurationError
from conductor.redaction import RunRedactor

logger = logging.getLogger(__name__)

__all__ = [
    "IndexedSecretUse",
    "ResolvedSecret",
    "SecretUseIndex",
    "SecretValueCache",
    "index_config",
]

# Mirrors ``redaction._SHORT_SECRET_THRESHOLD``: secrets shorter than this
# many characters are registered with a warning, since they risk matching
# unrelated run output and over-redacting it. The constant is duplicated here
# (rather than imported from a private name) so this module stays decoupled
# from the redactor's internals; both must move together.
_SHORT_SECRET_WARNING_CHARS = 8


class ResolvedSecret:
    """One resolved secret value plus the binding policy it resolved under.

    A plain class (not a Pydantic model): the value lives in a
    :class:`~pydantic.SecretStr` so ``repr`` and accidental string conversion
    can never leak plaintext, and the ``allow`` set is normalized to a
    ``frozenset`` at construction.

    Attributes:
        ref: The logical secret reference name from the environment document.
        allow: Allowed consumer classes (``"script"``, ``"mcp"``), or ``None``
            when the binding permits every consumer class forever.
    """

    def __init__(self, ref: str, value: SecretStr, allow: frozenset[str] | None) -> None:
        """Initialize the resolved secret.

        Args:
            ref: The logical secret reference name.
            value: The resolved plaintext, wrapped in a ``SecretStr``.
            allow: Allowed consumer classes, or ``None`` for unrestricted.
        """
        self.ref = ref
        self._value = value
        self.allow = allow

    @property
    def value(self) -> str:
        """The plaintext value — for delivery to the consumer only.

        Never log, serialize, or interpolate this into a message; the run
        redactor scrubs it from event streams once registered.
        """
        return self._value.get_secret_value()

    def __repr__(self) -> str:
        """Safe representation naming the ref and policy, never the value."""
        return f"ResolvedSecret(ref={self.ref!r}, allow={self.allow!r})"


class SecretValueCache:
    """The only run-scoped secret state.

    Resolves logical secret references against the bindings of one resolved
    environment document and caches the results per reference. The cache is
    synchronous and lives on a single event loop, so each ``resolve`` call is
    atomic with respect to concurrent for-each executions.

    Every resolved value is registered into the run's
    :class:`~conductor.redaction.RunRedactor` so event logs, dashboard state,
    checkpoints, and diagnostics are scrubbed of it.

    Cleanup ownership: the cache's owner (the run finalizer, wired in a later
    step) calls :meth:`clear` after the run's final sinks have drained. The
    redactor itself is owned by the run, not by this cache.
    """

    def __init__(self, environment: ResolvedEnvironment, redactor: RunRedactor) -> None:
        """Initialize the cache.

        Args:
            environment: The resolved environment document whose ``secrets``
                bindings back every reference.
            redactor: The run-scoped redactor every resolved value is
                registered into.
        """
        self._environment = environment
        self._redactor = redactor
        self._cache: dict[str, ResolvedSecret] = {}

    @property
    def redactor(self) -> RunRedactor:
        """The run redactor every resolved value is registered into.

        Read-only accessor so session owners (``ExecutionResolverSession``)
        can derive their sinks from the same pair a caller injected, instead
        of building a parallel redactor the values never reach.
        """
        return self._redactor

    def resolve(self, ref: str, consumer_class: str, consumer_label: str) -> ResolvedSecret:
        """Resolve a secret reference for one consumer.

        Idempotent per reference: a second call for the same ``ref`` returns
        the identical :class:`ResolvedSecret`. The binding's ``allow`` policy
        is enforced on *every* call — including cache hits — so a reference
        approved for one consumer class can never be re-served to a different
        class from the cache without a fresh policy check.

        Args:
            ref: The logical secret reference name.
            consumer_class: The class of consumer requesting the secret
                (e.g. ``"script"``, ``"mcp"``).
            consumer_label: A human-readable description of the consumer for
                error messages (e.g. ``"step 'run'"``).

        Returns:
            The cached or freshly resolved secret.

        Raises:
            ConfigurationError: If the environment declares no secrets, the
                reference is unknown, the binding's ``allow`` policy rejects
                the consumer class, or the source variable is unset/empty.
                Messages name the reference, variable, and consumer only —
                never a value.
        """
        bindings = self._environment.document.secrets
        if bindings is None:
            raise ConfigurationError(
                f"Secret reference '{ref}' requested by {consumer_label}: secret references "
                f"require an authored environment document, but environment "
                f"'{self._environment.name}' declares no secret bindings.",
                suggestion="Declare the secret under 'secrets' in a "
                ".conductor/environments/<name>.yaml document and select it with "
                "--environment <name>.",
            )

        binding = bindings.get(ref)
        if binding is None:
            available = ", ".join(sorted(bindings)) or "(none)"
            raise ConfigurationError(
                f"Unknown secret reference '{ref}' requested by {consumer_label}: "
                f"environment '{self._environment.name}' defines no binding named "
                f"'{ref}' (available: {available}).",
                suggestion="Declare the secret under 'secrets' in the environment "
                "document, or fix the reference name.",
            )

        allow: frozenset[str] | None = (
            frozenset(binding.allow) if binding.allow is not None else None
        )
        # Enforced before the cache lookup, on every call including cache
        # hits: a ref approved for "mcp" must never be re-served to a
        # "script" consumer later in the run merely because it is cached.
        if allow is not None and consumer_class not in allow:
            if allow:
                detail = f"its binding allows only: {', '.join(sorted(allow))}"
            else:
                detail = "its binding allows no consumer classes (fail-closed)"
            raise ConfigurationError(
                f"Secret '{ref}' cannot be used by {consumer_class} consumer "
                f"{consumer_label}: {detail}.",
                suggestion=f"Add '{consumer_class}' to the binding's 'allow' list in the "
                "environment document, or consume the secret from an allowed class.",
            )

        cached = self._cache.get(ref)
        if cached is not None:
            return cached

        env_var = binding.source.env
        if env_var is None:
            # Unreachable today: SecretBindingSource requires exactly one
            # source kind. Defensive so a future source kind fails as a
            # configuration error instead of an AttributeError.
            raise ConfigurationError(
                f"Secret '{ref}' requested by {consumer_label} declares no env source.",
                suggestion="Set the binding's 'source.env' to the name of the environment "
                "variable holding the value.",
            )
        value = os.environ.get(env_var)
        if value is None or value == "":
            raise ConfigurationError(
                f"Secret '{ref}' requested by {consumer_label} resolves from environment "
                f"variable '{env_var}', which is not set or is empty.",
                suggestion=f"Set '{env_var}' in the process environment before running.",
            )

        secret = ResolvedSecret(ref=ref, value=SecretStr(value), allow=allow)
        if len(value) < _SHORT_SECRET_WARNING_CHARS:
            logger.warning(
                "Secret '%s' resolved for %s is shorter than %d characters; short "
                "secrets risk accidental over-redaction across run output.",
                ref,
                consumer_label,
                _SHORT_SECRET_WARNING_CHARS,
            )
        self._redactor.register([value])
        self._cache[ref] = secret
        return secret

    def secret_for(self, ref: str) -> ResolvedSecret:
        """Read-only access to an already-resolved secret.

        Args:
            ref: The logical secret reference name.

        Returns:
            The cached resolved secret.

        Raises:
            ConfigurationError: If the reference has not been resolved in
                this run.
        """
        secret = self._cache.get(ref)
        if secret is None:
            raise ConfigurationError(
                f"Secret '{ref}' has not been resolved in this run.",
                suggestion="Resolve the reference through the config's secret index "
                "(or an explicit resolve) before reading its value.",
            )
        return secret

    def clear(self) -> None:
        """Drop every resolved secret from the cache.

        Called by the cache's owner once, after the run's final sinks have
        drained. The run-scoped redactor is owned by the run and is not
        cleared here.
        """
        self._cache.clear()


@dataclass(frozen=True)
class IndexedSecretUse:
    """One secret use-site recorded in a :class:`SecretUseIndex`.

    Carries the ``ref`` alongside the ``(delivery_kind, delivery_name)``
    pair: without the ref the value cannot be fetched from the cache, and
    without the kind/name the delivery target is ambiguous. Never holds
    plaintext — only the reference and delivery coordinates.
    """

    ref: str
    """Logical secret reference name resolved against the environment."""

    delivery_kind: Literal["env", "header"]
    """Delivery mechanism: process environment variable or HTTP header."""

    delivery_name: str
    """The concrete variable or header name the value is delivered under."""


def _delivery_parts(ref: StepSecretRef) -> tuple[Literal["env", "header"], str]:
    """Split a use-site's delivery block into its (kind, name) pair.

    ``SecretDelivery`` guarantees exactly one of ``env``/``header``; the
    second branch is defensive so a future schema change fails as a
    configuration error instead of propagating ``None``.
    """
    if ref.delivery.env is not None:
        return ("env", ref.delivery.env)
    header = ref.delivery.header
    if header is None:
        raise ConfigurationError(
            f"Secret reference '{ref.ref}' declares neither an env nor a header delivery target.",
            suggestion="Set exactly one of 'delivery.env' or 'delivery.header'.",
        )
    return ("header", header)


class SecretUseIndex:
    """Immutable per-config projection of secret use onto delivery targets.

    Owned by exactly one execution-resolver view: the root engine builds one
    over the run's cache, and every sub-workflow child builds its own. An
    index never mutates another index's maps — building a child index over a
    shared cache leaves the root's delivery maps untouched, and two child
    indexes over one cache are independent.

    The maps store :class:`IndexedSecretUse` records (refs, kinds, names) —
    never plaintext. Values are resolved on demand through the read-only
    cache link, so a cleared cache also empties every index built over it.

    Args:
        step_uses: Delivery records per executable-step identity key (bare
            step name or the ``for_each.<group>.agent`` qualified form).
        server_uses: Delivery records per MCP server name.
        cache: The run-scoped cache values are resolved through. The index
            only ever reads from it (``secret_for``/``value``); it never
            resolves new references or clears it.
    """

    def __init__(
        self,
        *,
        step_uses: Mapping[str, tuple[IndexedSecretUse, ...]],
        server_uses: Mapping[str, tuple[IndexedSecretUse, ...]],
        cache: SecretValueCache,
    ) -> None:
        """Initialize the index, freezing the delivery maps."""
        self._step_uses = MappingProxyType(dict(step_uses))
        self._server_uses = MappingProxyType(dict(server_uses))
        self._cache = cache

    def secret_env_for_step(self, key: str) -> dict[str, str]:
        """Environment-variable deliveries for one executable step.

        Args:
            key: The executable-step identity key (see
                ``run_manifest.executable_step_identity``).

        Returns:
            A fresh dict mapping delivery variable names to plaintext values,
            resolved through the cache at call time. Empty when the step has
            no env-kind secret uses.
        """
        return {
            use.delivery_name: self._cache.secret_for(use.ref).value
            for use in self._step_uses.get(key, ())
            if use.delivery_kind == "env"
        }

    def deliveries_for_server(self, name: str) -> tuple[IndexedSecretUse, ...]:
        """All secret deliveries declared for one MCP server.

        Args:
            name: The MCP server name from ``workflow.runtime.mcp_servers``.

        Returns:
            The delivery records for the server (both ``env`` and ``header``
            kinds), or an empty tuple when the server declares none.
        """
        return self._server_uses.get(name, ())

    def value_for(self, ref: str) -> str:
        """Read-only plaintext access to one resolved secret.

        The named operation MCP delivery (and any other consumer outside the
        step-env path) calls to fetch a value: it delegates to the cache
        rather than reaching into private state, and it never copies values
        into the index itself.

        Args:
            ref: The logical secret reference name.

        Returns:
            The plaintext value, for delivery to the declared consumer only.
        """
        return self._cache.secret_for(ref).value


def index_config(config: WorkflowConfig, cache: SecretValueCache) -> SecretUseIndex:
    """Build a :class:`SecretUseIndex` for one workflow config.

    Collects secret references from every executable step's
    ``execution.secrets`` (top-level steps keyed by bare name, inline
    for-each agents keyed by the ``for_each.<group>.agent`` qualified form —
    the same identity scheme the run manifest uses) and from every
    ``workflow.runtime.mcp_servers`` entry's ``secrets`` list, eagerly
    resolving each reference through ``cache`` so a bad reference fails fast
    for *this* config at index time rather than mid-execution.

    The step collection reuses ``run_manifest._iter_executable_steps`` so
    the identity keys and the duplicate-identity hard error stay identical
    between the manifest compiler and this index.

    Args:
        config: The workflow config to index (root or sub-workflow).
        cache: The run-scoped cache to resolve references through.

    Returns:
        The immutable index for this config.

    Raises:
        ConfigurationError: If any reference fails to resolve (unknown ref,
            allow violation, unset source variable, or a secret-less
            environment).
    """
    step_uses: dict[str, list[IndexedSecretUse]] = {}
    for key, step in _iter_executable_steps(config):
        execution = step.execution
        if execution is None:
            continue
        for ref in execution.secrets:
            cache.resolve(ref.ref, consumer_class=ref.scope, consumer_label=f"step '{key}'")
            kind, name = _delivery_parts(ref)
            step_uses.setdefault(key, []).append(
                IndexedSecretUse(ref=ref.ref, delivery_kind=kind, delivery_name=name)
            )

    server_uses: dict[str, list[IndexedSecretUse]] = {}
    agent_server_uses: list[IndexedSecretUse] = []
    for name, server in config.workflow.runtime.mcp_servers.items():
        for ref in server.secrets:
            cache.resolve(
                ref.ref,
                consumer_class=ref.scope,
                consumer_label=f"MCP server '{name}'",
            )
            kind, delivery_name = _delivery_parts(ref)
            if ref.scope == "agent":
                agent_server_uses.append(
                    IndexedSecretUse(ref=ref.ref, delivery_kind=kind, delivery_name=delivery_name)
                )
                continue
            server_uses.setdefault(name, []).append(
                IndexedSecretUse(ref=ref.ref, delivery_kind=kind, delivery_name=delivery_name)
            )

    if agent_server_uses:
        from conductor.config.schema import AgentDef
        from conductor.providers.resolution import provider_type_for_agent

        for key, step in _iter_executable_steps(config):
            if (
                isinstance(step, AgentDef)
                and step.tools != []
                and provider_type_for_agent(step, config.workflow.runtime.provider.name) != "hermes"
            ):
                step_uses.setdefault(key, []).extend(agent_server_uses)

    return SecretUseIndex(
        step_uses={key: tuple(uses) for key, uses in step_uses.items()},
        server_uses={name: tuple(uses) for name, uses in server_uses.items()},
        cache=cache,
    )
