"""Provider capability descriptors and validator/runtime cross-check primitives.

Every :class:`~conductor.providers.base.AgentProvider` subclass declares a
class-level :class:`ProviderCapabilities` descriptor so that ``conductor
validate`` can statically cross-check workflow features against what the
provider actually supports. See issue #241 for design rationale.

The schema is intentionally **declarative and provider-agnostic** — no
Conductor imports here so it can be referenced from anywhere without
risking circular imports. The lazy :func:`get_capabilities` resolver does
local imports of provider modules so callers don't need to instantiate
providers (i.e. no API keys required for ``validate``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from conductor.providers.base import AgentProvider


# Stable / experimental are the only tiers for v1. Promotion criteria for
# experimental → stable are documented in docs/providers/experimental.md.
ProviderTier = Literal["stable", "experimental"]

# How a provider enforces an agent's declared ``output:`` schema.
#
# - ``native`` — the SDK has first-class structured-output support
#   (e.g. JSON Schema / Tool Use that the model is constrained to follow).
# - ``prompt_injection`` — the schema is appended to the prompt and the
#   model is asked to comply. Works but may produce malformed JSON.
# - ``none`` — the provider has no schema enforcement at all; declaring
#   ``output:`` on an agent that uses such a provider is an error.
StructuredOutputMode = Literal["native", "prompt_injection", "none"]

# Vocabulary of reasoning effort levels recognized across providers. The
# capability descriptor lists which subset the provider supports; ``None``
# means the provider has no reasoning-effort concept at all.
ReasoningEffortLevel = Literal["low", "medium", "high", "xhigh"]


class ProviderCapabilities(BaseModel):
    """Declarative summary of what a provider does and does not support.

    Attached to each :class:`AgentProvider` subclass as a class-level
    ``CAPABILITIES`` attribute. The validator reads these declarations
    statically (without instantiating the provider) to surface workflow ↔
    provider mismatches at ``conductor validate`` time.

    Capability values are **contracts**: the provider MUST honor what it
    declares. Lying in the descriptor undermines the whole framework — if
    a capability claim cannot be honored under all conditions, declare the
    weaker value. The AGENTS.md "Experimental Providers" section lists
    which carve-outs are permitted for experimental providers.

    See ``docs/providers/experimental.md`` for promotion criteria and the
    stability disclaimer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: ProviderTier
    """Stability tier. ``experimental`` allows specific parity carve-outs
    (see AGENTS.md); ``stable`` may not."""

    mcp_tools: bool
    """``True`` when the provider forwards workflow-level ``mcp_servers``
    to the underlying SDK. Workflows that declare ``runtime.mcp_servers``
    against a provider with ``mcp_tools=False`` fail validation."""

    workflow_tools_passthrough: bool
    """``True`` when an agent's ``tools:`` allowlist is honored by the
    provider. Workflows that set ``tools:`` against a provider with
    ``workflow_tools_passthrough=False`` fail validation — silently
    dropping the allowlist is a security regression."""

    streaming_events: bool
    """``True`` when the provider emits ``agent_message`` / ``agent_tool_*``
    events incrementally during execution (vs. only at completion)."""

    agent_reasoning_events: bool
    """``True`` when the provider emits ``agent_reasoning`` events for
    thinking / chain-of-thought content."""

    reasoning_effort: tuple[ReasoningEffortLevel, ...] | None
    """Supported reasoning-effort levels. ``None`` means the provider has
    no reasoning-effort concept at all. An agent declaring
    ``reasoning.effort: X`` against a provider whose tuple does not
    include ``X`` fails validation. Empty tuple is invalid — use ``None``."""

    structured_output: StructuredOutputMode
    """How the provider enforces an agent's ``output:`` schema. See
    :data:`StructuredOutputMode`."""

    interrupt: bool
    """``True`` when the provider monitors ``interrupt_signal`` and can
    return partial output mid-execution (Esc / Ctrl+G in the CLI)."""

    max_session_seconds: bool
    """``True`` when the provider enforces the workflow's
    ``max_session_seconds`` wall-clock timeout. False means the setting
    is silently ignored — workflows that set it fail validation."""

    checkpoint_resume: bool
    """``True`` when provider session state survives ``conductor resume``
    cleanly (re-establishes session_id, tool state, etc.)."""

    usage_tracking: bool
    """``True`` when the provider reports ``input_tokens`` /
    ``output_tokens`` / ``model`` on every :class:`AgentOutput`. Required
    for budget enforcement and cost reporting."""

    concurrent_safe: bool
    """``True`` when N copies of the provider can run in parallel safely
    (no shared mutable state, no file-system contention). An agent that
    uses a ``concurrent_safe=False`` provider may not appear in a
    :class:`ParallelGroup`, and may only appear in a :class:`ForEachDef`
    whose ``max_concurrent`` is 1."""

    upstream_pin: str | None = None
    """Upstream package pin surfaced in the experimental banner, e.g.
    ``"claude-agent-sdk>=0.1.0"``. ``None`` for providers that have no
    explicit upstream pin (typically stable providers)."""

    maintainer: str | None = None
    """Free-form maintainer attribution surfaced in the experimental
    banner, e.g. ``"@external-contributor (best-effort)"``. ``None`` for
    providers maintained by the core team."""

    @property
    def is_experimental(self) -> bool:
        """Shorthand: ``True`` iff ``tier == "experimental"``."""
        return self.tier == "experimental"

    def declared_limitations(self) -> list[str]:
        """Human-readable list of capability fields that read as ``false`` / ``None``.

        Used by the experimental banner to auto-generate the limitations
        line so the operator can see at a glance what's missing without
        cross-referencing the docs.
        """
        items: list[str] = []
        if not self.mcp_tools:
            items.append("no MCP servers")
        if not self.workflow_tools_passthrough:
            items.append("no per-agent tools allowlist")
        if not self.streaming_events:
            items.append("no streaming events")
        if not self.agent_reasoning_events:
            items.append("no reasoning events")
        if self.reasoning_effort is None:
            items.append("reasoning_effort ignored")
        if self.structured_output == "none":
            items.append("no structured output")
        elif self.structured_output == "prompt_injection":
            items.append("structured output via prompt injection")
        if not self.interrupt:
            items.append("no mid-stream interrupt")
        if not self.max_session_seconds:
            items.append("max_session_seconds ignored")
        if not self.checkpoint_resume:
            items.append("no checkpoint resume")
        if not self.usage_tracking:
            items.append("no usage tracking")
        if not self.concurrent_safe:
            items.append("not safe to run in parallel")
        return items


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

