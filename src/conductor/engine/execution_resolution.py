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
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from conductor.config.environment import AcaProfileOptions, ResolvedEnvironment
from conductor.config.schema import AgentDef, MCPStepDef, ScriptStepDef, WorkflowConfig
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
from conductor.exceptions import ConfigurationError, ExecutionError
from conductor.execution import (
    BundleRef,
    LocalRunnerBackend,
    ResolvedExecutionSpec,
    RunnerBackend,
    RunOutcome,
    RunSpec,
    WorkspaceIdentity,
    WorkspaceLease,
)
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.local_process_scope import LocalProcessScopeController
from conductor.execution.types import LocalWorkspaceBinding
from conductor.redaction import RunRedactor

if TYPE_CHECKING:
    from conductor.bundle.collector import CollectedAgentDependencies
    from conductor.engine.aca_execution import AcaGateway
    from conductor.providers.base import AgentProvider
    from conductor.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_QUARANTINED_SECRETS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, set[tuple[SecretValueCache, RunRedactor]]
] = WeakKeyDictionary()


def quarantined_secrets(
    loop: asyncio.AbstractEventLoop,
) -> tuple[tuple[SecretValueCache, RunRedactor], ...]:
    """Retain only secret pairs, not sessions that would keep a weak loop key alive."""
    return tuple(_QUARANTINED_SECRETS.get(loop, ()))


