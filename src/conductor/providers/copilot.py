"""GitHub Copilot SDK provider implementation.

This module provides the CopilotProvider class for executing agents
using the GitHub Copilot SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

from conductor.exceptions import ProviderError, ValidationError
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
from conductor.providers.context_tier import ContextTier, resolve_context_tier
from conductor.providers.reasoning import ReasoningEffort, resolve_reasoning_effort

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, OutputField, ProviderSettings
    from conductor.engine.pricing import ModelPricing

logger = logging.getLogger(__name__)

# GitHub Copilot bills token usage in "AI Credits". The SDK's per-model
# ``billing.token_prices`` are quoted in credits per batch of ``batch_size``
# tokens; converting to USD uses this observed rate (100 credits = $1). It is an
# *observed* rate, not a published one — kept as a single named constant so it's
# trivial to correct. When a model's ``token_prices`` are absent or malformed,
# ``get_model_pricing`` returns ``None`` and cost falls back to the static
# table rather than emitting a confident-wrong number. See #265.
_COPILOT_USD_PER_CREDIT: float = 0.01


def _is_finite_nonneg(value: object) -> TypeGuard[float]:
    """Return True only for a finite, non-negative real number.

    Rejects ``None``, booleans, non-numeric types, ``NaN`` and ``inf`` — any of
    which would turn a malformed SDK price into a confident-wrong cost. Used to
    validate Copilot ``token_prices`` before deriving a :class:`ModelPricing`.
    Narrows the value to ``float`` for the caller on success. Never raises: a
    pathologically large ``int`` (unconvertible to ``float``) is rejected rather
    than allowed to raise ``OverflowError``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= 0.0
    except OverflowError:
        return False


# Events that should NOT reset the idle-detection clock. These are internal
# bookkeeping / lifecycle events that can fire continuously (e.g. during stuck
# MCP initialization) without reflecting real agent progress.
#   - pending_messages.modified: fires repeatedly while MCP messages are queued
#   - session.start: one-time lifecycle event at session setup
#   - session.info: one-time informational metadata at session setup
_IDLE_IGNORED_EVENTS: frozenset[str] = frozenset(
    {
        "pending_messages.modified",
        "session.start",
        "session.info",
    }
)

# Try to import the Copilot SDK
try:
    from copilot import CopilotClient
    from copilot.session import PermissionHandler

    COPILOT_SDK_AVAILABLE = True
except ImportError:
    COPILOT_SDK_AVAILABLE = False
    CopilotClient = None  # type: ignore[misc, assignment]
    PermissionHandler = None  # type: ignore[misc, assignment]

# RuntimeConnection was added to the SDK after CopilotClient; import it
# separately so an older-but-present SDK still enables the default nested-spawn
# provider. A runtime connection is only required when explicitly requested,
# and that path raises a clear ProviderError if RuntimeConnection is missing.
try:
    from copilot.client import RuntimeConnection
except ImportError:
    RuntimeConnection = None  # type: ignore[misc, assignment]


@dataclass
class RetryConfig:
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
    max_parse_recovery_attempts: int = 5


@dataclass
class IdleRecoveryConfig:
    """Configuration for idle detection and recovery behavior.

    When a Copilot SDK session stops sending events for too long, this config
    controls how we detect the idle state and attempt to recover by sending
    a prompt asking the agent to continue.

    Attributes:
        idle_timeout_seconds: Time without any SDK events before considering session idle.
        max_recovery_attempts: Maximum number of "continue" messages to send before failing.
        max_session_seconds: Hard wall-clock limit on total session duration. Prevents
            sessions from hanging indefinitely even if non-idle events keep flowing.
        recovery_prompt: Template for the recovery message sent to stuck sessions.
            Use {last_activity} placeholder for context about what was happening.
    """

    idle_timeout_seconds: float = 90.0  # 90 seconds
    max_recovery_attempts: int = 5
    max_session_seconds: float = 1800.0  # 30 minutes
    recovery_prompt: str = (
        "It appears you may have gotten stuck or stopped responding. "
        "Your last activity was: {last_activity}. "
        "Please continue with your task from where you left off."
    )


@dataclass
class SDKResponse:
    """Response from a Copilot SDK call with usage data.

    Attributes:
        content: The response content string.
        input_tokens: Number of input tokens used (from assistant.usage event).
        output_tokens: Number of output tokens generated (from assistant.usage event).
        cache_read_tokens: Tokens read from cache (if available).
        cache_write_tokens: Tokens written to cache (if available).
        partial: Whether this response is partial (from a mid-agent interrupt).
        resolved_model: Model name the SDK reported in the assistant.usage event's
            model field. None when that event is absent (error or interrupt paths)
            or carries no usable model name (the model field is missing or empty).
    """

    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    partial: bool = False
    resolved_model: str | None = None