# Maps a provider name (as it appears in YAML / ``ProviderType``) to the
# fully-qualified import path of its provider class. Used by the resolver
# so that ``conductor validate`` can read capabilities without instantiating
# the provider (instantiation can require API keys / network).
_PROVIDER_CLASS_PATHS: Final[dict[str, str]] = {
    "copilot": "conductor.providers.copilot:CopilotProvider",
    "claude": "conductor.providers.claude:ClaudeProvider",
    "claude-agent-sdk": "conductor.providers.claude_agent_sdk:ClaudeAgentSdkProvider",
}

# Provider names that appear in the schema / factory but are not yet
# implemented. The resolver returns a permissive placeholder for these so
# the validator does NOT pre-empt the factory's "not yet implemented"
# error — the workflow author should see one clear failure at run time,
# not a misleading "no capabilities declared" error at validate time.
_NOT_YET_IMPLEMENTED_PROVIDERS: Final[frozenset[str]] = frozenset({"openai-agents"})


def _build_unimplemented_placeholder() -> ProviderCapabilities:
    """Permissive capability set used for known-but-unimplemented providers.

    All fields read as supported so the validator never produces a
    capability-mismatch error — the factory's "not yet implemented" error
    at runtime is the authoritative failure for these names.
    """
    return ProviderCapabilities(
        tier="experimental",
        mcp_tools=True,
        workflow_tools_passthrough=True,
        streaming_events=True,
        agent_reasoning_events=True,
        reasoning_effort=("low", "medium", "high", "xhigh"),
        structured_output="native",
        interrupt=True,
        max_session_seconds=True,
        checkpoint_resume=True,
        usage_tracking=True,
        concurrent_safe=True,
        upstream_pin=None,
        maintainer="(not yet implemented)",
    )


