"""GitHub Copilot SDK provider implementation.

This module provides the CopilotProvider class for executing agents
using the GitHub Copilot SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from conductor.exceptions import ProviderError
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef

logger = logging.getLogger(__name__)

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

    COPILOT_SDK_AVAILABLE = True
except ImportError:
    COPILOT_SDK_AVAILABLE = False
    CopilotClient = None  # type: ignore[misc, assignment]


@dataclass
class RetryConfig:
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

    idle_timeout_seconds: float = 300.0  # 5 minutes
    max_recovery_attempts: int = 3
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
    """

    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    partial: bool = False


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

    def __init__(
        self,
        mock_handler: Callable[[AgentDef, str, dict[str, Any]], dict[str, Any]] | None = None,
        retry_config: RetryConfig | None = None,
        model: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        idle_recovery_config: IdleRecoveryConfig | None = None,
        temperature: float | None = None,
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
        """
        self._client: Any = None  # Will hold Copilot SDK client
        self._mock_handler = mock_handler
        self._call_history: list[dict[str, Any]] = []
        self._retry_config = retry_config or RetryConfig()
        self._retry_history: list[dict[str, Any]] = []  # For testing retries
        self._default_model = model or "gpt-4o"
        self._mcp_servers = mcp_servers or {}
        self._started = False
        self._start_lock = asyncio.Lock()
        self._session_create_lock = asyncio.Lock()
        self._idle_recovery_config = idle_recovery_config or IdleRecoveryConfig()
        self._temperature = temperature
        self._session_ids: dict[str, str] = {}
        self._resume_session_ids: dict[str, str] = {}
        self._interrupted_session: Any = None
        self._abort_supported: bool | None = None

    @staticmethod
    def _default_permission_handler(
        request: dict[str, Any],
        invocation: dict[str, str],
    ) -> dict[str, Any]:
        """Default permission handler that approves all requests.

        SDK v0.1.28+ requires a permission handler on session creation.
        In orchestration mode, we approve all tool permissions since the
        workflow author controls which tools are available to each agent.
        """
        logger.debug("auto-approved permission request: %s", request)
        return {"kind": "approved"}

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
        config = self._retry_config

        for attempt in range(1, config.max_attempts + 1):
            try:
                content, sdk_response = await self._execute_sdk_call(
                    agent,
                    rendered_prompt,
                    context,
                    tools,
                    interrupt_signal=interrupt_signal,
                    event_callback=event_callback,
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
                    model=agent.model or self._default_model,
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

                # Don't retry if this was the last attempt
                if attempt >= config.max_attempts:
                    break

                # Calculate delay with exponential backoff
                delay = self._calculate_delay(attempt, config)

                logger.debug(f"Retrying agent '{agent.name}' in {delay:.2f}s")

                # Log retry attempt (for testing visibility)
                self._retry_history[-1]["delay"] = delay

                await asyncio.sleep(delay)

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
    ) -> tuple[dict[str, Any], SDKResponse | None]:
        """Execute the actual SDK call or mock handler.

        Args:
            agent: Agent definition from workflow config.
            rendered_prompt: Jinja2-rendered user prompt.
            context: Accumulated workflow context.
            tools: List of tool names available to this agent.
            interrupt_signal: Optional event for mid-agent interrupt signaling.
            event_callback: Optional callback for streaming SDK events upstream.

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
        if agent.output:
            schema_for_prompt = {
                name: {
                    "type": field.type,
                    "description": field.description or f"The {name} field",
                }
                for name, field in agent.output.items()
            }
            schema_desc = json.dumps(schema_for_prompt, indent=2)
            full_prompt += (
                f"\n\n**IMPORTANT: You MUST respond with a JSON object matching this schema:**\n"
                f"```json\n{schema_desc}\n```\n"
                f"Return ONLY the JSON object, no other text."
            )

        try:
            # Build session config with MCP servers from workflow configuration
            session_config: dict[str, Any] = {
                "model": model,
                "on_permission_request": self._default_permission_handler,
            }

            # Add temperature if configured
            if self._temperature is not None:
                session_config["temperature"] = self._temperature

            # Add MCP servers if configured
            if self._mcp_servers:
                session_config["mcp_servers"] = self._mcp_servers

            # Attempt to resume a previous session if one exists for this agent
            session: Any = None
            resume_sid = self._resume_session_ids.get(agent.name)
            if resume_sid is not None:
                try:
                    session = await self._client.resume_session(
                        resume_sid,
                        {"on_permission_request": self._default_permission_handler},
                    )
                    logger.info(f"Resumed Copilot session {resume_sid} for agent '{agent.name}'")
                except Exception as exc:
                    logger.warning(
                        f"Could not resume session {resume_sid} for agent "
                        f"'{agent.name}': {exc}. Falling back to new session."
                    )
                    session = None

            # Fall back to creating a new session.
            # Serialize session creation to prevent a race condition in the
            # Copilot SDK where overlapping create_session calls (e.g. from
            # for-each groups with max_concurrent > 1) can cause the CLI to
            # send permission.request for a session not yet registered in
            # _sessions, raising "Permission denied".  See #27 / #29.
            if session is None:
                async with self._session_create_lock:
                    session = await self._client.create_session(session_config)

            # Track session ID for checkpoint persistence
            sid = getattr(session, "session_id", None)
            if sid is not None:
                self._session_ids[agent.name] = sid

            # Capture verbose state before callback (contextvars don't propagate to sync callbacks)
            from conductor.cli.app import is_full, is_verbose

            verbose_enabled = is_verbose()
            full_enabled = is_full()

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
                    )
                    return partial_content, partial_usage

                # Track cumulative usage across potential recovery calls
                total_input_tokens = sdk_response.input_tokens
                total_output_tokens = sdk_response.output_tokens
                cache_read_tokens = sdk_response.cache_read_tokens
                cache_write_tokens = sdk_response.cache_write_tokens

                # If no output schema, we're done
                if not agent.output:
                    final_usage = SDKResponse(
                        content=response_content,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        cache_write_tokens=cache_write_tokens,
                    )
                    return {"result": response_content}, final_usage

                # Try to parse the response as JSON with recovery loop
                max_recovery = self._retry_config.max_parse_recovery_attempts
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
                            )

                        # Build recovery prompt and send to same session
                        recovery_prompt = self._build_parse_recovery_prompt(
                            parse_error=last_parse_error,
                            original_response=response_content,
                            schema=schema_for_prompt,  # type: ignore[arg-type]
                        )

                        # Send recovery prompt and get new response
                        recovery_response = await self._send_and_wait(
                            session, recovery_prompt, verbose_enabled, full_enabled
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

                # All recovery attempts exhausted
                expected_fields = list(agent.output.keys())
                raise ProviderError(
                    f"Failed to parse structured output from agent response: {last_parse_error}",
                    suggestion=(
                        f"Agent was expected to return JSON with fields: {expected_fields}. "
                        f"Response started with: {response_content[:200]}..."
                    ),
                    is_retryable=True,
                )

            finally:
                # Destroy session unless it was kept alive for follow-up
                if not session_destroyed:
                    await session.destroy()

        except ProviderError:
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
                logger.debug("sdk_event: %s%s", event_type, tool_info)

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

            # Forward structured events upstream via event_callback
            if event_callback is not None:
                self._forward_event(event_type, event, event_callback)

            # Verbose logging for intermediate progress
            if verbose_enabled:
                self._log_event_verbose(event_type, event, full_enabled)

        session.on(on_event)
        await session.send({"prompt": prompt})

        # If interrupt_signal is provided, race between done and interrupt
        if interrupt_signal is not None:
            was_interrupted = await self._wait_with_interrupt(
                done,
                session,
                interrupt_signal,
                last_activity_ref,
                verbose_enabled,
                full_enabled,
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
                )
        else:
            # Wait with idle detection and recovery (original path)
            await self._wait_with_idle_detection(
                done, session, verbose_enabled, full_enabled, last_activity_ref
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
        )

    async def _wait_with_interrupt(
        self,
        done: asyncio.Event,
        session: Any,
        interrupt_signal: asyncio.Event,
        last_activity_ref: list[Any],
        verbose_enabled: bool,
        full_enabled: bool,
    ) -> bool:
        """Wait for session completion or interrupt signal, whichever comes first.

        If the interrupt signal fires first, attempts to abort the session
        and waits briefly for a post-abort event (idle or error) before
        returning.

        Args:
            done: Event that signals session completion.
            session: The Copilot SDK session.
            interrupt_signal: Event that signals a user interrupt request.
            last_activity_ref: Mutable [last_event_type, last_tool_call, timestamp].
            verbose_enabled: Whether verbose logging is enabled.
            full_enabled: Whether full logging mode is enabled.

        Returns:
            True if interrupted, False if completed normally.
        """
        # Create tasks for both events
        done_waiter = asyncio.create_task(done.wait())
        interrupt_waiter = asyncio.create_task(interrupt_signal.wait())

        try:
            finished, pending = await asyncio.wait(
                {done_waiter, interrupt_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            if interrupt_waiter in finished:
                # Interrupt fired — attempt to abort the session
                interrupt_signal.clear()
                logger.info("Mid-agent interrupt received, attempting session abort")
                await self._abort_session(session, done)
                return True

            # Normal completion
            return False

        except Exception:
            # Cleanup on unexpected error
            for t in (done_waiter, interrupt_waiter):
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
            raise

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

    async def send_followup(self, session: Any, guidance: str) -> AgentOutput:
        """Send follow-up guidance to an interrupted session.

        After a mid-agent interrupt, the session is kept alive so that
        the user's guidance can be sent as a follow-up message. This
        method sends the guidance, waits for the response, and then
        destroys the session.

        Args:
            session: The Copilot SDK session handle (kept alive after interrupt).
            guidance: User-provided guidance text to send as follow-up.

        Returns:
            AgentOutput with the follow-up response content.
        """
        from conductor.cli.app import is_full, is_verbose

        verbose_enabled = is_verbose()
        full_enabled = is_full()

        try:
            sdk_response = await self._send_and_wait(
                session, guidance, verbose_enabled, full_enabled
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
                model=self._default_model,
            )
        finally:
            await session.destroy()

    def _log_parse_recovery(
        self,
        attempt: int,
        max_attempts: int,
        error: str,
    ) -> None:
        """Log a parse recovery attempt in verbose mode.

        Args:
            attempt: Current recovery attempt number (1-based).
            max_attempts: Maximum number of recovery attempts.
            error: The parse error message.
        """
        from rich.console import Console
        from rich.text import Text

        console = Console(stderr=True, highlight=False)

        text = Text()
        text.append("    ├─ ", style="dim")
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

        # Look for ```json ... ``` blocks
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
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

        raise ValueError(f"Could not extract JSON from response: {content[:200]}...")

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

    def _log_event_verbose(self, event_type: str, event: Any, full_mode: bool) -> None:
        """Log SDK events in verbose mode for progress visibility.

        Note: Caller must check is_verbose() before calling - contextvars
        don't propagate to sync callbacks from the SDK.

        Args:
            event_type: The event type string.
            event: The event object.
            full_mode: If True, show full details (args, results, reasoning).
        """
        from rich.console import Console
        from rich.text import Text

        from conductor.cli.run import _file_console

        console = Console(stderr=True, highlight=False)

        def _print(renderable: Any) -> None:
            console.print(renderable)
            if _file_console is not None:
                _file_console.print(renderable)

        # Log interesting events with Rich styling
        if event_type == "tool.execution_start":
            tool_name = (
                getattr(event.data, "tool_name", None)
                or getattr(event.data, "name", None)
                or "unknown"
            )

            text = Text()
            text.append("    ├─ ", style="dim")
            text.append("🔧 ", style="")
            text.append(str(tool_name), style="cyan bold")
            _print(text)

            # In full mode, try to show arguments
            if full_mode:
                args = getattr(event.data, "arguments", None) or getattr(event.data, "args", None)
                if args:
                    args_str = str(args)
                    args_preview = args_str[:200] + "..." if len(args_str) > 200 else args_str
                    arg_text = Text()
                    arg_text.append("    │     ", style="dim")
                    arg_text.append("args: ", style="dim italic")
                    arg_text.append(args_preview, style="dim")
                    _print(arg_text)

        elif event_type == "tool.execution_complete":
            # tool.execution_complete may not have tool name, just acknowledge completion
            tool_name = getattr(event.data, "tool_name", None) or getattr(event.data, "name", None)
            if tool_name:
                text = Text()
                text.append("    │  ", style="dim")
                text.append("✓ ", style="green")
                text.append(str(tool_name), style="dim")
                _print(text)

            # In full mode, try to show result preview
            if full_mode:
                result = getattr(event.data, "result", None) or getattr(event.data, "output", None)
                if result:
                    result_str = str(result)
                    if len(result_str) > 200:
                        result_preview = result_str[:200] + "..."
                    else:
                        result_preview = result_str
                    result_text = Text()
                    result_text.append("    │     ", style="dim")
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
                    text.append("💭 ", style="")
                    text.append(display_reasoning.replace("\n", " "), style="italic dim")
                    _print(text)

        elif event_type == "subagent.started":
            agent_name = getattr(event.data, "name", None) or "unknown"
            text = Text()
            text.append("    ├─ ", style="dim")
            text.append("🤖 ", style="")
            text.append("Sub-agent: ", style="dim")
            text.append(str(agent_name), style="magenta bold")
            _print(text)

        elif event_type == "subagent.completed":
            agent_name = getattr(event.data, "name", None) or "unknown"
            text = Text()
            text.append("    │  ", style="dim")
            text.append("✓ ", style="green")
            text.append(f"Sub-agent done: {agent_name}", style="dim")
            _print(text)

        elif event_type == "assistant.turn_start":
            # Only show processing indicator in full mode
            if full_mode:
                turn = getattr(event.data, "turn", None)
                turn_info = f" (turn {turn})" if turn else ""
                text = Text()
                text.append("    │  ", style="dim")
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
                        "arguments": str(arguments)[:500] if arguments else None,
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
                        "result": str(result)[:500] if result else None,
                    },
                )

            elif event_type == "assistant.turn_start":
                turn = getattr(event.data, "turn", None)
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
    ) -> None:
        """Log a recovery attempt in verbose mode.

        Args:
            attempt: Current recovery attempt number (1-based).
            last_event_type: The type of the last event received.
            last_tool_call: The name of the last tool that was executing.
        """
        from rich.console import Console
        from rich.text import Text

        console = Console(stderr=True, highlight=False)

        text = Text()
        text.append("    ├─ ", style="dim")
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
    ) -> None:
        """Wait for session completion with idle detection and recovery.

        This method replaces a simple `await done.wait()` with intelligent
        idle detection. If no SDK events are received for the configured
        idle timeout, it sends a recovery prompt to nudge the session to
        continue.

        Args:
            done: Event that signals session completion.
            session: The Copilot SDK session (for sending recovery messages).
            verbose_enabled: Whether verbose logging is enabled.
            full_enabled: Whether full logging mode is enabled.
            last_activity_ref: Mutable [last_event_type, last_tool_call, timestamp]
                              for tracking last activity.

        Raises:
            ProviderError: If all recovery attempts are exhausted, or if the
                session exceeds max_session_seconds wall-clock duration.
        """
        recovery_attempts = 0
        idle_timeout = self._idle_recovery_config.idle_timeout_seconds
        session_start = time.monotonic()
        max_session = self._idle_recovery_config.max_session_seconds

        while True:
            # Check if done was already set (avoids race where session.idle
            # arrived between a previous done.clear() and the next wait).
            if done.is_set():
                return

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

            try:
                # Wait for done with idle timeout
                await asyncio.wait_for(
                    done.wait(),
                    timeout=idle_timeout,
                )
                return  # Completed successfully

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
                    self._log_recovery_attempt(recovery_attempts, last_event_type, last_tool_call)

                # Send recovery message
                recovery_prompt = self._build_recovery_prompt(last_event_type, last_tool_call)
                await session.send({"prompt": recovery_prompt})

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
                self._client = CopilotClient()
            if not self._started:
                await self._client.start()
                self._started = True

                # Ensure subprocess pipes are in blocking mode to prevent
                # BlockingIOError on large payloads. The asyncio event loop
                # may set O_NONBLOCK on inherited file descriptors.
                self._fix_pipe_blocking_mode()

    def _fix_pipe_blocking_mode(self) -> None:
        """Clear O_NONBLOCK on the Copilot CLI subprocess pipes.

        Large JSON-RPC messages (e.g., prompts with many gathered articles)
        can exceed the OS pipe buffer. When O_NONBLOCK is set, writes raise
        BlockingIOError instead of blocking until the reader drains the pipe.
        Since the SDK already runs writes in a thread-pool executor, blocking
        is safe and correct here.
        """
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

        try:
            await self._ensure_client_started()
            return True
        except Exception as e:
            raise ProviderError(
                f"Failed to connect to Copilot SDK: {e}",
                suggestion=(
                    "Ensure the Copilot CLI is installed and you have an active "
                    "GitHub Copilot subscription. Install CLI: "
                    "https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli"
                ),
                is_retryable=False,
            ) from e

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
