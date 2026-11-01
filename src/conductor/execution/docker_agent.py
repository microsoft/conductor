"""Streaming host-side Docker exec transport for one agent invocation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from conductor.exceptions import ConfigurationError, ProviderError
from conductor.execution.agent_wire import agent_spec_to_wire_body
from conductor.execution.types import AgentEventSink, AgentResult, AgentSpec

_MAX_FRAME_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 8192
_INTERRUPT_TIMEOUT_SECONDS = 30
_BRIDGE_COMMAND = ("python", "-m", "conductor.runner.bridge")
type InterruptOutcome = Literal["accepted", "conflict"]


async def _reap(process: asyncio.subprocess.Process, *, kill: bool) -> bool:
    """Wait normally after EOF; shield the wait only during kill/teardown."""
    if not kill:
        _ = await process.wait()
        return False
    if sys.platform == "win32" and process.returncode is None:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    waiter = asyncio.create_task(process.wait())
    cancelled = False
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


async def _stderr_tail(stream: asyncio.StreamReader) -> bytes:
    tail = b""
    while chunk := await stream.read(4096):
        tail = (tail + chunk)[-_MAX_STDERR_BYTES:]
    return tail


def _scrub(value: bytes, spec: AgentSpec) -> str:
    """Bound and redact bridge diagnostics before they enter an exception."""
    text = value[-_MAX_STDERR_BYTES:].decode("utf-8", "replace")
    credentials = [*(spec.env_overlay or {}).values(), *(spec.provider_credentials or {}).values()]
    for raw in credentials:
        reveal = getattr(raw, "get_secret_value", None)
        secret = reveal() if callable(reveal) else raw
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[REDACTED]")
    return text


async def stream_agent(
    binary: str,
    cli_env: Mapping[str, str],
    container: str,
    spec: AgentSpec,
    on_event: AgentEventSink | None,
    interrupt_signal: asyncio.Event | None,
    can_interrupt: bool,
    send_interrupt: Callable[[str], Awaitable[InterruptOutcome]],
) -> AgentResult:
    """Stream one Docker agent invocation through the runner bridge.

    Args:
        binary: Docker CLI executable.
        cli_env: Environment for the Docker CLI process.
        container: Lease-owned runner container name.
        spec: Resolved agent invocation sent to the runner.
        on_event: Optional sink for non-terminal agent frames.
        interrupt_signal: Optional signal requesting a graceful interruption.
        can_interrupt: Whether the runner advertises targeted interrupts.
        send_interrupt: Callback delivering an interrupt by execution ID.

    Returns:
        The terminal agent result, or a partial result on an unsupported
        runner interrupted while reading its stream.

    Raises:
        ConfigurationError: If the bridge stream is malformed or incomplete.
        ProviderError: If the runner reports an agent execution error.
    """
    process = await asyncio.create_subprocess_exec(
        binary,
        "exec",
        "-i",
        container,
        *_BRIDGE_COMMAND,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(cli_env),
        limit=_MAX_FRAME_BYTES + 1,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stderr_task = asyncio.create_task(_stderr_tail(process.stderr))
    interrupt_task: asyncio.Task[None] | None = None
    interrupt_deadline: float | None = None
    interrupt_outcome: InterruptOutcome | None = None
    interrupt_sending = False
    terminal: AgentResult | None = None
    finished = False

    async def interrupt() -> None:
        nonlocal interrupt_deadline, interrupt_outcome, interrupt_sending
        assert interrupt_signal is not None
        await interrupt_signal.wait()
        if terminal is not None:
            return
        interrupt_sending = True
        interrupt_outcome = await send_interrupt(spec.execution_id)
        interrupt_deadline = asyncio.get_running_loop().time() + _INTERRUPT_TIMEOUT_SECONDS

    async def wait_transport[T](awaitable: Awaitable[T]) -> T:
        """Observe interrupt delivery and its absolute deadline through every drain stage."""
        task = asyncio.ensure_future(awaitable)
        try:
            if interrupt_task is not None:
                if not interrupt_task.done():
                    await asyncio.wait({task, interrupt_task}, return_when=asyncio.FIRST_COMPLETED)
                if interrupt_task.done():
                    await interrupt_task
            if interrupt_deadline is None:
                return await task
            return await asyncio.wait_for(
                task, max(0, interrupt_deadline - asyncio.get_running_loop().time())
            )
        except TimeoutError as exc:
            message = (
                "Docker runner interrupt conflicted without completing its terminal drain"
                if interrupt_outcome == "conflict"
                else "Docker runner interrupt was acknowledged without a partial result"
            )
            raise ConfigurationError(message) from exc
        finally:
            if task is not stderr_task:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        body = json.dumps(agent_spec_to_wire_body(spec), ensure_ascii=True).encode("utf-8")
        process.stdin.write(body)
        await process.stdin.drain()
        process.stdin.close()
        while True:
            try:
                if interrupt_signal is not None and not can_interrupt:
                    read_task = asyncio.create_task(process.stdout.readline())
                    signal_task = asyncio.create_task(interrupt_signal.wait())
                    try:
                        done, _pending = await asyncio.wait(
                            {read_task, signal_task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if signal_task in done:
                            return AgentResult(content={}, partial=True)
                        line = read_task.result()
                    finally:
                        read_task.cancel()
                        signal_task.cancel()
                        await asyncio.gather(read_task, signal_task, return_exceptions=True)
                else:
                    line = await wait_transport(process.stdout.readline())
            except ValueError as exc:
                raise ConfigurationError("Docker runner emitted an oversized NDJSON frame") from exc
            if not line:
                break
            if len(line) > _MAX_FRAME_BYTES:
                raise ConfigurationError("Docker runner emitted an oversized NDJSON frame")
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"Docker runner bridge in container {container!r} emitted malformed NDJSON "
                    f"for execution {spec.execution_id!r}. Check runner logs and rebuild the "
                    "runner image if it is stale."
                ) from exc
            if not isinstance(frame, dict) or not isinstance(frame.get("data"), dict):
                raise ConfigurationError("Docker runner emitted an invalid frame")
            frame_type = frame.get("type")
            data: dict[str, Any] = frame["data"]
            if (
                interrupt_signal is not None
                and can_interrupt
                and interrupt_task is None
                and frame_type not in ("result", "error")
            ):
                interrupt_task = asyncio.create_task(interrupt())
            if frame_type == "bridge_ready":
                continue
            if frame_type == "result":
                if terminal is not None:
                    raise ConfigurationError("Docker runner emitted multiple terminal frames")
                if not isinstance(data.get("content", {}), dict) or not isinstance(
                    data.get("partial", False), bool
                ):
                    raise ConfigurationError("Docker runner emitted an invalid result frame")
                terminal = AgentResult(
                    content=data.get("content", {}),
                    model=data.get("model"),
                    input_tokens=data.get("input_tokens"),
                    output_tokens=data.get("output_tokens"),
                    cache_read_tokens=data.get("cache_read_tokens"),
                    cache_write_tokens=data.get("cache_write_tokens"),
                    last_call_input_tokens=data.get("last_call_input_tokens"),
                    session_seconds=data.get("session_seconds"),
                    partial=data.get("partial", False),
                )
                if interrupt_task is not None and not interrupt_sending:
                    interrupt_task.cancel()
                    await asyncio.gather(interrupt_task, return_exceptions=True)
                    interrupt_task = None
            elif frame_type == "error":
                raise ProviderError(
                    _scrub(str(data.get("message", "runner reported an error")).encode(), spec),
                    provider_name=spec.model_provider,
                )
            elif terminal is not None:
                raise ConfigurationError("Docker runner emitted an event after its terminal frame")
            elif isinstance(frame_type, str) and on_event is not None:
                on_event(frame_type, data)
        cancelled = await wait_transport(_reap(process, kill=False))
        stderr = await wait_transport(stderr_task)
        if interrupt_task is not None:
            await interrupt_task
        finished = True
        if cancelled:
            raise asyncio.CancelledError
        if process.returncode != 0:
            raise ConfigurationError(
                f"Docker runner bridge exited {process.returncode}: {_scrub(stderr, spec)}"
            )
        if terminal is None:
            raise ConfigurationError("Docker runner stream ended without a terminal frame")
        if interrupt_outcome == "accepted" and not terminal.partial:
            raise ConfigurationError(
                "Docker runner interrupt was acknowledged without a partial result"
            )
        return terminal
    finally:
        if interrupt_task is not None:
            interrupt_task.cancel()
            await asyncio.gather(interrupt_task, return_exceptions=True)
        if not finished:
            await _reap(process, kill=True)
        if not stderr_task.done():
            stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task