def get_capabilities(provider_type: str) -> ProviderCapabilities:
    """Resolve the :class:`ProviderCapabilities` for a provider name.

    Imports the provider's module lazily — never instantiates the provider —
    so this is safe to call from ``conductor validate`` without any
    API keys or network access.

    Args:
        provider_type: Provider name as it appears in workflow YAML, e.g.
            ``"copilot"`` or ``"claude-agent-sdk"``.

    Returns:
        The provider class's declared ``CAPABILITIES`` descriptor.

    Raises:
        KeyError: If ``provider_type`` is not a known provider name.
        AttributeError: If the resolved provider class is missing the
            required class-level ``CAPABILITIES`` attribute. Every
            production provider must declare one.
    """
    if provider_type in _NOT_YET_IMPLEMENTED_PROVIDERS:
        # Permissive placeholder so the validator does not pre-empt the
        # factory's "not yet implemented" error at validate time.
        return _build_unimplemented_placeholder()
    try:
        dotted_path = _PROVIDER_CLASS_PATHS[provider_type]
    except KeyError as e:
        raise KeyError(
            f"Unknown provider {provider_type!r}. Known providers: "
            f"{sorted(set(_PROVIDER_CLASS_PATHS) | _NOT_YET_IMPLEMENTED_PROVIDERS)}"
        ) from e
    """Resolve the :class:`ProviderCapabilities` for a provider name.

    Imports the provider's module lazily — never instantiates the provider —
    so this is safe to call from ``conductor validate`` without any
    API keys or network access.

    Args:
        provider_type: Provider name as it appears in workflow YAML, e.g.
            ``"copilot"`` or ``"claude-agent-sdk"``.

    Returns:
        The provider class's declared ``CAPABILITIES`` descriptor.

    Raises:
        KeyError: If ``provider_type`` is not a known provider name.
        AttributeError: If the resolved provider class is missing the
            required class-level ``CAPABILITIES`` attribute. Every
            production provider must declare one.
    """
    try:
        dotted_path = _PROVIDER_CLASS_PATHS[provider_type]
    except KeyError as e:
        raise KeyError(
            f"Unknown provider {provider_type!r}. Known providers: {sorted(_PROVIDER_CLASS_PATHS)}"
        ) from e

    module_path, _, class_name = dotted_path.partition(":")
    import importlib

    module = importlib.import_module(module_path)
    provider_cls: type[AgentProvider] = getattr(module, class_name)

    capabilities = getattr(provider_cls, "CAPABILITIES", None)
    if not isinstance(capabilities, ProviderCapabilities):
        raise AttributeError(
            f"Provider class {provider_cls.__module__}.{provider_cls.__name__} "
            f"does not declare a class-level CAPABILITIES: ProviderCapabilities "
            f"attribute. Every production provider must declare one. "
            f"See conductor.providers.capabilities for the schema."
        )
    return capabilities


def known_provider_names() -> tuple[str, ...]:
    """Tuple of all provider names the resolver knows about.

    Includes both implemented providers and known-but-unimplemented names
    (e.g. ``openai-agents``) for which the resolver returns a permissive
    placeholder. Useful for validator helpers that need to iterate the
    full set without producing spurious "unknown provider" warnings.
    """
    return tuple(_PROVIDER_CLASS_PATHS) + tuple(_NOT_YET_IMPLEMENTED_PROVIDERS)


# Convenience re-export so callers can write ``from conductor.providers.capabilities
# import ProviderCapabilities, get_capabilities`` without remembering paths.
__all__ = [
    "ProviderCapabilities",
    "ProviderTier",
    "ReasoningEffortLevel",
    "StructuredOutputMode",
    "get_capabilities",
    "known_provider_names",
]