class CopilotProvider(AgentProvider):
    """GitHub Copilot SDK provider.

    Translates Conductor agent definitions into Copilot SDK calls and
    normalizes responses into AgentOutput format.

    For testing purposes, this provider supports a mock_handler that can
    be used to simulate agent responses without requiring the actual SDK.

    Example:
        >>> provider = CopilotProvider()
        >>> await provider.validate_connection()
        True
        >>> await provider.close()

        # Using mock handler for testing
        >>> def mock_handler(agent, prompt, context):
        ...     return {"answer": "Mocked response"}
        >>> provider = CopilotProvider(mock_handler=mock_handler)
        >>> output = await provider.execute(agent, {}, "prompt")
        >>> output.content["answer"]
        'Mocked response'
    """

    CAPABILITIES = ProviderCapabilities(
        tier="stable",
        # Copilot honors workflow-level ``runtime.mcp_servers`` and forwards
        # them to the SDK session.
        mcp_tools=True,
        # Per-agent ``tools:`` allowlist is enforced — the executor passes
        # the resolved subset down into ``execute()`` and the SDK respects it.
        workflow_tools_passthrough=True,
        # Streaming events (``agent_message``, ``agent_tool_*``) fire
        # incrementally during execution.
        streaming_events=True,
        # ``agent_reasoning`` is emitted for thinking-equivalent content
        # from models that expose it (GPT-5 / o1 series).
        agent_reasoning_events=True,
        # Copilot accepts the full reasoning-effort vocabulary; the SDK
        # validates per-model support against ``supported_reasoning_efforts``
        # at session creation and raises a clear error for unsupported levels.
        reasoning_effort=("low", "medium", "high", "xhigh", "max"),
        # Copilot has no native JSON-mode; structured ``output:`` schemas
        # are appended to the prompt and the model is asked to comply.
        # Stable providers using prompt_injection do not trigger the
        # tier-gated validation warning (see #241).
        structured_output="prompt_injection",
        # ``interrupt_signal`` is checked between tool iterations; mid-turn
        # interrupts return partial output.
        interrupt=True,
        # ``max_session_seconds`` is enforced via ``IdleRecoveryConfig``.
        max_session_seconds=True,
        # Copilot session IDs are persisted in checkpoints and re-applied
        # at ``conductor resume`` via ``set_resume_session_ids``.
        checkpoint_resume=True,
        # Token counts / model / usage are populated on every AgentOutput
        # (with the documented mock_handler exception in test contexts).
        usage_tracking=True,
        # No global mutable state — safe to run N parallel agents.
        concurrent_safe=True,
        # The resolved ``working_dir`` is applied to the SDK session's
        # ``working_directory`` and stamped onto each stdio MCP server
        # config per execution (no shared-dict mutation).
        working_dir=True,
        upstream_pin=None,
        maintainer="@microsoft/conductor",
    )

    def __init__(
        self,
        mock_handler: Callable[[AgentDef, str, dict[str, Any]], dict[str, Any]] | None = None,
        retry_config: RetryConfig | None = None,
        model: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        idle_recovery_config: IdleRecoveryConfig | None = None,
        temperature: float | None = None,
        max_agent_iterations: int | None = None,
        default_reasoning_effort: ReasoningEffort | None = None,
        default_context_tier: ContextTier | None = None,
        provider_settings: ProviderSettings | None = None,
    ) -> None:
        """Initialize the Copilot provider.

        Args:
            mock_handler: Optional function that receives (agent, prompt, context)
                         and returns a dict output. Used for testing.
            retry_config: Optional retry configuration. Uses default if not provided.
            model: Default model to use if not specified in agent. Defaults to "gpt-4o".
            mcp_servers: MCP server configurations to pass to the SDK.
                Note: The Copilot CLI has a bug where 'env' vars in MCP server
                configs are not passed to MCP server subprocesses.
                See: https://github.com/github/copilot-sdk/issues/163
            idle_recovery_config: Optional idle detection and recovery configuration.
                                  Uses default if not provided.
            temperature: Default temperature for generation (0.0-1.0). Optional.
            max_agent_iterations: Maximum tool-use iterations per agent execution.
                None means no iteration limit (only wall-clock timeout applies).
            default_reasoning_effort: Workflow-wide default ``reasoning_effort``
                applied to ``create_session`` when an agent does not specify
                its own ``reasoning.effort``. One of ``low``, ``medium``,
                ``high``, ``xhigh``, ``max``, or ``None`` to send no value.
            default_context_tier: Workflow-wide default ``context_tier`` applied
                to ``create_session`` when an agent does not specify its own
                ``context_tier``. One of ``default``, ``long_context``, or
                ``None`` to send no value.
            provider_settings: Optional structured provider settings from
                ``runtime.provider``. When ``has_custom_routing()`` is True,
                the resolved SDK ``ProviderConfig`` is attached to every
                ``create_session`` call (both agent execution and dialog
                turns), enabling custom OpenAI-compatible / Azure / Anthropic
                endpoints. ``runtime_url`` instead selects an existing Copilot
                CLI process and can be combined with that routing. Env-var fallbacks
                (``COPILOT_PROVIDER_BASE_URL`` → ``OPENAI_BASE_URL``,
                ``COPILOT_PROVIDER_API_KEY`` → ``OPENAI_API_KEY``,
                ``COPILOT_PROVIDER_BEARER_TOKEN``) fill missing fields once
                custom routing is activated; ambient OpenAI env vars never
                activate custom routing on their own.
        """
        self._client: Any = None  # Will hold Copilot SDK client
        self._mock_handler = mock_handler
        self._call_history: list[dict[str, Any]] = []
        self._retry_config = retry_config or RetryConfig()
        self._retry_history: list[dict[str, Any]] = []  # For testing retries
        # Track whether the caller actually supplied a default model so the
        # custom-routing warning can fire reliably without depending on the
        # value of the sentinel default below.
        self._default_model_explicit = model is not None
        self._default_model = model or "gpt-4o"
        self._mcp_servers = mcp_servers or {}
        self._started = False
        self._start_lock = asyncio.Lock()
        self._idle_recovery_config = idle_recovery_config or IdleRecoveryConfig()
        self._temperature = temperature
        self._default_max_agent_iterations = max_agent_iterations
        self._default_reasoning_effort = default_reasoning_effort
        self._default_context_tier = default_context_tier
        self._max_schema_depth = 10  # Max nesting depth for recursive schema building
        self._session_ids: dict[str, str] = {}
        self._resume_session_ids: dict[str, str] = {}
        self._session_cwds: dict[str, str] = {}
        self._resume_session_cwds: dict[str, str] = {}
        self._interrupted_session: Any = None
        self._abort_supported: bool | None = None
        self._provider_settings = provider_settings
        self._warn_custom_routing_default_model()

    @staticmethod
    def _default_permission_handler(
        request: Any,
        invocation: dict[str, str],
    ) -> Any:
        """Default permission handler that approves all requests.

        The SDK requires a permission handler on session creation.
        In orchestration mode, we approve all tool permissions since the
        workflow author controls which tools are available to each agent.

        Returns a PermissionRequestResult from the SDK.
        """
        logger.debug("auto-approved permission request: %s", request)
        return PermissionHandler.approve_all(request, invocation)

    def _warn_custom_routing_default_model(self) -> None:
        """Warn if custom routing is active but no default model is set.

        Custom endpoints (Ollama, vLLM, Azure deployments) rarely expose
        the SDK's built-in default model. Surface this early so users
        don't get a confusing 404 on the first ``create_session``.

        Uses ``_default_model_explicit`` (captured in ``__init__``) so
        the heuristic does not depend on the value of the sentinel
        fallback — a future change to the fallback model name would not
        break this warning, and a user who deliberately picks the same
        model name as the fallback does not get a false positive.
        """
        settings = self._provider_settings
        if settings is None or not settings.has_custom_routing():
            return
        if not self._default_model_explicit:
            logger.warning(
                "Custom Copilot provider routing is active (base_url=%s) but no "
                "runtime.default_model is set. The SDK fallback %r is unlikely "
                "to exist on the custom endpoint; configure runtime.default_model.",
                settings.base_url or "<from env>",
                self._default_model,
            )

    def _resolve_sdk_provider_config(self) -> dict[str, Any] | None:
        """Build the SDK ``ProviderConfig`` dict to forward, or ``None``.

        Returns ``None`` when no structured ``runtime.provider`` was
        configured, or when the configured object did not activate custom
        routing (i.e. only ``name`` was set). When custom routing is
        active, fills missing fields from environment variables in this
        precedence order:

        - ``base_url``: ``COPILOT_PROVIDER_BASE_URL`` → ``OPENAI_BASE_URL``
        - ``api_key``: ``COPILOT_PROVIDER_API_KEY`` (only)
        - ``bearer_token``: ``COPILOT_PROVIDER_BEARER_TOKEN`` (only)

        Ambient ``OPENAI_API_KEY`` is intentionally NOT used as an
        implicit fallback for ``api_key`` — that would silently send an
        OpenAI credential to whatever ``base_url`` points at, which is
        a real credential-leak risk in dev shells. Users who want
        OpenAI-environment-style behavior should opt in explicitly via
        ``api_key: ${OPENAI_API_KEY}`` interpolation in YAML.

        ``type`` defaults to ``"openai"`` when ``base_url`` is set but
        ``type`` is not — OpenAI-compatible endpoints (Ollama, vLLM, LM
        Studio) are the dominant use case.

        When both ``api_key`` and ``bearer_token`` resolve (from any
        combination of YAML and env vars), both are forwarded; the
        Copilot SDK silently prefers ``bearer_token`` and a warning is
        emitted.

        Raises ``ProviderError`` when custom routing is activated but
        every routing field resolves to a falsy value (e.g. all
        intended env vars are unset). Silently returning ``None`` in
        that case would mask user misconfiguration as default behavior.
        """
        settings = self._provider_settings
        if settings is None or not settings.has_custom_routing():
            return None

        base_url = (
            settings.base_url
            or os.environ.get("COPILOT_PROVIDER_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
        )
        api_key = (
            settings.api_key.get_secret_value()
            if settings.api_key is not None
            else os.environ.get("COPILOT_PROVIDER_API_KEY")
        )
        bearer_token = (
            settings.bearer_token.get_secret_value()
            if settings.bearer_token is not None
            else os.environ.get("COPILOT_PROVIDER_BEARER_TOKEN")
        )

        # Operate on the resolved locals so the warning also fires when
        # one credential comes from YAML and the other from an env var.
        if api_key and bearer_token:
            logger.warning(
                "Both api_key and bearer_token resolved (possibly from different sources, "
                "YAML and/or env vars); the Copilot SDK silently prefers bearer_token."
            )

        provider_type: str | None = settings.type
        if provider_type is None and base_url:
            provider_type = "openai"

        cfg: dict[str, Any] = {}
        if provider_type:
            cfg["type"] = provider_type
        if settings.wire_api:
            cfg["wire_api"] = settings.wire_api
        if base_url:
            cfg["base_url"] = base_url
        if api_key:
            cfg["api_key"] = api_key
        if bearer_token:
            cfg["bearer_token"] = bearer_token
        if settings.headers:
            cfg["headers"] = dict(settings.headers)
        # Always emit the azure block when the settings carry one — the
        # schema validator already requires at least ``api_version`` to
        # be set, so this never produces an empty sub-dict.
        if settings.azure is not None:
            azure_cfg: dict[str, Any] = {}
            if settings.azure.api_version is not None:
                azure_cfg["api_version"] = settings.azure.api_version
            cfg["azure"] = azure_cfg

        if not cfg:
            raise ProviderError(
                "runtime.provider opted into custom routing but no usable fields "
                "resolved. Set base_url / api_key / bearer_token in YAML or via the "
                "COPILOT_PROVIDER_* environment variables.",
                suggestion=(
                    "Check that any ${VAR} interpolations in runtime.provider resolved "
                    "to non-empty values and that the intended COPILOT_PROVIDER_* env "
                    "vars are exported in the current shell."
                ),
                is_retryable=False,
            )

        return cfg

    def _apply_provider_config(self, session_kwargs: dict[str, Any]) -> None:
        """Attach the resolved SDK provider config to ``session_kwargs``.

        Called from every ``create_session`` site (main agent execution and
        dialog turns) so all sessions for this provider instance hit the
        same endpoint.
        """
        provider_cfg = self._resolve_sdk_provider_config()
        if provider_cfg is not None:
            session_kwargs["provider"] = provider_cfg

    def _resolve_runtime_connection(self) -> tuple[str, str | None] | None:
        """Resolve whether to connect to an already-running Copilot runtime.

        Returns ``(url, connection_token)`` when Conductor should connect to
        an external runtime instead of spawning its own child process, or
        ``None`` for the default (spawn) behavior.

        Resolution order for each field is YAML (``runtime.provider``) first,
        then a namespaced environment variable:

        - ``url``: ``runtime_url`` → ``COPILOT_PROVIDER_RUNTIME_URL``
        - ``token``: ``runtime_token`` → ``COPILOT_PROVIDER_RUNTIME_TOKEN``

        The environment variables activate the connection on their own (no
        YAML required), which is the intended zero-config path for external
        orchestrators: they launch one authenticated
        ``copilot --headless`` process and export these two variables. The
        variables are namespaced (``COPILOT_PROVIDER_*``) specifically so
        unrelated ambient shell state cannot silently divert default Copilot
        traffic.
        """
        settings = self._provider_settings

        url = settings.runtime_url if settings is not None else None
        url = url or os.environ.get("COPILOT_PROVIDER_RUNTIME_URL")

        token: str | None = None
        if settings is not None and settings.runtime_token is not None:
            token = settings.runtime_token.get_secret_value()
        if token is None:
            token = os.environ.get("COPILOT_PROVIDER_RUNTIME_TOKEN")

        # Values resolved from environment variables are not validated by the
        # schema, so normalize/validate them here to mirror the YAML rules and
        # avoid silently falling through to a nested spawn on a typo/unset env.
        if url is not None:
            url = url.strip()
            if not url:
                raise ProviderError(
                    "'runtime_url' is empty; remove it or supply a value.",
                    suggestion=(
                        "Set runtime.provider.runtime_url or COPILOT_PROVIDER_RUNTIME_URL "
                        "to a valid port, host:port, or full URL."
                    ),
                    is_retryable=False,
                )
        # An empty token is the legitimate no-auth / tokenless-runtime case;
        # normalize it to None rather than erroring.
        if token is not None:
            token = token.strip() or None
        if token is not None and url is None:
            raise ProviderError(
                "'runtime_token' requires 'runtime_url' to also be set",
                suggestion=(
                    "Set COPILOT_PROVIDER_RUNTIME_URL alongside "
                    "COPILOT_PROVIDER_RUNTIME_TOKEN, or remove the token."
                ),
                is_retryable=False,
            )

        if url is None:
            return None
        return (url, token)

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Execute an agent using the Copilot SDK.

        If a mock_handler is configured, it will be used instead of
        the actual SDK. This is useful for testing.

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context.
            rendered_prompt: Jinja2-rendered user prompt.
            tools: List of tool names available to this agent.
            interrupt_signal: Optional event for mid-agent interrupt signaling.
                When set during execution, the provider will attempt to abort
                the current session and return partial output.
            event_callback: Optional callback for streaming SDK events upstream.

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If execution fails after all retry attempts.
        """
        # Record the call for testing purposes
        self._call_history.append(
            {
                "agent_name": agent.name,
                "prompt": rendered_prompt,
                "context": context,
                "tools": tools,
                "model": agent.model,
            }
        )

        model_name = agent.model or self._default_model
        logger.info(f"Executing agent '{agent.name}' with model {model_name}")
        logger.debug(f"Prompt length: {len(rendered_prompt)} chars, Tools: {tools}")

        # Use retry logic for both mock and real SDK calls
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

    async def _execute_with_retry(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Execute with exponential backoff retry logic.

        Uses the per-agent retry policy if configured on the agent, otherwise
        falls back to the provider-level retry config.

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context.
            rendered_prompt: Jinja2-rendered user prompt.
            tools: List of tool names available to this agent.
            interrupt_signal: Optional event for mid-agent interrupt signaling.
            event_callback: Optional callback for streaming SDK events upstream.

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If execution fails after all retry attempts.
        """
        last_error: Exception | None = None
        config = self._resolve_retry_config(agent)

        for attempt in range(1, config.max_attempts + 1):
            try:
                content, sdk_response = await self._execute_sdk_call(
                    agent,
                    rendered_prompt,
                    context,
                    tools,
                    interrupt_signal=interrupt_signal,
                    event_callback=event_callback,
                    retry_config=config,
                )
                # Extract usage data from SDK response if available
                input_tokens = sdk_response.input_tokens if sdk_response else None
                output_tokens = sdk_response.output_tokens if sdk_response else None
                cache_read = sdk_response.cache_read_tokens if sdk_response else None
                cache_write = sdk_response.cache_write_tokens if sdk_response else None
                tokens_used = None
                if input_tokens is not None and output_tokens is not None:
                    tokens_used = input_tokens + output_tokens

                # Detect partial result from mid-agent interrupt
                is_partial = sdk_response.partial if sdk_response else False

                return AgentOutput(
                    content=content,
                    raw_response=json.dumps(content),
                    tokens_used=tokens_used,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    model=(
                        sdk_response.resolved_model
                        if sdk_response and agent.model in (None, "auto")
                        else None
                    )
                    or agent.model
                    or self._default_model,
                    partial=is_partial,
                )
            except ProviderError as e:
                last_error = e
                self._retry_history.append(
                    {
                        "attempt": attempt,
                        "agent_name": agent.name,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "is_retryable": e.is_retryable,
                    }
                )

                logger.warning(
                    f"Agent '{agent.name}' attempt {attempt}/{config.max_attempts} failed: {e}. "
                    f"Retryable: {e.is_retryable}"
                )

                # Don't retry non-retryable errors
                if not e.is_retryable:
                    raise

                # Check retry_on filter if per-agent retry is configured
                if config.retry_on is not None:
                    error_category = self._classify_error(e)
                    if error_category not in config.retry_on:
                        raise

                # Don't retry if this was the last attempt
                if attempt >= config.max_attempts:
                    break

                # Calculate delay with backoff
                delay = self._calculate_delay(attempt, config)

                logger.debug(f"Retrying agent '{agent.name}' in {delay:.2f}s")

                # Log retry attempt (for testing visibility)
                self._retry_history[-1]["delay"] = delay

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

            except ValidationError:
                # Configuration / capability errors are deterministic and
                # never recoverable by retrying. Surface them unwrapped so
                # the workflow engine can present the original message.
                raise
            except Exception as e:
                # Wrap unexpected errors as retryable
                last_error = e
                logger.error(f"Unexpected error in agent '{agent.name}': {type(e).__name__}: {e}")
                self._retry_history.append(
                    {
                        "attempt": attempt,
                        "agent_name": agent.name,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "is_retryable": True,
                    }
                )

                if attempt >= config.max_attempts:
                    break

                delay = self._calculate_delay(attempt, config)
                self._retry_history[-1]["delay"] = delay

                # Emit agent_retry event for unexpected errors too
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
            f"SDK call failed after {config.max_attempts} attempts: {last_error}",
            suggestion=f"Check provider configuration and connectivity. Last error: {last_error}",
            is_retryable=False,
        )

    async def _execute_sdk_call(
        self,
        agent: AgentDef,
        rendered_prompt: str,
        context: dict[str, Any],
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        retry_config: RetryConfig | None = None,
    ) -> tuple[dict[str, Any], SDKResponse | None]:
        """Execute the actual SDK call or mock handler.

        Args:
            agent: Agent definition from workflow config.
            rendered_prompt: Jinja2-rendered user prompt.
            context: Accumulated workflow context.
            tools: List of tool names available to this agent.
            interrupt_signal: Optional event for mid-agent interrupt signaling.
            event_callback: Optional callback for streaming SDK events upstream.
            retry_config: Resolved per-agent retry config (used for parse recovery limit).

        Returns:
            Tuple of (content dict, SDKResponse with usage data or None for mock).

        Raises:
            ProviderError: If the SDK call fails.
        """
        if self._mock_handler is not None:
            # Mock handler for testing - no usage data available
            return self._mock_handler(agent, rendered_prompt, context), None

        # Use the real Copilot SDK
        if not COPILOT_SDK_AVAILABLE:
            raise ProviderError(
                "GitHub Copilot SDK is not installed",
                suggestion="Install with: pip install github-copilot-sdk",
                is_retryable=False,
            )

        # Ensure client is started; lock serializes concurrent first calls
        await self._ensure_client_started()

        model = agent.model or self._default_model

        # Build the full prompt with system prompt if provided
        full_prompt = rendered_prompt
        if agent.system_prompt:
            full_prompt = f"System: {agent.system_prompt}\n\nUser: {rendered_prompt}"

        # Build schema description for output schema (used in prompt and recovery)
        schema_for_prompt: dict[str, Any] | None = None
        output_schema = agent.effective_output_schema()
        if output_schema is not None:
            schema_for_prompt = self._build_prompt_schema(output_schema)
            schema_desc = json.dumps(schema_for_prompt, indent=2)
            full_prompt += (
                f"\n\n**IMPORTANT: You MUST respond with a JSON object matching this schema:**\n"
                f"```json\n{schema_desc}\n```\n"
                f"Return ONLY the JSON object, no other text."
            )

        try:
            # Resolve the session working directory: the engine resolves
            # ``agent.working_dir`` (Jinja render, absolutize, is_dir check)
            # before dispatching to the provider; ``None`` keeps the legacy
            # process-cwd behavior.
            resolved_cwd = agent.working_dir or os.getcwd()

            # Build session kwargs for the SDK.
            #
            # ``streaming=True`` is required: in non-streaming mode the model
            # must emit its entire turn (text + tool_use blocks + arguments)
            # under a single per-turn output budget. For agents that issue
            # large tool-call arguments (e.g., ``create`` with multi-KB
            # ``file_text``), that budget is exhausted mid-JSON and the CLI
            # silently executes the partial tool call (e.g. ``{"path": "..."}``
            # with ``file_text`` missing). The model sees the tool succeed
            # with no content, retries the same broken call, and loops until
            # the wall-clock session limit fires. The interactive ``copilot``
            # CLI defaults to streaming, which is why the same model + tool
            # combination works there but not via the SDK without this flag.
            session_kwargs: dict[str, Any] = {
                "model": model,
                "on_permission_request": self._default_permission_handler,
                "working_directory": resolved_cwd,
                "streaming": True,
            }

            # Note: Copilot SDK >=0.2.0 does not support temperature as a
            # session parameter. If a temperature was configured, log a warning
            # so the user knows it's being ignored (provider parity with Claude).
            if self._temperature is not None:
                logger.warning(
                    "Copilot SDK does not support 'temperature' as a session parameter; "
                    "ignoring configured value %.2f",
                    self._temperature,
                )

            # Add MCP servers if configured. Stdio/local servers get a
            # per-execution copy stamped with the resolved working directory;
            # the shared ``self._mcp_servers`` mapping is never mutated and
            # remote (http/sse) servers are left untouched.
            if self._mcp_servers:
                session_kwargs["mcp_servers"] = self._mcp_servers_for_cwd(resolved_cwd)

            # Apply custom provider routing (Ollama / vLLM / Azure / etc.)
            # when runtime.provider opted into it.
            self._apply_provider_config(session_kwargs)

            # Resolve reasoning effort: per-agent override wins over runtime default.
            # When set, validate against the model's advertised capabilities
            # before forwarding to the SDK.
            effort = resolve_reasoning_effort(agent, self._default_reasoning_effort)
            if effort is not None:
                await self._validate_reasoning_effort_for_model(model, effort)
                session_kwargs["reasoning_effort"] = effort
                logger.debug(
                    "Setting reasoning_effort=%s for agent %r (model=%s)",
                    effort,
                    agent.name,
                    model,
                )

            # Resolve context tier: per-agent override wins over runtime default.
            # Unlike reasoning effort (validated against the model's advertised
            # supported_reasoning_efforts first), the tier is forwarded as-is:
            # there is no advertised supported_context_tiers, so the SDK is the
            # sole authority and validates it at session creation.
            tier = resolve_context_tier(agent, self._default_context_tier)
            if tier is not None:
                session_kwargs["context_tier"] = tier
                logger.debug(
                    "Setting context_tier=%s for agent %r (model=%s)",
                    tier,
                    agent.name,
                    model,
                )

            # Attempt to resume a previous session if one exists for this agent.
            # Resume is only valid when the session was originally created with
            # the same working directory: the SDK bakes cwd into the session's
            # workspace, so resuming under a different cwd would silently run
            # the agent in the wrong directory. When the recorded cwd differs
            # (or is unknown for pre-cwd checkpoints, where we keep the legacy
            # resume-by-id behavior), fall through to ``create_session``.
            session: Any = None
            resume_sid = self._resume_session_ids.get(agent.name)
            if resume_sid is not None:
                recorded_cwd = self._resume_session_cwds.get(agent.name)
                should_resume = True
                if recorded_cwd is None:
                    logger.info(
                        "Resuming Copilot session %s for agent '%s' without a "
                        "recorded working directory; checkpoint predates "
                        "working_dir tracking, preserving legacy resume-by-id "
                        "behavior with resolved cwd %s.",
                        resume_sid,
                        agent.name,
                        resolved_cwd,
                    )
                elif recorded_cwd != resolved_cwd:
                    logger.warning(
                        "Skipping resume of Copilot session %s for agent '%s': "
                        "working directory changed from %s to %s. Creating a new session.",
                        resume_sid,
                        agent.name,
                        recorded_cwd,
                        resolved_cwd,
                    )
                    should_resume = False

                if should_resume:
                    try:
                        resume_kwargs: dict[str, Any] = {
                            "on_permission_request": self._default_permission_handler,
                            "working_directory": resolved_cwd,
                        }
                        if self._mcp_servers:
                            resume_kwargs["mcp_servers"] = self._mcp_servers_for_cwd(resolved_cwd)
                        session = await self._client.resume_session(resume_sid, **resume_kwargs)
                        logger.info(
                            f"Resumed Copilot session {resume_sid} for agent '{agent.name}'"
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Could not resume session {resume_sid} for agent "
                            f"'{agent.name}': {exc}. Falling back to new session."
                        )
                        session = None

            # Fall back to creating a new session
            if session is None:
                session = await self._client.create_session(**session_kwargs)

            # Track session ID and resolved cwd for checkpoint persistence
            sid = getattr(session, "session_id", None)
            if sid is not None:
                self._session_ids[agent.name] = sid
                self._session_cwds[agent.name] = resolved_cwd

            # Capture verbose state before callback (contextvars don't propagate to sync callbacks)
            from conductor.cli.app import is_full, is_verbose

            verbose_enabled = is_verbose()
            full_enabled = is_full()

            # Resolve per-agent max_session_seconds override
            effective_max_session = (
                agent.max_session_seconds or self._idle_recovery_config.max_session_seconds
            )

            # Resolve per-agent max_agent_iterations override
            effective_max_iterations = (
                agent.max_agent_iterations
                if agent.max_agent_iterations is not None
                else self._default_max_agent_iterations
            )

            session_destroyed = False
            try:
                # Send initial prompt and get response
                sdk_response = await self._send_and_wait(
                    session,
                    full_prompt,
                    verbose_enabled,
                    full_enabled,
                    interrupt_signal=interrupt_signal,
                    event_callback=event_callback,
                    max_session_seconds=effective_max_session,
                    max_agent_iterations=effective_max_iterations,
                    agent_name=agent.name,
                )
                response_content = sdk_response.content

                # Handle mid-agent interrupt: return partial content
                # and keep session alive for follow-up
                if sdk_response.partial:
                    self._interrupted_session = session
                    session_destroyed = True  # Prevent finally from destroying it
                    partial_content: dict[str, Any]
                    try:
                        partial_content = self._extract_json(response_content)
                    except (json.JSONDecodeError, ValueError):
                        partial_content = {"result": response_content}
                    partial_usage = SDKResponse(
                        content=response_content,
                        input_tokens=sdk_response.input_tokens,
                        output_tokens=sdk_response.output_tokens,
                        cache_read_tokens=sdk_response.cache_read_tokens,
                        cache_write_tokens=sdk_response.cache_write_tokens,
                        partial=True,
                        resolved_model=sdk_response.resolved_model,
                    )
                    return partial_content, partial_usage

                # Track cumulative usage across potential recovery calls
                total_input_tokens = sdk_response.input_tokens
                total_output_tokens = sdk_response.output_tokens
                cache_read_tokens = sdk_response.cache_read_tokens
                cache_write_tokens = sdk_response.cache_write_tokens
                current_resolved_model = sdk_response.resolved_model

                # If no output schema (or output_mode is raw), we're done
                if output_schema is None:
                    final_usage = SDKResponse(
                        content=response_content,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        cache_write_tokens=cache_write_tokens,
                        resolved_model=current_resolved_model,
                    )
                    return {"result": response_content}, final_usage

                # Try to parse the response as JSON with recovery loop
                max_recovery = (retry_config or self._retry_config).max_parse_recovery_attempts
                last_parse_error: str | None = None

                for recovery_attempt in range(max_recovery + 1):  # +1 for initial attempt
                    try:
                        parsed_content = self._extract_json(response_content)
                        final_usage = SDKResponse(
                            content=response_content,
                            input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens,
                            cache_read_tokens=cache_read_tokens,
                            cache_write_tokens=cache_write_tokens,
                            resolved_model=current_resolved_model,
                        )
                        return parsed_content, final_usage
                    except (json.JSONDecodeError, ValueError) as e:
                        last_parse_error = str(e)

                        # If this was the last recovery attempt, break and raise
                        if recovery_attempt >= max_recovery:
                            break

                        # Log recovery attempt in verbose mode
                        if verbose_enabled:
                            self._log_parse_recovery(
                                recovery_attempt + 1,
                                max_recovery,
                                last_parse_error,
                                agent_name=agent.name,
                            )

                        # Build recovery prompt and send to same session
                        recovery_prompt = self._build_parse_recovery_prompt(
                            parse_error=last_parse_error,
                            original_response=response_content,
                            schema=schema_for_prompt,  # type: ignore[arg-type]
                        )

                        # Send recovery prompt and get new response
                        recovery_response = await self._send_and_wait(
                            session,
                            recovery_prompt,
                            verbose_enabled,
                            full_enabled,
                            agent_name=agent.name,
                        )
                        response_content = recovery_response.content

                        # Accumulate usage from recovery calls
                        if recovery_response.input_tokens is not None:
                            total_input_tokens = (
                                total_input_tokens or 0
                            ) + recovery_response.input_tokens
                        if recovery_response.output_tokens is not None:
                            total_output_tokens = (
                                total_output_tokens or 0
                            ) + recovery_response.output_tokens
                        # Keep the latest resolved model (recovery uses the same session/model)
                        if recovery_response.resolved_model:
                            current_resolved_model = recovery_response.resolved_model

                # All recovery attempts exhausted
                expected_fields = list(output_schema.keys())
                raise ProviderError(
                    f"Failed to parse structured output from agent response: {last_parse_error}",
                    suggestion=(
                        f"Agent was expected to return JSON with fields: {expected_fields}. "
                        f"Response started with: {response_content[:500]}... "
                        "Tip: if this agent produces large or free-form output, "
                        "add 'output_mode: raw' to skip JSON extraction."
                    ),
                    is_retryable=False,
                )

            finally:
                # Disconnect session unless it was kept alive for follow-up
                if not session_destroyed:
                    await session.disconnect()

        except ProviderError:
            raise
        except ValidationError:
            # Configuration errors (e.g. unsupported reasoning_effort) are
            # deterministic; surface unwrapped so retries don't mask them.
            raise
        except Exception as e:
            raise ProviderError(
                f"Copilot SDK call failed: {e}",
                suggestion="Check that copilot CLI is installed and authenticated",
                is_retryable=True,
            ) from e

    async def _send_and_wait(
        self,
        session: Any,
        prompt: str,
        verbose_enabled: bool,
        full_enabled: bool,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        max_session_seconds: float | None = None,
        max_agent_iterations: int | None = None,
        agent_name: str | None = None,
    ) -> SDKResponse:
        """Send a prompt to the session and wait for response.

        Args:
            session: The Copilot SDK session.
            prompt: The prompt to send.
            verbose_enabled: Whether verbose logging is enabled.
            full_enabled: Whether full logging mode is enabled.
            interrupt_signal: Optional event for mid-agent interrupt signaling.
                When set, the method will attempt to abort the session and
                return partial content with ``partial=True``.
            event_callback: Optional callback for streaming SDK events upstream.
            max_session_seconds: Per-agent wall-clock session limit override.
                If None, uses the provider-level IdleRecoveryConfig default.
            max_agent_iterations: Maximum tool-use iterations for this session.
                None means no iteration limit.
            agent_name: Optional agent identifier (e.g., ``"processor[item_a]"``)
                used to tag verbose log output so concurrent parallel/for-each
                iterations can be distinguished. When ``None``, no tag is
                emitted. The Copilot provider's ``_execute_sdk_call`` always
                passes ``agent.name`` here, so sequential agents are also
                tagged with their plain name.

        Returns:
            SDKResponse with content and usage data. If interrupted,
            ``SDKResponse.partial`` will be True.

        Raises:
            ProviderError: If an error occurs during the SDK call or session gets stuck.
        """
        response_content = ""
        done = asyncio.Event()
        error_message: str | None = None

        # Mutable container for last activity: [event_type, tool_call, timestamp]
        # Using a list so the nested callback can mutate it
        last_activity_ref: list[Any] = [None, None, time.monotonic()]

        # Mutable container for usage data: [input_tokens, output_tokens, cache_read, cache_write]
        usage_ref: list[int | None] = [None, None, None, None]

        # Mutable container for the resolved model name (from assistant.usage event)
        resolved_model_ref: list[str | None] = [None]

        # Mutable container for tool iteration counting
        tool_iteration_ref: list[int] = [0]

        def on_event(event: Any) -> None:
            nonlocal response_content, error_message
            event_type = event.type.value if hasattr(event.type, "value") else str(event.type)

            # Log every SDK event for debugging stalls (visible via --log-file)
            if logger.isEnabledFor(logging.DEBUG):
                tool_info = ""
                if (
                    event_type == "tool.execution_start"
                    and hasattr(event, "data")
                    and event.data is not None
                ):
                    tn = getattr(event.data, "tool_name", None) or getattr(event.data, "name", "?")
                    tool_info = f" tool={tn}"
                agent_info = f" agent={agent_name}" if agent_name else ""
                logger.debug("sdk_event:%s %s%s", agent_info, event_type, tool_info)

            # Only update the idle clock for events that indicate real agent
            # work. Bookkeeping/lifecycle events are excluded via the
            # module-level _IDLE_IGNORED_EVENTS constant.
            if event_type not in _IDLE_IGNORED_EVENTS:
                last_activity_ref[0] = event_type
                last_activity_ref[2] = time.monotonic()

            if event_type == "assistant.message":
                response_content = event.data.content
            elif event_type == "assistant.usage":
                # Capture token usage from the assistant.usage event
                input_tokens = getattr(event.data, "input_tokens", None)
                output_tokens = getattr(event.data, "output_tokens", None)
                cache_read = getattr(event.data, "cache_read_tokens", None)
                cache_write = getattr(event.data, "cache_write_tokens", None)
                # Convert floats to ints if needed (SDK sometimes returns floats)
                if input_tokens is not None:
                    usage_ref[0] = int(input_tokens)
                if output_tokens is not None:
                    usage_ref[1] = int(output_tokens)
                if cache_read is not None:
                    usage_ref[2] = int(cache_read)
                if cache_write is not None:
                    usage_ref[3] = int(cache_write)
                # Capture the actual model resolved by the SDK (e.g., when model="auto")
                sdk_model = getattr(event.data, "model", None)
                if sdk_model:
                    resolved_model_ref[0] = sdk_model
            elif event_type == "session.idle":
                done.set()
            elif event_type == "error" or event_type == "session.error":
                error_message = getattr(event.data, "message", str(event.data))
                done.set()
            elif event_type == "tool.execution_start":
                # Track which tool is executing for better recovery context
                tool_name = getattr(event.data, "tool_name", None) or getattr(
                    event.data, "name", "unknown"
                )
                last_activity_ref[1] = tool_name
                # Count tool-use iterations
                tool_iteration_ref[0] += 1

            # Forward structured events upstream via event_callback
            if event_callback is not None:
                self._forward_event(event_type, event, event_callback)

            # Verbose logging for intermediate progress
            if verbose_enabled:
                self._log_event_verbose(event_type, event, full_enabled, agent_name=agent_name)

        session.on(on_event)

        # Signal that we're about to call the SDK — this marks the start
        # of the "dead zone" where we're waiting for the model's response
        if event_callback is not None:
            event_callback("agent_turn_start", {"turn": "awaiting_model"})

        await session.send(prompt)

        # If interrupt_signal is provided, race between done and interrupt,
        # while also running idle detection. If no interrupt_signal, just
        # run idle detection alone.
        was_interrupted = await self._wait_with_idle_detection(
            done,
            session,
            verbose_enabled,
            full_enabled,
            last_activity_ref,
            max_session_seconds=max_session_seconds,
            tool_iteration_ref=tool_iteration_ref,
            max_agent_iterations=max_agent_iterations,
            interrupt_signal=interrupt_signal,
            agent_name=agent_name,
        )
        if was_interrupted:
            # Return partial content (don't check error_message for partial)
            return SDKResponse(
                content=response_content,
                input_tokens=usage_ref[0],
                output_tokens=usage_ref[1],
                cache_read_tokens=usage_ref[2],
                cache_write_tokens=usage_ref[3],
                partial=True,
                resolved_model=resolved_model_ref[0],
            )

        if error_message:
            raise ProviderError(
                f"Copilot SDK error: {error_message}",
                is_retryable=True,
            )

        return SDKResponse(
            content=response_content,
            input_tokens=usage_ref[0],
            output_tokens=usage_ref[1],
            cache_read_tokens=usage_ref[2],
            cache_write_tokens=usage_ref[3],
            resolved_model=resolved_model_ref[0],
        )

    async def _abort_session(self, session: Any, done: asyncio.Event) -> None:
        """Attempt to abort a Copilot SDK session.

        Tries ``session.abort()`` first, then falls back to a raw RPC
        call. After aborting, waits up to 5 seconds for a post-abort
        event (session.idle or error).

        Args:
            session: The Copilot SDK session to abort.
            done: Event that signals session completion (may be set by
                post-abort events).
        """
        # Skip abort if previously determined to be unsupported
        if self._abort_supported is False:
            logger.debug("Skipping abort — previously detected as unsupported")
            return

        abort_called = False

        # Try method-based abort first
        if hasattr(session, "abort") and callable(session.abort):
            try:
                await session.abort()
                abort_called = True
                logger.debug("Session aborted via session.abort()")
            except Exception as exc:
                logger.warning(f"session.abort() failed: {exc}")

        # Fallback to raw RPC if abort method not available or failed
        if not abort_called and hasattr(session, "rpc"):
            try:
                await session.rpc("session/abort", {})
                abort_called = True
                logger.debug("Session aborted via raw RPC")
            except Exception as exc:
                logger.warning(f"RPC abort failed: {exc}")

        if not abort_called:
            logger.warning("Could not abort session — abort capability not available")
            self._abort_supported = False
            return

        self._abort_supported = True

        # Wait briefly for post-abort event (idle or error)
        try:
            await asyncio.wait_for(done.wait(), timeout=5.0)
        except TimeoutError:
            logger.debug("Post-abort wait timed out after 5s")

    async def send_followup(
        self,
        session: Any,
        guidance: str,
        agent_name: str | None = None,
        agent_model: str | None = None,
    ) -> AgentOutput:
        """Send follow-up guidance to an interrupted session.

        After a mid-agent interrupt, the session is kept alive so that
        the user's guidance can be sent as a follow-up message. This
        method sends the guidance, waits for the response, and then
        disconnects the session.

        Args:
            session: The Copilot SDK session handle (kept alive after interrupt).
            guidance: User-provided guidance text to send as follow-up.
            agent_name: Optional agent identifier forwarded to verbose log
                output for this follow-up turn. Defaults to ``None``. Today
                interrupts only fire on sequential agents (for-each iterations
                do not forward ``interrupt_signal`` to the executor), so the
                tag, when supplied, is the unqualified agent name.
            agent_model: Optional configured model for the interrupted agent,
                used when the follow-up response does not report a resolved model.

        Returns:
            AgentOutput with the follow-up response content.
        """
        from conductor.cli.app import is_full, is_verbose

        verbose_enabled = is_verbose()
        full_enabled = is_full()

        try:
            sdk_response = await self._send_and_wait(
                session,
                guidance,
                verbose_enabled,
                full_enabled,
                agent_name=agent_name,
            )

            content: dict[str, Any]
            try:
                content = self._extract_json(sdk_response.content)
            except (json.JSONDecodeError, ValueError):
                content = {"result": sdk_response.content}

            tokens_used = None
            if sdk_response.input_tokens is not None and sdk_response.output_tokens is not None:
                tokens_used = sdk_response.input_tokens + sdk_response.output_tokens

            return AgentOutput(
                content=content,
                raw_response=sdk_response.content,
                tokens_used=tokens_used,
                input_tokens=sdk_response.input_tokens,
                output_tokens=sdk_response.output_tokens,
                cache_read_tokens=sdk_response.cache_read_tokens,
                cache_write_tokens=sdk_response.cache_write_tokens,
                model=(sdk_response.resolved_model if agent_model in (None, "auto") else None)
                or agent_model
                or self._default_model,
            )
        finally:
            await session.disconnect()

    def _log_parse_recovery(
        self,
        attempt: int,
        max_attempts: int,
        error: str,
        agent_name: str | None = None,
    ) -> None:
        """Log a parse recovery attempt in verbose mode.

        Args:
            attempt: Current recovery attempt number (1-based).
            max_attempts: Maximum number of recovery attempts.
            error: The parse error message.
            agent_name: Optional agent identifier used to attribute the
                recovery message to a specific concurrent agent.
        """
        from rich.console import Console
        from rich.text import Text

        console = Console(stderr=True, highlight=False)

        text = Text()
        text.append("    ├─ ", style="dim")
        if agent_name:
            text.append(f"[{agent_name}] ", style="magenta")
        text.append("🔄 ", style="")
        text.append(f"Parse Recovery {attempt}/{max_attempts}", style="yellow bold")
        text.append(" - ", style="dim")
        # Truncate error message for display
        error_preview = error[:100] + "..." if len(error) > 100 else error
        text.append(error_preview, style="dim italic")
        console.print(text)

    def _extract_json(self, content: str) -> dict[str, Any]:
        """Extract JSON from response content.

        Handles responses that may have markdown code blocks or extra text.

        Args:
            content: The response content string.

        Returns:
            Parsed JSON as dict.

        Raises:
            ValueError: If no valid JSON found.
        """
        # Try direct parse first
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Try to find JSON in code blocks
        import re

        # Two-stage strategy (kept in parity with executor.output.parse_json_output):
        # 1. Non-greedy findall + try-parse each candidate. First valid JSON
        #    wins. Handles multiple fenced blocks in one response (e.g. an
        #    initial answer followed by a revised answer).
        # 2. Greedy single capture as fallback. Handles literal ``` inside a
        #    JSON string field, which breaks non-greedy matching at the inner
        #    fence but is recovered by closing at the LAST fence.
        candidates = re.findall(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        for candidate in candidates:
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                continue
        json_match = re.search(r"```(?:json)?\s*\n?(.*)\n?```", content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Look for {...} pattern
        brace_match = re.search(r"\{.*\}", content, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract JSON from response: {content[:500]}...")

    def _build_parse_recovery_prompt(
        self,
        parse_error: str,
        original_response: str,
        schema: dict[str, Any],
    ) -> str:
        """Build a prompt to recover from JSON parse failures.

        When an agent's response cannot be parsed as valid JSON, this method
        creates a follow-up prompt that provides the model with:
        - The specific parse error encountered
        - A truncated view of its original response
        - The expected JSON schema

        This allows the model to understand what went wrong and correct its
        response format without starting a new conversation.

        Args:
            parse_error: The error message from the parse attempt.
            original_response: The agent's malformed response.
            schema: The expected output schema as a dict.

        Returns:
            A prompt asking the agent to correct its response format.
        """
        # Truncate the original response to avoid overwhelming the context
        truncated_response = original_response[:500]
        if len(original_response) > 500:
            truncated_response += "..."

        schema_desc = json.dumps(schema, indent=2)

        return (
            f"Your previous response could not be parsed as valid JSON.\n\n"
            f"**Parse Error:** {parse_error}\n\n"
            f"**Your response started with:**\n```\n{truncated_response}\n```\n\n"
            f"**Expected JSON schema:**\n```json\n{schema_desc}\n```\n\n"
            f"Please respond with ONLY a valid JSON object matching the schema above. "
            f"Do NOT include markdown code blocks, explanatory text, or anything other "
            f"than the raw JSON object."
        )

    def _build_prompt_schema(
        self, schema: dict[str, OutputField], depth: int = 0
    ) -> dict[str, Any]:
        """Build a prompt-facing schema description from OutputField definitions."""
        if depth > self._max_schema_depth:
            raise ValidationError(
                f"Schema nesting depth exceeds maximum of {self._max_schema_depth} levels",
                suggestion="Simplify your output schema to reduce nesting depth",
            )
        return {
            field_name: self._build_prompt_field_schema(field_name, field_def, depth=depth)
            for field_name, field_def in schema.items()
        }

    def _build_prompt_field_schema(
        self,
        field_name: str,
        field_def: OutputField,
        depth: int = 0,
    ) -> dict[str, Any]:
        """Build a prompt-facing schema description for a named field."""
        schema: dict[str, Any] = {
            "type": field_def.type,
            "description": field_def.description or f"The {field_name} field",
        }

        if field_def.type == "object" and field_def.properties:
            schema["properties"] = self._build_prompt_schema(field_def.properties, depth=depth + 1)
            schema["required"] = list(field_def.properties.keys())

        if field_def.type == "array" and field_def.items:
            schema["items"] = self._build_prompt_item_schema(field_def.items, depth=depth + 1)

        return schema

    def _build_prompt_item_schema(self, field_def: OutputField, depth: int = 0) -> dict[str, Any]:
        """Build a prompt-facing schema description for an array item."""
        if depth > self._max_schema_depth:
            raise ValidationError(
                f"Schema nesting depth exceeds maximum of {self._max_schema_depth} levels",
                suggestion="Simplify your output schema to reduce nesting depth",
            )
        schema: dict[str, Any] = {
            "type": field_def.type,
        }

        if field_def.description:
            schema["description"] = field_def.description

        if field_def.type == "object" and field_def.properties:
            schema["properties"] = self._build_prompt_schema(field_def.properties, depth=depth + 1)
            schema["required"] = list(field_def.properties.keys())

        if field_def.type == "array" and field_def.items:
            schema["items"] = self._build_prompt_item_schema(field_def.items, depth=depth + 1)

        return schema

    def _log_event_verbose(
        self,
        event_type: str,
        event: Any,
        full_mode: bool,
        agent_name: str | None = None,
    ) -> None:
        """Log SDK events in verbose mode for progress visibility.

        Note: Caller must check is_verbose() before calling - contextvars
        don't propagate to sync callbacks from the SDK.

        Args:
            event_type: The event type string.
            event: The event object.
            full_mode: If True, show full details (args, results, reasoning).
            agent_name: Optional agent identifier (e.g.,
                ``"processor[item_a]"``). When set, every rendered line is
                tagged with ``[agent_name]`` between the tree prefix and the
                event icon so concurrent parallel/for-each iterations can be
                distinguished in interleaved logs.
        """
        from rich.console import Console
        from rich.text import Text

        from conductor.cli.run import _file_console

        console = Console(stderr=True, highlight=False)

        def _print(renderable: Any) -> None:
            console.print(renderable)
            if _file_console is not None:
                _file_console.print(renderable)

        def _append_tag(text: Text) -> None:
            """Append the optional [agent_name] tag to a Rich Text line."""
            if agent_name:
                text.append(f"[{agent_name}] ", style="magenta")

        # Log interesting events with Rich styling
        if event_type == "tool.execution_start":
            tool_name = (
                getattr(event.data, "tool_name", None)
                or getattr(event.data, "name", None)
                or "unknown"
            )

            text = Text()
            text.append("    ├─ ", style="dim")
            _append_tag(text)
            text.append("🔧 ", style="")
            text.append(str(tool_name), style="cyan bold")
            _print(text)

            # In full mode, try to show arguments
            if full_mode:
                args = getattr(event.data, "arguments", None) or getattr(event.data, "args", None)
                if args:
                    args_preview = format_tool_arguments(args, max_length=200) or ""
                    arg_text = Text()
                    arg_text.append("    │     ", style="dim")
                    _append_tag(arg_text)
                    arg_text.append("args: ", style="dim italic")
                    arg_text.append(args_preview, style="dim")
                    _print(arg_text)

        elif event_type == "tool.execution_complete":
            # tool.execution_complete may not have tool name, just acknowledge completion
            tool_name = getattr(event.data, "tool_name", None) or getattr(event.data, "name", None)
            if tool_name:
                text = Text()
                text.append("    │  ", style="dim")
                _append_tag(text)
                text.append("✓ ", style="green")
                text.append(str(tool_name), style="dim")
                _print(text)

            # In full mode, try to show result preview
            if full_mode:
                result = getattr(event.data, "result", None) or getattr(event.data, "output", None)
                if result:
                    result_preview = extract_tool_result_text(result, max_length=200) or ""
                    result_text = Text()
                    result_text.append("    │     ", style="dim")
                    _append_tag(result_text)
                    result_text.append("result: ", style="dim italic")
                    result_text.append(result_preview, style="dim")
                    _print(result_text)

        elif event_type == "assistant.reasoning":
            # Only show reasoning in full mode
            if full_mode:
                reasoning = getattr(event.data, "content", "")
                if reasoning:
                    # Truncate long reasoning for readability
                    if len(reasoning) > 150:
                        display_reasoning = reasoning[:150] + "..."
                    else:
                        display_reasoning = reasoning
                    text = Text()
                    text.append("    │  ", style="dim")
                    _append_tag(text)
                    text.append("💭 ", style="")
                    text.append(display_reasoning.replace("\n", " "), style="italic dim")
                    _print(text)

        elif event_type == "subagent.started":
            subagent_name = getattr(event.data, "name", None) or "unknown"
            text = Text()
            text.append("    ├─ ", style="dim")
            _append_tag(text)
            text.append("🤖 ", style="")
            text.append("Sub-agent: ", style="dim")
            text.append(str(subagent_name), style="magenta bold")
            _print(text)

        elif event_type == "subagent.completed":
            subagent_name = getattr(event.data, "name", None) or "unknown"
            text = Text()
            text.append("    │  ", style="dim")
            _append_tag(text)
            text.append("✓ ", style="green")
            text.append(f"Sub-agent done: {subagent_name}", style="dim")
            _print(text)

        elif event_type == "assistant.turn_start":
            # Only show processing indicator in full mode
            if full_mode:
                turn = getattr(event.data, "turn_id", None)
                turn_info = f" (turn {turn})" if turn else ""
                text = Text()
                text.append("    │  ", style="dim")
                _append_tag(text)
                text.append("⏳ ", style="yellow")
                text.append(f"Processing{turn_info}...", style="dim italic")
                _print(text)

    @staticmethod
    def _forward_event(event_type: str, event: Any, callback: EventCallback) -> None:
        """Forward an SDK event to an upstream callback as a structured dict.

        Maps SDK event types to Conductor streaming event types and extracts
        relevant data from each event.

        Args:
            event_type: The raw SDK event type string.
            event: The SDK event object.
            callback: The upstream callback to invoke with (event_type, data).
        """
        try:
            if event_type == "assistant.reasoning":
                content = getattr(event.data, "content", "")
                if content:
                    callback("agent_reasoning", {"content": content})

            elif event_type == "tool.execution_start":
                tool_name = (
                    getattr(event.data, "tool_name", None)
                    or getattr(event.data, "name", None)
                    or "unknown"
                )
                arguments = getattr(event.data, "arguments", None) or getattr(
                    event.data, "args", None
                )
                callback(
                    "agent_tool_start",
                    {
                        "tool_name": str(tool_name),
                        "arguments": format_tool_arguments(arguments),
                    },
                )

            elif event_type == "tool.execution_complete":
                tool_name = getattr(event.data, "tool_name", None) or getattr(
                    event.data, "name", None
                )
                result = getattr(event.data, "result", None) or getattr(event.data, "output", None)
                callback(
                    "agent_tool_complete",
                    {
                        "tool_name": str(tool_name) if tool_name else None,
                        "result": extract_tool_result_text(result),
                    },
                )

            elif event_type == "assistant.turn_start":
                turn = getattr(event.data, "turn_id", None)
                callback("agent_turn_start", {"turn": turn})

            elif event_type == "assistant.message":
                content = getattr(event.data, "content", "")
                if content:
                    callback("agent_message", {"content": content})

        except Exception:
            # Never let callback errors break the SDK event loop
            logger.debug("Error forwarding event %s to callback", event_type, exc_info=True)

    def _build_recovery_prompt(
        self,
        last_event_type: str | None,
        last_tool_call: str | None,
    ) -> str:
        """Build a recovery prompt based on last activity.

        Args:
            last_event_type: The type of the last event received.
            last_tool_call: The name of the last tool that was executing.

        Returns:
            A formatted recovery prompt to send to the stuck session.
        """
        if last_tool_call:
            last_activity = f"executing tool '{last_tool_call}'"
        elif last_event_type:
            activity_map = {
                "tool.execution_start": "starting a tool call",
                "assistant.reasoning": "reasoning about the problem",
                "assistant.turn_start": "beginning a response",
                "assistant.message": "sending a message",
            }
            last_activity = activity_map.get(last_event_type, f"'{last_event_type}' event")
        else:
            last_activity = "unknown (no events received)"

        return self._idle_recovery_config.recovery_prompt.format(last_activity=last_activity)

    def _build_stuck_info(
        self,
        last_event_type: str | None,
        last_tool_call: str | None,
    ) -> str:
        """Build a descriptive string about where the session got stuck.

        Args:
            last_event_type: The type of the last event received.
            last_tool_call: The name of the last tool that was executing.

        Returns:
            A human-readable description of the last activity.
        """
        if last_tool_call:
            return f"Last activity: tool '{last_tool_call}' was executing."
        elif last_event_type:
            return f"Last activity: '{last_event_type}' event."
        else:
            return "Last activity: unknown (no events received)."

    def _log_recovery_attempt(
        self,
        attempt: int,
        last_event_type: str | None,
        last_tool_call: str | None,
        agent_name: str | None = None,
    ) -> None:
        """Log a recovery attempt in verbose mode.

        Args:
            attempt: Current recovery attempt number (1-based).
            last_event_type: The type of the last event received.
            last_tool_call: The name of the last tool that was executing.
            agent_name: Optional agent identifier used to attribute the
                recovery message to a specific concurrent agent.
        """
        from rich.console import Console
        from rich.text import Text

        console = Console(stderr=True, highlight=False)

        text = Text()
        text.append("    ├─ ", style="dim")
        if agent_name:
            text.append(f"[{agent_name}] ", style="magenta")
        text.append("⚠️ ", style="yellow")
        text.append(
            f"Idle Recovery {attempt}/{self._idle_recovery_config.max_recovery_attempts}",
            style="yellow bold",
        )

        if last_tool_call:
            text.append(f" - last: tool '{last_tool_call}'", style="dim italic")
        elif last_event_type:
            text.append(f" - last: {last_event_type}", style="dim italic")

        console.print(text)

    async def _wait_with_idle_detection(
        self,
        done: asyncio.Event,
        session: Any,
        verbose_enabled: bool,
        full_enabled: bool,
        last_activity_ref: list[Any],
        max_session_seconds: float | None = None,
        tool_iteration_ref: list[int] | None = None,
        max_agent_iterations: int | None = None,
        interrupt_signal: asyncio.Event | None = None,
        agent_name: str | None = None,
    ) -> bool:
        """Wait for session completion with idle detection, recovery, and optional interrupt.

        Combines idle detection (sending recovery prompts to stuck sessions)
        with interrupt support (aborting on user request). When the model is
        actively working (SDK events flowing), the idle timer continuously
        resets — so stuck-detection is suppressed while the model is actively
        working. The interrupt signal, however, is always raced regardless of
        activity.

        Args:
            done: Event that signals session completion.
            session: The Copilot SDK session (for sending recovery messages).
            verbose_enabled: Whether verbose logging is enabled.
            full_enabled: Whether full logging mode is enabled.
            last_activity_ref: Mutable [last_event_type, last_tool_call, timestamp]
                              for tracking last activity.
            max_session_seconds: Per-agent wall-clock session limit override.
                If None, uses the provider-level IdleRecoveryConfig default.
            tool_iteration_ref: Mutable [count] tracking tool execution starts.
            max_agent_iterations: Maximum tool-use iterations allowed.
                None means no iteration limit.
            interrupt_signal: Optional event that signals user interrupt/revive.
                When set, aborts the session and returns True.
            agent_name: Optional agent identifier forwarded to verbose recovery
                logging so that idle-recovery messages emitted from concurrent
                for-each or parallel iterations can be attributed to a specific
                agent. ``None`` means no attribution tag.

        Returns:
            True if interrupted, False if completed normally.

        Raises:
            ProviderError: If all recovery attempts are exhausted, if the
                session exceeds max_session_seconds wall-clock duration, or
                if max_agent_iterations is exceeded.
        """
        recovery_attempts = 0
        idle_timeout = self._idle_recovery_config.idle_timeout_seconds
        session_start = time.monotonic()
        max_session = max_session_seconds or self._idle_recovery_config.max_session_seconds

        while True:
            # Check if done was already set (avoids race where session.idle
            # arrived between a previous done.clear() and the next wait).
            if done.is_set():
                return False

            # Hard wall-clock limit — prevents sessions from hanging
            # indefinitely even if events keep flowing (e.g. repeated
            # pending_messages.modified during stuck MCP initialization).
            # Note: this check runs at idle_timeout_seconds granularity, so
            # actual max duration is approximately max_session + idle_timeout.
            elapsed = time.monotonic() - session_start
            if elapsed > max_session:
                last_event_type = last_activity_ref[0]
                last_tool_call = last_activity_ref[1]
                time_since_last = time.monotonic() - last_activity_ref[2]
                stuck_info = self._build_stuck_info(last_event_type, last_tool_call)
                raise ProviderError(
                    f"Session exceeded maximum duration of {max_session:.0f}s. "
                    f"{stuck_info} Last real event {time_since_last:.0f}s ago.",
                    suggestion=(
                        f"The session ran for {elapsed:.0f}s without completing. "
                        "This may indicate a stuck MCP server, infinite tool loop, "
                        "or provider issue. Enable --log-file to capture full debug output."
                    ),
                    is_retryable=False,  # Don't retry — same root cause will recur
                )

            # Check tool-use iteration limit
            if (
                max_agent_iterations is not None
                and tool_iteration_ref is not None
                and tool_iteration_ref[0] > max_agent_iterations
            ):
                raise ProviderError(
                    f"Agent exceeded maximum tool-use iterations ({max_agent_iterations})",
                    suggestion=(
                        "The agent performed too many tool calls. "
                        "Increase max_agent_iterations in runtime config or per-agent "
                        "settings if the agent legitimately needs more iterations."
                    ),
                    is_retryable=False,
                )

            try:
                # Wait for done with idle timeout, also racing interrupt signal
                if interrupt_signal is not None:
                    done_waiter = asyncio.create_task(done.wait())
                    interrupt_waiter = asyncio.create_task(interrupt_signal.wait())
                    try:
                        finished, pending = await asyncio.wait(
                            {done_waiter, interrupt_waiter},
                            timeout=idle_timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await t
                    except Exception:
                        for t in (done_waiter, interrupt_waiter):
                            if not t.done():
                                t.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await t
                        raise

                    if interrupt_waiter in finished:
                        logger.info("Mid-agent interrupt received, attempting session abort")
                        interrupt_signal.clear()
                        await self._abort_session(session, done)
                        return True

                    if done_waiter in finished:
                        return False  # Completed successfully

                    # Neither finished → idle timeout (fall through to idle check)
                    raise TimeoutError()
                else:
                    await asyncio.wait_for(
                        done.wait(),
                        timeout=idle_timeout,
                    )
                    return False  # Completed successfully

            except TimeoutError as e:
                # Timeout fired — but check if events were recently received.
                # The agent may be actively working (tool calls, reasoning) without
                # having reached session.idle yet. Only consider it stuck if no
                # events at all arrived within the idle timeout window.
                last_event_time = last_activity_ref[2]
                time_since_last_event = time.monotonic() - last_event_time

                if time_since_last_event < idle_timeout:
                    # Events are still flowing — the agent is actively working,
                    # just hasn't finished yet. Reset recovery counter (new task)
                    # and keep waiting.
                    recovery_attempts = 0
                    # Only clear if done hasn't been set in the meantime
                    # (prevents race where session.idle arrives right as we
                    # check time_since_last_event).
                    if not done.is_set():
                        done.clear()
                    continue

                # Genuinely idle — no events for the full timeout period
                recovery_attempts += 1

                last_event_type = last_activity_ref[0]
                last_tool_call = last_activity_ref[1]

                if recovery_attempts > self._idle_recovery_config.max_recovery_attempts:
                    # All recovery attempts exhausted
                    stuck_info = self._build_stuck_info(last_event_type, last_tool_call)
                    raise ProviderError(
                        f"Session appears stuck after {recovery_attempts - 1} recovery attempts. "
                        f"{stuck_info}",
                        suggestion=(
                            f"The agent did not respond for "
                            f"{idle_timeout}s "
                            "despite recovery prompts. This may indicate a persistent issue "
                            "with the SDK, network connection, or the agent's ability to "
                            "complete the task. Enable --log-file to capture full debug output."
                        ),
                        is_retryable=False,  # Don't retry at provider level
                    ) from e

                # Log recovery attempt
                if verbose_enabled:
                    self._log_recovery_attempt(
                        recovery_attempts,
                        last_event_type,
                        last_tool_call,
                        agent_name=agent_name,
                    )

                # Send recovery message
                recovery_prompt = self._build_recovery_prompt(last_event_type, last_tool_call)
                await session.send(recovery_prompt)

                # Reset the done event to wait again — but only if it hasn't
                # been set since the recovery prompt was sent.
                if not done.is_set():
                    done.clear()

    async def _ensure_client_started(self) -> None:
        """Ensure the Copilot client is started.

        Uses a lock to prevent concurrent agents (parallel groups or
        for-each iterations) from racing to start the same client
        subprocess multiple times.
        """
        async with self._start_lock:
            if self._client is None:
                self._client = self._build_client()
            if not self._started:
                await self._client.start()
                self._started = True

                # Ensure subprocess pipes are in blocking mode to prevent
                # BlockingIOError on large payloads. The asyncio event loop
                # may set O_NONBLOCK on inherited file descriptors.
                self._fix_pipe_blocking_mode()

    def _build_client(self) -> Any:
        """Construct the Copilot SDK client.

        When a runtime connection is resolved (via ``runtime_url`` /
        ``COPILOT_PROVIDER_RUNTIME_URL``), connect to that already-running
        runtime instead of spawning a nested ``copilot`` child process. The
        SDK's ``start()`` skips process spawning for URI connections and its
        ``stop()`` leaves the externally-owned server running, so Conductor
        reuses the authenticated runtime process while creating a separate SDK
        session for each agent.
        """
        connection = self._resolve_runtime_connection()
        if connection is None:
            return CopilotClient()

        url, token = connection
        if RuntimeConnection is None:
            raise ProviderError(
                "Connecting to an existing Copilot runtime (runtime_url) requires a "
                "Copilot SDK that provides RuntimeConnection, which is unavailable in "
                "the installed SDK version.",
                suggestion=(
                    "Upgrade the copilot SDK, or unset runtime_url / "
                    "COPILOT_PROVIDER_RUNTIME_URL to spawn a nested runtime instead."
                ),
                is_retryable=False,
            )
        logger.info(
            "Connecting to existing Copilot runtime at %s (no nested runtime spawned)",
            url,
        )
        return CopilotClient(connection=RuntimeConnection.for_uri(url, connection_token=token))

    def _fix_pipe_blocking_mode(self) -> None:
        """Clear O_NONBLOCK on the Copilot CLI subprocess pipes.

        Large JSON-RPC messages (e.g., prompts with many gathered articles)
        can exceed the OS pipe buffer. When O_NONBLOCK is set, writes raise
        BlockingIOError instead of blocking until the reader drains the pipe.
        Since the SDK already runs writes in a thread-pool executor, blocking
        is safe and correct here.

        Skipped on Windows where O_NONBLOCK does not exist and pipes are
        always blocking.
        """
        import sys

        if sys.platform == "win32":
            return

        import fcntl
        import os

        process = getattr(self._client, "_process", None)
        if not process:
            return

        for name, stream in [("stdin", process.stdin), ("stdout", process.stdout)]:
            if stream is None:
                continue
            try:
                fd = stream.fileno()
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                if flags & os.O_NONBLOCK:
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
                    logger.debug(f"Cleared O_NONBLOCK on Copilot CLI {name}")
            except (OSError, ValueError):
                pass  # fd may already be closed or invalid

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
        from conductor.exceptions import TimeoutError as ConductorTimeoutError

        if isinstance(error, (ConductorTimeoutError, asyncio.TimeoutError)):
            return "timeout"
        if isinstance(error, ProviderError):
            if error.status_code == 408:
                return "timeout"
            if "timeout" in str(error).lower():
                return "timeout"
        return "provider_error"

    def _generate_stub_output(self, agent: AgentDef) -> dict[str, Any]:
        """Generate stub output based on agent's output schema.

        Args:
            agent: Agent definition with output schema.

        Returns:
            Dict with stub values matching the schema.
        """
        if not agent.output:
            return {"result": "stub response"}

        result: dict[str, Any] = {}
        for field_name, field_def in agent.output.items():
            result[field_name] = self._generate_stub_value(field_def.type)

        return result

    def _generate_stub_value(self, field_type: str) -> Any:
        """Generate a stub value for a given type.

        Args:
            field_type: The type string (string, number, boolean, array, object).

        Returns:
            A stub value of the appropriate type.
        """
        type_defaults: dict[str, Any] = {
            "string": "stub",
            "number": 0,
            "boolean": True,
            "array": [],
            "object": {},
        }
        return type_defaults.get(field_type, "stub")

    async def validate_connection(self) -> bool:
        """Verify Copilot SDK connection.

        Returns:
            True if connection is valid, False otherwise.

        Raises:
            ProviderError: If SDK is not available or connection fails.
        """
        if self._mock_handler is not None:
            return True

        if not COPILOT_SDK_AVAILABLE:
            raise ProviderError(
                "GitHub Copilot SDK is not installed",
                suggestion="Install with: pip install github-copilot-sdk",
                is_retryable=False,
            )

        external_runtime = False
        try:
            external_runtime = self._resolve_runtime_connection() is not None
            await self._ensure_client_started()
            return True
        except ProviderError:
            raise
        except Exception as e:
            if external_runtime:
                suggestion = (
                    "Verify the external Copilot runtime is running and reachable at "
                    "runtime.provider.runtime_url / COPILOT_PROVIDER_RUNTIME_URL, and that "
                    "COPILOT_PROVIDER_RUNTIME_TOKEN matches COPILOT_CONNECTION_TOKEN."
                )
            else:
                suggestion = (
                    "Ensure the Copilot CLI is installed and you have an active "
                    "GitHub Copilot subscription. Install CLI: "
                    "https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli"
                )
            raise ProviderError(
                f"Failed to connect to Copilot SDK: {e}",
                suggestion=suggestion,
                is_retryable=False,
            ) from e

    async def execute_dialog_turn(
        self,
        system_prompt: str,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
    ) -> str:
        """Execute a single dialog turn using a lightweight Copilot session.

        Creates a fresh session for the dialog, sends the conversation
        context, and returns the agent's response. The session is destroyed
        after the turn completes.

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
        await self._ensure_client_started()

        # Build the full prompt from history + current message
        # System prompt is passed via create_session's system_message parameter
        # to replace the SDK's default identity instructions.
        parts = []
        for msg in history or []:
            role_label = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{role_label}: {msg['content']}")
        parts.append(f"User: {user_message}")
        full_prompt = "\n\n".join(parts)

        session = None
        try:
            dialog_kwargs: dict[str, Any] = {
                "model": model or self._default_model,
                "on_permission_request": self._default_permission_handler,
                "system_message": {"mode": "replace", "content": system_prompt},
            }

            # Honor the same custom provider routing as agent sessions so
            # dialog turns hit the same endpoint as agent execution.
            self._apply_provider_config(dialog_kwargs)

            # Dialog turns honor the workflow-wide default reasoning effort
            # only — there's no agent-scoped override at this layer.
            effort = self._default_reasoning_effort
            if effort is not None:
                await self._validate_reasoning_effort_for_model(dialog_kwargs["model"], effort)
                dialog_kwargs["reasoning_effort"] = effort
                logger.debug(
                    "Setting reasoning_effort=%s for dialog turn (model=%s)",
                    effort,
                    dialog_kwargs["model"],
                )

            # Dialog turns likewise honor only the workflow-wide default
            # context tier (no agent-scoped override at this layer).
            tier = self._default_context_tier
            if tier is not None:
                dialog_kwargs["context_tier"] = tier
                logger.debug(
                    "Setting context_tier=%s for dialog turn (model=%s)",
                    tier,
                    dialog_kwargs["model"],
                )

            session = await self._client.create_session(**dialog_kwargs)

            response_content = ""
            done = asyncio.Event()
            error_message: str | None = None

            def on_event(event: Any) -> None:
                nonlocal response_content, error_message
                event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
                if event_type == "assistant.message":
                    response_content = event.data.content
                elif event_type == "session.idle":
                    done.set()
                elif event_type in ("error", "session.error"):
                    error_message = getattr(event.data, "message", str(event.data))
                    done.set()

            session.on(on_event)
            await session.send(full_prompt)

            try:
                await asyncio.wait_for(done.wait(), timeout=120.0)
            except TimeoutError as exc:
                raise ProviderError(
                    "Dialog turn timed out after 120s",
                    is_retryable=False,
                ) from exc

            if error_message:
                raise ProviderError(
                    f"Dialog turn error: {error_message}",
                    is_retryable=False,
                )

            return response_content

        except ProviderError:
            raise
        except ValidationError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Dialog turn failed: {exc}",
                is_retryable=False,
            ) from exc
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.destroy()

    async def close(self) -> None:
        """Close Copilot SDK client.

        Releases any resources held by the SDK client.
        """
        if self._client is not None and self._started:
            with contextlib.suppress(Exception):
                await self._client.stop()
        self._client = None
        self._started = False
        self._call_history.clear()
        self._retry_history.clear()

    async def get_max_prompt_tokens(self, model: str) -> int | None:
        """Return the Copilot SDK's ``max_prompt_tokens`` for ``model``.

        Queries ``client.list_models()`` (cached internally by the SDK),
        resolves any aliases (e.g. ``-latest``, dated suffixes, base-name
        vs versioned-name) via :func:`match_model_id`, and returns
        ``capabilities.limits.max_prompt_tokens`` for the matched entry.

        Returns ``None`` in mock-handler mode, when the SDK is unavailable,
        when no match is found, or when the SDK call fails — context-window
        metadata must never block workflow execution.

        Catches ``Exception`` (not ``BaseException``) at the SDK boundary so
        ``asyncio.CancelledError``/``KeyboardInterrupt``/``SystemExit`` still
        propagate. The broad catch is required because Copilot SDK >=0.3.0
        eagerly parses every entry in the ``models.list`` response with
        dataclass ``from_dict`` helpers that raise ``ValueError`` on any
        missing required field — e.g. ``ModelBilling`` requires ``multiplier``,
        and certain models (such as ``claude-opus-4.7-1m-internal``) ship a
        ``billing`` object without one, which kills the entire listing.
        """
        if self._mock_handler is not None or not COPILOT_SDK_AVAILABLE:
            return None
        try:
            await self._ensure_client_started()
            models = await self._client.list_models()
        except Exception as e:
            logger.debug("Failed to list Copilot models for %r: %s", model, e)
            return None
        by_id = {info.id: info for info in models}
        matched_id = match_model_id(model, by_id.keys())
        if matched_id is None:
            return None
        info = by_id[matched_id]
        limits = getattr(info.capabilities, "limits", None)
        return getattr(limits, "max_prompt_tokens", None)

    async def get_model_pricing(self, model: str) -> ModelPricing | None:
        """Derive per-token USD pricing for ``model`` from the SDK billing data.

        Implements the :meth:`AgentProvider.get_model_pricing` hook (see #265).
        Queries ``client.list_models()`` (cached internally by the SDK), resolves
        aliases via :func:`match_model_id`, and converts the matched model's
        ``billing.token_prices`` — quoted in AI Credits per batch of
        ``batch_size`` tokens — into a :class:`ModelPricing` (USD per million
        tokens) using the :data:`_COPILOT_USD_PER_CREDIT` rate.

        Returns ``None`` (so cost falls back to the static table) in mock-handler
        mode, when the SDK is unavailable, when the model can't be matched, when
        the model carries no usable ``token_prices`` (missing ``batch_size`` /
        input / output price, or a negative / non-finite price), or when the SDK
        call fails. Never raises — pricing metadata must not interrupt workflow
        execution.

        Uses the model's default-tier ``token_prices`` only: the
        ``long_context`` tier rate and the per-request ``billing.multiplier``
        are intentionally ignored, because token cost is billed per token via
        ``token_prices`` and ``output.model`` is identical across context tiers.
        A long-context request is therefore priced at the default per-token rate.

        Catches ``Exception`` (not ``BaseException``) at the SDK boundary so
        ``asyncio.CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit`` still
        propagate. The broad catch is required because Copilot SDK >=0.3.0
        eagerly parses every entry in the ``models.list`` response with dataclass
        ``from_dict`` helpers that raise ``ValueError`` on any missing required
        field (see :meth:`get_max_prompt_tokens`).
        """
        if self._mock_handler is not None or not COPILOT_SDK_AVAILABLE:
            return None
        try:
            await self._ensure_client_started()
            models = await self._client.list_models()
            by_id = {info.id: info for info in models}
        except Exception as e:
            logger.debug("Failed to list Copilot models for pricing of %r: %s", model, e)
            return None
        matched_id = match_model_id(model, by_id.keys())
        if matched_id is None:
            return None
        # getattr chains never raise, so the derivation below honors the
        # never-raises contract without a second try/except.
        token_prices = getattr(getattr(by_id[matched_id], "billing", None), "token_prices", None)
        if token_prices is None:
            return None
        batch_size = getattr(token_prices, "batch_size", None)
        input_price = getattr(token_prices, "input_price", None)
        output_price = getattr(token_prices, "output_price", None)
        cache_price = getattr(token_prices, "cache_price", None)
        # Require a positive batch size and finite, non-negative input/output
        # prices. Missing or malformed values (None / negative / NaN / inf) mean
        # we can't produce a trustworthy cost, so fall back to the static table
        # rather than emitting a confident-wrong number. A genuine 0.0 rate is a
        # legitimately-free model and passes (ModelPricing(0, 0) encodes "free").
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            return None
        if not _is_finite_nonneg(input_price):
            return None
        if not _is_finite_nonneg(output_price):
            return None

        from conductor.engine.pricing import ModelPricing

        def _per_mtok(credits_per_batch: float) -> float:
            # credits/batch → credits/token → credits/Mtok → USD/Mtok
            # (the code applies ``* 1_000_000`` then ``* USD_PER_CREDIT``).
            return (credits_per_batch / batch_size) * 1_000_000 * _COPILOT_USD_PER_CREDIT

        return ModelPricing(
            input_per_mtok=_per_mtok(input_price),
            output_per_mtok=_per_mtok(output_price),
            # The SDK exposes a single cached-token price (a read rate); Copilot
            # models have no separate cache-write price.
            cache_read_per_mtok=_per_mtok(cache_price) if _is_finite_nonneg(cache_price) else 0.0,
            cache_write_per_mtok=0.0,
        )

    async def list_models(self) -> list[str] | None:
        """Return the model ids advertised by the Copilot SDK.

        Queries ``client.list_models()`` and returns the ``id`` of every
        entry. Used by ``conductor doctor --models``.

        Returns ``None`` in mock-handler mode, when the SDK is unavailable,
        or when the SDK call fails — diagnostics must never raise.

        Catches ``Exception`` (not ``BaseException``) at the SDK boundary so
        ``asyncio.CancelledError``/``KeyboardInterrupt``/``SystemExit`` still
        propagate, mirroring :meth:`get_max_prompt_tokens`.
        """
        if self._mock_handler is not None or not COPILOT_SDK_AVAILABLE:
            return None
        try:
            await self._ensure_client_started()
            models = await self._client.list_models()
        except Exception as e:
            logger.debug("Failed to list Copilot models: %s", e)
            return None
        return [info.id for info in models]

    async def _validate_reasoning_effort_for_model(
        self, model: str, effort: ReasoningEffort
    ) -> None:
        """Validate ``effort`` against the model's advertised capabilities.

        Looks up the model via ``client.list_models()`` (resolving aliases via
        :func:`match_model_id`) and inspects the matched ``Model``'s
        ``supported_reasoning_efforts`` (a top-level field on ``Model``, not
        nested under ``capabilities`` — see :meth:`get_model_capabilities`).
        When that list is present and ``effort`` is not in it, raises
        :class:`ValidationError`.

        When the field is missing/``None`` (capability unknown), or when the
        model can't be matched, or when listing fails, validation is skipped
        — capability metadata must never block a workflow that the SDK might
        otherwise accept.

        Catches ``Exception`` (not ``BaseException``) at the SDK boundary so
        ``asyncio.CancelledError``/``KeyboardInterrupt``/``SystemExit`` still
        propagate. The broad catch is required because Copilot SDK >=0.3.0
        eagerly parses every entry in the ``models.list`` response with
        dataclass ``from_dict`` helpers that raise ``ValueError`` on any
        missing required field — e.g. ``ModelBilling`` requires ``multiplier``,
        and certain models (such as ``claude-opus-4.7-1m-internal``) ship a
        ``billing`` object without one, which kills the entire listing.

        Skipped entirely in mock-handler mode and when the SDK is not
        installed.
        """
        if self._mock_handler is not None or not COPILOT_SDK_AVAILABLE:
            return
        try:
            await self._ensure_client_started()
            models = await self._client.list_models()
        except Exception as e:
            logger.debug(
                "Failed to list Copilot models for reasoning_effort validation of %r: %s",
                model,
                e,
            )
            return
        by_id = {info.id: info for info in models}
        matched_id = match_model_id(model, by_id.keys())
        if matched_id is None:
            return
        info = by_id[matched_id]
        supported = getattr(info, "supported_reasoning_efforts", None)
        if supported is None:
            return
        if effort not in supported:
            raise ValidationError(
                f"Model {model!r} does not support reasoning_effort={effort!r}; "
                f"supported values: {sorted(supported)}",
                suggestion=(
                    "Choose an effort listed in the model's capabilities, "
                    "or pick a different model."
                ),
            )

    async def get_model_capabilities(self, model: str) -> ModelCapabilityInfo | None:
        """Return reasoning-effort support and context-window limits for ``model``.

        Implements the :meth:`AgentProvider.get_model_capabilities` hook (see
        #301). Queries ``client.list_models()`` (cached internally by the SDK),
        resolves aliases via :func:`match_model_id`, and reads:

        * ``supported_reasoning_efforts`` / ``default_reasoning_effort`` — both
          top-level fields on the matched ``Model`` (not nested under
          ``capabilities`` — see the fix in
          :meth:`_validate_reasoning_effort_for_model`).
        * ``capabilities.limits.max_prompt_tokens`` / ``max_output_tokens`` /
          ``max_context_window_tokens``.

        Returns ``None`` in mock-handler mode, when the SDK is unavailable,
        when no match is found, or when the SDK call fails — capability
        metadata must never block ``doctor`` or workflow execution.

        Catches ``Exception`` (not ``BaseException``) at the SDK boundary so
        ``asyncio.CancelledError``/``KeyboardInterrupt``/``SystemExit`` still
        propagate, mirroring :meth:`get_max_prompt_tokens`.
        """
        if self._mock_handler is not None or not COPILOT_SDK_AVAILABLE:
            return None
        try:
            await self._ensure_client_started()
            models = await self._client.list_models()
        except Exception as e:
            logger.debug("Failed to list Copilot models for capabilities of %r: %s", model, e)
            return None
        by_id = {info.id: info for info in models}
        matched_id = match_model_id(model, by_id.keys())
        if matched_id is None:
            return None
        info = by_id[matched_id]
        limits = getattr(info.capabilities, "limits", None)
        return ModelCapabilityInfo(
            supported_reasoning_efforts=getattr(info, "supported_reasoning_efforts", None),
            default_reasoning_effort=getattr(info, "default_reasoning_effort", None),
            max_prompt_tokens=getattr(limits, "max_prompt_tokens", None),
            max_output_tokens=getattr(limits, "max_output_tokens", None),
            max_context_window_tokens=getattr(limits, "max_context_window_tokens", None),
        )

    def _mcp_servers_for_cwd(self, resolved_cwd: str) -> dict[str, Any]:
        """Build a per-execution MCP server mapping stamped with ``resolved_cwd``.

        Stdio/local servers get a shallow-copied config with
        ``working_directory`` set (the SDK translates it to the spawned
        server's cwd). Remote (``http``/``sse``) servers are returned as-is —
        a working directory is meaningless for a remote process. The shared
        ``self._mcp_servers`` mapping and its nested dicts are never mutated,
        so parallel agents with different cwds cannot race each other.

        Args:
            resolved_cwd: Absolute working directory resolved by the engine
                (or ``os.getcwd()`` when the agent declares no working_dir).

        Returns:
            A new mapping of server name to config dict.
        """
        stamped: dict[str, Any] = {}
        for name, config in self._mcp_servers.items():
            if isinstance(config, dict) and config.get("type") in ("stdio", "local", None):
                server_copy = dict(config)
                server_copy["working_directory"] = resolved_cwd
                stamped[name] = server_copy
            else:
                stamped[name] = config
        return stamped

    def get_session_ids(self) -> dict[str, str]:
        """Get tracked session IDs for all executed agents.

        Returns a copy of the mapping from agent name to Copilot session ID.
        Session IDs are captured after ``create_session()`` and remain valid
        even after ``session.destroy()`` (which only releases local resources).

        Returns:
            Dict mapping agent names to their Copilot session IDs.
        """
        return self._session_ids.copy()

    def set_resume_session_ids(self, ids: dict[str, str]) -> None:
        """Set session IDs to attempt resuming on next execution.

        When executing an agent, the provider will check this mapping
        for a stored session ID and attempt ``client.resume_session()``
        before falling back to ``create_session()``.

        Args:
            ids: Mapping of agent names to session IDs from a checkpoint.
        """
        self._resume_session_ids = dict(ids)

    def get_session_cwds(self) -> dict[str, str]:
        """Get the resolved working directory per executed agent.

        Mirrors :meth:`get_session_ids` — the engine persists this mapping in
        the checkpoint (``copilot_session_cwds``) so a later resume can detect
        when the resolved cwd changed since session creation and start a fresh
        session instead of resuming into the wrong directory.

        Returns:
            Dict mapping agent names to their session's working directory.
        """
        return self._session_cwds.copy()

    def set_resume_session_cwds(self, cwds: dict[str, str]) -> None:
        """Set session working directories restored from a checkpoint.

        Args:
            cwds: Mapping of agent names to the working directory their
                stored session was created with. Agents missing from this
                mapping (pre-cwd checkpoints) keep the legacy resume-by-id
                behavior.
        """
        self._resume_session_cwds = dict(cwds)

    def get_interrupted_session(self) -> Any | None:
        """Get the session handle kept alive after a mid-agent interrupt.

        Returns:
            The Copilot SDK session if one was interrupted, None otherwise.
            The session handle is cleared after retrieval.
        """
        session = self._interrupted_session
        self._interrupted_session = None
        return session

    def get_call_history(self) -> list[dict[str, Any]]:
        """Get the history of execute calls.

        This is useful for testing to verify that agents were
        called with the expected parameters.

        Returns:
            List of call records with agent_name, prompt, context, tools, model.
        """
        return self._call_history.copy()

    def get_retry_history(self) -> list[dict[str, Any]]:
        """Get the history of retry attempts.

        This is useful for testing retry behavior.

        Returns:
            List of retry records with attempt, agent_name, error, is_retryable, delay.
        """
        return self._retry_history.copy()

    def clear_call_history(self) -> None:
        """Clear the call history."""
        self._call_history.clear()

    def clear_retry_history(self) -> None:
        """Clear the retry history."""
        self._retry_history.clear()

    def set_retry_config(self, config: RetryConfig) -> None:
        """Update the retry configuration.

        Args:
            config: New retry configuration.
        """
        self._retry_config = config
