"""Interrupt-aware runner for Pydantic AI agents.

This module provides a reusable helper that drives a Pydantic AI agent run
while honoring Conductor's ``interrupt_signal``.  It mirrors the interrupt
semantics of ``ClaudeProvider``:

1. Check the signal between agentic iterations (tool/model turns).  When it
   fires, stop the loop and request a short final model response that
   returns the best partial result.
2. Race in-flight API calls against the signal.  When the signal wins, the
   run task is cancelled (hard abort).
3. Return ``AgentOutput(partial=True)`` for partial results without running
   the output through Conductor schema validation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError as PydanticValidationError
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.run import AgentRunResult
from pydantic_graph.basenode import End

from conductor.config.schema import MCPServerDef, ToolOutputConfig
from conductor.exceptions import ProviderError
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import ManagedWorkspaceContext
from conductor.filesystem import is_dir_strict
from conductor.mcp.manager import MCPManager
from conductor.providers._pydantic_ai.events import (
    emit_agent_turn_start,
    emit_output_recovery_event,
    emit_pydantic_event,
)
from conductor.providers._pydantic_ai.usage import last_request_input_tokens
from conductor.telemetry import guards

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class _ManagedRun:
    """Mutable accounting for resource leaves, never an engine orchestration task."""

    workspace: _ManagedWorkspace
    cwd: str
    invocation_id: str = field(default_factory=lambda: uuid4().hex)
    owner: asyncio.Task[Any] | None = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    cancelled: set[asyncio.Task[Any]] = field(default_factory=set)
    stopping: bool = False
    deadline: float | None = None

    def check_dispatch(self) -> None:
        if self.workspace.sealed or self.stopping:
            raise ProviderError("Managed Pydantic AI execution is sealed", is_retryable=False)
        self.check_publish()

    def check_publish(self) -> None:
        scopes = self.workspace.context.process_scopes
        if self.workspace.sealed or not scopes.owners.mutations_allowed(None):
            raise ProviderError("Managed Pydantic AI workspace is sealed", is_retryable=False)
        if self.owner is not None and not self.stopping:
            try:
                # Re-registration checks both seals before further dispatch.
                # Publication after joined teardown uses the workspace guard.
                scopes.owners.register(self.invocation_id, self.owner)
            except ExecutionSpecError as error:
                raise ProviderError(
                    "Managed Pydantic AI workspace is sealed", is_retryable=False
                ) from error

    def callback(self, callback: EventCallback | None) -> EventCallback | None:
        if callback is None:
            return None

        def emit(event_type: str, data: dict[str, Any]) -> None:
            try:
                self.check_dispatch()
            except ProviderError:
                return
            callback(event_type, data)

        return emit

    def spawn[T](
        self, operation: Callable[[], Coroutine[Any, Any, T]], *, primary: bool = False
    ) -> asyncio.Task[T]:
        """Register an actual leaf before its coroutine can acquire resources."""
        self.check_dispatch()
        registered = asyncio.Event()

        async def run() -> T:
            await registered.wait()
            self.check_dispatch()
            return await operation()

        task = asyncio.create_task(run())
        self.tasks.add(task)
        if primary:
            self.owner = task
        task.add_done_callback(self._observe)
        try:
            self.workspace.context.process_scopes.owners.register(self.invocation_id, task)
        except ExecutionSpecError as error:
            task.cancel()
            raise ProviderError(
                "Managed Pydantic AI resource registration is sealed", is_retryable=False
            ) from error
        registered.set()
        return task

    @staticmethod
    def _observe(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    def begin_teardown(self) -> None:
        self.stopping = True
        scopes = self.workspace.context.process_scopes
        scopes.begin_teardown(self.invocation_id, deadline=self.deadline)
        deadline = scopes.owners.deadline(self.invocation_id)
        if deadline is not None:
            self.deadline = min(self.deadline, deadline) if self.deadline is not None else deadline

    def cancel(self, task: asyncio.Task[Any]) -> None:
        if not task.done() and task not in self.cancelled:
            self.cancelled.add(task)
            task.cancel()

    async def join(self, task: asyncio.Task[Any]) -> bool:
        """Observe the real task, never wait for a resistant cancel acknowledgement."""
        scopes = self.workspace.context.process_scopes
        observer = asyncio.create_task(scopes.owners.observe(self.invocation_id, task))
        cancelled: asyncio.CancelledError | None = None
        try:
            while not observer.done():
                try:
                    await asyncio.wait({observer})
                except asyncio.CancelledError as error:
                    cancelled = cancelled or error
                    self.begin_teardown()
                    self.cancel(task)
            joined = observer.result()
            if not joined:
                self.workspace.failed = True
            if cancelled is not None:
                raise cancelled
            return joined
        finally:
            observer.cancel()

    async def wait[T](self, task: asyncio.Task[T]) -> T:
        if not await self.join(task):
            raise ProviderError("Managed Pydantic AI owner is unconfirmed", is_retryable=False)
        return task.result()

    async def finish(self) -> None:
        self.begin_teardown()
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                proof = await self.workspace.context.process_scopes.await_release(
                    self.invocation_id
                )
                break
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
        if proof.status != "released":
            self.workspace.failed = True
            logger.warning(
                "Managed Pydantic AI invocation release is unconfirmed: %s", proof.reasons
            )
            if cancelled is None:
                raise ProviderError(
                    "Managed Pydantic AI invocation release is unconfirmed", is_retryable=False
                )
        if cancelled is not None:
            raise cancelled


@dataclass(slots=True)
class _ManagedWorkspace:
    """Retain attempted managers and leaves for one immutable context identity."""

    context: ManagedWorkspaceContext
    runs: list[_ManagedRun] = field(default_factory=list)
    managers: list[MCPManager] = field(default_factory=list)
    tool_grants: dict[MCPManager, dict[str, frozenset[str] | None]] = field(default_factory=dict)
    pool: dict[str, MCPManager] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    closers: list[asyncio.Task[None]] = field(default_factory=list)
    sealed: bool = False
    failed: bool = False

    @classmethod
    def for_context(
        cls, workspaces: dict[str, _ManagedWorkspace], context: ManagedWorkspaceContext
    ) -> _ManagedWorkspace:
        workspace = workspaces.get(context.root)
        if workspace is None:
            workspace = cls(context)
            workspaces[context.root] = workspace
        if (
            workspace.sealed
            or workspace.context.incarnation != context.incarnation
            or workspace.context.process_scopes is not context.process_scopes
        ):
            raise ProviderError(
                "Managed Pydantic AI workspace is sealed or stale", is_retryable=False
            )
        return workspace

    async def call[T](
        self, cwd: str | None, operation: Callable[[_ManagedRun], Coroutine[Any, Any, T]]
    ) -> T:
        work_root = (Path(self.context.root) / "work").resolve()
        directory = Path(cwd) if cwd is not None else None
        if (
            directory is None
            or not directory.is_absolute()
            or not directory.resolve().is_relative_to(work_root)
            or not is_dir_strict(directory)
        ):
            raise ProviderError(
                "Managed Pydantic AI requires an explicit mapped cwd "
                "inside the workspace work tree",
                is_retryable=False,
            )
        invocation = _ManagedRun(self, str(directory))
        self.runs.append(invocation)

        async def run() -> T:
            try:
                return await operation(invocation)
            finally:
                invocation.begin_teardown()

        owner = invocation.spawn(run, primary=True)
        try:
            result = await invocation.wait(owner)
        except BaseException:
            try:
                await invocation.finish()
            except (ProviderError, asyncio.CancelledError) as cleanup_error:
                # The primary SDK error/cancellation stays primary. Failed
                # proofs are already sticky and separately reach root release.
                logger.warning(
                    "Managed Pydantic AI teardown raised %s; preserving the primary failure",
                    type(cleanup_error).__name__,
                )
            raise
        await invocation.finish()
        invocation.check_publish()
        return result

    async def manager_for(
        self,
        invocation: _ManagedRun,
        servers: dict[str, Any] | None,
        tool_output: ToolOutputConfig,
    ) -> MCPManager | None:
        invocation.check_dispatch()
        if not servers:
            return None
        from conductor.mcp.manager import MCP_SDK_AVAILABLE

        if not MCP_SDK_AVAILABLE:
            raise ProviderError(
                "Managed Pydantic AI MCP requires the installed SDK and stdio servers",
                is_retryable=False,
            )
        grants: dict[str, frozenset[str] | None] = {}
        for name, config in servers.items():
            try:
                server = MCPServerDef.model_validate(config)
            except PydanticValidationError as error:
                raise ProviderError(
                    "Invalid managed Pydantic AI MCP configuration", is_retryable=False
                ) from error
            if server.type != "stdio" or server.url is not None or server.headers:
                raise ProviderError(
                    "Managed Pydantic AI MCP supports stdio command/args/env, "
                    "not HTTP/SSE settings",
                    is_retryable=False,
                )
            grants[name] = None if "*" in server.tools else frozenset(server.tools)
        lock = self.locks.setdefault(invocation.cwd, asyncio.Lock())
        async with lock:
            invocation.check_dispatch()
            if invocation.cwd in self.pool:
                return self.pool[invocation.cwd]
            manager = MCPManager(tool_output=tool_output, managed_workspace=self.context)
            self.managers.append(manager)
            self.tool_grants[manager] = grants
            for name, config in servers.items():
                invocation.check_dispatch()
                try:
                    await manager.connect_server(
                        name=name,
                        command=config["command"],
                        args=config.get("args", []),
                        env=config.get("env"),
                        timeout=config.get("timeout"),
                        cwd=invocation.cwd,
                    )
                except Exception:
                    logger.warning("Managed MCP server '%s' failed to connect", name)
            invocation.check_dispatch()
            if manager.has_servers():
                self.pool[invocation.cwd] = manager
            return manager

    async def release(self) -> None:
        """Join only this provider's resources; the caller owns the root barrier."""
        scopes = self.context.process_scopes
        if not self.sealed:
            self.sealed = True
            # This intent preserves an earlier root/invocation deadline. Never
            # await the root barrier containing the provider release hook.
            scopes.begin_teardown()
            self.pool.clear()
            for invocation in self.runs:
                invocation.begin_teardown()
                for task in invocation.tasks:
                    invocation.cancel(task)
            for manager in self.managers:
                task = asyncio.create_task(manager.close(strict=True))
                self.closers.append(task)
                task.add_done_callback(self._observe_close)
        pending = {
            task
            for task in [*self.closers, *(task for run in self.runs for task in run.tasks)]
            if not task.done()
        }
        terminal = asyncio.create_task(scopes.owners.wait_terminal())
        cancelled: asyncio.CancelledError | None = None
        try:
            while pending:
                if terminal.done() and terminal.result().status == "unconfirmed":
                    self.failed = True
                    break
                try:
                    done, _ = await asyncio.wait(
                        pending | ({terminal} if not terminal.done() else set()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError as error:
                    cancelled = cancelled or error
                    continue
                pending.difference_update(done)
                if terminal in done and terminal.result().status == "unconfirmed":
                    self.failed = True
                    break
            for task in self.closers:
                if task.done():
                    self._observe_close(task)
            if pending:
                self.failed = True
            if not scopes.owners.mutations_allowed(None):
                proof = await scopes.owners.wait_terminal()
                if proof.status != "released":
                    self.failed = True
            if cancelled is not None:
                raise cancelled
            if self.failed:
                raise ProviderError(
                    "Managed Pydantic AI workspace release is unconfirmed", is_retryable=False
                )
        finally:
            terminal.cancel()

    def _observe_close(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is not None:
            self.failed = True


_INTERRUPTION_USER_PROMPT = (
    "The user has interrupted execution. Please immediately provide "
    "your best partial result based on the work completed so far. "
    "Return whatever you have, even if incomplete."
)

_INTERRUPTION_TOOL_PROMPT = (
    "The user has interrupted execution. Please immediately provide "
    "your best partial result based on the work completed so far. "
    "Return whatever you have, even if incomplete. Respond with plain text; "
    "do not call any tools."
)


def _session_expired_message(max_session_seconds: float) -> str:
    """Return a ProviderError message for a session timeout."""
    return f"Agent exceeded maximum session duration of {max_session_seconds:.0f}s"


def _iterations_exceeded_message(request_limit: int) -> str:
    """Return a ProviderError message for an exceeded iteration limit."""
    return f"Agentic loop exceeded maximum iterations ({request_limit})"


@dataclass
class RunOutcome:
    """Result of an interrupt-aware Pydantic AI agent run.

    Attributes:
        result: Final pydantic-ai run result when the run completed normally.
            ``None`` when a partial result was returned or the run was
            cancelled.
        partial_output: Best-effort partial content when the run was
            interrupted, or ``None`` for normal completion.
        is_partial: Whether the outcome represents a partial/interrupted
            result.
        is_cancelled: Whether the run was cancelled (hard abort).
        total_usage: Aggregated token usage across all model calls in this
            helper. May be ``None`` when no usage information is available.
        last_call_input_tokens: Prompt tokens of the most recent single API
            call, for the context-window bar (issue #412) — distinct from
            ``total_usage``, which aggregates the whole run.
    """

    result: AgentRunResult[Any] | None = None
    partial_output: Any = None
    is_partial: bool = False
    is_cancelled: bool = False
    total_usage: dict[str, Any] | None = None
    last_call_input_tokens: int | None = None


def _make_interrupt_message(has_output_schema: bool) -> UserPromptPart:
    """Build the user message asking the model for a partial result."""
    content = _INTERRUPTION_TOOL_PROMPT if has_output_schema else _INTERRUPTION_USER_PROMPT
    return UserPromptPart(content=content)


def _usage_from_result(result: AgentRunResult[Any] | None) -> dict[str, Any] | None:
    """Return JSON-safe usage from a pydantic-ai result, or None."""
    if result is None or not hasattr(result, "usage"):
        return None
    usage = result.usage
    return {
        "requests": getattr(usage, "requests", None),
        "request_tokens": getattr(usage, "request_tokens", None),
        "response_tokens": getattr(usage, "response_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _has_stream(node: Any) -> bool:
    """Return whether a graph node supports event streaming."""
    return hasattr(node, "stream")


async def _stream_node_events(
    agent: Agent[Any, Any],
    node: Any,
    run_context: Any,
    event_callback: EventCallback | None,
    recovery_attempt: list[int],
    max_parse_recovery_attempts: int,
) -> None:
    """Stream a node's events through the Conductor callback."""
    if event_callback is None or not _has_stream(node):
        return
    async with node.stream(run_context) as stream:
        async for event in stream:
            emit_pydantic_event(agent, event, event_callback)
            next_attempt = recovery_attempt[0] + 1
            if emit_output_recovery_event(
                event,
                event_callback,
                attempt=next_attempt,
                max_attempts=max_parse_recovery_attempts,
            ):
                recovery_attempt[0] = next_attempt


async def _execute_node(
    agent: Agent[Any, Any],
    run: Any,
    node: Any,
    event_callback: EventCallback | None,
    recovery_attempt: list[int],
    max_parse_recovery_attempts: int,
) -> Any:
    """Execute a single graph node and emit its streaming events.

    Pydantic AI exposes streaming events on nodes that have a ``stream``
    method (``ModelRequestNode`` and ``CallToolsNode``).  We consume the
    stream to forward Conductor events, then advance the graph with
    ``run.next(node)`` to obtain the following node.
    """
    await _stream_node_events(
        agent,
        node,
        run.ctx,
        event_callback,
        recovery_attempt,
        max_parse_recovery_attempts,
    )
    return await run.next(node)


async def run_with_interrupt(
    agent: Agent[Any, Any],
    user_prompt: str,
    *,
    interrupt_signal: asyncio.Event | None,
    event_callback: EventCallback | None,
    has_output_schema: bool,
    usage_limits: UsageLimits | None = None,
    max_session_seconds: float | None = None,
    max_parse_recovery_attempts: int = 0,
    message_history: Sequence[ModelMessage] | None = None,
    managed_run: _ManagedRun | None = None,
) -> RunOutcome:
    """Run a Pydantic AI agent with Conductor interrupt support.

    The helper drives the agent node-by-node so it can check
    ``interrupt_signal`` between model/tool turns.  When the signal is set
    at a boundary, it exits the current run, appends a user message asking
    for the best partial result, and starts a fresh run with the captured
    message history.  When the signal wins a race against an in-flight
    node execution, the task is cancelled and ``is_cancelled=True`` is
    returned.

    Args:
        agent: The configured Pydantic AI agent.
        user_prompt: The rendered user prompt for this agent step.
        interrupt_signal: Optional asyncio event that requests a graceful
            interrupt.  When ``None``, the helper behaves like a normal run.
        event_callback: Optional Conductor event callback for streaming
            events.
        has_output_schema: Whether the agent was built with a structured
            output schema.  Affects the wording of the interrupt prompt.
        usage_limits: Optional pydantic-ai usage limits forwarded to
            ``agent.iter``.  ``UsageLimits(request_limit=N)``
            caps model requests; when exceeded pydantic-ai raises
            ``UsageLimitExceeded``, which is mapped to a non-retryable
            ``ProviderError`` matching the legacy ``ClaudeProvider``
            max-iterations behavior.
        max_session_seconds: Optional wall-clock cap for the whole session.
            Enforced at the start of each agentic iteration, matching the
            legacy ``ClaudeProvider`` loop semantics.  When exceeded, a
            non-retryable ``ProviderError`` is raised.
        max_parse_recovery_attempts: Configured output-correction budget used
            in ``agent_parse_recovery`` event payloads.
        message_history: Optional completed-run messages to continue before
            adding ``user_prompt`` as the next user turn.

    Returns:
        A ``RunOutcome`` describing normal completion, partial output, or
        cancellation.
    """
    if managed_run is not None:
        managed_run.check_dispatch()
    interrupt_signal = interrupt_signal or asyncio.Event()

    if interrupt_signal.is_set():
        logger.info("Pydantic AI agent interrupted before first iteration")
        interrupt_signal.clear()
        return await _request_partial_output(
            agent,
            list(message_history or []),
            event_callback,
            has_output_schema,
            **({"managed_run": managed_run} if managed_run is not None else {}),
        )

    recovery_attempt = [0]
    iteration = 0
    session_start = time.monotonic()

    try:
        async with agent.iter(
            user_prompt,
            usage_limits=usage_limits,
            message_history=message_history,
            conversation_id=guards.current_run_id(),
        ) as run:
            next_node = run.next_node
            while not isinstance(next_node, End):
                if managed_run is not None:
                    managed_run.check_dispatch()
                iteration += 1

                if max_session_seconds is not None:
                    elapsed = time.monotonic() - session_start
                    if elapsed > max_session_seconds:
                        raise ProviderError(
                            _session_expired_message(max_session_seconds),
                            suggestion="The agent session exceeded its wall-clock limit.",
                            is_retryable=False,
                        )

                if interrupt_signal.is_set():
                    logger.info("Pydantic AI agent interrupted between iterations")
                    interrupt_signal.clear()
                    return await _request_partial_output(
                        agent,
                        list(run.all_messages()),
                        event_callback,
                        has_output_schema,
                        **({"managed_run": managed_run} if managed_run is not None else {}),
                    )

                emit_agent_turn_start(event_callback, iteration)

                if type(next_node).__name__ == "ModelRequestNode":
                    emit_agent_turn_start(event_callback, "awaiting_model")

                    def execute_node(node: Any = next_node) -> Coroutine[Any, Any, Any]:
                        return _execute_node(
                            agent,
                            run,
                            node,
                            event_callback,
                            recovery_attempt,
                            max_parse_recovery_attempts,
                        )

                    node_task = (
                        asyncio.create_task(execute_node())
                        if managed_run is None
                        else managed_run.spawn(execute_node)
                    )
                    signal_task = asyncio.create_task(interrupt_signal.wait())
                    finished: set[asyncio.Task[Any]]
                    try:
                        finished, _ = await asyncio.wait(
                            {node_task, signal_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except BaseException:
                        if managed_run is not None:
                            managed_run.begin_teardown()
                            managed_run.cancel(node_task)
                        else:
                            node_task.cancel()
                        signal_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            if managed_run is None:
                                await node_task
                            else:
                                await managed_run.join(node_task)
                        with contextlib.suppress(asyncio.CancelledError):
                            await signal_task
                        raise

                    if signal_task in finished:
                        if managed_run is not None:
                            managed_run.begin_teardown()
                            managed_run.cancel(node_task)
                        else:
                            node_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            if managed_run is None:
                                await node_task
                            else:
                                await managed_run.join(node_task)
                        interrupt_signal.clear()
                        logger.info("Pydantic AI agent interrupted during model API call")
                        return RunOutcome(is_cancelled=True)

                    signal_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await signal_task

                    try:
                        next_node = await node_task
                    except asyncio.CancelledError:
                        raise
                else:
                    if managed_run is None:
                        next_node = await _execute_node(
                            agent,
                            run,
                            next_node,
                            event_callback,
                            recovery_attempt,
                            max_parse_recovery_attempts,
                        )
                    else:
                        next_node = await managed_run.wait(
                            managed_run.spawn(
                                lambda node=next_node: _execute_node(
                                    agent,
                                    run,
                                    node,
                                    event_callback,
                                    recovery_attempt,
                                    max_parse_recovery_attempts,
                                )
                            )
                        )

    except UsageLimitExceeded as exc:
        request_limit = usage_limits.request_limit if usage_limits is not None else None
        raise ProviderError(
            _iterations_exceeded_message(request_limit)
            if request_limit is not None
            else f"Agent exceeded usage limits: {exc}",
            suggestion="The agent may be stuck in a tool-use loop. Check your MCP tools.",
            is_retryable=False,
        ) from exc

    final_result = run.result
    if final_result is None:
        raise RuntimeError("Pydantic AI run ended without a result")
    return RunOutcome(
        result=final_result,
        total_usage=_usage_from_result(final_result),
        last_call_input_tokens=last_request_input_tokens(final_result.all_messages()),
    )


async def _request_partial_output(
    agent: Agent[Any, Any],
    history: list[ModelMessage],
    event_callback: EventCallback | None,
    has_output_schema: bool,
    *,
    managed_run: _ManagedRun | None = None,
) -> RunOutcome:
    """Run one final model call asking for the best partial result.

    This performs a hard cut: tools are not available on this run so the model
    must answer directly.  The returned content is marked partial and is not
    validated against the Conductor output schema.
    """
    emit_agent_turn_start(event_callback, "awaiting_model")
    interrupt_message = _make_interrupt_message(has_output_schema)
    partial_history = list(history)
    partial_history.append(ModelRequest(parts=[interrupt_message]))
    # Enforce the hard cut this path promises. agent.override(toolsets=[],
    # tools=[]) is required because the toolsets= run() kwarg is additive in
    # pydantic-ai (it never clears toolsets registered at construction time);
    # output_type=str bypasses the ToolOutput schema so the model answers
    # directly instead of retrying output-tool validation.
    with agent.override(toolsets=[], tools=[]):
        if managed_run is None:
            result = await agent.run(
                user_prompt=None,
                message_history=partial_history,
                output_type=str,
                conversation_id=guards.current_run_id(),
            )
        else:
            result = await managed_run.wait(
                managed_run.spawn(
                    lambda: agent.run(
                        user_prompt=None,
                        message_history=partial_history,
                        output_type=str,
                        conversation_id=guards.current_run_id(),
                    )
                )
            )
            managed_run.check_dispatch()
    return RunOutcome(
        partial_output=result.output,
        is_partial=True,
        total_usage=_usage_from_result(result),
        last_call_input_tokens=last_request_input_tokens(result.all_messages()),
    )
