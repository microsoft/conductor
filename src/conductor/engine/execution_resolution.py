"""Run-shared execution state and per-engine resolution views.

An :class:`ExecutionResolverSession` owns backend instances and workspace
leases for one run. Each root or child workflow gets its own
:class:`ExecutionResolver` view over that shared session, so nested workflow
configuration is compiled only when the child engine is constructed while all
steps in the run still use the same backend instances and leases.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from conductor.config.environment import AcaProfileOptions, ResolvedEnvironment
from conductor.config.schema import WorkflowConfig
from conductor.engine.bundle_prep import materialize_run_bundle
from conductor.engine.run_manifest import (
    BACKEND_CAPABILITY_PROVIDERS,
    ManifestExecutionSpec,
    ResolvedRunManifest,
    compile_run_manifest,
    executable_step_identity,
)
from conductor.engine.secrets import (
    IndexedSecretUse,
    SecretUseIndex,
    SecretValueCache,
    index_config,
)
from conductor.exceptions import ConfigurationError
from conductor.execution import (
    LocalRunnerBackend,
    ResolvedExecutionSpec,
    RunnerBackend,
    RunOutcome,
    RunSpec,
    WorkspaceLease,
)
from conductor.execution.docker import DockerRunnerBackend
from conductor.redaction import RunRedactor

if TYPE_CHECKING:
    from conductor.engine.aca_execution import AcaGateway

logger = logging.getLogger(__name__)


def _aca_transport(options: AcaProfileOptions) -> AcaGateway:
    from conductor.config.schema import ProviderSettings
    from conductor.providers.aca import AcaRuntimeProvider

    return AcaRuntimeProvider(
        provider_settings=ProviderSettings(
            name="aca", **options.model_dump(mode="python", exclude_none=True)
        )
    )


def _aca_backend() -> RunnerBackend:
    # The optional ACA SDK is consulted only when this backend is first used.
    from conductor.engine.aca_execution import AcaRunnerBackend

    return AcaRunnerBackend(transport_factory=_aca_transport)


BACKEND_FACTORIES: dict[str, Callable[[], RunnerBackend]] = {
    "local": LocalRunnerBackend,
    # The Docker backend construction is I/O-free (it only stores the binary
    # name and an environment snapshot), so registering the factory here is
    # safe: no daemon contact happens until a docker-backed command runs.
    "docker": DockerRunnerBackend,
    "aca": _aca_backend,
}

# A capability lookup must not import the optional HTTP or Azure SDK.
BACKEND_CAPABILITY_PROVIDERS.setdefault("aca", _aca_backend())


def _spec_to_contract(spec: ManifestExecutionSpec) -> ResolvedExecutionSpec:
    """Adapt the manifest audit model to the stdlib-only execution contract.

    The ``conductor.execution`` leaf must not import the manifest's Pydantic
    models, so this adapter — living at the engine layer that owns both sides —
    copies the normalized requested configuration field by field.
    """
    return ResolvedExecutionSpec(
        image=spec.image,
        platform=spec.platform,
        network=spec.network,
        user=spec.user,
        init=spec.init,
        read_only=spec.read_only,
        cap_drop_all=spec.cap_drop_all,
        no_new_privileges=spec.no_new_privileges,
        tmpfs=spec.tmpfs,
        cpu=spec.cpu,
        memory=spec.memory,
        pids=spec.pids,
    )


class ExecutionResolverSession:
    """Run-shared backend instances and their prepared workspace leases."""

    def __init__(
        self,
        environment: ResolvedEnvironment,
        default_backend: RunnerBackend | None = None,
        secret_cache: SecretValueCache | None = None,
        redactor: RunRedactor | None = None,
    ) -> None:
        """Create a session seeded with the run's local backend instance.

        Secret-primitive pairing: when a ``secret_cache`` is injected without
        a redactor, the session redactor is derived from the cache (the one
        its values are registered into) so runtime sinks scrub exactly the
        values the cache resolved. An explicitly injected ``(cache, redactor)``
        pair whose two members are not the same objects is a wiring bug:
        delivered values would be registered into one redactor while sinks
        scrub with the other, so it is rejected here rather than discovered
        as an unredacted leak later.
        """
        self.environment = environment
        self.backends: dict[str, RunnerBackend] = {
            "local": default_backend or BACKEND_FACTORIES["local"](),
        }
        self.leases: dict[str, WorkspaceLease] = {}
        self._aca_profiles: dict[str, RunnerBackend] = {}
        self._root_run_spec: RunSpec | None = None
        self._root_workflow_path: Path | None = None
        self._ensure_lock = asyncio.Lock()
        self._owns_secrets = secret_cache is None
        if secret_cache is not None:
            if redactor is not None and secret_cache.redactor is not redactor:
                raise ValueError(
                    "secret_cache and redactor must belong to the same run pair "
                    "(the cache's redactor is the one its resolved values are "
                    "registered into); derive the session redactor from the "
                    "injected cache instead of passing a separate instance."
                )
            self._redactor = secret_cache.redactor
        else:
            self._redactor = redactor
        self._secret_cache = secret_cache

    @property
    def owns_secrets(self) -> bool:
        """Whether this session owns secret cleanup rather than its caller."""
        return self._owns_secrets

    @property
    def redactor(self) -> RunRedactor:
        """Return the run redactor, creating the self-owned pair lazily."""
        self._ensure_secrets()
        assert self._redactor is not None
        return self._redactor

    @property
    def secret_cache(self) -> SecretValueCache:
        """Return the run secret cache, creating the self-owned pair lazily."""
        self._ensure_secrets()
        assert self._secret_cache is not None
        return self._secret_cache

    def _ensure_secrets(self) -> None:
        """Create missing run-scoped secret primitives for direct engine users."""
        if self._redactor is None:
            self._redactor = RunRedactor()
        if self._secret_cache is None:
            self._secret_cache = SecretValueCache(self.environment, self._redactor)

    def reset_secrets(self) -> None:
        """Clear self-owned secret state before a new run generation."""
        if not self._owns_secrets:
            return
        self.secret_cache.clear()
        self.redactor.clear()

    async def ensure_backends(self, names: Iterable[str]) -> None:
        """Instantiate and, after root preparation, lease requested backends.

        Child workflows compile their own manifests only when reached. A
        child-only backend is therefore discovered after the root leases have
        been prepared and must acquire a lease against the same root run spec.
        The lock spans construction, defensive bundle materialization, and
        ``prepare_run`` so concurrent for-each children cannot duplicate any
        of those run-scoped side effects.
        """
        missing = set(names).difference(self.backends)
        if not missing:
            return

        async with self._ensure_lock:
            for name in sorted(missing):
                if name in self.backends:
                    continue
                factory = BACKEND_FACTORIES.get(name)
                if factory is None:
                    raise ConfigurationError(
                        f"Execution backend '{name}' is unknown in this build of Conductor.",
                        suggestion=(
                            "Install or register a backend factory for this execution profile."
                        ),
                    )

                backend = factory()
                if name == "aca":
                    from conductor.engine.aca_execution import AcaRunnerBackend

                    profiles = {
                        profile_name: profile.aca
                        for profile_name, profile in sorted(
                            self.environment.document.profiles.items()
                        )
                        if profile.backend == "aca" and profile.aca is not None
                    }
                    if isinstance(backend, AcaRunnerBackend):
                        self._aca_profiles.update(backend.bind_profiles(profiles))
                    else:
                        self._aca_profiles.update(dict.fromkeys(profiles, backend))
                run_spec = self._root_run_spec
                if run_spec is not None and name != "local":
                    if run_spec.bundle is None and backend.capabilities().shared_workspace:
                        bundle = await materialize_run_bundle(
                            self._root_workflow_path,
                            self.environment,
                        )
                        run_spec = replace(run_spec, bundle=bundle)
                        self._root_run_spec = run_spec
                    lease = await backend.prepare_run(run_spec)
                    self.leases[name] = lease
                self.backends[name] = backend

    async def prepare_leases(
        self,
        run_spec: RunSpec,
        *,
        workflow_path: Path | None = None,
    ) -> None:
        """Prepare one lease per distinct backend, idempotently."""
        self._root_run_spec = run_spec
        self._root_workflow_path = workflow_path
        leases_by_backend: dict[int, WorkspaceLease] = {
            id(self.backends[name]): lease for name, lease in self.leases.items()
        }
        for name, backend in self.backends.items():
            lease = leases_by_backend.get(id(backend))
            if lease is None:
                lease = await backend.prepare_run(run_spec)
                leases_by_backend[id(backend)] = lease
            self.leases[name] = lease

    def lease_for_backend(self, name: str) -> WorkspaceLease | None:
        """Return the prepared lease for ``name``, if one exists."""
        return self.leases.get(name)

    async def finalize_leases(self, outcome: RunOutcome) -> None:
        """Best-effort finalize every prepared lease without masking outcomes.

        Leases are detached before cleanup starts, preventing a repeated run
        from reusing or double-finalizing a handle. Cleanup is shielded from a
        racing cancellation; cancellation is re-raised only after every
        backend has had its chance to finalize.
        """
        leases = self.leases
        self.leases = {}
        cancelled = False
        finalized_backends: set[int] = set()

        for name, lease in leases.items():
            backend = self.backends[name]
            if id(backend) in finalized_backends:
                continue
            finalized_backends.add(id(backend))
            finalize_task = asyncio.ensure_future(backend.finalize_run(lease, outcome))
            while True:
                try:
                    await asyncio.shield(finalize_task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if finalize_task.done():
                        break
                except Exception:
                    logger.warning(
                        "Execution backend '%s' finalize_run failed (outcome=%s); "
                        "the run outcome is unaffected.",
                        name,
                        outcome,
                        exc_info=True,
                    )
                    break

        if self._owns_secrets:
            self.reset_secrets()
        if cancelled:
            raise asyncio.CancelledError


class ExecutionResolver:
    """Per-engine execution-resolution view over a run-shared session."""

    def __init__(
        self,
        config: WorkflowConfig,
        session: ExecutionResolverSession,
        *,
        workflow_path: Path | None,
        publish_manifest: bool,
    ) -> None:
        """Compile this engine's step map immediately from its own config."""
        self._session = session
        self._config = config
        self.publish_manifest = publish_manifest
        self._manifest = compile_run_manifest(
            config,
            workflow_path=workflow_path,
            environment=session.environment,
        )
        self._secret_uses = index_config(config, session.secret_cache)

    @property
    def manifest(self) -> ResolvedRunManifest:
        """Return this engine view's compiled execution manifest."""
        return self._manifest

    def backend_for_step(
        self,
        name: str,
        *,
        for_each_group: str | None = None,
    ) -> RunnerBackend:
        """Return the backend resolved for a top-level or inline step."""
        key = executable_step_identity(name, for_each_group=for_each_group)
        backend_name = self._manifest.profiles[key].backend
        if backend_name == "aca":
            profile = self._manifest.profiles[key].profile
            return self._session._aca_profiles[profile]
        return self._session.backends[backend_name]

    def refresh_secret_uses(self) -> None:
        """Re-resolve this view's secret uses for a new run generation."""
        self._secret_uses = index_config(self._config, self._session.secret_cache)

    def inherit_env_for_step(
        self,
        name: str,
        *,
        for_each_group: str | None = None,
    ) -> bool:
        """Return the compiled control-environment inheritance policy."""
        key = executable_step_identity(name, for_each_group=for_each_group)
        return self._manifest.profiles[key].inherit_control_environment

    def execution_spec_for_step(
        self,
        name: str,
        *,
        for_each_group: str | None = None,
    ) -> ResolvedExecutionSpec | None:
        """Return the compiled container execution payload for one step.

        ``None`` means the step runs on its backend's default execution (the
        local path). The manifest guarantees backend/payload consistency: a
        payload is only present when the step's profile resolved to a
        container backend at compile time.
        """
        key = executable_step_identity(name, for_each_group=for_each_group)
        execution = self._manifest.profiles[key].execution
        if execution is None:
            return None
        return _spec_to_contract(execution)

    def secret_env_for_step(
        self,
        name: str,
        *,
        for_each_group: str | None = None,
    ) -> dict[str, str]:
        """Return this view's environment deliveries for one step."""
        key = executable_step_identity(name, for_each_group=for_each_group)
        return self._secret_uses.secret_env_for_step(key)

    def deliveries_for_server(self, name: str) -> tuple[IndexedSecretUse, ...]:
        """Return this view's secret deliveries for one MCP server."""
        return self._secret_uses.deliveries_for_server(name)

    @property
    def secret_uses(self) -> SecretUseIndex:
        """This view's per-config secret-use index.

        Exposed so MCP configuration resolution (``type: mcp`` steps connect
        in the engine, not the CLI) can deliver this config's declared secret
        values, exactly as ``_build_mcp_servers`` does for the provider
        connection path. Values are read through the cache at delivery time;
        the index itself never holds plaintext.
        """
        return self._secret_uses
