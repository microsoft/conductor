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
import logging
import os
from typing import Any, get_args

from pydantic import BaseModel

from conductor.config.schema import AgentDef, OutputField, ToolOutputConfig
from conductor.exceptions import ProviderError, ValidationError
from conductor.mcp.manager import (
    MCPManager,
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
        # ``interrupt_signal`` is monitored by the Pydantic AI interrupt helper
        # and triggers a partial output request.
        interrupt=True,
        # ``max_session_seconds`` is enforced by the Pydantic AI interrupt helper.
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
        # Skill content is eagerly injected into the rendered prompt by
        # AgentExecutor (Claude's Messages API has no server-side skill
        # surface without adopting the container/code-execution beta).
        skills=True,
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
        """Execute a single dialog turn using a Pydantic AI agent.

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
        from anthropic.types.beta.beta_thinking_config_enabled_param import (
            BetaThinkingConfigEnabledParam,
        )
        from pydantic_ai import Agent
        from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
        from pydantic_ai.models.anthropic import AnthropicModelSettings

        from conductor.config.schema import AgentDef
        from conductor.providers._pydantic_ai.agent_builder import (
            _coerce_for_thinking,
            _resolve_anthropic_model,
        )

        resolved_model = model or self._default_model

        pydantic_history: list[ModelRequest | ModelResponse] = []
        for msg in history or []:
            if msg["role"] == "user":
                pydantic_history.append(
                    ModelRequest(parts=[UserPromptPart(content=msg["content"])])
                )
            elif msg["role"] == "assistant":
                pydantic_history.append(ModelResponse(parts=[TextPart(content=msg["content"])]))

        model_settings = AnthropicModelSettings()
        max_tokens = 4096

        if self._default_reasoning_effort is not None:
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
            thinking = BetaThinkingConfigEnabledParam(type="enabled", budget_tokens=budget)
            _, coerced_max = _coerce_for_thinking(
                temperature=None,
                max_tokens=max_tokens,
                thinking={"type": "enabled", "budget_tokens": budget},
                model=resolved_model,
            )
            coerced_max = coerced_max or max_tokens
            model_settings["max_tokens"] = coerced_max
            model_settings["anthropic_thinking"] = thinking
        else:
            model_settings["max_tokens"] = max_tokens

        try:
            dummy_agent = AgentDef(
                name="dialog_agent",
                model=resolved_model,
                prompt="",
            )
            pydantic_model = _resolve_anthropic_model(
                agent=dummy_agent,
                default_model=self._default_model,
                api_key=self._api_key,
                base_url=self._base_url,
                auth_token=self._auth_token,
                timeout=self._timeout,
            )

            pydantic_agent = Agent(
                model=pydantic_model,
                output_type=str,
                system_prompt=system_prompt,
                model_settings=model_settings,
                retries=0,
            )
            result = await pydantic_agent.run(
                user_prompt=user_message,
                message_history=pydantic_history,
            )
            return str(result.output)
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
        skill_directories: list[str] | None = None,
    ) -> AgentOutput:
        """Execute an agent using the Pydantic AI pipeline.

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context.
            rendered_prompt: Jinja2-rendered user prompt.
            tools: List of tool names available to this agent. ``None`` grants
                all MCP tools, ``[]`` grants none, and a list filters to the
                named tools.
            interrupt_signal: Optional event for mid-agent interrupt signaling.
                When set during the agentic loop, the agent is asked for a
                partial result and the output is returned with ``partial=True``.
            skill_directories: Ignored. Claude has no server-side skill
                surface without adopting the container/code-execution
                beta, so :class:`AgentExecutor` has already eager-injected
                the skill content into ``rendered_prompt`` for this
                provider (see :attr:`AgentProvider.supports_native_skills`).

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If SDK execution fails.
            ValidationError: If output doesn't match schema.
        """
        del skill_directories  # Claude relies on eager preamble injection (see docstring).
        from pydantic_ai import UsageLimits

        from conductor.providers._pydantic_ai.agent_builder import build_agent
        from conductor.providers._pydantic_ai.interrupt import run_with_interrupt
        from conductor.providers._pydantic_ai.mcp_toolset import MCPManagerToolset
        from conductor.providers._pydantic_ai.retry import (
            RetryConfig as PydanticRetryConfig,
        )
        from conductor.providers._pydantic_ai.retry import (
            _resolve_retry_config,
            execute_with_retry,
        )
        from conductor.providers._pydantic_ai.structured_output import extract_content
        from conductor.providers._pydantic_ai.usage import build_agent_output

        resolved_cwd = agent.working_dir or os.getcwd()
        manager = await self._get_mcp_manager_for_cwd(resolved_cwd)

        toolsets: list[Any] = []
        if manager is not None:
            # Issue #37: an empty resolved filter means "no filter" — pass None
            # so MCPManagerToolset exposes every manager tool instead of none.
            toolsets.append(
                MCPManagerToolset(
                    manager,
                    tools or None,
                    self._tool_output_config,
                )
            )

        pydantic_agent = build_agent(
            agent=agent,
            system_prompt=agent.system_prompt or "",
            rendered_prompt=rendered_prompt,
            default_model=self._default_model,
            default_temperature=self._default_temperature,
            default_max_tokens=self._default_max_tokens,
            default_reasoning_effort=self._default_reasoning_effort,
            api_key=self._api_key,
            auth_token=self._auth_token,
            base_url=self._base_url,
            timeout=self._timeout,
            toolsets=toolsets,
        )

        max_iterations = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else self._default_max_agent_iterations
        )
        max_session = (
            agent.max_session_seconds
            if agent.max_session_seconds is not None
            else self._default_max_session_seconds
        )
        retry_cfg = _resolve_retry_config(
            agent,
            PydanticRetryConfig(
                max_attempts=self._retry_config.max_attempts,
                base_delay=self._retry_config.base_delay,
                max_delay=self._retry_config.max_delay,
                jitter=self._retry_config.jitter,
                backoff=self._retry_config.backoff,
                retry_on=(
                    list(self._retry_config.retry_on)
                    if self._retry_config.retry_on is not None
                    else None
                ),
                max_parse_recovery_attempts=self._retry_config.max_parse_recovery_attempts,
            ),
        )

        self._retry_history.clear()

        def intercepting_callback(event_type: str, data: dict[str, Any]) -> None:
            if event_type == "agent_retry":
                self._retry_history.append(data)
            if event_callback is not None:
                event_callback(event_type, data)

        try:
            outcome = await execute_with_retry(
                coro_factory=lambda: run_with_interrupt(
                    agent=pydantic_agent,
                    user_prompt=rendered_prompt,
                    interrupt_signal=interrupt_signal,
                    event_callback=intercepting_callback,
                    has_output_schema=bool(agent.output),
                    usage_limits=UsageLimits(request_limit=max_iterations),
                    max_session_seconds=max_session,
                ),
                retry_config=retry_cfg,
                event_callback=intercepting_callback,
                agent_name=agent.name,
            )
        finally:
            pass

        if outcome.is_cancelled:
            raise asyncio.CancelledError()

        model_name = self._model_name_from_pydantic_agent(pydantic_agent)

        if outcome.is_partial:
            content = self._build_partial_content(outcome.partial_output, agent.output, agent.name)
            total_usage = outcome.total_usage or {}
            return AgentOutput(
                content=content,
                raw_response=outcome.partial_output,
                tokens_used=total_usage.get("total_tokens"),
                input_tokens=total_usage.get("request_tokens"),
                output_tokens=total_usage.get("response_tokens"),
                partial=True,
                model=model_name,
            )

        if outcome.result is None:
            raise ProviderError(
                f"Agent '{agent.name}' produced no result",
                suggestion="Check the model and prompt configuration.",
                is_retryable=False,
            )

        content = extract_content(outcome.result.output, agent.output, agent.name)
        return build_agent_output(
            content=content,
            raw_response=outcome.result,
            usage=outcome.result.usage,
            model=model_name,
        )

    def _model_name_from_pydantic_agent(self, pydantic_agent: Any) -> str:
        """Return a resolved model name from a Pydantic AI agent instance."""
        model = pydantic_agent.model
        if model is None:
            return self._default_model
        if hasattr(model, "model_name"):
            return model.model_name
        if hasattr(model, "name"):
            return model.name
        return str(model)

    def _build_partial_content(
        self,
        partial_output: Any,
        output_schema: dict[str, OutputField] | None,
        agent_name: str,
    ) -> dict[str, Any]:
        """Build a content dict from a partial/interrupted output."""
        from conductor.executor.output import parse_json_output

        if isinstance(partial_output, BaseModel):
            return partial_output.model_dump()

        if isinstance(partial_output, str):
            if output_schema is not None:
                try:
                    return parse_json_output(partial_output)
                except ValidationError:
                    pass
            return {"result": partial_output}

        return {"result": partial_output}

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
