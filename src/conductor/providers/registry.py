"""Provider registry for multi-provider workflow support.

This module provides the ProviderRegistry class for managing multiple
provider instances with lazy instantiation and caching.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from weakref import WeakKeyDictionary

from conductor import redaction
from conductor.config.schema import ProviderName
from conductor.exceptions import ProviderError
from conductor.providers.base import AgentProvider
from conductor.providers.factory import create_provider
from conductor.providers.resolution import provider_type_for_agent

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, ProviderSettings, RuntimeConfig, WorkflowConfig
    from conductor.execution.types import ManagedWorkspaceContext
    from conductor.redaction import RunRedactor


ProviderType = ProviderName

_WORKSPACE_RELEASE_TIMEOUT_SECONDS = 30.0
logger = logging.getLogger(__name__)
_WORKSPACE_RELEASE_OWNERS: WeakKeyDictionary[asyncio.AbstractEventLoop, set[asyncio.Task[None]]] = (
    WeakKeyDictionary()
)


@dataclass(slots=True)
class _WorkspaceRelease:
    """Accumulate root ownership and terminal release state, retaining late owners."""

    context: ManagedWorkspaceContext
    providers: list[AgentProvider] = field(default_factory=list)
    pending: set[asyncio.Task[None]] = field(default_factory=set)
    errors: list[BaseException] = field(default_factory=list)
    started: bool = False
    released: bool = False
    root_uncertain: bool = False
    deadline: float | None = None
    redactor: RunRedactor | None = None

    def observe(self, task: asyncio.Task[None]) -> None:
        """Observe completion once without granting late finalization rights."""
        if task not in self.pending:
            return
        self.pending.remove(task)
        if task.cancelled():
            self.errors.append(
                ProviderError("Workspace provider release task was cancelled", is_retryable=False)
            )
        else:
            error = task.exception()
            if error is not None:
                self.errors.append(error)
                scrubber = self.redactor or redaction.current()
                logger.warning(
                    "Workspace provider release failed: %s",
                    scrubber.scrub(str(error)) if scrubber is not None else str(error),
                )


class ProviderRegistry:
    """Manages multiple provider instances with lazy instantiation and caching.

    The ProviderRegistry enables multi-provider workflows where different
    agents can use different providers (e.g., Claude for research, Copilot
    for code generation). Providers are created lazily on first use and
    cached for subsequent agent executions.

    Example:
        >>> async with ProviderRegistry(config) as registry:
        ...     provider = await registry.get_provider(agent)
        ...     output = await executor.execute(agent, context)

    Key behaviors:
    - **Lazy creation**: Providers created on first agent that needs them
    - **Caching**: Same provider type reused across agents
    - **Serialized creation**: Concurrent agents resolving one provider type
      share a single instance; cached reads are lock-free
    - **Lifecycle management**: Closes all providers at workflow end
    """

    def __init__(
        self,
        config: WorkflowConfig,
        mcp_servers: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the ProviderRegistry.

        Args:
            config: The workflow configuration.
            mcp_servers: MCP server configurations to pass to providers.
        """
        self._config = config
        self._mcp_servers = mcp_servers
        self._providers: dict[ProviderType, AgentProvider] = {}
        # Serializes provider *creation* only. Without it,
        # ``_get_or_create_provider`` is a check-then-act with an ``await``
        # between the cache check and the cache write, so two agents
        # resolving one type concurrently could each construct an instance
        # and the second write replace the first — leaving them holding
        # different objects for the same provider type. One lock for all
        # types rather than one per type: creation happens a handful of
        # times per run, so serializing across types costs nothing
        # measurable and one lock is simpler to reason about. Cached reads
        # never take it.
        self._provider_lock = asyncio.Lock()
        self._default_provider_type: ProviderType = config.workflow.runtime.provider.name
        self._resume_session_ids: dict[str, str] = {}
        self._resume_session_cwds: dict[str, str] = {}
        self._workspace_releases: dict[str, _WorkspaceRelease] = {}
        self._close_tasks: set[asyncio.Task[None]] = set()

    @property
    def default_provider_type(self) -> ProviderType:
        """Get the default provider type from workflow config."""
        return self._default_provider_type

    @property
    def runtime(self) -> RuntimeConfig:
        """Return the root runtime used to configure providers in child workflows too."""
        return self._config.workflow.runtime

    @property
    def mcp_servers(self) -> dict[str, Any] | None:
        """Return the same resolved servers passed to locally constructed providers."""
        return self._mcp_servers

    def provider_type_for(self, agent: AgentDef) -> ProviderType:
        """Determine which provider type an agent should use.

        Args:
            agent: The agent definition.

        Returns:
            The provider type to use for this agent.
        """
        return cast(ProviderType, provider_type_for_agent(agent, self._default_provider_type))

    async def get_provider(
        self, agent: AgentDef, *, managed_workspace: ManagedWorkspaceContext | None = None
    ) -> AgentProvider:
        """Get the provider for an agent, creating it if necessary.

        This method resolves which provider the agent should use (based on
        agent.provider override or workflow default) and returns a cached
        or newly created provider instance.

        Args:
            agent: The agent definition.
            managed_workspace: Register before dispatch and defer native validation
                to the provider's owned execute path.

        Returns:
            The AgentProvider instance for this agent.

        Raises:
            ProviderError: If provider creation fails.
        """
        provider_type = self.provider_type_for(agent)
        if managed_workspace is not None:
            return await self._get_or_create_provider(
                provider_type, managed_workspace=managed_workspace
            )
        return await self._get_or_create_provider(provider_type)

    async def _get_or_create_provider(
        self,
        provider_type: ProviderType,
        *,
        managed_workspace: ManagedWorkspaceContext | None = None,
    ) -> AgentProvider:
        """Get or create a provider of the specified type.

        Args:
            provider_type: The provider type to get or create.
            managed_workspace: Original runtime context for a managed caller, if any.

        Returns:
            The AgentProvider instance.

        Raises:
            ProviderError: If provider creation fails.
        """
        # Lock-free fast path: an already-created provider needs no
        # serialization, so the steady state never contends.
        if managed_workspace is not None:
            managed_workspace.process_scopes.check_admission()
        cached = self._providers.get(provider_type)
        if cached is not None:
            if managed_workspace is not None:
                self.register_workspace_provider(managed_workspace, cached)
            return cached

        async with self._provider_lock:
            if managed_workspace is not None:
                managed_workspace.process_scopes.check_admission()
            # Re-check inside the lock: a caller that waited while another
            # was constructing must reuse that instance, not build a second.
            cached = self._providers.get(provider_type)
            if cached is not None:
                if managed_workspace is not None:
                    self.register_workspace_provider(managed_workspace, cached)
                return cached

            # Construction, resume-session wiring and the cache write all
            # happen under the lock, so no waiter can observe a provider
            # before its restored state is applied. A failure propagates
            # unchanged and caches nothing, leaving a retry free to proceed.
            runtime = self._config.workflow.runtime
            # Only forward structured provider settings to the matching
            # provider — settings for Copilot must not bleed into a per-agent
            # Claude override (and vice versa once Claude grows its own
            # structured config).
            provider_settings = runtime.provider if runtime.provider.name == provider_type else None
            provider = await create_provider(
                provider_type=provider_type,
                # Native validation belongs to execute(managed_workspace=...),
                # after the provider is registered, never to an unowned factory probe.
                validate=managed_workspace is None,
                mcp_servers=self._mcp_servers,
                default_model=runtime.default_model,
                temperature=runtime.temperature,
                max_tokens=runtime.max_tokens,
                timeout=runtime.timeout,
                max_session_seconds=runtime.max_session_seconds,
                max_agent_iterations=runtime.max_agent_iterations,
                idle_timeout_seconds=runtime.idle_timeout_seconds,
                max_idle_recovery_attempts=runtime.max_idle_recovery_attempts,
                default_reasoning_effort=runtime.default_reasoning_effort,
                provider_settings=provider_settings,
                tool_output=runtime.tool_output,
            )
            if managed_workspace is not None:
                self.register_workspace_provider(managed_workspace, provider)

            # Pass stored resume session IDs to newly created providers
            if self._resume_session_ids and hasattr(provider, "set_resume_session_ids"):
                provider.set_resume_session_ids(self._resume_session_ids)  # type: ignore[union-attr]
            if self._resume_session_cwds and hasattr(provider, "set_resume_session_cwds"):
                provider.set_resume_session_cwds(self._resume_session_cwds)  # type: ignore[union-attr]

            self._providers[provider_type] = provider
            return provider

    def provider_settings_for(self, provider_type: str) -> ProviderSettings | None:
        """Return the structured settings this registry constructs a provider with.

        Sub-workflow engines share this registry, so the answer reflects the
        root configuration that actually built the provider instance — not
        the child workflow's own ``runtime.provider``, which may carry no
        connection settings at all (e.g. an inherited external Copilot
        ``runtime_url``). Mirrors the matching-name rule in
        ``_get_or_create_provider``: settings apply only to the provider they
        name.
        """
        provider = self._config.workflow.runtime.provider
        return provider if provider.name == provider_type else None

    def register_workspace_provider(
        self, managed_workspace: ManagedWorkspaceContext, provider: AgentProvider
    ) -> None:
        """Account for a managed provider BEFORE execute/dialog/startup can await.

        Register injected providers too; membership is independent of the lazy
        provider cache and successful session pools. Adapters still own their
        attempted resources and must register launch scopes before payload start.
        Root strings are exact binding keys, not paths to normalize or discover.
        One incarnation/controller owns a root for this registry's lifetime.
        Registration is sealed by the first release attempt.
        """
        root = managed_workspace.root
        state = self._workspace_releases.get(root)
        if state is None:
            state = _WorkspaceRelease(managed_workspace, redactor=redaction.current())
            self._workspace_releases[root] = state
        if (
            state.context.incarnation != managed_workspace.incarnation
            or state.context.process_scopes is not managed_workspace.process_scopes
        ):
            raise ProviderError("Managed workspace ownership does not match", is_retryable=False)
        if state.started:
            raise ProviderError("Managed workspace registration is sealed", is_retryable=False)
        if not any(registered is provider for registered in state.providers):
            state.providers.append(provider)

    async def release_workspace(self, workspace_root: str) -> None:
        """Release registered providers for one root without globally closing them.

        All hooks start concurrently, including providers whose startup failed.
        Failures are raised as BaseExceptionGroup with original exceptions
        (ExceptionGroup when all failures are ordinary exceptions).
        Unknown roots fail closed; empty lazy caches never prove resource release.
        Cancellation propagates unchanged and marks release unconfirmed. A finite
        observation budget never awaits cancellation acknowledgement from an
        uncooperative hook. Pending tasks remain strongly owned and their eventual
        exceptions are recorded; terminal uncertainty cannot become success.

        This is the provider part of the root barrier, NOT process proof. The root
        owner must seal its controller and combine provider release with controller
        proof and owner joins under its original absolute teardown deadline.
        Adapters must bound their native teardown using that same context.
        """
        state = self._workspace_releases.get(workspace_root)
        if state is None:
            raise ProviderError("Managed workspace was not registered", is_retryable=False)
        if state.errors:
            raise BaseExceptionGroup(
                "Managed workspace provider release failed", list(state.errors)
            )
        if state.released:
            return
        if state.started:
            raise ProviderError(
                "Managed workspace release is already in progress", is_retryable=False
            )
        state.started = True
        if not state.providers:
            state.released = True
            return
        # The loop's quarantine outlives this registry if a native owner ignores
        # cancellation. Completion observes errors but cannot reopen the root.
        owners = _WORKSPACE_RELEASE_OWNERS.setdefault(asyncio.get_running_loop(), set())
        for provider in state.providers:
            task = asyncio.create_task(provider.release_workspace(workspace_root))
            state.pending.add(task)
            owners.add(task)
            task.add_done_callback(state.observe)
            task.add_done_callback(owners.discard)
        try:
            deadline = state.context.process_scopes.owners.deadline()
            timeout = _WORKSPACE_RELEASE_TIMEOUT_SECONDS
            if isinstance(deadline, (int, float)):
                timeout = min(timeout, max(0, deadline - asyncio.get_running_loop().time()))
            if state.deadline is not None:
                timeout = min(timeout, max(0, state.deadline - asyncio.get_running_loop().time()))
            done, pending = await asyncio.wait(state.pending, timeout=timeout)
            for task in done:
                state.observe(task)
            if pending:
                state.errors.append(
                    ProviderError("Workspace provider release deadline expired", is_retryable=False)
                )
                for task in pending:
                    task.cancel()
        except asyncio.CancelledError:
            state.errors.append(
                ProviderError(
                    "Workspace provider release is unconfirmed after cancellation",
                    is_retryable=False,
                )
            )
            for task in state.pending:
                task.cancel()
            raise
        if state.errors:
            raise BaseExceptionGroup(
                "Managed workspace provider release failed", list(state.errors)
            )
        state.released = True

    def account_workspace_cleanup(
        self,
        workspace_root: str,
        *,
        uncertain: bool = False,
        deadline: float | None = None,
        managed_workspace: ManagedWorkspaceContext | None = None,
        redactor: RunRedactor | None = None,
    ) -> None:
        """Make root uncertainty sticky for owned exit without another managed drain."""
        state = self._workspace_releases.get(workspace_root)
        if state is None:
            if managed_workspace is None or managed_workspace.root != workspace_root:
                raise ProviderError("Managed workspace was not registered", is_retryable=False)
            state = _WorkspaceRelease(managed_workspace)
            self._workspace_releases[workspace_root] = state
        state.root_uncertain |= uncertain
        if redactor is not None:
            state.redactor = redactor
        if deadline is not None:
            state.deadline = (
                min(state.deadline, deadline) if state.deadline is not None else deadline
            )

    @property
    def pending_close_tasks(self) -> tuple[asyncio.Task[None], ...]:
        """Expose owned leaves for CLI accounting, never as reusable provider handles."""
        return tuple(task for task in self._close_tasks if not task.done())

    async def close(self) -> None:
        """Close all provider instances.

        This method should be called when the workflow completes to
        clean up all provider resources.
        """
        errors: list[Exception] = []
        deadlines = [
            state.deadline
            for state in self._workspace_releases.values()
            if state.deadline is not None
        ]
        quarantined = {
            id(provider)
            for state in self._workspace_releases.values()
            if state.root_uncertain
            or state.pending
            or (state.started and not state.released)
            or state.deadline is None
            for provider in state.providers
        }
        if deadlines:
            providers, self._providers = self._providers, {}
            owners = _WORKSPACE_RELEASE_OWNERS.setdefault(asyncio.get_running_loop(), set())

            def observed(task: asyncio.Task[None]) -> None:
                self._close_tasks.discard(task)
                owners.discard(task)
                if task.cancelled() or task.exception() is None:
                    return
                message = str(task.exception())
                for state in self._workspace_releases.values():
                    if state.redactor is not None:
                        message = state.redactor.scrub(message)
                logger.warning("Owned provider close failed after managed release: %s", message)

            for provider in {id(item): item for item in providers.values()}.values():
                if id(provider) in quarantined:
                    continue
                task = asyncio.create_task(provider.close())
                self._close_tasks.add(task)
                owners.add(task)
                task.add_done_callback(observed)
            if self._close_tasks:
                _, pending = await asyncio.wait(
                    set(self._close_tasks),
                    timeout=(
                        0
                        if any(state.root_uncertain for state in self._workspace_releases.values())
                        else max(0, min(deadlines) - asyncio.get_running_loop().time())
                    ),
                )
                if pending:
                    raise ProviderError(
                        "Owned provider cleanup is unconfirmed under the root deadline",
                        is_retryable=False,
                    )
            return
        for _provider_type, provider in self._providers.items():
            if id(provider) in quarantined:
                continue
            try:
                await provider.close()
            except Exception as e:
                # Collect errors but continue closing other providers
                errors.append(e)

        self._providers.clear()

        # Re-raise the first error if any occurred
        if errors:
            raise errors[0]

    async def __aenter__(self) -> ProviderRegistry:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit - closes all providers."""
        try:
            await self.close()
        except Exception as error:
            if exc_val is None:
                raise
            scrubber = redaction.current()
            logger.warning(
                "Provider registry close failed while preserving the primary error: %s",
                scrubber.scrub(str(error)) if scrubber is not None else str(error),
            )

    def get_active_providers(self) -> dict[ProviderType, AgentProvider]:
        """Get all currently active providers.

        Returns:
            Dictionary mapping provider type to provider instance.
        """
        return self._providers.copy()

    def is_provider_active(self, provider_type: ProviderType) -> bool:
        """Check if a provider type is currently active.

        Args:
            provider_type: The provider type to check.

        Returns:
            True if the provider is active, False otherwise.
        """
        return provider_type in self._providers

    def set_resume_session_ids(self, ids: dict[str, str]) -> None:
        """Store restored session IDs for provider session resume.

        The IDs are forwarded to providers that support
        ``set_resume_session_ids`` — both already-active providers
        and providers created lazily in the future.

        Args:
            ids: Merged provider session map from the checkpoint. It is shared
                by every provider, so each picks out the entries it recognises
                and ignores the rest.
        """
        self._resume_session_ids = dict(ids)
        for provider in self._providers.values():
            if hasattr(provider, "set_resume_session_ids"):
                provider.set_resume_session_ids(ids)  # type: ignore[union-attr]

    def set_resume_session_cwds(self, cwds: dict[str, str]) -> None:
        """Store session working directories restored from a checkpoint.

        Forwarded to providers that support ``set_resume_session_cwds`` —
        both already-active providers and providers created lazily in the
        future. The mapping lets a provider skip resuming a session whose
        working directory no longer matches the agent's resolved cwd.

        Args:
            cwds: Mapping of agent names to session working directories.
        """
        self._resume_session_cwds = dict(cwds)
        for provider in self._providers.values():
            if hasattr(provider, "set_resume_session_cwds"):
                provider.set_resume_session_cwds(cwds)  # type: ignore[union-attr]
