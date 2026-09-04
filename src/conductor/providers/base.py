"""Abstract base class for SDK providers.

This module defines the AgentProvider ABC and AgentOutput dataclass that
all provider implementations must use to ensure a consistent interface.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from conductor.exceptions import ProviderError

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef
    from conductor.engine.pricing import ModelPricing
    from conductor.providers.capabilities import ProviderCapabilities

# Type alias for event callbacks that receive structured SDK events.
# Callback signature: (event_type: str, data: dict[str, Any]) -> None
EventCallback = Callable[[str, dict[str, Any]], None]


# Suffixes that providers may strip when matching aliased model names against
# their SDK's canonical IDs (e.g. "claude-3-5-sonnet-latest" -> base name).
_VERSION_SUFFIX_RE = re.compile(r"-(\d{8}|latest|preview)$")


def refuse_mcp_server_clashes(
    plugin_servers: Iterable[str], workflow_servers: Iterable[str]
) -> None:
    """Refuse a plugin MCP server whose name a workflow server already claims.

    One phrasing for every provider, so two providers cannot describe the
    same clash two ways — the same reason
    :func:`~conductor.plugins.registry.describe_dropped_components` is
    shared between ``conductor validate`` and ``conductor run``.

    A collision is refused, not resolved by precedence: the server name
    prefixes the tool names the model sees, so one of the two would be
    unreachable, and silently dropping a declared component is the
    failure plugins exist to remove. ``conductor validate`` reports the
    same clash, but ``conductor run`` never invokes the static
    validator, so the guard has to exist at the provider seam too.

    Args:
        plugin_servers: Server names contributed by enabled plugins.
        workflow_servers: Server names from ``runtime.mcp_servers``.

    Raises:
        ProviderError: If any name appears in both.
    """
    clashes = sorted(set(plugin_servers) & set(workflow_servers))
    if clashes:
        raise ProviderError(
            f"MCP server name(s) {clashes!r} are declared by both an enabled "
            f"plugin and the workflow's 'runtime.mcp_servers'. The server name "
            f"prefixes the tool names the model sees, so one would be unreachable.",
            suggestion="Rename the workflow's server, or set 'mcp: false' on the plugin.",
            is_retryable=False,
        )


def match_model_id(requested: str, known_ids: Iterable[str]) -> str | None:
    """Find the canonical SDK ID matching a possibly aliased model name.

    Match strategies, in order:

    1. Exact match.
    2. Boundary prefix match (longest first), in either direction. Handles
       both ``"claude-3-5-sonnet-20241022"`` for requested
       ``"claude-3-5-sonnet"`` *and* the reverse, where the SDK lists a
       dated ID and the user specified the base name.
    3. Suffix-strip (``-YYYYMMDD``, ``-latest``, ``-preview``) on the
       requested name, then re-try strategies 1 and 2.

    Returns the matching SDK ID, or ``None`` if no strategy succeeds.
    """
    ids = [str(i) for i in known_ids]
    if not ids:
        return None
    if requested in ids:
        return requested
    sorted_ids = sorted(ids, key=lambda s: len(s), reverse=True)
    for known in sorted_ids:
        if requested.startswith(known + "-") or known.startswith(requested + "-"):
            return known
    simplified = _VERSION_SUFFIX_RE.sub("", requested)
    if simplified == requested:
        return None
    if simplified in ids:
        return simplified
    for known in sorted_ids:
        if simplified.startswith(known + "-") or known.startswith(simplified + "-"):
            return known
    return None


@dataclass
class AgentOutput:
    """Normalized output from any SDK provider.

    Provides a consistent interface for agent execution results regardless
    of the underlying provider (Copilot, OpenAI, Claude, etc.).

    Attributes:
        content: Parsed structured output matching the agent's output schema.
        raw_response: Provider-specific raw response for debugging/logging.
        tokens_used: Total token count (input + output) if provided by the SDK.
        input_tokens: Total prompt tokens summed across every API call,
            **inclusive** of ``cache_read_tokens`` and ``cache_write_tokens``.
        output_tokens: Number of output/completion tokens generated.
        cache_read_tokens: Tokens read from cache (prompt caching). A subset
            of ``input_tokens``, not an addition to it.
        cache_write_tokens: Tokens written to cache (prompt caching). A subset
            of ``input_tokens``, not an addition to it.
        last_call_input_tokens: Prompt tokens of the most recent single API
            call in this execution. A point-in-time context measurement,
            unlike ``input_tokens`` which sums every call for billing.
            ``None`` when the provider cannot isolate one call, in which case
            the dashboard hides the context-window bar (issue #412).
        model: Actual model used (may differ from requested if aliased).
        session_seconds: Sandbox wall-clock time reported by a remote-runtime
            provider (e.g. ``aca``), separate from token cost (FR7). ``None``
            for providers that execute on-host and have no distinct sandbox
            time to report.
    """

    content: dict[str, Any]
    """Parsed structured output matching the agent's output schema."""

    raw_response: Any
    """Provider-specific raw response for debugging/logging."""

    tokens_used: int | None = None
    """Total token count (input + output) if provided by the SDK."""

    input_tokens: int | None = None
    """Total prompt tokens summed across every API call in this execution.

    **Inclusive** of :attr:`cache_read_tokens` and :attr:`cache_write_tokens`
    — the cached buckets are subsets of this figure, not additions to it.
    Providers whose SDK reports cached tokens *outside* its own input counter
    (the raw Anthropic shape) must add them in before populating this field,
    so one convention holds across providers and
    :func:`conductor.engine.pricing.calculate_cost` can price each physical
    token exactly once.
    """

    output_tokens: int | None = None
    """Number of output/completion tokens generated."""

    cache_read_tokens: int | None = None
    """Tokens read from cache (prompt caching); a subset of
    :attr:`input_tokens`."""

    cache_write_tokens: int | None = None
    """Tokens written to cache (prompt caching); a subset of
    :attr:`input_tokens`."""

    last_call_input_tokens: int | None = None
    """Prompt tokens of the most recent single API call in this execution.

    A point-in-time context measurement, unlike ``input_tokens`` which sums
    every call for billing. ``None`` when the provider cannot isolate one
    call, in which case the dashboard hides the context-window bar (issue
    #412).
    """

    model: str | None = None
    """Actual model used (may differ from requested if aliased)."""

    partial: bool = False
    """Whether this output is partial (from a mid-agent interrupt)."""

    session_seconds: float | None = None
    """Sandbox wall-clock time reported by a remote-runtime provider (issue #284,
    FR7). ``None`` for providers with no distinct sandbox time to report."""


@dataclass(frozen=True)
class ModelCapabilityInfo:
    """Provider-reported reasoning-effort and context-window metadata for a model.

    Returned by the optional :meth:`AgentProvider.get_model_capabilities` hook
    (see issue #301) and surfaced by ``conductor doctor --models``. Every field
    is best-effort and independently optional — a provider that can report
    token limits but not reasoning-effort support (or vice versa) should
    leave the unknown fields at their ``None`` default rather than omit the
    whole object.
    """

    supported_reasoning_efforts: list[str] | None = None
    """Reasoning-effort levels the model accepts, or ``None`` when unknown.

    An empty list is a meaningful, distinct value: it means the provider
    positively knows the model supports *no* reasoning-effort levels
    (e.g. a non-thinking Claude model), whereas ``None`` means the provider
    could not determine support either way.
    """

    default_reasoning_effort: str | None = None
    """The model's default reasoning-effort level, or ``None`` when unknown
    or not applicable."""

    max_prompt_tokens: int | None = None
    """Maximum prompt (input) tokens, or ``None`` when unknown."""

    max_output_tokens: int | None = None
    """Maximum output (completion) tokens, or ``None`` when unknown."""

    max_context_window_tokens: int | None = None
    """Maximum total context window (prompt + output) tokens, or ``None``
    when unknown."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "supported_reasoning_efforts": self.supported_reasoning_efforts,
            "default_reasoning_effort": self.default_reasoning_effort,
            "max_prompt_tokens": self.max_prompt_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_context_window_tokens": self.max_context_window_tokens,
        }


class AgentProvider(ABC):
    """Abstract base class for SDK providers.

    Providers translate between the normalized Conductor interface
    and specific SDK implementations (Copilot, OpenAI, Claude).

    Implementations must provide:
    - execute(): Run an agent and return normalized output
    - validate_connection(): Verify backend connectivity
    - close(): Clean up resources
    - CAPABILITIES: class-level :class:`ProviderCapabilities` descriptor

    Every production provider MUST declare a class-level ``CAPABILITIES``
    attribute so that ``conductor validate`` can statically cross-check
    workflow features against provider behavior. See issue #241 and
    :mod:`conductor.providers.capabilities` for the schema.

    Example:
        >>> from conductor.providers.capabilities import ProviderCapabilities
        >>> class MyProvider(AgentProvider):
        ...     CAPABILITIES = ProviderCapabilities(
        ...         tier="stable",
        ...         mcp_tools=True,
        ...         ...,
        ...     )
        ...     async def execute(self, agent, context, rendered_prompt, tools=None):
        ...         # Call SDK and return AgentOutput
        ...         pass
        ...     async def validate_connection(self):
        ...         return True
        ...     async def close(self):
        ...         pass

    Test fakes / mocks that don't need a real capability declaration can
    opt out at subclass-definition time with ``abstract=True``:

        >>> class _FakeProvider(AgentProvider, abstract=True):
        ...     async def execute(self, *a, **kw): ...
        ...     async def validate_connection(self): return True
        ...     async def close(self): ...

    Production subclasses (no ``abstract=True``) MUST set ``CAPABILITIES``
    to a :class:`ProviderCapabilities` instance — enforced at import time
    via :meth:`__init_subclass__`.
    """

    # Subclasses MUST override with their declared descriptor.
    # Typed as Optional so the abstract base itself can declare ``None``;
    # __init_subclass__ enforces the override on every non-abstract
    # subclass at import time.
    CAPABILITIES: ClassVar[ProviderCapabilities | None] = None

    @property
    def supports_native_skills(self) -> bool:
        """Whether the provider loads skill content natively.

        When ``True``, the :class:`~conductor.executor.agent.AgentExecutor`
        passes resolved skill directories to :meth:`execute` via
        ``skill_directories`` and skips eager preamble injection — the
        provider's SDK is expected to discover and load skill content
        itself (e.g. Copilot's session-level ``skill_directories``, or
        the claude-agent-sdk's plugin-scoped ``skills`` option).

        When ``False`` (default), the executor eagerly injects the full
        ``SKILL.md`` plus ``references/*.md`` content into the agent's
        rendered prompt — appropriate for providers like Claude where
        there is no server-side skill surface without adopting the
        container/code-execution beta.

        Providers MUST also declare ``skills=True`` on their
        :class:`ProviderCapabilities` descriptor regardless of which
        mechanism they use — the capability flag asserts the user-facing
        contract ("the agent has access to the named skill"), this
        property selects the mechanism.
        """
        return False

    @property
    def supports_native_plugins(self) -> bool:
        """Whether the provider can register a plugin's subagents.

        Plugins are **deconstructed** rather than handed to the SDK
        whole: skills reach the provider through ``skill_directories``
        (which :attr:`supports_native_skills` already governs), MCP
        servers through ``extra_mcp_servers``, and subagents through
        ``custom_agents``. This property covers the last of those,
        because it is the one surface with no fallback — a subagent
        cannot be approximated by injecting text into a prompt.

        Providers MUST also declare ``plugins=True`` on their
        :class:`ProviderCapabilities` descriptor. As with skills, the
        capability flag asserts the user-facing contract and this
        property selects the mechanism.
        """
        return False

    @property
    def skills_require_plugin_root(self) -> bool:
        """Whether reaching a skill means registering its whole plugin root.

        ``True`` on providers whose SDK has no bare skill-directory
        surface (``claude-agent-sdk``), where a skill is enabled by
        registering the plugin that owns it. That registration is not
        filterable — the SDK documents it as also providing the plugin's
        commands, agents, and hooks — so on such a provider a plugin's
        subagents cannot be declined while its skills are enabled, and
        its hooks are exposed rather than dropped.

        Both :mod:`conductor.config.validator` and
        :class:`~conductor.executor.agent.AgentExecutor` branch on this
        so the two agree about what a plugin actually delivers.
        """
        return False

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs: Any) -> None:
        """Enforce that a production subclass declares what it can honour.

        Converts a latent "lazily caught at validator/runtime" failure
        into an import-time error so missing or mistyped descriptors
        cannot ship. Test fakes opt out with ``abstract=True``:

            class _Fake(AgentProvider, abstract=True): ...

        Also checks the one capability with no fallback. A provider
        declaring ``plugins=True`` must accept all three delivery kwargs
        on :meth:`execute` and declare
        :attr:`supports_native_plugins` — a plugin's subagents and MCP
        servers cannot be approximated by injecting text into a prompt,
        so declaring support and dropping a channel silently reinstates
        the partial load the feature exists to remove. Signature drift is
        catchable here; "accepts the kwarg then ignores it" is not, and
        is covered per-provider by ``tests/test_plugins/test_providers.py``.
        """
        super().__init_subclass__(**kwargs)
        if abstract:
            return
        # Local import to avoid base.py → capabilities.py cycle at module load.
        from conductor.providers.capabilities import ProviderCapabilities

        caps = cls.__dict__.get("CAPABILITIES")
        if not isinstance(caps, ProviderCapabilities):
            raise TypeError(
                f"{cls.__module__}.{cls.__name__} must declare a class-level "
                f"CAPABILITIES: ProviderCapabilities attribute (see "
                f"conductor.providers.capabilities). Test fakes can opt out "
                f"with `class {cls.__name__}(AgentProvider, abstract=True)`."
            )

        if caps.plugins:
            import inspect

            accepted = set(inspect.signature(cls.execute).parameters)
            missing = sorted({"skill_directories", "custom_agents", "extra_mcp_servers"} - accepted)
            if missing:
                raise TypeError(
                    f"{cls.__module__}.{cls.__name__} declares capabilities.plugins=True "
                    f"but execute() does not accept {missing} — a plugin's components "
                    f"would be dropped without a word."
                )
            declared = inspect.getattr_static(cls, "supports_native_plugins", None)
            if isinstance(declared, property) and declared.fget is not None:
                try:
                    # Evaluated with no instance: these declarations are
                    # constants. A getter that genuinely needs instance state
                    # cannot be checked here, so it is left to the per-provider
                    # tests rather than failing the import.
                    resolved = declared.fget(cast("AgentProvider", None))
                except (AttributeError, TypeError):
                    resolved = True
            else:
                resolved = declared
            if not resolved:
                raise TypeError(
                    f"{cls.__module__}.{cls.__name__} declares capabilities.plugins=True "
                    f"but supports_native_plugins is falsy. The two describe the same "
                    f"contract and must agree."
                )

    @abstractmethod
    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        skill_directories: list[str] | None = None,
        custom_agents: list[dict[str, Any]] | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
    ) -> AgentOutput:
        """Execute an agent and return normalized output.

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context.
            rendered_prompt: Jinja2-rendered user prompt.
            tools: List of tool names available to this agent.
            interrupt_signal: Optional event that, when set, signals a
                mid-agent interrupt request. Providers that support
                mid-agent interrupts should monitor this event during
                execution and return partial output when it fires.
                Providers that do not support mid-agent interrupts may
                ignore this parameter.
            event_callback: Optional callback for streaming SDK events
                upstream (reasoning, tool calls, messages). Called with
                (event_type, data_dict) for each interesting SDK event.
            skill_directories: Optional skill directories resolved from
                the agent's effective ``skills`` list. Providers that
                set :attr:`supports_native_skills` to ``True`` should
                forward these to their SDK's native skill-loading
                mechanism. Providers that do not support native skills
                may ignore this parameter (the executor will have
                eager-injected the skill content into
                ``rendered_prompt`` instead).
            custom_agents: Optional subagent definitions resolved from
                the agent's effective ``plugins`` list, each shaped like
                the Copilot SDK's ``CustomAgentConfig`` and named
                ``<plugin>:<agent>``. Providers that set
                :attr:`supports_native_plugins` to ``True`` should
                register these so the model can dispatch to them.
            extra_mcp_servers: Optional MCP servers contributed by the
                agent's effective ``plugins`` list, merged on top of the
                workflow-level ``runtime.mcp_servers`` for this call
                only. Per-call rather than per-provider because
                ``plugins:`` is a per-agent field and providers are
                cached per type.

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If SDK execution fails.
            ValidationError: If output doesn't match schema.
        """
        ...

    async def execute_dialog_turn(
        self,
        system_prompt: str,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
    ) -> str:
        """Execute a single dialog turn for agent-user conversation.

        Used by the dialog evaluator and dialog handler for lightweight
        conversational exchanges. Creates a fresh, short-lived session
        for each call — not tied to the agent's main execution session.

        Args:
            system_prompt: System prompt providing dialog context.
            user_message: The latest user message.
            history: Optional prior conversation history as a list of
                ``{"role": "user"|"assistant", "content": "..."}`` dicts.
            model: Optional model override. If not provided, uses the
                provider's default model.

        Returns:
            The agent's response text.

        Raises:
            ProviderError: If the dialog turn fails.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support dialog turns")

    @abstractmethod
    async def validate_connection(self) -> bool:
        """Verify the provider can connect to its backend.

        This method should perform a lightweight check to ensure the
        provider is properly configured and can reach its backend service.

        Returns:
            True if connection successful, False otherwise.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release provider resources and close connections.

        This method should clean up any resources held by the provider,
        such as HTTP clients or session state.
        """
        ...

    async def get_max_prompt_tokens(self, model: str) -> int | None:
        """Return the SDK-reported maximum input (prompt) tokens for ``model``.

        This is the authoritative cap on prompt size enforced by the underlying
        SDK or backend (e.g. the Copilot ``max_prompt_tokens`` field, or the
        Anthropic ``max_input_tokens`` field). It is typically lower than the
        model's theoretical context window — for example, the Copilot SDK
        currently caps most GPT-5 variants at 128K despite a 400K model max.

        Implementations should:

        * Query their SDK's model-listing endpoint (cached after the first call).
        * Return ``None`` when the model is unknown to the provider, when the
          SDK call fails, or when no metadata is available.
        * Never raise — context-window metadata is best-effort and must not
          interrupt workflow execution.

        The default implementation returns ``None``, which causes the
        dashboard's context-window bar to be hidden and any future enforcement
        to be skipped — both safe degradations.

        Args:
            model: The model identifier as it would be sent to the SDK
                (e.g. ``"gpt-5.2"``, ``"claude-sonnet-4-5-20250929"``).

        Returns:
            The maximum prompt (input) tokens the SDK will accept, or ``None``.
        """
        return None

    async def get_max_output_tokens(self, model: str) -> int | None:
        """Return the SDK-reported maximum output (completion) tokens for ``model``.

        This is the provider's output cap for a single response. It is used by
        compaction to reserve enough headroom in the context window for the
        model's own answer.

        Implementations should:

        * Query their SDK's model-listing endpoint (cached after the first call).
        * Return ``None`` when the model is unknown to the provider, when the
          SDK call fails, or when no metadata is available.
        * Never raise — context-window metadata is best-effort and must not
          interrupt workflow execution.

        The default implementation returns ``None``.

        Args:
            model: The model identifier as it would be sent to the SDK
                (e.g. ``"gpt-5.2"``, ``"claude-sonnet-4-5-20250929"``).

        Returns:
            The maximum output tokens the SDK will accept, or ``None``.
        """
        return None

    async def get_model_pricing(self, model: str) -> ModelPricing | None:
        """Return provider-supplied pricing for ``model``, or ``None``.

        This is the provider hook in the cost-resolution chain (see #265).
        The :class:`~conductor.engine.usage.UsageTracker` resolves pricing in
        this order:

        **workflow ``cost.pricing`` override → this hook → ``DEFAULT_PRICING`` →
        ``None``.**

        A provider that knows its own rates (e.g. the Copilot SDK exposes
        per-model billing metadata) should return a
        :class:`~conductor.engine.pricing.ModelPricing` so cost reporting stays
        current without waiting for the static table to be refreshed on every
        model release. Providers whose SDK exposes no pricing (e.g. the
        Anthropic API's ``models.list()``) should return ``None`` and let the
        static table handle it — which is exactly what this default does.

        Implementations must:

        * Return ``None`` when the model is unknown to the provider, when the
          SDK exposes no usable pricing, or when the SDK call fails.
        * Never raise — pricing metadata is best-effort and must not interrupt
          workflow execution. Cost is always optional.

        Args:
            model: The model identifier as it would be sent to the SDK
                (e.g. ``"gpt-5.2"``, ``"claude-sonnet-4-5-20250929"``).

        Returns:
            A :class:`ModelPricing` when the provider can supply rates for
            ``model``, otherwise ``None``.
        """
        return None

    async def list_models(self) -> list[str] | None:
        """Return the model identifiers the provider can enumerate, if any.

        Used by ``conductor doctor --models`` to surface the models a
        provider exposes. Implementations should query their SDK's
        model-listing endpoint and return the resulting identifiers.

        Implementations should:

        * Return a list of model id strings on success (possibly empty).
        * Return ``None`` when the provider cannot enumerate models — either
          because the SDK is unavailable, the provider has no model-listing
          concept, or the listing call failed.
        * Never raise — diagnostics are best-effort and must not interrupt
          the caller.

        The default implementation returns ``None`` so providers that have no
        model-enumeration concept (e.g. those delegating to an external CLI)
        are reported as "n/a" rather than an error.

        Returns:
            A list of available model identifiers, or ``None`` when the
            provider does not enumerate models.
        """
        return None

    async def get_model_capabilities(self, model: str) -> ModelCapabilityInfo | None:
        """Return provider-supplied reasoning-effort and context-window metadata.

        This is the provider hook behind ``conductor doctor --models`` (see
        issue #301), alongside :meth:`get_max_prompt_tokens` and
        :meth:`get_model_pricing`. A provider that knows which
        ``reasoning.effort`` levels a model accepts (and its default), plus
        its prompt/output/context token limits, should return a
        :class:`ModelCapabilityInfo` populating whichever fields it can
        determine — fields the provider can't determine should stay at their
        ``None`` default rather than causing the whole call to fail.

        Implementations must:

        * Return ``None`` when the model is unknown to the provider, the SDK
          exposes no usable capability metadata, or the SDK call fails.
        * Never raise — capability metadata is best-effort and must not
          interrupt workflow execution or the ``doctor`` command.

        The default implementation returns ``None``, which causes ``doctor``
        to render every capability column as "n/a" for this provider — a
        safe degradation matching the sibling hooks above.

        Args:
            model: The model identifier as it would be sent to the SDK
                (e.g. ``"gpt-5.2"``, ``"claude-sonnet-4-5-20250929"``).

        Returns:
            A :class:`ModelCapabilityInfo` when the provider can supply
            capability metadata for ``model``, otherwise ``None``.
        """
        return None