def release_loop_secrets(loop: asyncio.AbstractEventLoop) -> None:
    """Release retained scrubbers only after the CLI-owned loop has closed."""
    if not loop.is_closed():
        raise RuntimeError("Close the owned loop before releasing quarantined secrets")
    for cache, redactor in _QUARANTINED_SECRETS.pop(loop, ()):
        cache.clear()
        redactor.clear()


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
        # Run-local collector output, supplied with the prepared original lease.
        # Never serialize this view into BundleRef or a checkpoint.
        self.agent_dependencies: tuple[CollectedAgentDependencies, ...] | None = None
        self._aca_profiles: dict[str, RunnerBackend] = {}
        self._root_run_spec: RunSpec | None = None
        self._root_workflow_path: Path | None = None
        self._resume_identities: Mapping[str, WorkspaceIdentity] | None = None
        self._expect_staged: Mapping[str, bool] | None = None
        self._claim_active = False
        self._executed_backends: set[str] = set()
        self._file_consumers: dict[str, set[str]] = {}
        self._mixed_files_warned = False
        self.retained_participants: set[str] = set()
        self._local_consumer = False
        self.sealed = False
        self.cleanup_uncertain = False
        self.cli_cleanup_pending = False
        self.cleanup_deadline: float | None = None
        self._managed_binding: LocalWorkspaceBinding | None = None
        self._terminal_identities: dict[str, WorkspaceIdentity] = {}
        self._providers: list[AgentProvider] = []
        self._registry: ProviderRegistry | None = None
        self._resource_releases: dict[int, Callable[[], Awaitable[None]]] = {}
        self._resource_tasks: set[asyncio.Future[Any]] = set()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._cleanup_expired = asyncio.Event()
        self._cleanup_timer: asyncio.TimerHandle | None = None
        self.root_task: asyncio.Task[Any] | None = None
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
        if not self._owns_secrets or self.quarantined:
            return
        self.secret_cache.clear()
        self.redactor.clear()

    def begin_generation(self) -> None:
        """Refuse poisoned reuse and discard only the previous generation's views."""
        self.retain_registry_cleanup()
        if self.quarantined:
            raise ExecutionError(
                "Managed root cleanup is unconfirmed; this session cannot be reused."
            )
        self.sealed = False
        self.cleanup_deadline = None
        self._cleanup_expired = asyncio.Event()
        self._cleanup_task = None
        self._managed_binding = None
        self._terminal_identities = {}
        self.agent_dependencies = None
        self.retained_participants.clear()
        self._file_consumers.clear()
        self._mixed_files_warned = False
        self._local_consumer = False
        self._providers.clear()
        self._resource_releases.clear()
        self._root_run_spec = None
        self._registry = None
        self.root_task = None

    @property
    def quarantined(self) -> bool:
        """Whether either root resources or later CLI-owned leaves need a retained scrubber."""
        return self.cleanup_uncertain or self.cli_cleanup_pending

    def retain_registry_cleanup(self) -> None:
        """Keep pending owned registry leaves out of pools and alive through loop close."""
        if self._registry is None or not self.managed:
            return
        pending = self._registry.pending_close_tasks
        if pending:
            self.cli_cleanup_pending = True
            _QUARANTINED_SECRETS.setdefault(asyncio.get_running_loop(), set()).add(
                (self.secret_cache, self.redactor)
            )
            for task in pending:
                if task not in self._resource_tasks:
                    self._track(task)

    @property
    def managed(self) -> bool:
        """Whether this generation owns an actual retained-local binding."""
        return self._managed_binding is not None

    @property
    def run_bundle(self) -> BundleRef | None:
        """Return the original bundle, including defensive late preparation."""
        return self._root_run_spec.bundle if self._root_run_spec is not None else None

    def register_provider(self, provider: AgentProvider, registry: ProviderRegistry | None) -> None:
        """Register every attempted provider before its first resource dispatch."""
        if not self.managed:
            return
        if self.sealed:
            raise ExecutionError("Managed root admission is sealed.")
        if not any(item is provider for item in self._providers):
            self._providers.append(provider)
        self._registry = registry
        if registry is not None:
            assert self._managed_binding is not None
            registry.register_workspace_provider(self._managed_binding.runtime_context, provider)

    def register_resource(self, resource: object, release: Callable[[], Awaitable[None]]) -> None:
        """Account attempted native resources independently of successful caches."""
        if self.sealed:
            raise ExecutionError("Managed root admission is sealed.")
        self._resource_releases.setdefault(id(resource), release)

    def _track[T](self, task: asyncio.Future[T]) -> asyncio.Future[T]:
        self._resource_tasks.add(task)

        def finished(owner: asyncio.Future[T]) -> None:
            self._resource_tasks.discard(owner)
            if not owner.cancelled() and (error := owner.exception()) is not None:
                logger.warning(
                    "Managed resource finished with an error: %s", self.redactor.scrub(str(error))
                )

        task.add_done_callback(finished)
        return task

    async def invoke[T](
        self,
        coroutine: Coroutine[Any, Any, T],
        *,
        nested_timeout: asyncio.Timeout | None = None,
    ) -> T:
        """Own a resource leaf, never a detached engine orchestration continuation."""
        if not self.managed:
            return await coroutine
        if self.sealed:
            coroutine.close()
            raise ExecutionError("Managed root admission is sealed.")
        owner = self._track(asyncio.create_task(coroutine))
        try:
            return await asyncio.shield(owner)
        except asyncio.CancelledError:
            # A root cancellation seals immediately. A nested invocation timeout
            # keeps its adapter's invocation-scoped teardown and fail-open policy.
            root_requests = self.root_task.cancelling() if self.root_task is not None else 1
            nested_cancel = nested_timeout is not None and nested_timeout.expired()
            if root_requests and (not nested_cancel or root_requests > 1):
                self.begin_teardown("cancelled", retain=True)
            owner.cancel()
            raise

    async def close_cli_resource(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Attempt unrelated CLI cleanup without renewing a quarantined root's budget."""
        if not self.quarantined:
            await coroutine
            return
        assert self.cleanup_deadline is not None
        task = self._track(asyncio.create_task(coroutine))
        await asyncio.wait({task}, timeout=0)
        if not task.done():
            logger.warning("CLI cleanup owner remains pending under the original root deadline")
        elif not task.cancelled():
            task.result()

    def begin_teardown(self, outcome: RunOutcome, *, retain: bool) -> None:
        """Detach reusable leases and start all cleanup attempts without awaiting joins."""
        if self._cleanup_task is not None:
            return
        binding = self._managed_binding
        if binding is None:
            return
        self._terminal_identities = self.workspace_identities()
        self.sealed = True
        leases, self.leases = self.leases, {}
        invocations = tuple(self._resource_tasks)
        controller = binding.runtime_context.process_scopes
        loop = asyncio.get_running_loop()
        earlier = controller.owners.deadline()
        self.cleanup_deadline = loop.time() + 30
        if earlier is not None:
            self.cleanup_deadline = min(self.cleanup_deadline, earlier)
        controller.begin_teardown(deadline=self.cleanup_deadline)
        native_deadline = controller.owners.deadline()
        if native_deadline is not None:
            self.cleanup_deadline = min(self.cleanup_deadline, native_deadline)
        self._cleanup_timer = loop.call_at(self.cleanup_deadline, self._cleanup_expired.set)
        attempts: set[asyncio.Future[Any]] = set()
        finalized: set[int] = set()
        local = self.backends["local"]
        local_lease = leases["local"]
        if self._registry is not None:
            self._registry.account_workspace_cleanup(
                binding.workspace_root,
                deadline=self.cleanup_deadline,
                managed_workspace=binding.runtime_context,
                redactor=self.redactor,
            )
        for name, lease in leases.items():
            backend = self.backends[name]
            if id(backend) in finalized:
                continue
            finalized.add(id(backend))
            if backend is local:
                continue
            attempts.add(
                self._track(
                    asyncio.create_task(backend.finalize_run(lease, outcome, retain=retain))
                )
            )
        if self._registry is not None:
            attempts.add(
                self._track(
                    asyncio.create_task(self._registry.release_workspace(binding.workspace_root))
                )
            )
        else:
            for provider in self._providers:
                attempts.add(
                    self._track(
                        asyncio.create_task(provider.release_workspace(binding.workspace_root))
                    )
                )
        for release in self._resource_releases.values():
            attempts.add(self._track(asyncio.ensure_future(release())))
        for invocation in invocations:
            if not invocation.done():
                invocation.cancel()
        self._cleanup_task = asyncio.create_task(
            self._finish_managed(local, local_lease, outcome, retain, attempts)
        )

    async def _finish_managed(
        self,
        local: RunnerBackend,
        lease: WorkspaceLease,
        outcome: RunOutcome,
        retain: bool,
        attempts: set[asyncio.Future[Any]],
    ) -> None:
        binding = self._managed_binding
        assert binding is not None and isinstance(local, LocalRunnerBackend)
        assert self.cleanup_deadline is not None
        controller = binding.runtime_context.process_scopes
        proof_task = asyncio.create_task(controller.owners.wait_terminal())
        waiting = self._resource_tasks | attempts | {proof_task}
        expired = asyncio.create_task(self._cleanup_expired.wait())
        pending = set(waiting)
        while pending and not self._cleanup_expired.is_set():
            done, _ = await asyncio.wait(pending | {expired}, return_when=asyncio.FIRST_COMPLETED)
            pending.difference_update(done)
            if proof_task in done and proof_task.result().status != "released":
                break
        expired.cancel()
        if not proof_task.done():
            proof_task.cancel()
        await asyncio.wait({expired, proof_task}, timeout=0)
        proven = (
            proof_task.done()
            and not proof_task.cancelled()
            and proof_task.result().status == "released"
        )
        self.cleanup_uncertain = bool(pending) or not proven
        if not self.cleanup_uncertain:
            confirmation = self._track(asyncio.create_task(local.confirm_workspace_released(lease)))
            expiry = asyncio.create_task(self._cleanup_expired.wait())
            await asyncio.wait({confirmation, expiry}, return_when=asyncio.FIRST_COMPLETED)
            expiry.cancel()
            await asyncio.wait({expiry}, timeout=0)
            if (
                not confirmation.done()
                or confirmation.cancelled()
                or confirmation.exception() is not None
                or self._cleanup_expired.is_set()
            ):
                self.cleanup_uncertain = True
        if self.cleanup_uncertain:
            local.mark_workspace_uncertain(lease)
            _QUARANTINED_SECRETS.setdefault(asyncio.get_running_loop(), set()).add(
                (self.secret_cache, self.redactor)
            )
            logger.warning(
                "Managed root cleanup is unconfirmed; retaining workspace and refusing reuse: %s",
                self.redactor.scrub(binding.workspace_root),
            )
            if isinstance(controller, LocalProcessScopeController):
                self._track(asyncio.create_task(controller.close()))
        if self._registry is not None and self._providers:
            self._registry.account_workspace_cleanup(
                binding.workspace_root, uncertain=self.cleanup_uncertain
            )
        # Disposal has exactly one owner and one already-decided retain value.
        disposal = self._track(
            asyncio.create_task(
                local.finalize_run(lease, outcome, retain=retain or self.cleanup_uncertain)
            )
        )
        await asyncio.wait(
            {disposal},
            timeout=(
                0
                if self.cleanup_uncertain
                else max(0, self.cleanup_deadline - asyncio.get_running_loop().time())
            ),
        )
        if self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
        if not disposal.done():
            self.cleanup_uncertain = True
            local.mark_workspace_uncertain(lease)
            _QUARANTINED_SECRETS.setdefault(asyncio.get_running_loop(), set()).add(
                (self.secret_cache, self.redactor)
            )

    def set_agent_dependencies(self, dependencies: tuple[CollectedAgentDependencies, ...]) -> None:
        """Replace the generation's view with the same published bundle's topology."""
        self.agent_dependencies = dependencies

    def note_participants(self, config: WorkflowConfig, manifest: ResolvedRunManifest) -> None:
        """Distinguish actual file consumers from technical workflow placement."""
        steps = [(step.name, step) for step in config.agents]
        steps.extend(
            (executable_step_identity(group.agent.name, for_each_group=group.name), group.agent)
            for group in config.for_each
        )
        for key, step in steps:
            if not isinstance(step, (AgentDef, ScriptStepDef, MCPStepDef)):
                continue
            backend = manifest.profiles[key].backend
            if backend == "local":
                self._local_consumer = True
            if BACKEND_CAPABILITY_PROVIDERS[backend].capabilities().retained_workspace:
                self.retained_participants.add(backend)

    def note_known_children(self, root_config: WorkflowConfig, workflow_path: Path | None) -> None:
        """Preflight offline known children using the bundle collector's resolution."""
        if workflow_path is None:
            return
        from conductor.bundle.collector import MAX_SUBWORKFLOW_DEPTH, _Collector
        from conductor.bundle.errors import BundleError
        from conductor.config.loader import load_config
        from conductor.engine.bundle_prep import _workflow_steps

        resolver = _Collector(workflow_path, self.environment, False, logger.warning)
        visited: set[Path] = set()

        def scan(path: Path, depth: int) -> None:
            if path in visited or depth > MAX_SUBWORKFLOW_DEPTH:
                return
            visited.add(path)
            config = root_config if path == workflow_path else load_config(path)
            manifest = compile_run_manifest(
                config, workflow_path=path, environment=self.environment
            )
            self.note_participants(config, manifest)
            for step in _workflow_steps(config):
                try:
                    child, _ = resolver._resolve_subworkflow(step.workflow, path.parent)
                except BundleError:
                    # Bundle preparation owns the actionable incomplete-closure error.
                    continue
                scan(child, depth + 1)

        scan(workflow_path, 0)

    async def ensure_backends(self, names: Iterable[str]) -> None:
        """Instantiate and, after root preparation, lease requested backends.

        Child workflows compile their own manifests only when reached. A
        child-only backend is therefore discovered after the root leases have
        been prepared and must acquire a lease against the same root run spec.
        The lock spans construction, defensive bundle materialization, and
        ``prepare_run`` so concurrent for-each children cannot duplicate any
        of those run-scoped side effects.
        """
        if self.sealed:
            raise ExecutionError("Managed root admission is sealed.")
        names = set(names)
        missing = names.difference(self.backends)
        upgrade_local = (
            self._local_consumer
            and self._root_run_spec is not None
            and self._root_run_spec.workspace_persistence is not None
            and ("local" not in self.leases or self.leases["local"].location is None)
        )
        if not missing and not upgrade_local:
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
                            on_topology=self.set_agent_dependencies,
                        )
                        run_spec = replace(run_spec, bundle=bundle)
                        self._root_run_spec = run_spec
                    backend_spec = replace(
                        run_spec,
                        workspace_persistence=(
                            run_spec.workspace_persistence
                            if backend.capabilities().retained_workspace
                            else None
                        ),
                    )
                    identity = (self._resume_identities or {}).get(name)
                    if identity is not None:
                        lease = await backend.attach_run(
                            backend_spec,
                            identity,
                            expect_staged=(self._expect_staged or {}).get(name, False),
                        )
                    else:
                        if self._resume_identities is not None and name in self._executed_backends:
                            raise ConfigurationError(
                                f"Checkpoint is missing the required identity for {name!r}."
                            )
                        if self._resume_identities is not None and not self._claim_active:
                            raise ConfigurationError(
                                f"Cannot prepare late backend {name!r} without a workspace claim."
                            )
                        lease = await backend.prepare_run(backend_spec)
                    self.leases[name] = lease
                self.backends[name] = backend
            if upgrade_local and (
                "local" not in self.leases or self.leases["local"].location is None
            ):
                if not self._claim_active:
                    raise ConfigurationError("Cannot prepare managed local without a root claim.")
                run_spec = self._root_run_spec
                assert run_spec is not None
                if "local" in self._executed_backends and (
                    self._resume_identities is not None and "local" not in self._resume_identities
                ):
                    raise ConfigurationError("Checkpoint is missing the required local identity.")
                if run_spec.bundle is None:
                    bundle = await materialize_run_bundle(
                        self._root_workflow_path,
                        self.environment,
                        on_topology=self.set_agent_dependencies,
                    )
                    run_spec = replace(run_spec, bundle=bundle)
                    self._root_run_spec = run_spec
                backend = self.backends["local"]
                old = self.leases.pop("local", None)
                if old is not None:
                    await backend.finalize_run(old, "succeeded")
                spec = self._backend_run_spec("local", run_spec)
                identity = (self._resume_identities or {}).get("local")
                self.leases["local"] = (
                    await backend.attach_run(
                        spec,
                        identity,
                        expect_staged=(self._expect_staged or {}).get("local", False),
                    )
                    if identity is not None
                    else await backend.prepare_run(spec)
                )
                self.retained_participants.add("local")
                if isinstance(backend, LocalRunnerBackend):
                    self._managed_binding = backend.local_workspace_binding(self.leases["local"])
                    if self._managed_binding is not None:
                        controller = self._managed_binding.runtime_context.process_scopes
                        if isinstance(controller, LocalProcessScopeController):
                            controller.scrub = self.redactor.scrub
                        backend.mark_workspace_in_use(self.leases["local"])

    def _backend_run_spec(self, name: str, run: RunSpec) -> RunSpec:
        managed_local = name == "local" and self._local_consumer
        retained = self.backends[name].capabilities().retained_workspace and (
            name != "local" or managed_local
        )
        root = None
        if managed_local and run.workspace_persistence is not None:
            import os

            home = Path(os.environ.get("CONDUCTOR_HOME", Path.home() / ".conductor"))
            root = str((home / "workspaces" / run.run_id / "local").absolute())
        return replace(
            run,
            workspace_persistence=run.workspace_persistence if retained else None,
            local_workspace_root=root,
        )

    async def prepare_leases(
        self,
        run_spec: RunSpec,
        *,
        workflow_path: Path | None = None,
        resume_identities: Mapping[str, WorkspaceIdentity] | None = None,
        expect_staged: Mapping[str, bool] | None = None,
        claim_active: bool = False,
    ) -> None:
        """Prepare or attach one lease per distinct backend, idempotently."""
        unknown = set(resume_identities or {}).difference(self.backends | BACKEND_FACTORIES)
        if unknown:
            raise ConfigurationError(
                f"Workspace identities refer to unknown backends: {', '.join(sorted(unknown))}."
            )
        self._resume_identities = resume_identities
        self._expect_staged = expect_staged
        self._claim_active = claim_active
        self._root_run_spec = run_spec
        self._root_workflow_path = workflow_path
        if run_spec.local_workspace_root is not None:
            self._local_consumer = True
        leases_by_backend: dict[int, WorkspaceLease] = {
            id(self.backends[name]): lease for name, lease in self.leases.items()
        }
        for name, backend in self.backends.items():
            backend_spec = self._backend_run_spec(name, run_spec)
            if backend_spec.local_workspace_root is not None and not claim_active:
                raise ConfigurationError("Cannot prepare managed local without a root claim.")
            lease = leases_by_backend.get(id(backend))
            if lease is None:
                identity = (resume_identities or {}).get(name)
                if identity is not None:
                    lease = await backend.attach_run(
                        backend_spec,
                        identity,
                        expect_staged=(expect_staged or {}).get(name, False),
                    )
                else:
                    lease = await backend.prepare_run(backend_spec)
                leases_by_backend[id(backend)] = lease
            self.leases[name] = lease
            if backend_spec.workspace_persistence is not None:
                self.retained_participants.add(name)
            if (
                name == "local"
                and isinstance(backend, LocalRunnerBackend)
                and backend_spec.local_workspace_root is not None
            ):
                binding = backend.local_workspace_binding(lease)
                if (
                    binding is None
                    or binding.run_id != run_spec.run_id
                    or binding.workspace_root != backend_spec.local_workspace_root
                    or binding.bundle != run_spec.bundle
                ):
                    raise ConfigurationError(
                        "Prepared local lease does not belong to this root run."
                    )
                self._managed_binding = binding
                if self._managed_binding is not None:
                    controller = self._managed_binding.runtime_context.process_scopes
                    if isinstance(controller, LocalProcessScopeController):
                        controller.scrub = self.redactor.scrub
                    # The whole root is active even before a reachable local step.
                    # A later uncertain-marker write failure must leave active, not idle.
                    backend.mark_workspace_in_use(lease)

    def lease_for_backend(self, name: str) -> WorkspaceLease | None:
        """Return the prepared lease for ``name``, if one exists."""
        return self.leases.get(name)

    def record_backend_execution(self, name: str, *, step: str | None = None) -> None:
        """Remember a backend that executed a step in this run."""
        self._executed_backends.add(name)
        if step is None:
            return
        self._file_consumers.setdefault(name, set()).add(step)
        if (
            self._root_run_spec is not None
            and self._root_run_spec.workspace_persistence is not None
            and not self._mixed_files_warned
            and {"local", "docker"} <= self._file_consumers.keys()
        ):
            self._mixed_files_warned = True
            assignments = "; ".join(
                f"{realm}: {', '.join(sorted(self._file_consumers[realm]))}"
                for realm in ("local", "docker")
            )
            logger.warning(
                "%s",
                self.redactor.scrub(
                    f"Workflow uses independent local/Docker file workspaces ({assignments}). "
                    "There is no filesystem synchronization between realms; "
                    "context JSON outputs are shared unchanged."
                ),
            )

    def executed_backends(self) -> frozenset[str]:
        """Return the accumulated executed backend names."""
        return frozenset(self._executed_backends)

    def restore_executed_backends(self, names: Iterable[str]) -> None:
        """Merge checkpoint history without erasing work in this process."""
        self._executed_backends.update(names)

    def workspace_identities(self) -> dict[str, WorkspaceIdentity]:
        """Describe leases belonging to backends that support retention."""
        if self.sealed:
            return dict(self._terminal_identities)
        return {
            name: WorkspaceIdentity(
                backend=lease.backend,
                lease_id=lease.lease_id,
                incarnation=lease.incarnation,
                location=lease.location,
            )
            for name, lease in self.leases.items()
            if name in self.retained_participants
            and (name != "local" or self._managed_binding is not None)
        }

    async def finalize_leases(self, outcome: RunOutcome, *, retain: bool = False) -> None:
        """Best-effort finalize every prepared lease without masking outcomes.

        Leases are detached before cleanup starts, preventing a repeated run
        from reusing or double-finalizing a handle. Cleanup is shielded from a
        racing cancellation; cancellation is re-raised only after every
        backend has had its chance to finalize.
        """
        if self.managed:
            self.begin_teardown(outcome, retain=retain)
            assert self._cleanup_task is not None
            managed_cancel: asyncio.CancelledError | None = None
            while not self._cleanup_task.done():
                try:
                    await asyncio.shield(self._cleanup_task)
                except asyncio.CancelledError as error:
                    managed_cancel = managed_cancel or error
            self._cleanup_task.result()
            if managed_cancel is not None:
                raise managed_cancel
            return
        leases = self.leases
        self.leases = {}
        cancelled = False
        finalized_backends: set[int] = set()

        for name, lease in leases.items():
            backend = self.backends[name]
            if id(backend) in finalized_backends:
                continue
            finalized_backends.add(id(backend))
            finalize_task = asyncio.ensure_future(
                backend.finalize_run(lease, outcome, retain=retain)
            )
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
