"""OpenAI provider implementation on the shared Pydantic AI runtime.

This module provides the ``OpenAIProvider`` class for executing agents via the
OpenAI API using the Pydantic AI agent loop shared with ``ClaudeProvider``. It
uses the same :mod:`~conductor.providers._pydantic_ai.runner` pipeline, the
same MCP toolset bridge, and the same retry/interrupt/event contracts.

Error Handling Strategy:
- ValidationError: Used for invalid inputs, schema violations, and parameter range
  errors. These are non-retryable and indicate user/configuration errors that
  should fail fast. Examples: temperature out of range, missing API key, invalid
  output schema.
- ProviderError: Used for API failures, network errors, and SDK exceptions. These
  may be retryable (connection errors, rate limits) or non-retryable (invalid API
  key).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from conductor.config.schema import AgentDef, ToolOutputConfig
from conductor.exceptions import ProviderError, ValidationError
from conductor.mcp.manager import MCPManager
from conductor.providers.base import (
    AgentOutput,
    AgentProvider,
    EventCallback,
    ModelCapabilityInfo,
)
from conductor.providers.capabilities import ProviderCapabilities
from conductor.providers.reasoning import ReasoningEffort

if TYPE_CHECKING:
    from conductor.engine.pricing import ModelPricing


def _import_openai_sdk() -> tuple[bool, Any]:
    """Import the OpenAI SDK and return availability flag plus module reference."""
    try:
        import openai

        return True, openai
    except ImportError:
        return False, None


OPENAI_SDK_AVAILABLE, openai = _import_openai_sdk()

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_OPENAI_REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh")


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
    max_parse_recovery_attempts: int = 2


class OpenAIProvider(AgentProvider):
    """OpenAI API provider built on the shared Pydantic AI runtime.

    Translates Conductor agent definitions into Pydantic AI/OpenAI API calls and
    normalizes responses into :class:`~conductor.providers.base.AgentOutput`.
    Supports incremental event streaming, structured output via tool use, retry
    logic, interrupts, and workflow-level MCP servers.

    Example:
        >>> provider = OpenAIProvider(api_key="sk-...")
        >>> await provider.validate_connection()
        True
        >>> await provider.close()
    """

    CAPABILITIES = ProviderCapabilities(
        tier="stable",
        # ``runtime.mcp_servers`` are forwarded via the Pydantic AI MCP toolset bridge.
        mcp_tools=True,
        # Per-agent ``tools:`` allowlists are passed through to the Pydantic AI agent.
        workflow_tools_passthrough=True,
        streaming_events=True,
        # Reasoning content is surfaced as ``agent_reasoning`` events when the model
        # returns it.
        agent_reasoning_events=True,
        # OpenAI supports reasoning_effort on its reasoning models (o-series, etc.).
        reasoning_effort=("low", "medium", "high", "xhigh"),
        # Tool-based structured output: the schema is enforced via a forced tool call.
        structured_output="native",
        # ``interrupt_signal`` is monitored by the shared Pydantic AI interrupt helper.
        interrupt=True,
        # ``max_session_seconds`` is enforced by the shared Pydantic AI interrupt helper.
        max_session_seconds=True,
        # OpenAI's API is stateless per-request — no session state to persist across
        # ``conductor resume``.
        checkpoint_resume=False,
        # Token counts and model identifier are populated on every AgentOutput.
        usage_tracking=True,
        # No global mutable state — safe to run N parallel agents.
        concurrent_safe=True,
        # The resolved ``working_dir`` selects the MCPManager pool key and is forwarded
        # to stdio MCP server ``cwd``.
        working_dir=True,
        # Skill content is eagerly injected into the rendered prompt by
        # :class:`~conductor.executor.agent.AgentExecutor` (OpenAI's Responses/Chat
        # Completions API has no native skill-directory surface).
        skills=True,
        # No plugin support: there is no subagent or MCP surface native to the OpenAI
        # API that Conductor can deconstruct into.
        plugins=False,
        upstream_pin=None,
        maintainer="@microsoft/conductor",
    )

    @property
    def supports_native_skills(self) -> bool:
        """OpenAI has no native skill-directory surface; rely on eager injection."""
        return False

    @property
    def supports_native_plugins(self) -> bool:
        """OpenAI has no native plugin/subagent surface."""
        return False

    def __init__(
        self,
        api_key: str | None = None,
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
        """Initialize the OpenAI provider.

        Args:
            api_key: OpenAI API key. If None, falls back to ``OPENAI_API_KEY`` env var.
            base_url: Optional custom API endpoint. If set, ``api_key`` must be provided
                explicitly — Conductor does not rely on ambient ``OPENAI_API_KEY`` for
                authenticated custom endpoints.
            model: Default model to use. Defaults to ``gpt-5-mini``.
            temperature: Default temperature (0.0-2.0).
            max_tokens: Maximum output tokens. Defaults to 4096.
            timeout: Request timeout in seconds. Defaults to 600s.
            retry_config: Optional retry configuration. Uses default if not provided.
            mcp_servers: Optional MCP server configurations for tool support.
                Each server config should have: command, args, env (optional).
            max_agent_iterations: Maximum tool-use iterations per agent execution.
                Defaults to 50 if not specified.
            max_session_seconds: Maximum wall-clock duration for agent sessions.
                Defaults to None (unlimited).
            default_reasoning_effort: Workflow-wide default reasoning effort applied
                when an agent does not declare its own ``reasoning`` config. Mapped to
                OpenAI's ``reasoning_effort`` parameter on supported models.
            tool_output: MCP tool result output-size configuration.

        Raises:
            ProviderError: If the OpenAI SDK is not installed.
            ValidationError: If no API key is available or parameters are out of range.
        """
        if not OPENAI_SDK_AVAILABLE:
            raise ProviderError(
                "OpenAI SDK not installed",
                suggestion="Install with: uv add 'openai>=1.0.0'",
            )

        self._client: AsyncOpenAI | None = None
        self._api_key = api_key
        self._base_url = base_url
        self._default_model = model or "gpt-5-mini"

        if temperature is not None:
            self._validate_temperature(temperature)
        self._default_temperature = temperature

        if max_tokens is not None:
            self._validate_max_tokens(max_tokens)
        self._default_max_tokens = max_tokens or 4096

        self._timeout = timeout
        self._sdk_version: str | None = None
        self._retry_config = retry_config or RetryConfig()
        self._retry_history: list[dict[str, Any]] = []
        self._default_max_agent_iterations = (
            max_agent_iterations if max_agent_iterations is not None else 50
        )
        self._default_max_session_seconds = max_session_seconds
        self._default_reasoning_effort: ReasoningEffort | None = default_reasoning_effort
        self._tool_output_config = tool_output or ToolOutputConfig()

        self._mcp_servers_config = mcp_servers
        self._mcp_managers: dict[str, MCPManager] = {}
        self._mcp_manager_locks: dict[str, asyncio.Lock] = {}

        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initialize the OpenAI client and log SDK version.

        Model verification is deferred to :meth:`validate_connection` to keep
        initialization synchronous.
        """
        if not OPENAI_SDK_AVAILABLE or openai is None:
            return

        from openai import AsyncOpenAI

        client_kwargs: dict[str, Any] = {"timeout": self._timeout, "max_retries": 0}
        effective_api_key = self._api_key
        if effective_api_key is None:
            effective_api_key = os.environ.get("OPENAI_API_KEY")

        if not effective_api_key:
            raise ValidationError(
                "OPENAI_API_KEY environment variable is not set and no api_key was provided",
                suggestion="Set OPENAI_API_KEY or pass api_key to the provider.",
            )

        self._api_key = effective_api_key

        client_kwargs["api_key"] = effective_api_key
        if self._base_url is not None:
            client_kwargs["base_url"] = self._base_url

        self._client = AsyncOpenAI(**client_kwargs)

        if openai is not None:
            self._sdk_version = getattr(openai, "__version__", "unknown")
            logger.info(f"Initialized OpenAI provider with SDK version {self._sdk_version}")

    def _validate_temperature(self, temperature: float) -> None:
        """Validate temperature parameter is in the OpenAI-acceptable range.

        Args:
            temperature: Temperature value to validate.

        Raises:
            ValidationError: If temperature is out of range (0.0-2.0).
        """
        if not (0.0 <= temperature <= 2.0):
            raise ValidationError(
                f"Temperature must be between 0.0 and 2.0 (OpenAI range), got {temperature}",
                suggestion="Adjust temperature to be within the valid range",
            )

    def _validate_max_tokens(self, max_tokens: int) -> None:
        """Validate max_tokens parameter is in an acceptable range.

        Args:
            max_tokens: Max tokens value to validate.

        Raises:
            ValidationError: If max_tokens is out of range (1-200000).
        """
        if not (1 <= max_tokens <= 200000):
            raise ValidationError(
                f"max_tokens must be between 1 and 200000, got {max_tokens}",
                suggestion="Adjust max_tokens to be within the valid range",
            )

    def get_retry_history(self) -> list[dict[str, Any]]:
        """Get the retry history for debugging purposes.

        Returns:
            List of dictionaries containing retry attempt details.
        """
        return self._retry_history.copy()

    async def validate_connection(self) -> bool:
        """Verify the provider can connect to the OpenAI API.

        Returns:
            True if connection successful, False otherwise.
        """
        if self._client is None:
            return False

        try:
            await self._client.models.list()
            return True
        except Exception as e:
            logger.error(f"Connection validation failed: {e}")
            return False

    async def list_models(self) -> list[str] | None:
        """Return the model ids advertised by the OpenAI API.

        Returns ``None`` when the client is unavailable or the listing call fails.
        """
        if not OPENAI_SDK_AVAILABLE or self._client is None:
            return None
        try:
            page = await self._client.models.list()
        except Exception as e:  # noqa: BLE001 - diagnostics must never raise
            logger.debug("Failed to list OpenAI models: %s", e)
            return None
        return [model.id for model in page.data]

    async def get_max_prompt_tokens(self, model: str) -> int | None:
        """Return the maximum prompt tokens for ``model``.

        The OpenAI API does not expose per-model input limits through
        ``models.list()``, so this always returns ``None``.
        """
        del model
        return None

    async def get_model_capabilities(self, model: str) -> ModelCapabilityInfo | None:
        """Return reasoning-effort support and prompt-token limits for ``model``.

        OpenAI does not expose token limits or reasoning defaults through its model
        listing, so only the reasoning-effort capability is inferred from the model
        id: models that are part of the OpenAI reasoning family advertise the
        provider's supported effort tuple; other models advertise an empty list
        ("supports none"). Unknown fields remain ``None``.
        """
        lowered = model.lower()
        is_reasoning_model = lowered.startswith("o") or "reasoning" in lowered
        supported = list(_OPENAI_REASONING_EFFORTS) if is_reasoning_model else []
        return ModelCapabilityInfo(
            supported_reasoning_efforts=supported,
            default_reasoning_effort=None,
            max_prompt_tokens=None,
            max_output_tokens=None,
            max_context_window_tokens=None,
        )

    async def get_model_pricing(self, model: str) -> ModelPricing | None:
        """OpenAI pricing is not provided by the SDK; return None."""
        del model
        return None

    async def _get_mcp_manager_for_cwd(self, resolved_cwd: str) -> MCPManager | None:
        """Return the pooled MCPManager for ``resolved_cwd``, connecting on first use.

        Mirrors :meth:`ClaudeProvider._get_mcp_manager_for_cwd` exactly.
        """
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

        lock = self._mcp_manager_locks.get(resolved_cwd)
        if lock is None:
            lock = asyncio.Lock()
            self._mcp_manager_locks[resolved_cwd] = lock

        async with lock:
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
                else:
                    logger.warning(
                        f"MCP server '{name}' has unsupported type '{server_type}' "
                        "(OpenAI provider only supports 'stdio')"
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
        """Release provider resources and close connections."""
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
            client = self._client
            self._client = None
            await client.close()
            logger.debug("OpenAI provider closed")

    async def execute_dialog_turn(
        self,
        system_prompt: str,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        model: str | None = None,
    ) -> str:
        """Execute a single dialog turn using a Pydantic AI OpenAI agent.

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
        from pydantic_ai import Agent
        from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
        from pydantic_ai.models.openai import OpenAIChatModelSettings

        from conductor.providers._pydantic_ai.agent_builder import (
            _resolve_openai_model,
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

        model_settings: OpenAIChatModelSettings = OpenAIChatModelSettings()
        max_tokens = 4096
        model_settings["max_tokens"] = max_tokens

        if self._default_reasoning_effort is not None:
            model_settings["openai_reasoning_effort"] = self._default_reasoning_effort

        try:
            dummy_agent = AgentDef(
                name="dialog_agent",
                model=resolved_model,
                prompt="",
                max_depth=None,
                timeout_seconds=None,
                max_session_seconds=None,
                max_agent_iterations=None,
            )
            pydantic_model = _resolve_openai_model(
                agent=dummy_agent,
                default_model=self._default_model,
                api_key=self._api_key,
                base_url=self._base_url,
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
        custom_agents: list[dict[str, Any]] | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
    ) -> AgentOutput:
        """Execute an agent using the shared Pydantic AI pipeline.

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context.
            rendered_prompt: Jinja2-rendered user prompt.
            tools: List of tool names available to this agent. ``None`` grants all
                MCP tools, ``[]`` grants none.
            interrupt_signal: Optional event for mid-agent interrupt signaling.
            event_callback: Optional callback for streaming SDK events.
            skill_directories: Ignored. OpenAI has no native skill surface; the
                executor has already eager-injected skill content into the prompt.
            custom_agents: Ignored. ``plugins=False``.
            extra_mcp_servers: Ignored. ``plugins=False``.

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If SDK execution fails.
            ValidationError: If output doesn't match schema.
        """
        del skill_directories, custom_agents, extra_mcp_servers
        from conductor.providers._pydantic_ai.agent_builder import build_agent
        from conductor.providers._pydantic_ai.retry import RetryConfig as PydanticRetryConfig
        from conductor.providers._pydantic_ai.runner import run_agent_pipeline

        resolved_cwd = agent.working_dir or os.getcwd()
        manager = await self._get_mcp_manager_for_cwd(resolved_cwd)

        def build_agent_fn(toolsets: list[Any], *, max_parse_recovery_attempts: int) -> Any:
            return build_agent(
                agent=agent,
                system_prompt=agent.system_prompt or "",
                rendered_prompt=rendered_prompt,
                default_model=self._default_model,
                default_temperature=self._default_temperature,
                default_max_tokens=self._default_max_tokens,
                default_reasoning_effort=self._default_reasoning_effort,
                max_parse_recovery_attempts=max_parse_recovery_attempts,
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                toolsets=toolsets,
                backend="openai",
                http_client=None,
            )

        retry_config = PydanticRetryConfig(
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

        self._retry_history.clear()

        return await run_agent_pipeline(
            agent=agent,
            rendered_prompt=rendered_prompt,
            mcp_manager=manager,
            tools=tools,
            tool_output_config=self._tool_output_config,
            retry_config=retry_config,
            interrupt_signal=interrupt_signal,
            event_callback=event_callback,
            max_agent_iterations=max_iterations,
            max_session_seconds=max_session,
            default_model=self._default_model,
            retry_history=self._retry_history,
            build_agent_fn=build_agent_fn,
        )
