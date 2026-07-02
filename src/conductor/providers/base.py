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
from typing import TYPE_CHECKING, Any, ClassVar

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
        input_tokens: Number of input/prompt tokens used.
        output_tokens: Number of output/completion tokens generated.
        cache_read_tokens: Tokens read from cache (Claude prompt caching).
        cache_write_tokens: Tokens written to cache (Claude prompt caching).
        model: Actual model used (may differ from requested if aliased).
    """

    content: dict[str, Any]
    """Parsed structured output matching the agent's output schema."""

    raw_response: Any
    """Provider-specific raw response for debugging/logging."""

    tokens_used: int | None = None
    """Total token count (input + output) if provided by the SDK."""

    input_tokens: int | None = None
    """Number of input/prompt tokens used."""

    output_tokens: int | None = None
    """Number of output/completion tokens generated."""

    cache_read_tokens: int | None = None
    """Tokens read from cache (Claude prompt caching)."""

    cache_write_tokens: int | None = None
    """Tokens written to cache (Claude prompt caching)."""

    model: str | None = None
    """Actual model used (may differ from requested if aliased)."""

    partial: bool = False
    """Whether this output is partial (from a mid-agent interrupt)."""


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

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs: Any) -> None:
        """Enforce that every production subclass declares ``CAPABILITIES``.

        Converts a latent "lazily caught at validator/runtime" failure
        into an import-time error so missing or mistyped descriptors
        cannot ship. Test fakes opt out with ``abstract=True``:

            class _Fake(AgentProvider, abstract=True): ...
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

    @abstractmethod
    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
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
