"""Azure Container Apps (ACA) dynamic-sessions runtime provider.

This module provides ``AcaRuntimeProvider`` — a thin host-side transport
shim implementing :class:`~conductor.providers.base.AgentProvider` that
delegates agent execution to an in-sandbox ``conductor-agent-runner``
process running inside an Azure Container Apps dynamic-sessions pool.

The library is an optional dependency — install with:
    pip install 'conductor-cli[aca]'
(pins ``azure-identity``, used to acquire a ``dynamicsessions.io`` bearer
token via ``DefaultAzureCredential``.)

Status: this module currently ships only the provider skeleton and its
declared ``CAPABILITIES`` descriptor (see issue #284, epic E3-T1). The
transport shim itself — identifier derivation, AAD auth, NDJSON streaming,
interrupt handling — is implemented by epic E3. ``execute()``,
``validate_connection()``, and ``close()`` raise ``NotImplementedError``
until that work lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from conductor.exceptions import ProviderError
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.capabilities import ProviderCapabilities
from conductor.providers.reasoning import ReasoningEffort

if TYPE_CHECKING:
    import asyncio

    from conductor.config.schema import AgentDef, ProviderSettings, ToolOutputConfig

# azure-identity ships DefaultAzureCredential, used to acquire a
# dynamicsessions.io bearer token for the Session Executor role (FR6). It is
# gated behind the `aca` extra (pyproject.toml) rather than a base
# dependency, mirroring the anthropic/hermes-agent/claude-agent-sdk optional
# SDK guards elsewhere in this package.
try:
    from azure.identity import DefaultAzureCredential  # ty: ignore[unresolved-import]

    AZURE_IDENTITY_AVAILABLE = True
except ImportError:
    AZURE_IDENTITY_AVAILABLE = False
    DefaultAzureCredential: Any = None


class AcaRuntimeProvider(AgentProvider):
    """Host-side transport shim for the ``aca`` (Azure Container Apps) provider.

    Owns no agentic logic itself: it derives a session ``identifier``,
    authenticates against the dynamic-sessions pool, and relays the
    in-sandbox runner's NDJSON event stream verbatim to ``event_callback``,
    parsing the terminal ``result`` frame into :class:`AgentOutput`.

    Requires the ``azure-identity`` package:
        pip install 'conductor-cli[aca]'

    Example:
        >>> provider = AcaRuntimeProvider(provider_settings=settings)
        >>> await provider.close()
    """

    CAPABILITIES = ProviderCapabilities(
        tier="experimental",
        # Full `runtime.mcp_servers` is forwarded to the runner, which wraps
        # a real `CopilotProvider` in-container (runner-image contract).
        mcp_tools=True,
        # Per-agent `tools:` allowlist is forwarded and enforced by the
        # inner SDK running inside the sandbox.
        workflow_tools_passthrough=True,
        # Branch S (single streaming request, #312) relays event frames
        # incrementally as the runner emits them.
        streaming_events=True,
        # The runner forwards reasoning frames from the inner provider.
        agent_reasoning_events=True,
        # The inner provider (Copilot) translates reasoning effort natively.
        reasoning_effort=("low", "medium", "high", "xhigh", "max"),
        # Inherits the real CopilotProvider's prompt-injection schema
        # enforcement — Copilot has no native JSON mode.
        structured_output="prompt_injection",
        # Real interrupt via the in-flight stream (Branch S); `stopSession`
        # remains available as a hard-abort fallback.
        interrupt=True,
        # Enforced by a runner-side wall-clock guard (the default `Timed`
        # lifecycle does not honor the pool's `maxAlivePeriodInSeconds`).
        max_session_seconds=True,
        # Sessions are ephemeral with no volume mount; `conductor resume`
        # re-runs the agent rather than restoring in-sandbox state.
        checkpoint_resume=False,
        # The runner returns token counts on the terminal result frame.
        usage_tracking=True,
        # Honest via the mandatory concurrency discriminator in the
        # identifier derivation (DD5).
        concurrent_safe=True,
        # Interpreted container-relative: a path inside the session
        # filesystem, never resolved against the host workflow directory.
        working_dir=True,
        upstream_pin="azure-identity>=1.19.0",
        maintainer=None,
    )

    def __init__(
        self,
        provider_settings: ProviderSettings,
        mcp_servers: dict[str, Any] | None = None,
        default_model: str | None = None,
        max_agent_iterations: int | None = None,
        default_reasoning_effort: ReasoningEffort | None = None,
        max_session_seconds: float | None = None,
        tool_output: ToolOutputConfig | None = None,
    ) -> None:
        """Initialize the ACA runtime provider.

        Args:
            provider_settings: Structured ``runtime.provider`` settings with
                ``name="aca"``. Carries ``pool_endpoint``, ``api_version``,
                ``inner_provider``, ``identifier_scope``, ``egress``,
                ``lifecycle``, and ``auth`` — the only place these
                ``aca``-only fields live (see :class:`ProviderSettings`).
            mcp_servers: MCP server configurations forwarded to the runner.
            default_model: Default model for agents that don't specify one.
            max_agent_iterations: Maximum tool-use iterations per execution.
            default_reasoning_effort: Workflow-wide default reasoning effort.
            max_session_seconds: Maximum wall-clock duration for agent
                sessions, enforced runner-side.
            tool_output: MCP tool result output-size configuration.

        Raises:
            ProviderError: If the ``azure-identity`` package is not
                installed.
        """
        if not AZURE_IDENTITY_AVAILABLE:
            raise ProviderError(
                "aca provider requires the azure-identity package",
                suggestion="Install with: uv add 'conductor-cli[aca]'",
            )

        self._provider_settings = provider_settings
        self._mcp_servers = mcp_servers
        self._default_model = default_model
        self._default_max_agent_iterations = max_agent_iterations
        self._default_reasoning_effort = default_reasoning_effort
        self._default_max_session_seconds = max_session_seconds
        self._tool_output_config = tool_output

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Not yet implemented — the transport shim is delivered by epic E3."""
        raise NotImplementedError(
            "AcaRuntimeProvider.execute() is not yet implemented (see issue #284, epic E3)"
        )

    async def validate_connection(self) -> bool:
        """Not yet implemented — the transport shim is delivered by epic E3."""
        raise NotImplementedError(
            "AcaRuntimeProvider.validate_connection() is not yet implemented "
            "(see issue #284, epic E3)"
        )

    async def close(self) -> None:
        """Not yet implemented — the transport shim is delivered by epic E3."""
        raise NotImplementedError(
            "AcaRuntimeProvider.close() is not yet implemented (see issue #284, epic E3)"
        )
