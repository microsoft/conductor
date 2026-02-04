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
import json
import logging
import random
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from conductor.exceptions import ProviderError, ValidationError
from conductor.executor.output import validate_output
from conductor.providers.base import AgentOutput, AgentProvider

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, OutputField
    from conductor.mcp.manager import MCPManager

# Try to import the Anthropic SDK
try:
    import anthropic
    from anthropic import AsyncAnthropic

    ANTHROPIC_SDK_AVAILABLE = True
except ImportError:
    ANTHROPIC_SDK_AVAILABLE = False
    AsyncAnthropic = None  # type: ignore[misc, assignment]
    anthropic = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# Protocol for Claude API response structure (improves type safety)
class ClaudeContentBlock(Protocol):
    """Protocol for Claude response content blocks."""

    type: str
    text: str  # for text blocks
    id: str  # for tool_use blocks
    name: str  # for tool_use blocks
    input: dict[str, Any]  # for tool_use blocks


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
        max_parse_recovery_attempts: Maximum number of in-session recovery attempts
            for JSON parse failures. When parsing fails, a follow-up message is sent
            to the same session asking the model to correct its response format.
    """

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.25
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

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 600.0,
        retry_config: RetryConfig | None = None,
        mcp_servers: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the Claude provider.

        Args:
            api_key: Anthropic API key. If None, uses ANTHROPIC_API_KEY env var.
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
        self._max_parse_recovery_attempts = 2  # Max retry attempts for malformed JSON
        self._max_schema_depth = 10  # Max nesting depth for recursive schema building

        # MCP server configuration for tool support
        self._mcp_servers_config = mcp_servers
        self._mcp_manager: MCPManager | None = None

        # Initialize the client (sync initialization)
        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initialize the Anthropic client and log SDK version.

        Note: Model verification is deferred to validate_connection() to keep
        initialization synchronous and avoid async operations in __init__.
        """
        if not ANTHROPIC_SDK_AVAILABLE or AsyncAnthropic is None:
            return

        self._client = AsyncAnthropic(
            api_key=self._api_key,
            timeout=self._timeout,
        )

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

        Consolidated from the former _verify_available_models method.
        """
        if self._client is None:
            return

        try:
            # Call client.models.list() to get available models (async)
            logger.debug("Discovering available Claude models via client.models.list()...")
            models_page = await self._client.models.list()
            available_models = [model.id for model in models_page.data]

            logger.info(f"Available Claude models: {', '.join(available_models)}")

            # Warn if default model not in list
            if self._default_model not in available_models:
                logger.warning(
                    f"Requested model '{self._default_model}' is not in the list of "
                    f"available models. API calls may fail. Available: {available_models}"
                )
            else:
                logger.debug(f"Default model '{self._default_model}' verified in available models")
        except Exception as e:
            logger.warning(f"Could not list available models (discovery failed): {e}")

    async def _ensure_mcp_connected(self) -> None:
        """Connect to MCP servers if configured.

        This method lazily initializes MCP connections on first use.
        It creates an MCPManager instance and connects to all configured
        MCP servers.
        """
        # Skip if already initialized or no servers configured
        if self._mcp_manager is not None:
            return
        if not self._mcp_servers_config:
            return

        from conductor.mcp.manager import MCP_SDK_AVAILABLE, MCPManager

        if not MCP_SDK_AVAILABLE:
            logger.warning(
                "MCP servers configured but MCP SDK not installed. "
                "Install with: uv add 'mcp>=1.0.0'"
            )
            return

        self._mcp_manager = MCPManager()

        for name, config in self._mcp_servers_config.items():
            server_type = config.get("type", "stdio")
            if server_type == "stdio":
                try:
                    await self._mcp_manager.connect_server(
                        name=name,
                        command=config["command"],
                        args=config.get("args", []),
                        env=config.get("env"),
                        timeout=config.get("timeout"),
                    )
                    logger.info(f"Connected to MCP server '{name}'")
                except Exception as e:
                    logger.error(f"Failed to connect to MCP server '{name}': {e}")
                    # Continue with other servers
            else:
                logger.warning(
                    f"MCP server '{name}' has unsupported type '{server_type}' "
                    "(Claude provider only supports 'stdio')"
                )

    def _convert_mcp_tools_to_claude(
        self,
        tool_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert MCP tools to Claude tool format.

        Args:
            tool_filter: Optional list of tool names to include (prefixed names).
                If None, all tools are included.

        Returns:
            List of tool definitions in Claude's expected format.
        """
        if not self._mcp_manager:
            return []

        claude_tools: list[dict[str, Any]] = []
        for tool in self._mcp_manager.get_all_tools():
            # Apply filter if specified
            if tool_filter is not None and tool["name"] not in tool_filter:
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
        """Release provider resources and close connections."""
        # Close MCP connections first
        if self._mcp_manager is not None:
            await self._mcp_manager.close()
            self._mcp_manager = None
            logger.debug("MCP manager closed")

        if self._client is not None:
            # AsyncAnthropic uses httpx AsyncClient internally which should be closed
            await self._client.close()
            self._client = None
            logger.debug("Claude provider closed")

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
    ) -> AgentOutput:
        """Execute an agent using the Claude SDK.

        Args:
            agent: Agent definition from workflow config.
            context: Accumulated workflow context.
            rendered_prompt: Jinja2-rendered user prompt.
            tools: List of tool names available to this agent (currently unused).

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If SDK execution fails.
            ValidationError: If output doesn't match schema.
        """
        # Use retry logic wrapper for execution
        return await self._execute_with_retry(agent, context, rendered_prompt, tools)

    def _is_retryable_error(self, exception: Exception) -> bool:
        """Determine if an error should trigger a retry.

        Args:
            exception: The exception to check.

        Returns:
            True if the error is transient and should be retried.
        """
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
        """Calculate delay with exponential backoff and jitter.

        Args:
            attempt: Current attempt number (1-indexed).
            config: Retry configuration.

        Returns:
            Delay in seconds before next retry.
        """
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

        Returns:
            Normalized AgentOutput with structured content.

        Raises:
            ProviderError: If execution fails after all retry attempts.
            ValidationError: If output validation fails.
        """
        if self._client is None:
            raise ProviderError("Claude client not initialized")

        # Connect to MCP servers if configured (lazy initialization)
        await self._ensure_mcp_connected()

        last_error: Exception | None = None
        config = self._retry_config

        # Build messages
        messages = self._build_messages(rendered_prompt)

        # Get model and parameters
        model = agent.model or self._default_model
        temperature = self._default_temperature
        max_tokens = self._default_max_tokens

        # Validate max_tokens against model-specific limits
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

        # Add emit_output tool if agent has output schema
        if agent.output is not None:
            all_tools.extend(self._build_tools_for_structured_output(agent.output))
            # Append instruction to use the tool
            messages[-1]["content"] += (
                "\n\nPlease use the 'emit_output' tool to return your response "
                "in the required structured format."
            )

        # Add MCP tools if available
        if self._mcp_manager and self._mcp_manager.has_servers():
            mcp_tools = self._convert_mcp_tools_to_claude(tools)  # tools is the filter
            all_tools.extend(mcp_tools)
            if mcp_tools:
                logger.debug(f"Added {len(mcp_tools)} MCP tools to request")

        # Use tools if any are defined
        request_tools: list[dict[str, Any]] | None = all_tools if all_tools else None

        # Track if agent has output schema
        has_output_schema = agent.output is not None

        for attempt in range(1, config.max_attempts + 1):
            try:
                # Execute with agentic tool loop
                response, total_tokens = await self._execute_agentic_loop(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=request_tools,
                    output_schema=agent.output,
                    has_output_schema=has_output_schema,
                )

                # Extract structured output
                content = self._extract_output(response, agent.output)

                # Validate output if schema is defined
                if agent.output:
                    validate_output(content, agent.output)

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
                    # Calculate delay with exponential backoff
                    delay = self._calculate_delay(attempt, config)
                    logger.info(
                        f"Calculated exponential backoff delay: {delay:.2f}s for attempt {attempt}"
                    )

                # Log retry attempt with full context
                logger.warning(
                    f"[Retry {attempt}/{config.max_attempts}] Retrying after {delay:.2f}s "
                    f"due to {type(e).__name__}: {e}"
                )
                retry_entry["delay"] = delay

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

    async def _execute_api_call(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
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

        # Build API call kwargs
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if temperature is not None:
            kwargs["temperature"] = temperature

        if tools:
            kwargs["tools"] = tools

        # Execute non-streaming API call (async)
        logger.debug(
            f"Executing non-streaming Claude API call: model={model}, "
            f"max_tokens={max_tokens}, timeout={self._timeout}s"
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
        max_iterations: int = 10,
    ) -> tuple[ClaudeResponse, int | None]:
        """Execute an agentic loop that handles MCP tool calls.

        This method implements a tool-use loop:
        1. Call the Claude API
        2. If Claude returns tool_use blocks (other than emit_output), execute them
        3. Send tool results back and continue the loop
        4. Terminate when Claude returns emit_output or a final text response

        Args:
            messages: Initial message history.
            model: Model identifier.
            temperature: Temperature setting.
            max_tokens: Maximum output tokens.
            tools: Tool definitions (emit_output + MCP tools).
            output_schema: Expected output schema.
            has_output_schema: Whether agent has output schema defined.
            max_iterations: Maximum number of tool-use iterations to prevent infinite loops.

        Returns:
            Tuple of (final_response, total_tokens_used).

        Raises:
            ProviderError: If execution fails or max iterations exceeded.
        """
        # Make a copy of messages to avoid mutating the original
        working_messages = list(messages)
        total_tokens = 0
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            logger.debug(f"Agentic loop iteration {iteration}/{max_iterations}")

            # Execute API call (with parse recovery for structured output)
            if has_output_schema:
                response = await self._execute_with_parse_recovery(
                    messages=working_messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    output_schema=output_schema,
                )
            else:
                response = await self._execute_api_call(
                    messages=working_messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                )

            # Accumulate token usage
            if hasattr(response, "usage"):
                input_tokens = getattr(response.usage, "input_tokens", 0)
                output_tokens = getattr(response.usage, "output_tokens", 0)
                total_tokens += input_tokens + output_tokens

            # Check for tool_use blocks
            tool_uses = [
                block
                for block in response.content
                if hasattr(block, "type") and block.type == "tool_use"
            ]

            if not tool_uses:
                # No tool calls, we're done
                logger.debug("No tool_use in response, exiting agentic loop")
                return response, total_tokens

            # Check if emit_output was called (structured output)
            emit_output = next((t for t in tool_uses if t.name == "emit_output"), None)
            if emit_output:
                # Final output received, we're done
                logger.debug("emit_output tool called, exiting agentic loop")
                return response, total_tokens

            # Handle MCP tool calls
            mcp_tool_uses = [t for t in tool_uses if t.name != "emit_output"]

            if not mcp_tool_uses:
                # No MCP tools to execute
                return response, total_tokens

            if not self._mcp_manager:
                logger.warning(
                    f"Claude called MCP tools but no MCP manager available: "
                    f"{[t.name for t in mcp_tool_uses]}"
                )
                return response, total_tokens

            logger.info(
                f"Executing {len(mcp_tool_uses)} MCP tool call(s): "
                f"{[t.name for t in mcp_tool_uses]}"
            )

            # Execute each MCP tool call
            tool_results: list[dict[str, Any]] = []
            for tool_use in mcp_tool_uses:
                try:
                    result = await self._mcp_manager.call_tool(
                        tool_use.name, dict(tool_use.input) if hasattr(tool_use, "input") else {}
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result,
                        }
                    )
                    logger.debug(f"MCP tool '{tool_use.name}' succeeded")
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

    async def _execute_with_parse_recovery(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        output_schema: dict[str, OutputField] | None,
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

        Returns:
            Claude API response.

        Raises:
            ProviderError: If all retry attempts fail with context about attempts.
        """
        # Track recovery attempts for error reporting
        recovery_history: list[str] = []

        # Initial attempt using non-streaming API call
        response = await self._execute_api_call(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
        )

        # If no output schema, return immediately (no recovery needed)
        if not output_schema:
            return response

        # Check if we got tool_use (success path)
        if self._extract_structured_output(response) is not None:
            logger.debug("Successfully extracted structured output from tool_use block")
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
            f"Starting parse recovery (max {self._max_parse_recovery_attempts} attempts)"
        )

        for attempt in range(1, self._max_parse_recovery_attempts + 1):
            logger.info(f"Parse recovery attempt {attempt}/{self._max_parse_recovery_attempts}")

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
            )

            # Check if recovery succeeded (tool_use)
            if self._extract_structured_output(response) is not None:
                logger.info(f"Parse recovery succeeded on attempt {attempt} (tool_use)")
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
            f"Parse recovery exhausted after {self._max_parse_recovery_attempts} attempts. "
            f"History: {'; '.join(recovery_history)}"
        )
        raise ProviderError(
            f"Failed to extract valid JSON after {self._max_parse_recovery_attempts} "
            "recovery attempts",
            suggestion=(
                "Claude did not use the emit_output tool and returned invalid JSON. "
                f"Recovery history: {'; '.join(recovery_history)}"
            ),
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
            Dict with 'text' key containing the response text.
        """
        text_parts = []
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                text_parts.append(block.text)

        return {"text": "\n".join(text_parts)}

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
