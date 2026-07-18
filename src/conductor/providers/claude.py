"""Anthropic Claude SDK provider implementation.

This module provides the ClaudeProvider class for executing agents
using the Anthropic Claude SDK with tool-based structured output.

Error Handling Strategy:
- ValidationError: Used for invalid inputs, schema violations, and parameter range errors.
  These are non-retryable and indicate user/configuration errors that should fail fast.
  Examples: temperature out of range, invalid output schema, malformed prompt.

- ProviderError: Used for API failures, network errors, and SDK exceptions.
  These may be retryable (connection errors, rate limits) or non-retryable (invalid API key).
  The error includes metadata (status_code, is_retryable) to guide retry logic.
  Examples: HTTP 500 errors, rate limits, authentication failures.

This distinction ensures clear error classification and appropriate retry behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import time
from typing import Any, Protocol, get_args

from pydantic import BaseModel

from conductor.config.schema import AgentDef, OutputField, ToolOutputConfig
from conductor.exceptions import ProviderError, ValidationError
from conductor.executor.output import validate_output
from conductor.mcp.manager import (
    FS_HINT,
    GENERIC_HINT,
    TAIL_WINDOW,
    TRUNCATION_MARKER_PREFIX,
    MCPManager,
)
from conductor.providers._event_format import (
    extract_tool_result_text,
    format_tool_arguments,
)
from conductor.providers.base import (
    AgentOutput,
    AgentProvider,
    EventCallback,
    ModelCapabilityInfo,
    match_model_id,
)
from conductor.providers.capabilities import ProviderCapabilities
from conductor.providers.reasoning import (
    CLAUDE_ANSWER_HEADROOM_TOKENS,
    CLAUDE_EXTENDED_THINKING_OUTPUT_CAP,
    ReasoningEffort,
    effort_to_budget_tokens,
    is_claude_thinking_model,
    resolve_reasoning_effort,
)

# Try to import the Anthropic SDK
try:
    import anthropic
    from anthropic import AnthropicError, AsyncAnthropic

    ANTHROPIC_SDK_AVAILABLE = True
except ImportError:
    ANTHROPIC_SDK_AVAILABLE = False
    AsyncAnthropic = None  # type: ignore[misc, assignment]
    anthropic = None  # type: ignore[assignment]
    AnthropicError = Exception  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)


# Protocol for Claude API response structure (improves type safety)
class ClaudeContentBlock(Protocol):
    """Protocol for Claude response content blocks."""

    type: str
    text: str  # for text blocks
    id: str  # for tool_use blocks
    name: str  # for tool_use blocks
    input: dict[str, Any]  # for tool_use blocks
    thinking: str  # for thinking blocks
    signature: str  # for thinking blocks (optional)
    data: str  # for redacted_thinking blocks


class ClaudeResponse(Protocol):
    """Protocol for Claude API response structure."""

    content: list[ClaudeContentBlock]
    usage: Any  # Usage object with input_tokens, output_tokens


class RetryConfig(BaseModel):
    """Configuration for retry behavior.

    Attributes:
        max_attempts: Maximum number of retry attempts (including first attempt).
        base_delay: Base delay in seconds before first retry.
        max_delay: Maximum delay in seconds between retries.
        jitter: Maximum random jitter to add to delay (0.0 to 1.0 fraction of delay).
        backoff: Backoff strategy: "exponential" or "fixed".
        retry_on: Error categories that trigger a retry ("provider_error", "timeout").
        max_parse_recovery_attempts: Maximum number of in-session recovery attempts
            for JSON parse failures. When parsing fails, a follow-up message is sent
            to the same session asking the model to correct its response format.
    """

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.25
    backoff: str = "exponential"
    retry_on: list[str] | None = None
    max_parse_recovery_attempts: int = 2  # Claude: 2 attempts (less than Copilot's 5)


class ClaudeProvider(AgentProvider):
    """Anthropic Claude SDK provider.

    Translates Conductor agent definitions into Claude SDK calls and
    normalizes responses into AgentOutput format. Uses tool-based
    structured output extraction for reliable JSON responses.

    Supports non-streaming message execution with error handling,
    retry logic, and temperature validation.

    Example:
        >>> provider = ClaudeProvider(api_key="sk-...")
        >>> await provider.validate_connection()
        True
        >>> await provider.close()
    """

    CAPABILITIES = ProviderCapabilities(
        tier="stable",
        # Claude provider accepts ``runtime.mcp_servers`` (stdio only —
        # see provider parity notes in comparison.md).
        mcp_tools=True,
        # Per-agent ``tools:`` allowlists are forwarded to the SDK.
        workflow_tools_passthrough=True,
        # The Claude provider buffers the API response before emitting any
        # events. Flip to True if/when a streaming codepath is wired up.
        streaming_events=False,
        # ``agent_reasoning`` events fire for extended-thinking content
        # when the model returns it.
        agent_reasoning_events=True,
        # Extended-thinking effort mapped to Anthropic budgets (low=2048,
        # medium=8192, high=16384, xhigh=32768, max=59904 tokens — see
        # providers/reasoning.py).
        reasoning_effort=("low", "medium", "high", "xhigh", "max"),
        # Tool-based structured output: schema is enforced via a forced
        # tool call rather than prompt injection.
        structured_output="native",
        # ``interrupt_signal`` is monitored between agentic iterations and
        # triggers ``_request_partial_output``.
        interrupt=True,
        # ``max_session_seconds`` is enforced at each agentic-loop iteration.
        max_session_seconds=True,
        # Anthropic's API is stateless per-request — no session state to
        # persist across ``conductor resume``.
        checkpoint_resume=False,
        # Token counts and model identifier populated on every AgentOutput.
        usage_tracking=True,
        # No global mutable state — safe to run N parallel agents.
        concurrent_safe=True,
        # The resolved ``working_dir`` selects the MCPManager pool key and is
        # forwarded to ``StdioServerParameters(cwd=...)`` for each stdio MCP
        # server the agent connects.
        working_dir=True,
        upstream_pin=None,
        maintainer="@microsoft/conductor",
    )

    def __init__(
        self,
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 600.0,
        retry_config: RetryConfig | None = None,
        mcp_servers: dict[str, Any] | None = None,
        max_agent_iterations: int | None = None,
        max_session_seconds: float | None = None,
        default_reasoning_effort: ReasoningEffort | None = None,
        tool_output: ToolOutputConfig | None = None,
    ) -> None:
        """Initialize the Claude provider.

        Args:
            api_key: Anthropic API key. If None, uses ANTHROPIC_API_KEY env var.
            auth_token: Bearer token for OAuth / gateway authentication. Sent as
                ``Authorization: Bearer <token>`` instead of ``x-api-key``.
                If None, falls back to ANTHROPIC_AUTH_TOKEN env var (SDK-native).
                Use this for Databricks AI Gateway, LiteLLM, or any proxy that
                expects a bearer token rather than a raw API key.
            base_url: Custom API endpoint (e.g. Databricks gateway URL).
                If None, falls back to ANTHROPIC_BASE_URL env var then the
                default Anthropic API endpoint.
            model: Default model to use. Defaults to "claude-3-5-sonnet-latest".
                This default is chosen for stability and to avoid dated model
                deprecation risk. The "-latest" suffix ensures compatibility
                with model updates without requiring configuration changes.
            temperature: Default temperature (0.0-1.0). SDK enforces range.
            max_tokens: Maximum output tokens. Defaults to 8192.
            timeout: Request timeout in seconds. Defaults to 600s.
            retry_config: Optional retry configuration. Uses default if not provided.
            mcp_servers: Optional MCP server configurations for tool support.
                Each server config should have: command, args, env (optional).
            max_agent_iterations: Maximum tool-use iterations per agent execution.
                Defaults to 50 if not specified.
            max_session_seconds: Maximum wall-clock duration for agent sessions.
                Defaults to None (unlimited).
            default_reasoning_effort: Workflow-wide default reasoning effort
                applied when an agent does not declare its own ``reasoning``
                config. Mapped to a Claude extended-thinking ``budget_tokens``
                value. Only valid on extended-thinking models — a per-agent
                model that does not support thinking will raise
                ``ValidationError`` at execute time.
            tool_output: MCP tool result output-size configuration. Defines the
                per-result character limit and spill-to-file behavior for MCP
                tool outputs. ``None`` means the default configuration is used.

        Raises:
            ProviderError: If SDK is not installed.
        """
        if not ANTHROPIC_SDK_AVAILABLE:
            raise ProviderError(
                "Anthropic SDK not installed",
                suggestion="Install with: uv add 'anthropic>=0.77.0,<1.0.0'",
            )

        self._client: AsyncAnthropic | None = None
        self._api_key = api_key
        self._auth_token = auth_token
        self._base_url = base_url
        self._default_model = model or "claude-3-5-sonnet-latest"

        # Validate and store temperature (enforce schema bounds at instantiation)
        if temperature is not None:
            self._validate_temperature(temperature)
        self._default_temperature = temperature

        # Validate and store max_tokens (enforce schema bounds at instantiation)
        if max_tokens is not None:
            self._validate_max_tokens(max_tokens)
        self._default_max_tokens = max_tokens or 8192

        self._timeout = timeout
        self._sdk_version: str | None = None
        self._retry_config = retry_config or RetryConfig()
        self._retry_history: list[dict[str, Any]] = []  # For testing/debugging retries
        self._max_schema_depth = 10  # Max nesting depth for recursive schema building
        self._default_max_agent_iterations = (
            max_agent_iterations if max_agent_iterations is not None else 50
        )
        self._default_max_session_seconds = max_session_seconds
        self._default_reasoning_effort: ReasoningEffort | None = default_reasoning_effort
        self._tool_output_config = tool_output or ToolOutputConfig()

        # MCP server configuration for tool support.
        # Managers are pooled by resolved working directory: each distinct
        # cwd gets its own MCPManager because stdio MCP servers are spawned
        # with the manager's cwd. The pool lifecycle is bounded by the
        # provider lifetime — close() shuts down every pooled manager. v1
        # intentionally has no eviction/LRU: the number of distinct cwds in
        # a workflow run is expected to be small, and evicting a live
        # manager would kill in-flight tool calls.
        self._mcp_servers_config = mcp_servers
        self._mcp_managers: dict[str, MCPManager] = {}
        self._mcp_manager_locks: dict[str, asyncio.Lock] = {}

        # Cache of model_id -> max_input_tokens populated lazily on first
        # get_max_prompt_tokens() call. Guarded by an asyncio.Lock to avoid
        # racing concurrent first-callers and emitting duplicate models.list()
        # requests.
        self._max_input_cache: dict[str, int | None] | None = None
        self._max_input_cache_lock = asyncio.Lock()

        # Initialize the client (sync initialization)
        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initialize the Anthropic client and log SDK version.

        Note: Model verification is deferred to validate_connection() to keep
        initialization synchronous and avoid async operations in __init__.
        """
        if not ANTHROPIC_SDK_AVAILABLE or AsyncAnthropic is None:
            return

        client_kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._api_key is not None:
            client_kwargs["api_key"] = self._api_key
        if self._auth_token is not None:
            client_kwargs["auth_token"] = self._auth_token
        if self._base_url is not None:
            client_kwargs["base_url"] = self._base_url
        self._client = AsyncAnthropic(**client_kwargs)

        # Log SDK version
        if anthropic is not None:
            self._sdk_version = getattr(anthropic, "__version__", "unknown")
            logger.info(f"Initialized Claude provider with SDK version {self._sdk_version}")

            # Warn if version is outside expected range
            if self._sdk_version != "unknown":
                try:
                    major, minor, patch = self._sdk_version.split(".")
                    version_parts = (int(major), int(minor))
                    if version_parts[0] == 0 and version_parts[1] < 77:
                        logger.warning(
                            f"Anthropic SDK version {self._sdk_version} is older than 0.77.0. "
                            "Some features may not work correctly."
                        )
                    elif version_parts[0] >= 1:
                        logger.warning(
                            f"Anthropic SDK version {self._sdk_version} is >= 1.0.0. "
                            "This provider was tested with 0.77.x. Compatibility issues may occur."
                        )
                except (ValueError, AttributeError):
                    logger.debug(f"Could not parse SDK version: {self._sdk_version}")

    def _validate_temperature(self, temperature: float) -> None:
        """Validate temperature parameter is in acceptable range.

        Enforces schema.py validation bounds (0.0-1.0) at provider instantiation
        to fail fast before workflow execution. SDK also enforces this range.

        Args:
            temperature: Temperature value to validate.

        Raises:
            ValidationError: If temperature is out of range (0.0-1.0).
        """
        if not (0.0 <= temperature <= 1.0):
            raise ValidationError(
                f"Temperature must be between 0.0 and 1.0 (schema validation), got {temperature}",
                suggestion="Adjust temperature to be within the valid range",
            )

    def _validate_max_tokens(self, max_tokens: int) -> None:
        """Validate max_tokens parameter is in acceptable range.

        Enforces schema.py validation bounds (1-200000) at provider instantiation
        to fail fast before workflow execution.

        Args:
            max_tokens: Max tokens value to validate.

        Raises:
            ValidationError: If max_tokens is out of range (1-200000).
        """
        if not (1 <= max_tokens <= 200000):
            raise ValidationError(
                f"max_tokens must be between 1 and 200000 (schema validation), got {max_tokens}",
                suggestion="Adjust max_tokens to be within the valid range",
            )

    def _resolve_thinking_for_agent(self, agent: AgentDef, model: str) -> dict[str, Any] | None:
        """Resolve effective extended-thinking kwargs for an agent.

        Combines the per-agent ``reasoning`` config with the workflow-wide
        ``default_reasoning_effort`` and validates that the chosen model
        supports Anthropic extended thinking.

        Args:
            agent: Agent definition (may declare ``reasoning.effort``).
            model: Resolved model id for this execution.

        Returns:
            ``{"type": "enabled", "budget_tokens": N}`` when reasoning is
            requested, or ``None`` when neither agent nor runtime default
            sets it.

        Raises:
            ValidationError: If reasoning effort is requested for a model
                that does not support extended thinking.
        """
        effort = resolve_reasoning_effort(agent, self._default_reasoning_effort)
        if effort is None:
            return None
        if not is_claude_thinking_model(model):
            raise ValidationError(
                f"Model {model!r} does not support extended thinking, but "
                f"reasoning.effort={effort!r} was requested for agent "
                f"{agent.name!r}.",
                suggestion=(
                    "Use a Claude 3.7+ or 4.x model (e.g. claude-opus-4-20250514, "
                    "claude-sonnet-4-20250514) or remove the reasoning config."
                ),
            )
        return {"type": "enabled", "budget_tokens": effort_to_budget_tokens(effort)}

    def get_retry_history(self) -> list[dict[str, Any]]:
        """Get the retry history for debugging purposes.

        Returns:
            List of dictionaries containing retry attempt details.
        """
        return self._retry_history.copy()

    async def validate_connection(self) -> bool:
        """Verify the provider can connect to the Claude API.

        This method serves dual purposes:
        1. Validates API connectivity and credentials
        2. Performs async model verification (deferred from __init__)

        Returns:
            True if connection successful, False otherwise.
        """
        if self._client is None:
            return False

        try:
            # Test: list models to verify API key works and perform model verification
            await self._client.models.list()
            # Log available models for debugging
            await self._log_available_models()
            return True
        except Exception as e:
            logger.error(f"Connection validation failed: {e}")
            return False

    async def _log_available_models(self) -> None:
        """List and log available models, warn if default model is unavailable.

        Also seeds ``_max_input_cache`` so the first call to
        :meth:`get_max_prompt_tokens` doesn't pay for an extra round-trip.
        """
        if self._client is None:
            return

        try:
            # Call client.models.list() to get available models (async)
            logger.debug("Discovering available Claude models via client.models.list()...")
            models_page = await self._client.models.list()
        except (TimeoutError, AnthropicError, OSError) as e:
            logger.warning(f"Could not list available models (discovery failed): {e}")
            return

        available_models = [model.id for model in models_page.data]
        logger.info(f"Available Claude models: {', '.join(available_models)}")

        # Warn if default model not in list (after stripping aliases like -latest).
        if match_model_id(self._default_model, available_models) is None:
            logger.warning(
                f"Requested model '{self._default_model}' is not in the list of "
                f"available models. API calls may fail. Available: {available_models}"
            )
        else:
            logger.debug(f"Default model '{self._default_model}' verified in available models")

        # Seed the metadata cache so get_max_prompt_tokens() is a pure lookup.
        self._install_max_input_cache(models_page.data)

    def _install_max_input_cache(self, models_data: list[Any]) -> None:
        """Replace ``_max_input_cache`` with a fresh mapping of id -> max_input."""
        self._max_input_cache = {
            info.id: getattr(info, "max_input_tokens", None) for info in models_data
        }

    async def get_max_prompt_tokens(self, model: str) -> int | None:
        """Return the Anthropic SDK's ``max_input_tokens`` for ``model``.

        On first call, populates a per-instance cache by enumerating
        ``client.models.list()``; subsequent calls are dictionary lookups.
        ``validate_connection()`` already populates the cache, so callers
        that go through normal connection setup never pay for an extra
        round-trip.

        Resolves aliases (``-latest``, dated suffixes, base/versioned name
        mismatches) via :func:`match_model_id`. Returns ``None`` when the
        SDK is unavailable, the model can't be resolved, or the listing
        call fails — context-window metadata must never block workflow
        execution.

        Note: the value reflects the API's *default* input window. Claude
        models with a 1M-context beta require an explicit beta header,
        which Conductor does not set today; for those models the API still
        reports the default window.
        """
        if not ANTHROPIC_SDK_AVAILABLE or self._client is None:
            return None

        if self._max_input_cache is None:
            # Fetch outside the lock so concurrent callers don't all queue
            # behind a slow round-trip; the lock only guards the install.
            try:
                page = await self._client.models.list()
            except (TimeoutError, AnthropicError, OSError) as e:
                # Don't cache the failure — let the next call retry.
                logger.debug("Failed to list Anthropic models: %s", e)
                return None
            async with self._max_input_cache_lock:
                if self._max_input_cache is None:
                    self._install_max_input_cache(page.data)

        # The block above either returned early on failure or installed the
        # cache, so it's guaranteed non-None here.
        cache = self._max_input_cache
        assert cache is not None
        matched_id = match_model_id(model, cache.keys())
        return cache.get(matched_id) if matched_id is not None else None

    async def list_models(self) -> list[str] | None:
        """Return the model ids advertised by the Anthropic API.

        Enumerates ``client.models.list()`` and returns each entry's ``id``.
        Used by ``conductor doctor --models``.

        Returns ``None`` when the SDK is unavailable, the client has not been
        constructed, or the listing call fails — diagnostics must never raise.
        """
        if not ANTHROPIC_SDK_AVAILABLE or self._client is None:
            return None
        try:
            page = await self._client.models.list()
        except Exception as e:  # noqa: BLE001 - diagnostics must never raise
            logger.debug("Failed to list Anthropic models: %s", e)
            return None
        return [model.id for model in page.data]

    async def get_model_capabilities(self, model: str) -> ModelCapabilityInfo | None:
        """Return reasoning-effort support and prompt-token limits for ``model``.

        Implements the :meth:`AgentProvider.get_model_capabilities` hook (see
        #301).

        Reasoning-effort support is derived from the same static heuristic
        used to gate extended thinking (:func:`is_claude_thinking_model`):
        thinking-capable models (Claude 3.7+ / 4.x) advertise all five
        :data:`ReasoningEffort` levels; other models advertise an empty list
        — a definitive "supports none", not "unknown". Anthropic has no
        notion of a model-specific *default* effort (unlike the Copilot SDK),
        so ``default_reasoning_effort`` is always ``None``.

        ``max_prompt_tokens`` reuses :meth:`get_max_prompt_tokens` (the
        Anthropic SDK's ``max_input_tokens``). ``max_output_tokens`` and
        ``max_context_window_tokens`` are always ``None`` — the Anthropic
        SDK's ``models.list()`` exposes no output/total-context split.

        Unlike :meth:`get_max_prompt_tokens` (which only catches its
        documented ``(TimeoutError, AnthropicError, OSError)`` tuple and lets
        anything else propagate, by design, for its own caller), this hook
        upholds the base class's stricter "never raise" contract on its own:
        each field is resolved behind its own guard, so a failure in one
        (e.g. an unexpected exception from the delegated
        ``get_max_prompt_tokens`` call, or a non-string ``model``) degrades
        only that field rather than the whole result or the caller. The
        reasoning-effort fields are populated even when the SDK is
        unavailable, ``model`` can't be resolved, or the token-limit lookup
        fails (the heuristic is a pure name match independent of the SDK
        call), so this never returns ``None`` outright.
        """
        try:
            supported_reasoning_efforts = (
                list(get_args(ReasoningEffort)) if is_claude_thinking_model(model) else []
            )
        except Exception as e:  # noqa: BLE001 - diagnostics must never raise
            logger.debug("Failed to resolve reasoning-effort support for %r: %s", model, e)
            supported_reasoning_efforts = None
        try:
            max_prompt_tokens = await self.get_max_prompt_tokens(model)
        except Exception as e:  # noqa: BLE001 - diagnostics must never raise
            logger.debug("Failed to resolve max_prompt_tokens for %r: %s", model, e)
            max_prompt_tokens = None
        return ModelCapabilityInfo(
            supported_reasoning_efforts=supported_reasoning_efforts,
            default_reasoning_effort=None,
            max_prompt_tokens=max_prompt_tokens,
            max_output_tokens=None,
            max_context_window_tokens=None,
        )

    async def _get_mcp_manager_for_cwd(self, resolved_cwd: str) -> MCPManager | None:
        """Return the pooled MCPManager for ``resolved_cwd``, connecting on first use.

        Each distinct working directory gets its own MCPManager so stdio MCP
        servers are spawned with that directory as their ``cwd``. The lazy
        connect is guarded by a per-cwd ``asyncio.Lock`` so parallel agents
        resolving the same cwd observe exactly one manager (no duplicate
        spawns), while agents with different cwds proceed concurrently.

        Per-server connect is fail-open: a server that fails to connect is
        logged and skipped, and the manager is still pooled as long as at
        least one server connected. When NO servers connect the manager is
        returned but not pooled, so the next agent for the same cwd retries
        the connect (a transient spawn failure does not become permanent).

        Pool lifecycle is bounded by the provider lifetime — ``close()``
        shuts down every pooled manager. v1 intentionally has no
        eviction/LRU.

        Args:
            resolved_cwd: Absolute, normalized working directory that keys
                the pool (``agent.working_dir or os.getcwd()`` at the call
                site; the engine has already resolved ``agent.working_dir``
                to an absolute normpath).

        Returns:
            The pooled manager, or None when no MCP servers are configured
            or the MCP SDK is not installed.
        """
        # Fast path: already pooled.
        if resolved_cwd in self._mcp_managers:
            return self._mcp_managers[resolved_cwd]
        if not self._mcp_servers_config:
            return None

        from conductor.mcp.manager import MCP_SDK_AVAILABLE, MCPManager

        if not MCP_SDK_AVAILABLE:
            logger.warning(
                "MCP servers configured but MCP SDK not installed. "
                "Install with: uv add 'mcp>=1.0.0'"
            )
            return None

        # No guard needed around lock creation: there is no await between the
        # fast-path check above and the per-cwd lock acquisition below, so
        # concurrent coroutines cannot interleave here.
        lock = self._mcp_manager_locks.get(resolved_cwd)
        if lock is None:
            lock = asyncio.Lock()
            self._mcp_manager_locks[resolved_cwd] = lock

        async with lock:
            # Re-check under the per-cwd lock: a concurrent agent may have
            # connected while we were waiting.
            if resolved_cwd in self._mcp_managers:
                return self._mcp_managers[resolved_cwd]

            manager = MCPManager(tool_output=self._tool_output_config)
            for name, config in self._mcp_servers_config.items():
                server_type = config.get("type", "stdio")
                if server_type == "stdio":
                    try:
                        await manager.connect_server(
                            name=name,
                            command=config["command"],
                            args=config.get("args", []),
                            env=config.get("env"),
                            timeout=config.get("timeout"),
                            cwd=resolved_cwd,
                        )
                        logger.info(f"Connected to MCP server '{name}' (cwd={resolved_cwd})")
                    except Exception as e:
                        logger.error(f"Failed to connect to MCP server '{name}': {e}")
                        # Continue with other servers (fail-open per server)
                else:
                    logger.warning(
                        f"MCP server '{name}' has unsupported type '{server_type}' "
                        "(Claude provider only supports 'stdio')"
                    )

            if manager.has_servers():
                self._mcp_managers[resolved_cwd] = manager
            else:
                logger.warning(
                    "No MCP servers connected for cwd=%s; manager not pooled so "
                    "the next agent for this cwd will retry the connect.",
                    resolved_cwd,
                )
            return manager

    def _convert_mcp_tools_to_claude(
        self,
        tool_filter: list[str] | None = None,
        manager: MCPManager | None = None,
    ) -> list[dict[str, Any]]:
        """Convert MCP tools to Claude tool format.

        Args:
            tool_filter: Optional list of tool names to include (prefixed names).
                If None, all tools are included.
            manager: The pooled MCPManager for this agent's working directory.
                The manager is passed explicitly (never read from shared
                mutable provider state) so parallel agents with different
                cwds cannot observe each other's tools.

        Returns:
            List of tool definitions in Claude's expected format.
        """
        if not manager:
            return []

        claude_tools: list[dict[str, Any]] = []
        for tool in manager.get_all_tools():
            # Apply filter if specified
            if tool_filter and tool["name"] not in tool_filter:
                continue

            claude_tools.append(
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["input_schema"],
                }
            )

        return claude_tools

    async def close(self) -> None:
        """Release provider resources and close connections.

        Shuts down every pooled MCPManager (one per distinct working
        directory). Idempotent: a second call is a no-op.
        """
        # Close MCP connections first (all pool entries).
        if self._mcp_managers:
            for cwd, manager in self._mcp_managers.items():
                try:
                    await manager.close()
                except Exception as e:
                    logger.warning(f"Error closing MCP manager for cwd={cwd}: {e}")
            self._mcp_managers.clear()
            self._mcp_manager_locks.clear()
            logger.debug("All pooled MCP managers closed")

        if self._client is not None:
            # Drop the client reference *before* awaiting close() so any
            # in-flight get_max_prompt_tokens() observes None on its next
            # access and skips the SDK call. Already-issued requests will
            # error and be swallowed by the metadata path's narrow except.
            client = self._client
            self._client = None
            await client.close()
            logger.debug("Claude provider closed")

            # Drop cached metadata so a re-initialized provider re-fetches.
            self._max_input_cache = None

    async def execute_dialog_turn(
        self,
        system_prompt: str,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
    ) -> str:
        """Execute a single dialog turn using the Claude messages API.

        Creates a lightweight message call with the conversation context
        and returns the agent's response text.

        Args:
            system_prompt: System prompt providing dialog context.
            user_message: The latest user message.
            history: Optional prior conversation history.
            model: Optional model override. Falls back to provider default.

        Returns:
            The agent's response text.

        Raises:
            ProviderError: If the dialog turn fails.
        """
        if self._client is None:
            raise ProviderError(
                "Claude client not initialized",
                suggestion="Call validate_connection() first",
            )

        # Build messages list from history + current message
        messages: list[dict[str, str]] = []
        for msg in history or []:
            messages.append(
                {
                    "role": msg["role"],
                    "content": msg["content"],
                }
            )
        messages.append({"role": "user", "content": user_message})

        try:
            kwargs: dict[str, Any] = {
                "model": model or self._default_model,
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": messages,
            }

            # Apply workflow-wide default reasoning effort if configured.
            # Per-agent reasoning is not available here (no AgentDef in scope).
            # Mirrors _resolve_thinking_for_agent: raise ValidationError when
            # the resolved model does not support extended thinking, rather
            # than silently dropping the reasoning request.
            if self._default_reasoning_effort is not None:
                resolved_model = kwargs["model"]
                if not is_claude_thinking_model(resolved_model):
                    raise ValidationError(
                        f"Model {resolved_model!r} does not support extended thinking, "
                        f"but default_reasoning_effort={self._default_reasoning_effort!r} "
                        "was configured.",
                        suggestion=(
                            "Use a Claude 3.7+ or 4.x model (e.g. claude-opus-4-20250514, "
                            "claude-sonnet-4-20250514) or remove the reasoning config."
                        ),
                    )
                budget = effort_to_budget_tokens(self._default_reasoning_effort)
                thinking = {"type": "enabled", "budget_tokens": budget}
                kwargs["thinking"] = thinking
                # Reuse the same clamp/validate logic as the main agentic-loop
                # path (_coerce_for_thinking) instead of duplicating the
                # budget + headroom arithmetic here — this keeps dialog turns
                # subject to the same 64000-token per-model cap and the same
                # defensive raise if a future budget ever collapses below it.
                # No temperature kwarg is sent for dialog turns (omitting it
                # satisfies the Anthropic "1.0 or omitted" requirement), so
                # only the max_tokens half of the returned tuple is used.
                _, kwargs["max_tokens"] = self._coerce_for_thinking(
                    temperature=None,
                    max_tokens=kwargs["max_tokens"],
                    model=resolved_model,
                    thinking=thinking,
                )

            response = await self._client.messages.create(**kwargs)

            # Extract text from response (skip thinking blocks)
            text_parts = []
            for block in response.content:
                if hasattr(block, "type") and block.type == "thinking":
                    continue
                if hasattr(block, "text"):
                    text_parts.append(block.text)

            return "\n".join(text_parts) if text_parts else ""

        except ValidationError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Dialog turn failed: {exc}",
                is_retryable=False,
            ) from exc

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Execute an agent using the Claude SDK.

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context.
            rendered_prompt: Jinja2-rendered user prompt.
            tools: List of tool names available to this agent (currently unused).
            interrupt_signal: Optional event for mid-agent interrupt signaling.
                When set during the agentic loop, Claude is asked to emit
                partial output via the ``emit_output`` tool, and the result
                is returned with ``partial=True``.

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If SDK execution fails.
            ValidationError: If output doesn't match schema.
        """
        # Use retry logic wrapper for execution
        return await self._execute_with_retry(
            agent,
            context,
            rendered_prompt,
            tools,
            interrupt_signal=interrupt_signal,
            event_callback=event_callback,
        )

    def _resolve_retry_config(self, agent: AgentDef) -> RetryConfig:
        """Resolve the retry config for an agent.

        If the agent has a per-agent retry policy, build a RetryConfig from it.
        Otherwise, fall back to the provider-level default.

        Args:
            agent: Agent definition that may contain a retry policy.

        Returns:
            RetryConfig to use for this agent's execution.
        """
        from conductor.config.schema import RetryPolicy

        retry = getattr(agent, "retry", None)
        if not isinstance(retry, RetryPolicy):
            return self._retry_config

        return RetryConfig(
            max_attempts=retry.max_attempts,
            base_delay=retry.delay_seconds,
            max_delay=self._retry_config.max_delay,
            jitter=self._retry_config.jitter,
            backoff=retry.backoff,
            retry_on=list(retry.retry_on),
            max_parse_recovery_attempts=(
                retry.max_parse_recovery_attempts
                if retry.max_parse_recovery_attempts is not None
                else self._retry_config.max_parse_recovery_attempts
            ),
        )

    @staticmethod
    def _classify_error(error: Exception) -> str:
        """Classify an error into a retry category.

        Maps exception types to the retry_on categories used in per-agent
        retry policies.

        Args:
            error: The exception to classify.

        Returns:
            Error category string: "provider_error" or "timeout".
        """
        from conductor.exceptions import ProviderError
        from conductor.exceptions import TimeoutError as ConductorTimeoutError

        if isinstance(error, (ConductorTimeoutError, asyncio.TimeoutError)):
            return "timeout"
        if isinstance(error, ProviderError):
            if error.status_code == 408:
                return "timeout"
            if "timeout" in str(error).lower():
                return "timeout"
        return "provider_error"

    def _is_retryable_error(self, exception: Exception) -> bool:
        """Determine if an error should trigger a retry.

        Args:
            exception: The exception to check.

        Returns:
            True if the error is transient and should be retried.
        """
        # A ProviderError carries its own retry classification (e.g. parse
        # exhaustion is raised with is_retryable=False). Honor it directly
        # rather than falling through to SDK-type heuristics that would never
        # match it.
        if isinstance(exception, ProviderError):
            return exception.is_retryable

        if anthropic is None:
            return False

        # Always retry these (use try-except to handle mocked exceptions)
        try:
            if isinstance(
                exception,
                (
                    anthropic.APIConnectionError,
                    anthropic.RateLimitError,
                    anthropic.APITimeoutError,
                ),
            ):
                return True
        except TypeError:
            # Handle mocked anthropic module in tests
            # Check by class name instead (includes Mock versions for testing)
            error_type_name = type(exception).__name__
            if error_type_name in (
                "APIConnectionError",
                "RateLimitError",
                "APITimeoutError",
                "MockRateLimitError",  # For testing
                "MockAPIConnectionError",  # For testing
                "MockAPITimeoutError",  # For testing
            ):
                return True

        # Check HTTP status codes for APIStatusError
        try:
            if isinstance(exception, anthropic.APIStatusError):
                # 5xx errors are retryable
                if 500 <= exception.status_code < 600:
                    return True
                # 429 is also retryable (though RateLimitError should catch this)
                if exception.status_code == 429:
                    return True
        except (TypeError, AttributeError):
            # Handle mocked exceptions - check by name and attributes
            error_type_name = type(exception).__name__
            is_api_status = error_type_name in ("APIStatusError", "MockAPIStatusError")
            if is_api_status and hasattr(exception, "status_code"):
                status_code: int = int(exception.status_code)  # type: ignore[attr-defined]
                if 500 <= status_code < 600 or status_code == 429:
                    return True

        # Everything else is non-retryable
        return False

    def _get_retry_after(self, exception: Exception) -> float | None:
        """Extract retry-after value from rate limit exception.

        Validated against Anthropic SDK exception structure via Context7 docs.
        RateLimitError inherits from APIStatusError which provides response attribute.

        Args:
            exception: The exception to check for retry-after header.

        Returns:
            Retry-after delay in seconds, or None if not present.
        """
        if anthropic is None:
            return None

        # Check if this is a RateLimitError (handle both real and mocked)
        is_rate_limit = False
        try:
            is_rate_limit = isinstance(exception, anthropic.RateLimitError)
        except TypeError:
            # Handle mocked exceptions
            is_rate_limit = type(exception).__name__ in ("RateLimitError", "MockRateLimitError")

        if is_rate_limit and hasattr(exception, "response") and exception.response:
            # Check response headers for retry-after
            # Anthropic SDK APIStatusError provides .response attribute with headers dict
            headers = getattr(exception.response, "headers", {})
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    pass
        return None

    def _calculate_delay(self, attempt: int, config: RetryConfig) -> float:
        """Calculate delay with backoff and jitter.

        Supports both exponential and fixed backoff strategies.

        Args:
            attempt: Current attempt number (1-indexed).
            config: Retry configuration.

        Returns:
            Delay in seconds before next retry.
        """
        if config.backoff == "fixed":
            delay = config.base_delay
        else:
            # Exponential backoff: base * 2^(attempt-1)
            delay = config.base_delay * (2 ** (attempt - 1))

        # Cap at max delay
        delay = min(delay, config.max_delay)

        # Add jitter (random fraction of delay)
        if config.jitter > 0:
            jitter_amount = delay * config.jitter * random.random()
            delay += jitter_amount

        return delay

    async def _execute_with_retry(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Execute with exponential backoff retry logic and MCP tool support.

        This method implements an agentic loop that:
        1. Sends the initial prompt to Claude
        2. If Claude returns tool_use blocks (other than emit_output), executes them
        3. Sends tool results back to Claude and continues the loop
        4. Terminates when Claude returns emit_output or a final text response

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context.
            rendered_prompt: Jinja2-rendered user prompt.
            tools: List of tool names available to this agent (for MCP tool filtering).
            interrupt_signal: Optional event for mid-agent interrupt signaling.
            event_callback: Optional callback for streaming SDK events upstream.

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If execution fails after all retry attempts.
            ValidationError: If output validation fails.
        """
        if self._client is None:
            raise ProviderError("Claude client not initialized")

        # Resolve this agent's MCP manager from the cwd pool (lazy connect).
        # The manager is a LOCAL variable threaded through the whole agentic
        # loop — never stored as shared mutable provider state — so parallel
        # agents with different working directories stay isolated.
        resolved_cwd = agent.working_dir or os.getcwd()
        mcp_manager = await self._get_mcp_manager_for_cwd(resolved_cwd)

        last_error: Exception | None = None
        config = self._resolve_retry_config(agent)

        # Build messages
        messages = self._build_messages(rendered_prompt)

        # Get model and parameters
        model = agent.model or self._default_model
        temperature = self._default_temperature
        max_tokens = self._default_max_tokens

        # Resolve per-agent iteration and session limits
        max_agent_iterations = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else self._default_max_agent_iterations
        )
        max_session_seconds = (
            agent.max_session_seconds
            if agent.max_session_seconds is not None
            else self._default_max_session_seconds
        )

        # Resolve extended-thinking kwarg (validates model compatibility).
        # Done before the per-model max_tokens warning so the warning logic
        # accounts for thinking-aware caps.
        thinking = self._resolve_thinking_for_agent(agent, model)
        # Use strip() only as a blank-prompt predicate: the rendered value is
        # forwarded verbatim for cross-provider parity (Copilot, Claude Agent
        # SDK, and Hermes all pass agent.system_prompt through unchanged).
        raw_system_prompt = agent.system_prompt
        system_prompt = (
            raw_system_prompt if raw_system_prompt and raw_system_prompt.strip() else None
        )

        # Validate max_tokens against model-specific limits.
        # Skip the warning when extended thinking is enabled — the per-call
        # cap is bumped to at least ``budget_tokens + 4096`` (capped at
        # 64000) by _coerce_for_thinking() to satisfy the
        # ``max_tokens > budget_tokens`` constraint.
        if thinking is None:
            if "haiku" in model.lower():
                if max_tokens > 4096:
                    logger.warning(
                        f"max_tokens={max_tokens} exceeds Haiku model limit of 4096. "
                        "API may reject request."
                    )
            elif max_tokens > 8192:
                logger.warning(
                    f"max_tokens={max_tokens} exceeds Sonnet/Opus model limit of 8192. "
                    "API may reject request."
                )

        # Build tools list: emit_output (for structured output) + MCP tools
        all_tools: list[dict[str, Any]] = []

        # Effective schema check: skip structured output when output_mode is raw
        output_schema = agent.effective_output_schema()
        has_output_schema = output_schema is not None

        # Add emit_output tool if agent has effective output schema
        if output_schema is not None:
            all_tools.extend(self._build_tools_for_structured_output(output_schema))
            # Append instruction to use the tool
            messages[-1]["content"] += (
                "\n\nPlease use the 'emit_output' tool to return your response "
                "in the required structured format."
            )

        # Add MCP tools if available
        if mcp_manager and mcp_manager.has_servers():
            mcp_tools = self._convert_mcp_tools_to_claude(tools, mcp_manager)  # tools is the filter
            all_tools.extend(mcp_tools)
            if mcp_tools:
                logger.debug(f"Added {len(mcp_tools)} MCP tools to request")

        # Use tools if any are defined
        request_tools: list[dict[str, Any]] | None = all_tools if all_tools else None

        for attempt in range(1, config.max_attempts + 1):
            try:
                # Execute with agentic tool loop
                response, total_tokens, is_partial = await self._execute_agentic_loop(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=request_tools,
                    output_schema=output_schema,
                    has_output_schema=has_output_schema,
                    max_iterations=max_agent_iterations,
                    max_session_seconds=max_session_seconds,
                    interrupt_signal=interrupt_signal,
                    event_callback=event_callback,
                    thinking=thinking,
                    max_parse_recovery_attempts=config.max_parse_recovery_attempts,
                    system_prompt=system_prompt,
                    mcp_manager=mcp_manager,
                )

                # Handle partial output from mid-agent interrupt
                if is_partial:
                    partial_content: dict[str, Any]
                    try:
                        partial_content = self._extract_output(response, agent.output)
                    except Exception:
                        # Best-effort extraction; fall back to text content
                        partial_content = self._extract_text_content(response)

                    tokens_used = (
                        total_tokens if total_tokens else self._extract_token_usage(response)
                    )
                    return AgentOutput(
                        content=partial_content,
                        raw_response=response,
                        tokens_used=tokens_used,
                        model=model,
                        partial=True,
                    )

                # Extract structured output
                content = self._extract_output(response, output_schema)

                # Validate output if schema is defined
                if output_schema is not None:
                    validate_output(content, output_schema)

                # Use total_tokens from the agentic loop (includes all turns)
                # If available, use it; otherwise fall back to extracting from final response
                tokens_used = total_tokens if total_tokens else self._extract_token_usage(response)

                # Extract detailed token breakdown from final response
                # Note: For multi-turn conversations, this only shows the final turn's breakdown
                input_tokens = None
                output_tokens = None
                cache_read_tokens = None
                cache_write_tokens = None

                if hasattr(response, "usage"):
                    usage = response.usage
                    input_tokens = getattr(usage, "input_tokens", None)
                    output_tokens = getattr(usage, "output_tokens", None)
                    cache_read_tokens = getattr(usage, "cache_read_input_tokens", None)
                    cache_write_tokens = getattr(usage, "cache_creation_input_tokens", None)

                return AgentOutput(
                    content=content,
                    raw_response=response,
                    tokens_used=tokens_used,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    model=model,
                )

            except ValidationError:
                # Re-raise ValidationError without wrapping (non-retryable)
                raise

            except Exception as e:
                last_error = e

                # Check if it's a BadRequestError from temperature validation
                if anthropic is not None:
                    try:
                        has_attr = hasattr(anthropic, "BadRequestError")
                        is_bad_request = has_attr and isinstance(e, anthropic.BadRequestError)
                        if is_bad_request and "temperature" in str(e).lower():
                            raise ValidationError(
                                f"Temperature validation failed: {e}",
                                suggestion=(
                                    "Temperature must be between 0.0 and 1.0 "
                                    "(enforced by Claude SDK)"
                                ),
                            ) from e
                    except TypeError:
                        # isinstance can fail if BadRequestError is not a proper type
                        pass

                # Determine if error is retryable
                is_retryable = self._is_retryable_error(e)

                # Track retry history with consistent metadata
                retry_entry = {
                    "attempt": attempt,
                    "agent_name": agent.name,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "is_retryable": is_retryable,
                }
                self._retry_history.append(retry_entry)

                # Log retry information
                logger.debug(
                    f"Execution attempt {attempt} failed: {type(e).__name__}: {e} "
                    f"(retryable={is_retryable})"
                )

                # Don't retry non-retryable errors
                if not is_retryable:
                    # Extract status code consistently
                    status_code = self._extract_status_code(e)

                    # Wrap as ProviderError with proper metadata
                    if status_code is not None:
                        raise ProviderError(
                            f"Claude API error: {e}",
                            suggestion="Check API key, model name, and request parameters",
                            status_code=status_code,
                            is_retryable=False,
                        ) from e
                    else:
                        raise ProviderError(
                            f"Claude API call failed: {e}",
                            suggestion="Check API key, model name, and request parameters",
                            is_retryable=False,
                        ) from e

                # Check retry_on filter if per-agent retry is configured
                if config.retry_on is not None:
                    error_category = self._classify_error(e)
                    if error_category not in config.retry_on:
                        raise

                # Don't retry if this was the last attempt
                if attempt >= config.max_attempts:
                    break

                # Check for retry-after header (overrides calculated delay)
                retry_after = self._get_retry_after(e)
                if retry_after is not None:
                    delay = retry_after
                    logger.warning(
                        f"Rate limit hit (HTTP 429), respecting retry-after header: {delay}s"
                    )
                else:
                    # Calculate delay with backoff
                    delay = self._calculate_delay(attempt, config)
                    logger.info(f"Calculated backoff delay: {delay:.2f}s for attempt {attempt}")

                # Log retry attempt with full context
                logger.warning(
                    f"[Retry {attempt}/{config.max_attempts}] Retrying after {delay:.2f}s "
                    f"due to {type(e).__name__}: {e}"
                )
                retry_entry["delay"] = delay

                # Emit agent_retry event
                if event_callback is not None:
                    with contextlib.suppress(Exception):
                        event_callback(
                            "agent_retry",
                            {
                                "agent_name": agent.name,
                                "attempt": attempt,
                                "max_attempts": config.max_attempts,
                                "error": str(e),
                                "error_type": type(e).__name__,
                                "delay": delay,
                            },
                        )

                await asyncio.sleep(delay)

        # All retries exhausted
        raise ProviderError(
            f"Claude API call failed after {config.max_attempts} attempts: {last_error}",
            suggestion=(f"Check API connectivity and rate limits. Last error: {last_error}"),
            is_retryable=False,
        )

    def _extract_status_code(self, exception: Exception) -> int | None:
        """Extract HTTP status code from exception if available.

        Args:
            exception: Exception to extract status code from.

        Returns:
            HTTP status code or None if not available.
        """
        # Try to extract from APIStatusError
        try:
            if anthropic and isinstance(exception, anthropic.APIStatusError):
                return exception.status_code
        except TypeError:
            # Handle mocked exceptions - check by attribute
            if hasattr(exception, "status_code"):
                status_code = getattr(exception, "status_code", None)
                if status_code is not None:
                    return int(status_code)

        return None

    def _coerce_for_thinking(
        self,
        temperature: float | None,
        max_tokens: int,
        model: str,
        thinking: dict[str, Any] | None,
    ) -> tuple[float | None, int]:
        """Adjust temperature and max_tokens to satisfy thinking constraints.

        When extended thinking is enabled the Anthropic API requires:

        - ``temperature == 1.0`` (or omitted)
        - ``max_tokens > budget_tokens``

        We force temperature to 1.0 (logging an info note if the caller
        configured a different non-1.0 value) and bump ``max_tokens`` to
        at least ``budget_tokens + 4096``, clamped to a per-model cap.
        Extended-thinking models accept up to 64000 output tokens, which
        is what we use here.

        When ``thinking`` is ``None`` the inputs are returned unchanged.

        Args:
            temperature: User-configured temperature (may be ``None``).
            max_tokens: User-configured max output tokens.
            model: Resolved model identifier.
            thinking: Resolved thinking kwarg or ``None``.

        Returns:
            Tuple of ``(effective_temperature, effective_max_tokens)``.
        """
        if thinking is None:
            return temperature, max_tokens

        budget = int(thinking.get("budget_tokens", 0))
        # Per-model cap when thinking is enabled. Extended-thinking models
        # accept up to CLAUDE_EXTENDED_THINKING_OUTPUT_CAP output tokens.
        per_model_cap = CLAUDE_EXTENDED_THINKING_OUTPUT_CAP
        required = budget + CLAUDE_ANSWER_HEADROOM_TOKENS
        effective_max_tokens = max(max_tokens, required)
        if effective_max_tokens > per_model_cap:
            logger.info(
                "Clamping max_tokens %s to %s for extended thinking on model %s "
                "(Anthropic API per-model cap)",
                effective_max_tokens,
                per_model_cap,
                model,
            )
            effective_max_tokens = per_model_cap
        if effective_max_tokens <= budget:
            # Defensive: if cap collapses below budget+1, this would still
            # violate the API constraint. Raise rather than silently send a
            # request the API will reject.
            raise ValidationError(
                f"Cannot satisfy thinking budget_tokens={budget} on model "
                f"{model!r}: per-model cap {per_model_cap} is not greater "
                f"than the requested budget.",
                suggestion="Lower reasoning.effort or use a model with a higher cap.",
            )

        if temperature is not None and temperature != 1.0:
            logger.info(
                "Coercing temperature %s to 1.0 for extended thinking on model %s "
                "(Anthropic API requirement)",
                temperature,
                model,
            )

        return 1.0, effective_max_tokens

    async def _execute_api_call(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
        thinking: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> ClaudeResponse:
        """Execute non-streaming Claude API call using AsyncAnthropic.

        This method makes an asynchronous (non-streaming) call to the Claude
        messages.create() API endpoint. It does not handle streaming responses.

        Args:
            messages: Message history to send.
            model: Model identifier.
            temperature: Temperature setting (0.0-1.0, enforced by SDK).
            max_tokens: Maximum output tokens.
            tools: Optional tool definitions for structured output.
            thinking: Optional extended-thinking kwarg for the SDK. When
                supplied, ``temperature`` is forced to 1.0 and ``max_tokens``
                is bumped to satisfy the API constraint
                ``max_tokens > budget_tokens``.
            system_prompt: Optional rendered system prompt passed as the
                top-level Anthropic ``system`` parameter.

        Returns:
            Claude API response object with content blocks and usage metadata.

        Raises:
            ProviderError: If client not initialized or API call fails.

        Note:
            This is a non-streaming implementation. Streaming support is
            deferred to Phase 2+ of the Claude SDK integration.
        """
        if self._client is None:
            raise ProviderError("Claude client not initialized")

        effective_temperature, effective_max_tokens = self._coerce_for_thinking(
            temperature, max_tokens, model, thinking
        )

        # Build API call kwargs
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": effective_max_tokens,
        }

        if effective_temperature is not None:
            kwargs["temperature"] = effective_temperature

        if tools:
            kwargs["tools"] = tools

        if thinking is not None:
            kwargs["thinking"] = thinking

        if system_prompt:
            kwargs["system"] = system_prompt

        # Execute non-streaming API call (async)
        logger.debug(
            f"Executing non-streaming Claude API call: model={model}, "
            f"max_tokens={effective_max_tokens}, timeout={self._timeout}s, "
            f"thinking={'enabled' if thinking else 'disabled'}"
        )
        response = await self._client.messages.create(**kwargs)

        return response

    async def _execute_agentic_loop(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        output_schema: dict[str, OutputField] | None,
        has_output_schema: bool,
        max_iterations: int = 50,
        max_session_seconds: float | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        thinking: dict[str, Any] | None = None,
        max_parse_recovery_attempts: int | None = None,
        system_prompt: str | None = None,
        mcp_manager: MCPManager | None = None,
    ) -> tuple[ClaudeResponse, int | None, bool]:
        """Execute an agentic loop that handles MCP tool calls.

        This method implements a tool-use loop:
        1. Call the Claude API
        2. If Claude returns tool_use blocks (other than emit_output), execute them
        3. Send tool results back and continue the loop
        4. Terminate when Claude returns emit_output or a final text response

        If ``interrupt_signal`` is set at the start of an iteration, the loop
        appends a user message asking Claude to call ``emit_output`` with its
        best partial result. The response is returned with ``partial=True``.

        Args:
            messages: Initial message history.
            model: Model identifier.
            temperature: Temperature setting.
            max_tokens: Maximum output tokens.
            tools: Tool definitions (emit_output + MCP tools).
            output_schema: Expected output schema.
            has_output_schema: Whether agent has output schema defined.
            max_iterations: Maximum number of tool-use iterations to prevent infinite loops.
            max_session_seconds: Maximum wall-clock duration for this agentic loop.
                None means no time limit.
            interrupt_signal: Optional event that signals a mid-agent interrupt.
            event_callback: Optional callback for streaming SDK events upstream.
            thinking: Optional extended-thinking kwarg forwarded to every API call.
            max_parse_recovery_attempts: Resolved per-agent parse recovery limit.
                None means use the provider-level default.
            system_prompt: Optional rendered system prompt forwarded to every API call.
            mcp_manager: Pooled MCPManager for this agent's working directory.
                Passed explicitly (never read from shared provider state) so
                parallel agents with different cwds execute their tool calls
                against the correct per-cwd connection pool.

        Returns:
            Tuple of (final_response, total_tokens_used, is_partial).

        Raises:
            ProviderError: If execution fails or max iterations exceeded.
        """
        # Make a copy of messages to avoid mutating the original
        working_messages = list(messages)
        total_tokens = 0
        iteration = 0
        session_start = time.monotonic()

        while iteration < max_iterations:
            iteration += 1
            logger.debug(f"Agentic loop iteration {iteration}/{max_iterations}")

            # Check wall-clock session timeout
            if max_session_seconds is not None:
                elapsed = time.monotonic() - session_start
                if elapsed > max_session_seconds:
                    raise ProviderError(
                        f"Agent exceeded maximum session duration of {max_session_seconds:.0f}s "
                        f"after {iteration} tool-use iterations",
                        is_retryable=False,
                    )

            # Emit turn start event
            if event_callback:
                try:
                    event_callback("agent_turn_start", {"turn": iteration})
                except Exception:
                    logger.debug("Error in event_callback for agent_turn_start", exc_info=True)

            # Check for mid-agent interrupt at top of each iteration
            if interrupt_signal is not None and interrupt_signal.is_set():
                interrupt_signal.clear()
                logger.info("Mid-agent interrupt detected in Claude agentic loop")

                # Ask Claude to emit partial output via a user message
                interrupt_response, interrupt_tokens = await self._request_partial_output(
                    working_messages=working_messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    has_output_schema=has_output_schema,
                    thinking=thinking,
                    system_prompt=system_prompt,
                )
                total_tokens += interrupt_tokens
                return interrupt_response, total_tokens, True

            # Execute API call (with parse recovery for structured output)
            if event_callback:
                try:
                    event_callback("agent_turn_start", {"turn": "awaiting_model"})
                except Exception:
                    logger.debug("Error in event_callback for awaiting_model", exc_info=True)

            # Race API call against interrupt signal so user can abort
            # a long-running API call (not just between iterations).
            # Applied to both structured-output and regular paths for
            # provider parity.
            if interrupt_signal is not None:
                if has_output_schema:
                    api_task = asyncio.create_task(
                        self._execute_with_parse_recovery(
                            messages=working_messages,
                            model=model,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            tools=tools,
                            output_schema=output_schema,
                            thinking=thinking,
                            max_parse_recovery_attempts=max_parse_recovery_attempts,
                            system_prompt=system_prompt,
                        )
                    )
                else:
                    api_task = asyncio.create_task(
                        self._execute_api_call(
                            messages=working_messages,
                            model=model,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            tools=tools,
                            thinking=thinking,
                            system_prompt=system_prompt,
                        )
                    )
                interrupt_task = asyncio.create_task(interrupt_signal.wait())
                try:
                    finished, pending = await asyncio.wait(
                        {api_task, interrupt_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await t
                except Exception:
                    for t in (api_task, interrupt_task):
                        if not t.done():
                            t.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await t
                    raise

                if interrupt_task in finished:
                    logger.info("Mid-agent interrupt during Claude API call")
                    interrupt_signal.clear()
                    partial_resp, partial_tokens = await self._request_partial_output(
                        working_messages=working_messages,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                        has_output_schema=has_output_schema,
                        thinking=thinking,
                        system_prompt=system_prompt,
                    )
                    total_tokens += partial_tokens
                    return partial_resp, total_tokens, True

                response = await api_task
            elif has_output_schema:
                response = await self._execute_with_parse_recovery(
                    messages=working_messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    output_schema=output_schema,
                    thinking=thinking,
                    max_parse_recovery_attempts=max_parse_recovery_attempts,
                    system_prompt=system_prompt,
                )
            else:
                response = await self._execute_api_call(
                    messages=working_messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    thinking=thinking,
                    system_prompt=system_prompt,
                )

            # Accumulate token usage
            if hasattr(response, "usage"):
                input_tokens = getattr(response.usage, "input_tokens", 0)
                output_tokens = getattr(response.usage, "output_tokens", 0)
                total_tokens += input_tokens + output_tokens

            # Emit agent_message events for text blocks in the response
            if event_callback:
                for block in response.content:
                    if hasattr(block, "type") and block.type == "text" and block.text:
                        try:
                            event_callback("agent_message", {"content": block.text})
                        except Exception:
                            logger.debug("Error in event_callback for agent_message", exc_info=True)
                    elif hasattr(block, "type") and block.type == "thinking":
                        thinking_text = getattr(block, "thinking", None) or getattr(
                            block, "text", None
                        )
                        if thinking_text:
                            try:
                                event_callback("agent_reasoning", {"content": thinking_text})
                            except Exception:
                                logger.debug(
                                    "Error in event_callback for agent_reasoning",
                                    exc_info=True,
                                )

            # Check for tool_use blocks
            tool_uses = [
                block
                for block in response.content
                if hasattr(block, "type") and block.type == "tool_use"
            ]

            if not tool_uses:
                # No tool calls, we're done
                logger.debug("No tool_use in response, exiting agentic loop")
                return response, total_tokens, False

            # Check if emit_output was called (structured output)
            emit_output = next((t for t in tool_uses if t.name == "emit_output"), None)
            if emit_output:
                # Final output received, we're done
                logger.debug("emit_output tool called, exiting agentic loop")
                return response, total_tokens, False

            # Handle MCP tool calls
            mcp_tool_uses = [t for t in tool_uses if t.name != "emit_output"]

            if not mcp_tool_uses:
                # No MCP tools to execute
                return response, total_tokens, False

            if not mcp_manager:
                logger.warning(
                    f"Claude called MCP tools but no MCP manager available: "
                    f"{[t.name for t in mcp_tool_uses]}"
                )
                return response, total_tokens, False

            logger.info(
                f"Executing {len(mcp_tool_uses)} MCP tool call(s): "
                f"{[t.name for t in mcp_tool_uses]}"
            )

            # Execute each MCP tool call
            tool_results: list[dict[str, Any]] = []
            for tool_use in mcp_tool_uses:
                # Emit tool start event
                if event_callback:
                    try:
                        arguments = (
                            format_tool_arguments(dict(tool_use.input))
                            if hasattr(tool_use, "input") and tool_use.input
                            else None
                        )
                        event_callback(
                            "agent_tool_start",
                            {"tool_name": tool_use.name, "arguments": arguments},
                        )
                    except Exception:
                        logger.debug("Error in event_callback for agent_tool_start", exc_info=True)

                try:
                    result = await mcp_manager.call_tool(
                        tool_use.name, dict(tool_use.input) if hasattr(tool_use, "input") else {}
                    )
                    result = self._maybe_rewrite_truncation_hint(result, tools)

                    truncation_info = self._parse_truncation_marker(result)
                    if truncation_info is not None and event_callback:
                        try:
                            event_callback(
                                "agent_tool_output_truncated",
                                {
                                    "tool_name": tool_use.name,
                                    "original_chars": truncation_info["original_chars"],
                                    "kept_chars": truncation_info["kept_chars"],
                                    "spill_path": truncation_info["spill_path"],
                                },
                            )
                        except Exception:
                            logger.debug(
                                "Error in event_callback for agent_tool_output_truncated",
                                exc_info=True,
                            )

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result,
                        }
                    )
                    logger.debug(f"MCP tool '{tool_use.name}' succeeded")

                    # Emit tool complete event (success)
                    if event_callback:
                        try:
                            event_callback(
                                "agent_tool_complete",
                                {
                                    "tool_name": tool_use.name,
                                    "result": extract_tool_result_text(result),
                                },
                            )
                        except Exception:
                            logger.debug(
                                "Error in event_callback for agent_tool_complete",
                                exc_info=True,
                            )

                except Exception as e:
                    logger.error(f"MCP tool '{tool_use.name}' failed: {e}")
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": f"Error executing tool: {e}",
                            "is_error": True,
                        }
                    )

                    # Emit tool complete event (failure)
                    if event_callback:
                        try:
                            event_callback(
                                "agent_tool_complete",
                                {
                                    "tool_name": tool_use.name,
                                    "result": f"Error: {e}",
                                },
                            )
                        except Exception:
                            logger.debug(
                                "Error in event_callback for agent_tool_complete",
                                exc_info=True,
                            )

            # Build assistant message with the tool_use content
            # We need to serialize the content blocks properly
            assistant_content: list[dict[str, Any]] = []
            for block in response.content:
                if hasattr(block, "type"):
                    if block.type == "text":
                        assistant_content.append(
                            {
                                "type": "text",
                                "text": block.text,
                            }
                        )
                    elif block.type == "tool_use":
                        assistant_content.append(
                            {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": dict(block.input) if hasattr(block, "input") else {},
                            }
                        )
                    elif block.type == "thinking":
                        # Extended thinking requires the unmodified thinking
                        # blocks (with signature) to be echoed back before
                        # any tool_use blocks they preceded — otherwise the
                        # API rejects the next request with a 400.
                        block_dict: dict[str, Any] = {
                            "type": "thinking",
                            "thinking": block.thinking,
                        }
                        sig = getattr(block, "signature", None)
                        if sig is not None:
                            block_dict["signature"] = sig
                        assistant_content.append(block_dict)
                    elif block.type == "redacted_thinking":
                        assistant_content.append({"type": "redacted_thinking", "data": block.data})

            # Add assistant response and tool results to message history
            working_messages.append(
                {
                    "role": "assistant",
                    "content": assistant_content,
                }
            )
            working_messages.append(
                {
                    "role": "user",
                    "content": tool_results,
                }
            )

        # Max iterations exceeded
        raise ProviderError(
            f"Agentic loop exceeded maximum iterations ({max_iterations})",
            suggestion="The agent may be stuck in a tool-use loop. Check your MCP tools.",
        )

    async def _request_partial_output(
        self,
        working_messages: list[dict[str, Any]],
        model: str,
        temperature: float | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        has_output_schema: bool,
        thinking: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> tuple[Any, int]:
        """Send a final API call requesting partial output after interrupt.

        Appends a user message asking Claude to call ``emit_output`` with
        its best partial result. If ``emit_output`` is not available (no
        output schema), asks for a text summary instead.

        Uses a copy of ``working_messages`` so the caller's history is
        not mutated by the interrupt prompt.

        Args:
            working_messages: Current message history (not modified).
            model: Model identifier.
            temperature: Temperature setting.
            max_tokens: Maximum output tokens.
            tools: Tool definitions (may include emit_output).
            has_output_schema: Whether the agent defines an output schema.
            thinking: Optional extended-thinking kwarg forwarded to the API call.
            system_prompt: Optional rendered system prompt forwarded to the API call.

        Returns:
            Tuple of (response, tokens_used_in_this_call).
        """
        if has_output_schema:
            interrupt_prompt = (
                "The user has interrupted execution. Please immediately call the "
                "'emit_output' tool with your best partial result based on the work "
                "completed so far. Return whatever you have, even if incomplete."
            )
        else:
            interrupt_prompt = (
                "The user has interrupted execution. Please immediately provide "
                "your best partial result based on the work completed so far. "
                "Return whatever you have, even if incomplete."
            )

        messages_copy = list(working_messages)
        messages_copy.append({"role": "user", "content": interrupt_prompt})

        response = await self._execute_api_call(
            messages=messages_copy,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            thinking=thinking,
            system_prompt=system_prompt,
        )

        call_tokens = 0
        if hasattr(response, "usage"):
            call_tokens = getattr(response.usage, "input_tokens", 0) + getattr(
                response.usage, "output_tokens", 0
            )

        return response, call_tokens

    async def _execute_with_parse_recovery(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        output_schema: dict[str, OutputField] | None,
        thinking: dict[str, Any] | None = None,
        max_parse_recovery_attempts: int | None = None,
        system_prompt: str | None = None,
    ) -> ClaudeResponse:
        """Execute API call with parse recovery for malformed JSON responses.

        This method handles the fallback case where Claude returns text instead
        of using the tool, and the text contains malformed JSON. It will retry
        up to max_parse_recovery_attempts times with clarifying prompts.

        Args:
            messages: Message history to send.
            model: Model identifier.
            temperature: Temperature setting.
            max_tokens: Maximum output tokens.
            tools: Tool definitions for structured output.
            output_schema: Expected output schema (None if no schema).
            thinking: Optional extended-thinking kwarg forwarded to every API call.
            max_parse_recovery_attempts: Resolved per-agent parse recovery limit.
                None means use the provider-level default.
            system_prompt: Optional rendered system prompt forwarded to every API call.

        Returns:
            Claude API response.

        Raises:
            ProviderError: If all retry attempts fail with context about attempts.
        """
        effective_max_recovery = (
            max_parse_recovery_attempts
            if max_parse_recovery_attempts is not None
            else self._retry_config.max_parse_recovery_attempts
        )
        # Track recovery attempts for error reporting
        recovery_history: list[str] = []

        # Initial attempt using non-streaming API call
        response = await self._execute_api_call(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            thinking=thinking,
            system_prompt=system_prompt,
        )

        # If no output schema, return immediately (no recovery needed)
        if not output_schema:
            return response

        # Check if we got tool_use (success path)
        if self._extract_structured_output(response) is not None:
            logger.debug("Successfully extracted structured output from tool_use block")
            return response

        # Check for MCP tool calls — return to agentic loop for execution
        if self._has_mcp_tool_use(response):
            logger.debug("Response contains MCP tool calls, returning to agentic loop")
            return response

        # Check if we can extract JSON from text (fallback success path)
        json_content = self._extract_json_fallback(response)
        if json_content is not None:
            logger.warning(
                "Claude returned text instead of tool_use, but JSON extraction succeeded "
                "via fallback parsing. Consider reviewing prompt to encourage tool usage."
            )
            return response

        # Parse recovery: JSON extraction failed
        initial_text = self._extract_text_from_response(response)
        failure_reason = self._diagnose_json_failure(initial_text)
        recovery_history.append(f"Attempt 0 (initial): {failure_reason}")
        logger.warning(
            f"Initial JSON extraction failed: {failure_reason}. "
            f"Starting parse recovery (max {effective_max_recovery} attempts)"
        )

        for attempt in range(1, effective_max_recovery + 1):
            logger.info(f"Parse recovery attempt {attempt}/{effective_max_recovery}")

            # Append recovery message with specific error context
            recovery_messages = messages.copy()
            recovery_messages.append(
                {
                    "role": "assistant",
                    "content": initial_text,
                }
            )
            recovery_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Your previous response did not contain valid JSON. {failure_reason} "
                        "Please provide your response in valid JSON format.\n\n"
                        "IMPORTANT: Use the 'emit_output' tool to return your response "
                        "in the required structured format."
                    ),
                }
            )

            # Retry API call using non-streaming method
            response = await self._execute_api_call(
                messages=recovery_messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                thinking=thinking,
                system_prompt=system_prompt,
            )

            # Check if recovery succeeded (tool_use)
            if self._extract_structured_output(response) is not None:
                logger.info(f"Parse recovery succeeded on attempt {attempt} (tool_use)")
                return response

            # Check for MCP tool calls — return to agentic loop for execution
            if self._has_mcp_tool_use(response):
                logger.debug(
                    f"Recovery attempt {attempt} returned MCP tool calls, returning to agentic loop"
                )
                return response

            # Check if recovery succeeded (JSON fallback)
            json_content = self._extract_json_fallback(response)
            if json_content is not None:
                logger.info(f"Parse recovery succeeded on attempt {attempt} (JSON)")
                return response

            # Record failure for this attempt
            attempt_text = self._extract_text_from_response(response)
            attempt_failure = self._diagnose_json_failure(attempt_text)
            recovery_history.append(f"Attempt {attempt}: {attempt_failure}")
            logger.warning(f"Parse recovery attempt {attempt} failed: {attempt_failure}")
            # Update for next iteration
            initial_text = attempt_text
            failure_reason = attempt_failure

        # All recovery attempts exhausted - raise detailed error
        logger.error(
            f"Parse recovery exhausted after {effective_max_recovery} attempts. "
            f"History: {'; '.join(recovery_history)}"
        )
        # is_retryable=False marks this as terminal: _is_retryable_error()
        # honors the flag directly for ProviderError, so the outer retry loop
        # will not retry parse exhaustion.
        raise ProviderError(
            f"Failed to extract valid JSON after {effective_max_recovery} recovery attempts",
            suggestion=(
                "Claude did not use the emit_output tool and returned invalid JSON. "
                f"Recovery history: {'; '.join(recovery_history)}. "
                "Tip: if this agent produces large or free-form output, "
                "add 'output_mode: raw' to skip JSON extraction."
            ),
            is_retryable=False,
        )

    def _diagnose_json_failure(self, text: str) -> str:
        """Diagnose why JSON extraction failed from text response.

        Args:
            text: The text content that failed to parse.

        Returns:
            Human-readable diagnosis of the failure.
        """
        if not text.strip():
            return "Response was empty."

        # Check for incomplete JSON patterns
        if "{" in text and "}" not in text:
            return "Found incomplete JSON (opening brace without closing)."
        if "[" in text and "]" not in text:
            return "Found incomplete JSON (opening bracket without closing)."

        # Check if it looks like JSON but has syntax errors
        if text.strip().startswith(("{", "[")):
            return "Found malformed JSON (syntax error in structure)."

        # Try to find JSON code block
        if "```" in text:
            if "```json" not in text:
                return "Found code block but not marked as JSON."
            return "Found JSON code block but it contains syntax errors."

        # No JSON-like content found
        return "No JSON content found in response text."

    def _process_response_content_blocks(
        self, response: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Process content blocks from Claude response for debugging.

        RETENTION RATIONALE: Reserved for Phase 2 MCP tool integration where
        detailed tool_use block inspection will be required for tool call tracing
        and debugging. Tested to ensure API contract remains stable.

        Args:
            response: Claude API response with content blocks.

        Returns:
            Tuple of (all_blocks, tool_use_data) where:
                - all_blocks: List of dicts describing each content block
                - tool_use_data: Dict from emit_output tool_use, or None
        """
        blocks = []
        tool_use_data = None

        for block in response.content:
            if hasattr(block, "type"):
                if block.type == "text":
                    blocks.append(
                        {
                            "type": "text",
                            "text": block.text,
                        }
                    )
                elif block.type == "tool_use":
                    blocks.append(
                        {
                            "type": "tool_use",
                            "name": block.name,
                            "id": getattr(block, "id", None),
                        }
                    )
                    # Capture emit_output tool data
                    if block.name == "emit_output":
                        tool_use_data = dict(block.input)

        logger.debug(f"Processed {len(blocks)} content blocks from response")
        return blocks, tool_use_data

    def _extract_token_usage(self, response: Any) -> int | None:
        """Extract token usage from Claude response.

        Args:
            response: Claude API response with usage metadata.

        Returns:
            Total tokens used (input + output), or None if not available.

        Note:
            Claude response.usage contains input_tokens and output_tokens.
            This method sums both to provide total usage.
        """
        if not hasattr(response, "usage"):
            logger.debug("Response does not contain usage metadata")
            return None

        usage = response.usage
        input_tokens = getattr(usage, "input_tokens", 0)
        output_tokens = getattr(usage, "output_tokens", 0)
        total = input_tokens + output_tokens

        logger.debug(f"Token usage: {input_tokens} input + {output_tokens} output = {total} total")
        return total

    def _build_messages(self, rendered_prompt: str) -> list[dict[str, str]]:
        """Build message list for Claude API.

        Args:
            rendered_prompt: The user prompt to send.

        Returns:
            List of message dicts with role and content.
        """
        return [
            {
                "role": "user",
                "content": rendered_prompt,
            }
        ]

    def _build_tools_for_structured_output(
        self, output_schema: dict[str, OutputField]
    ) -> list[dict[str, Any]]:
        """Convert output schema to Claude tool definition.

        Args:
            output_schema: Agent's output schema.

        Returns:
            List containing single tool definition for structured output.
        """
        # Build JSON schema from OutputField definitions
        properties = self._build_json_schema_properties(output_schema)
        required = list(output_schema.keys())

        return [
            {
                "name": "emit_output",
                "description": "Emit the structured output for this task",
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        ]

    def _build_json_schema_properties(
        self, schema: dict[str, OutputField], depth: int = 0
    ) -> dict[str, Any]:
        """Build JSON Schema properties from OutputField definitions.

        Recursively handles nested objects and arrays with depth limiting.

        Args:
            schema: Dictionary mapping field names to OutputField definitions.
            depth: Current nesting depth (for recursion safety).

        Returns:
            Dictionary of JSON Schema property definitions.

        Raises:
            ValidationError: If schema nesting exceeds max depth.
        """
        if depth > self._max_schema_depth:
            raise ValidationError(
                f"Schema nesting depth exceeds maximum of {self._max_schema_depth} levels",
                suggestion="Simplify your output schema to reduce nesting depth",
            )

        properties: dict[str, Any] = {}

        for field_name, field_def in schema.items():
            prop: dict[str, Any] = {
                "type": self._map_type_to_json_schema(field_def.type),
            }

            if field_def.description:
                prop["description"] = field_def.description

            # Handle nested object schemas
            if field_def.type == "object" and field_def.properties:
                prop["properties"] = self._build_json_schema_properties(
                    field_def.properties, depth=depth + 1
                )
                # All properties in OutputField schemas are required
                # (OutputField has no 'required' attribute, all fields are mandatory)
                prop["required"] = list(field_def.properties.keys())

            # Handle array schemas with item definitions
            if field_def.type == "array" and field_def.items:
                items_schema = self._build_single_field_schema(field_def.items, depth=depth + 1)
                prop["items"] = items_schema

            properties[field_name] = prop

        return properties

    def _build_single_field_schema(self, field: OutputField, depth: int = 0) -> dict[str, Any]:
        """Build JSON Schema for a single field (used for array items).

        Args:
            field: The OutputField definition.
            depth: Current nesting depth (for recursion safety).

        Returns:
            JSON Schema definition for the field.

        Raises:
            ValidationError: If schema nesting exceeds max depth.
        """
        if depth > self._max_schema_depth:
            raise ValidationError(
                f"Schema nesting depth exceeds maximum of {self._max_schema_depth} levels",
                suggestion="Simplify your output schema to reduce nesting depth",
            )

        schema: dict[str, Any] = {
            "type": self._map_type_to_json_schema(field.type),
        }

        if field.description:
            schema["description"] = field.description

        # Handle nested objects in array items
        if field.type == "object" and field.properties:
            schema["properties"] = self._build_json_schema_properties(
                field.properties, depth=depth + 1
            )
            # All properties are required
            schema["required"] = list(field.properties.keys())

        # Handle nested arrays (array of arrays)
        if field.type == "array" and field.items:
            schema["items"] = self._build_single_field_schema(field.items, depth=depth + 1)

        return schema

    def _map_type_to_json_schema(self, field_type: str) -> str:
        """Map OutputField type to JSON Schema type.

        Args:
            field_type: The OutputField type string.

        Returns:
            Corresponding JSON Schema type.
        """
        type_mapping = {
            "string": "string",
            "number": "number",
            "boolean": "boolean",
            "array": "array",
            "object": "object",
        }
        return type_mapping.get(field_type, "string")

    def _extract_output(
        self, response: Any, output_schema: dict[str, OutputField] | None
    ) -> dict[str, Any]:
        """Extract structured output from Claude response.

        Tries tool_use blocks first, falls back to text parsing.

        Args:
            response: Claude API response.
            output_schema: Expected output schema (None if no schema).

        Returns:
            Extracted content as dict.

        Raises:
            ProviderError: If extraction fails.
        """
        # If no schema, extract text content
        if not output_schema:
            return self._extract_text_content(response)

        # Try to extract from tool_use blocks
        content = self._extract_structured_output(response)
        if content is not None:
            return content

        # Fallback: try to parse JSON from text
        content = self._extract_json_fallback(response)
        if content is not None:
            return content

        # If both failed, raise error
        raise ProviderError(
            "Failed to extract structured output from Claude response",
            suggestion="Ensure the agent is using the emit_output tool or returning valid JSON",
        )

    def _extract_text_content(self, response: Any) -> dict[str, Any]:
        """Extract plain text content when no schema is defined.

        Args:
            response: Claude API response.

        Returns:
            Dict with 'result' key containing the response text.
            Uses 'result' (not 'text') to maintain parity with CopilotProvider.
        """
        text_parts = []
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                text_parts.append(block.text)

        return {"result": "\n".join(text_parts)}

    def _maybe_rewrite_truncation_hint(
        self,
        result: str,
        tools: list[dict[str, Any]] | None,
    ) -> str:
        """Replace the generic truncation hint with an fs hint when applicable.

        Truncation is detected by the presence of the ``[output truncated:``
        marker in the trailing part of the result string. The generic hint is
        only replaced when the marker also advertises a spill file (i.e. the
        marker contains the ``full output saved to:`` text), so the model is not
        told to read a file that does not exist.

        The replacement is an exact string substitution so no shared mutable
        state or placeholder mechanism is required.

        Args:
            result: The possibly truncated MCP tool result string.
            tools: The tool definitions sent to the model (may be None).

        Returns:
            The result string, possibly with the hint rewritten.
        """
        truncation_info = self._parse_truncation_marker(result)
        if truncation_info is None:
            return result
        if not self._has_fs_like_tool(tools):
            return result
        if not truncation_info.get("spill_path"):
            return result
        idx = result.rfind(GENERIC_HINT)
        if idx == -1:
            return result
        return result[:idx] + FS_HINT + result[idx + len(GENERIC_HINT) :]

    def _parse_truncation_marker(
        self,
        result: str,
    ) -> dict[str, Any] | None:
        """Parse truncation metadata from the marker appended to a result.

        The marker is generated in ``MCPManager._maybe_truncate_response`` and
        always has the form::

            [output truncated: {original} chars -> {kept} kept{; optional path}. {HINT}]

        This method detects the marker only in the trailing 8192 characters of
        ``result`` to avoid matching unrelated text while still accommodating
        long POSIX spill file paths (PATH_MAX ~4096 + marker overhead). The
        marker is parsed from the local string so no shared mutable state is
        needed, which keeps the parser safe when the same MCP manager is reused
        across parallel agents.

        Args:
            result: The possibly truncated MCP tool result string.

        Returns:
            A dict with ``original_chars``, ``kept_chars``, and ``spill_path``
            (``None`` when the marker omits a path), or ``None`` when the
            result is not truncated.
        """
        if not result or TRUNCATION_MARKER_PREFIX not in result[-TAIL_WINDOW:]:
            return None

        tail = result[-TAIL_WINDOW:]
        match = re.search(
            r".*"
            + re.escape(TRUNCATION_MARKER_PREFIX)
            + r"\s*(\d+)\s*chars\s*-\u003e\s*(\d+)\s*kept"
            + r"(?:;\s*full output saved to:\s*(.+?))?\."
            + r"\s*"
            + r"(?:"
            + re.escape(GENERIC_HINT)
            + r"|"
            + re.escape(FS_HINT)
            + r")"
            + r"\]\s*$",
            tail,
            re.DOTALL,
        )

        if not match:
            return None

        original_chars = int(match.group(1))
        kept_chars = int(match.group(2))
        spill_path = match.group(3).strip() if match.group(3) else None

        return {
            "original_chars": original_chars,
            "kept_chars": kept_chars,
            "spill_path": spill_path,
        }

    # Keywords marking a tool that can read a file's contents from a known
    # path. The agent already has the exact spill path from the marker, so the
    # only capability it needs is reading/grepping by path — not searching for
    # files (find/ls/glob) or writing them (edit/write). Whole-name substring
    # containment would trip on any tool whose name merely contains "ls" or
    # "file" (e.g. "translate", "fileupload"), rewriting the hint to advertise
    # filesystem tools the agent does not actually have, so a keyword must
    # equal a complete underscore/dash segment ("read_file" -> "read").
    _FS_TOOL_KEYWORDS = frozenset(
        {
            "read",
            "grep",
            "view",
            "cat",
            "open",
            "load",
            "file",
            "bash",
            "shell",
        }
    )

    def _has_fs_like_tool(self, tools: list[dict[str, Any]] | None) -> bool:
        """Return True if any tool looks like a filesystem/shell tool.

        The check is heuristic: after stripping the ``server__`` prefix, the
        tool name is split on non-alphanumeric characters and each resulting
        segment is compared (case-insensitively) against common
        filesystem/shell keywords. A keyword must equal an entire segment, so
        "translate" or "fileupload" do not match while "ls", "read_file" and
        "view_code" still do. A None tool list means no tools were allowed,
        so filesystem-like tools are not available.
        """
        if not tools:
            return False

        for tool in tools:
            name = tool.get("name", "")
            if "__" in name:
                name = name.split("__", 1)[1]
            segments = re.split(r"[^A-Za-z0-9]+", name.lower())
            if any(segment in self._FS_TOOL_KEYWORDS for segment in segments):
                return True
        return False

    def _extract_structured_output(self, response: Any) -> dict[str, Any] | None:
        """Extract structured output from tool_use content blocks.

        Args:
            response: Claude API response.

        Returns:
            Extracted content dict, or None if no tool_use found.
        """
        for block in response.content:
            is_tool_use = hasattr(block, "type") and block.type == "tool_use"
            if is_tool_use and block.name == "emit_output":
                return dict(block.input)
        return None

    def _has_mcp_tool_use(self, response: Any) -> bool:
        """Check if response contains non-emit_output tool_use blocks (MCP tool calls).

        Args:
            response: Claude API response.

        Returns:
            True if response contains MCP tool calls that the agentic loop should handle.
        """
        return any(
            hasattr(block, "type") and block.type == "tool_use" and block.name != "emit_output"
            for block in response.content
        )

    def _extract_json_fallback(self, response: Any) -> dict[str, Any] | None:
        """Fallback: parse JSON from text content.

        Args:
            response: Claude API response.

        Returns:
            Parsed JSON dict, or None if parsing fails.
        """
        text_parts = []
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                text_parts.append(block.text)

        text = "\n".join(text_parts)

        # Try to find and parse JSON
        try:
            # Look for JSON code blocks
            if "```json" in text:
                start_idx = text.find("```json")
                if start_idx != -1:
                    start = start_idx + 7
                    end = text.find("```", start)
                    if end != -1:
                        json_str = text[start:end].strip()
                        try:
                            return json.loads(json_str)
                        except json.JSONDecodeError as e:
                            logger.warning(f"JSON code block parsing failed: {e}")
                            # Fall through to try whole text

            # Try parsing the whole text
            result = json.loads(text)
            logger.debug("Successfully parsed JSON from text response")
            return result
        except json.JSONDecodeError as e:
            logger.debug(f"JSON fallback parsing failed: {e}")
            return None

    def _extract_text_from_response(self, response: Any) -> str:
        """Extract raw text content from Claude response.

        Used for building message history during parse recovery.

        Args:
            response: Claude API response.

        Returns:
            Combined text content from all text blocks.
        """
        text_parts = []
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                text_parts.append(block.text)

        return "\n".join(text_parts)
