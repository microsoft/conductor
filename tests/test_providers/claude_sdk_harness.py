"""Shared test seam for the ``claude-agent-sdk`` provider.

Three independent pieces, used by every test that drives the provider's SDK
session:

``patch_sdk``
    The lightweight shim. It keeps each test's own fake message generator and
    changes only *how* the provider reaches it, which is what let the provider
    move from ``query()`` to ``ClaudeSDKClient`` without rewriting a single
    assertion.

``FakeTransport``
    A real :class:`claude_agent_sdk.Transport` handed to the **real** SDK, so a
    test built on it exercises ``ClaudeSDKClient`` / ``Query`` /
    ``parse_message`` and the SDK's own teardown rather than a fake that models
    them. This is the only way to see the failure mode the shim cannot show:
    SDK-initiated teardown running *inside* the read the provider is awaiting.

``gated_scope``
    Failure-safe cleanup for any test that parks the provider on a gate. The
    gates are released and the owned tasks drained in a ``finally``, so a failing
    assertion reports itself instead of wedging the suite behind a deliberately
    shielded teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from typing import Any
from unittest.mock import patch

import claude_agent_sdk
from claude_agent_sdk import Transport

_PROVIDER = "conductor.providers.claude_agent_sdk"

#: Generous upper bound used only to fail a hung test instead of blocking the
#: suite. Nothing here depends on its value for correctness -- in particular it
#: is **not** a bound on SDK teardown, which is deliberately shielded.
HANG_GUARD_SECONDS = 5.0


def leaked_tasks(before: set[asyncio.Task[Any]]) -> list[asyncio.Task[Any]]:
    """Tasks that appeared since ``before`` and are still pending.

    Scoped to the delta so an unrelated task already running in the loop cannot
    report a leak that is not ours. Async-generator finalizers the SDK's
    ``receive_*`` chain schedules are transient and settle within a turn, so
    callers give the loop a chance to run before asking.
    """
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() - before if t is not current and not t.done()]


# ---------------------------------------------------------------------------
# The shim: the provider's SDK client, driven by a test's own fake ``query``
# ---------------------------------------------------------------------------


class ShimClient:
    """Stand-in for ``ClaudeSDKClient`` backed by a plain fake ``query``.

    The fake is called exactly as the old ``query()`` entry point was --
    ``fake(prompt=..., options=...)`` -- so every existing ``kwargs["options"]``
    assertion still reads what it always read.

    ``disconnect`` deliberately does **not** ``aclose()`` the iterator: the
    provider owns the object ``receive_response()`` handed it and closes it
    itself before shutting the client down, exactly as it would with the real
    SDK (where ``disconnect()`` tears down the transport, not the caller's
    iterator). Closing here as well would finalize one generator twice.
    """

    def __init__(self, options: Any = None, transport: Any = None) -> None:
        self.options = options
        self.transport = transport
        self.agen: Any = None
        self.connect_calls = 0
        self.query_calls = 0
        self.disconnect_calls = 0

    # -- ClaudeSDKClient surface the provider uses ----------------------
    async def connect(self, prompt: Any = None) -> None:
        self.connect_calls += 1
        await self._on_connect()

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.query_calls += 1
        self.sent_prompt = prompt
        self.sent_session_id = session_id
        await self._on_send()
        self.agen = self._fake_query(prompt=prompt, options=self.options)

    def receive_response(self) -> Any:
        return self.agen

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._trace.append("disconnect")
        await self._on_disconnect()

    # -- hooks a factory may override ------------------------------------
    async def _on_connect(self) -> None:
        return None

    async def _on_send(self) -> None:
        return None

    async def _on_disconnect(self) -> None:
        return None


def shim_client_factory(
    fake_query: Callable[..., Any],
    *,
    trace: list[str] | None = None,
    on_connect: Callable[[], Any] | None = None,
    on_send: Callable[[], Any] | None = None,
    on_disconnect: Callable[[], Any] | None = None,
    clients: list[ShimClient] | None = None,
) -> Callable[..., ShimClient]:
    """Build a ``ClaudeSDKClient`` replacement class bound to ``fake_query``."""
    shared_trace: list[str] = trace if trace is not None else []

    class _Shim(ShimClient):
        def __init__(self, options: Any = None, transport: Any = None) -> None:
            super().__init__(options, transport)
            self._fake_query = fake_query
            self._trace = shared_trace
            if clients is not None:
                clients.append(self)

        async def _on_connect(self) -> None:
            if on_connect is not None:
                await _maybe_await(on_connect())

        async def _on_send(self) -> None:
            if on_send is not None:
                await _maybe_await(on_send())

        async def _on_disconnect(self) -> None:
            if on_disconnect is not None:
                await _maybe_await(on_disconnect())

    return _Shim


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def patch_sdk(fake_query: Callable[..., Any], **kwargs: Any) -> Any:
    """Point the provider at ``fake_query`` through a fake ``ClaudeSDKClient``.

    Usable as a decorator or a context manager, like the ``patch`` call it
    replaces.
    """
    return patch(f"{_PROVIDER}.ClaudeSDKClient", shim_client_factory(fake_query, **kwargs))


# ---------------------------------------------------------------------------
# The black-box harness: a real Transport under the real SDK
# ---------------------------------------------------------------------------

_EOF = object()


class FakeTransport(Transport):
    """A real ``claude_agent_sdk.Transport`` backed by a scriptable queue.

    Everything above it is the SDK's own code, so a regression written against
    this one sees the real ``Query.close()`` -> ``transport.close()`` teardown
    chain -- including the case where that chain runs *inside* the read the
    provider is awaiting, which is what no fake iterator could reproduce.

    The ABC is documented as unstable. That is acceptable here and nowhere
    else: a shape change breaks these tests loudly instead of silently
    degrading the provider.
    """

    def __init__(
        self,
        *,
        fail_initialize: bool = False,
        close_failures: Iterable[BaseException | None] = (),
    ) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._fail_initialize = fail_initialize
        self._close_failures = list(close_failures)

        # Gates start open; a test closes only the one it wants to park on.
        self.connect_gate = asyncio.Event()
        self.connect_gate.set()
        self.write_gate = asyncio.Event()
        self.write_gate.set()
        #: Gates the prompt write only, leaving the control protocol free to
        #: complete -- otherwise parking the send would also park initialize,
        #: and the test would be measuring connect instead.
        self.prompt_gate = asyncio.Event()
        self.prompt_gate.set()
        self.close_gate = asyncio.Event()
        self.close_gate.set()

        self.connect_started = asyncio.Event()
        self.write_started = asyncio.Event()
        self.prompt_write_started = asyncio.Event()
        self.close_started = asyncio.Event()

        self.connect_calls = 0
        self.write_calls = 0
        self.close_calls = 0
        self.end_input_calls = 0
        self.connect_cancelled = 0
        self.write_cancelled = 0
        self.close_cancelled = 0
        self.close_completed = 0

        #: ``False`` only once a ``close()`` has run to completion. A cancelled
        #: close leaves it ``True``, modelling ``SubprocessCLITransport.close()``
        #: skipping terminate/kill when cancelled and discarding the still-live
        #: child from ``_ACTIVE_CHILDREN``. Modelled from the SDK source, never
        #: observed against a real process -- see the design's residual risks.
        self.alive = False

        self.trace: list[str] = []
        self.control_requests: list[str | None] = []
        self.user_messages: list[dict[str, Any]] = []
        #: Frames emitted once the prompt has been written, the way a real CLI
        #: only answers a request it has actually received.
        self.replies: list[dict[str, Any]] = []

        self._ready = False
        self._in_close = False

    # -- Transport ------------------------------------------------------
    async def connect(self) -> None:
        self.connect_calls += 1
        self.connect_started.set()
        try:
            await self.connect_gate.wait()
        except asyncio.CancelledError:
            self.connect_cancelled += 1
            raise
        self.alive = True
        self._ready = True
        self.trace.append("connect")

    async def write(self, data: str) -> None:
        self.write_calls += 1
        self.write_started.set()
        frames = [json.loads(line) for line in data.splitlines() if line.strip()]
        is_prompt = any(frame.get("type") != "control_request" for frame in frames)
        if is_prompt:
            self.prompt_write_started.set()
        try:
            await (self.prompt_gate if is_prompt else self.write_gate).wait()
        except asyncio.CancelledError:
            self.write_cancelled += 1
            raise
        for frame in frames:
            self._handle_outbound(frame)

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read_messages()

    async def _read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._queue.get()
            if item is _EOF:
                return
            yield item

    async def close(self) -> None:
        assert not self._in_close, "close() was re-entered while still running"
        self._in_close = True
        self.close_calls += 1
        self.close_started.set()
        attempt = self.close_calls - 1
        failure = self._close_failures[attempt] if attempt < len(self._close_failures) else None
        try:
            await self.close_gate.wait()
            if failure is not None:
                self.trace.append("close_failed")
                raise failure
        except asyncio.CancelledError:
            self.close_cancelled += 1
            raise
        finally:
            self._in_close = False
            self._queue.put_nowait(_EOF)
        self.close_completed += 1
        self.alive = False
        self.trace.append("close_completed")

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        # Deliberately does NOT end the message stream: the SDK closes stdin as
        # soon as the prompt is away (no SDK MCP servers, no hooks), while a
        # real CLI goes on streaming its answer and only closes stdout when it
        # exits. Ending here would make every blocked-read test finish instantly.
        self.end_input_calls += 1

    # -- scripting ------------------------------------------------------
    def emit(self, message: dict[str, Any]) -> None:
        """Queue one raw frame for the SDK's reader.

        A terminal ``result`` frame also ends the stream, because that is when a
        real CLI exits and its stdout closes. Without it the one-shot
        ``query()`` path would never see the iterator finish.
        """
        self._queue.put_nowait(message)
        if message.get("type") == "result":
            self._queue.put_nowait(_EOF)

    def reply_with(self, *frames: dict[str, Any]) -> None:
        """Answer the prompt with ``frames`` once it has actually been written."""
        self.replies.extend(frames)

    def _handle_outbound(self, message: dict[str, Any]) -> None:
        if message.get("type") != "control_request":
            self.user_messages.append(message)
            self.trace.append("user_message")
            for frame in self.replies:
                self.emit(frame)
            self.replies.clear()
            return
        request_id = message["request_id"]
        subtype = message.get("request", {}).get("subtype")
        self.control_requests.append(subtype)
        if subtype == "initialize" and self._fail_initialize:
            self.emit(
                {
                    "type": "control_response",
                    "response": {
                        "request_id": request_id,
                        "subtype": "error",
                        "error": "initialize refused by the fake transport",
                    },
                }
            )
            return
        self.emit(
            {
                "type": "control_response",
                "response": {
                    "request_id": request_id,
                    "subtype": "success",
                    "response": {},
                },
            }
        )


def assistant_frame(text: str = "hello", *, session_id: str = "s-1") -> dict[str, Any]:
    """A well-formed ``assistant`` frame."""
    return {
        "type": "assistant",
        "session_id": session_id,
        "message": {"model": "claude-test", "content": [{"type": "text", "text": text}]},
    }


def malformed_assistant_frame(*, session_id: str = "s-1") -> dict[str, Any]:
    """An ``assistant`` frame ``parse_message`` rejects.

    ``message.model`` is required, so its absence raises ``MessageParseError``
    from inside the SDK -- which is what makes the SDK end the stream itself and,
    on the ``query()`` path, run its whole teardown inside the read the provider
    is awaiting.
    """
    return {
        "type": "assistant",
        "session_id": session_id,
        "message": {"content": [{"type": "text", "text": "partial"}]},
    }


def result_frame(*, result: str = "done", session_id: str = "s-1") -> dict[str, Any]:
    """A well-formed terminal ``result`` frame."""
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": session_id,
        "result": result,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def patch_sdk_entry(transport: FakeTransport) -> Any:
    """Hand ``transport`` to the **real** SDK client the provider uses.

    The only thing replaced is the process boundary, so everything these
    regressions care about -- ``Query``, the control protocol, ``parse_message``
    and the whole teardown chain -- is the SDK's own code.
    """
    real_client = claude_agent_sdk.ClaudeSDKClient

    def _make_client(options: Any = None, **_: Any) -> Any:
        return real_client(options, transport=transport)

    return patch(f"{_PROVIDER}.ClaudeSDKClient", _make_client)


# ---------------------------------------------------------------------------
# Failure-safe cleanup for gated tests
# ---------------------------------------------------------------------------

#: Gate attributes the helpers in this suite expose. Registering an object
#: releases every gate it owns, so a test cannot forget one it did not set.
_GATE_ATTRS = (
    "release",
    "block",
    "allow_teardown_to_finish",
    "connect_gate",
    "write_gate",
    "prompt_gate",
    "close_gate",
)


class GatedScope:
    """Collects the gates and tasks a gated test must release on the way out."""

    def __init__(self) -> None:
        self.gates: list[asyncio.Event] = []
        self.tasks: list[asyncio.Future[Any]] = []

    def watch(self, obj: Any) -> Any:
        """Register every gate ``obj`` owns. Returns ``obj`` for chaining."""
        for name in _GATE_ATTRS:
            gate = getattr(obj, name, None)
            if isinstance(gate, asyncio.Event):
                self.gates.append(gate)
        return obj

    def gate(self, event: asyncio.Event) -> asyncio.Event:
        self.gates.append(event)
        return event

    def track(self, task: asyncio.Future[Any]) -> Any:
        self.tasks.append(task)
        return task

    async def release(self) -> None:
        for gate in self.gates:
            gate.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
            # Shielded: the task's own teardown is deliberately uncancellable,
            # so the guard below bounds *waiting*, never the teardown itself.
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(task), HANG_GUARD_SECONDS)


@contextlib.asynccontextmanager
async def gated_scope(
    before: set[asyncio.Task[Any]] | None = None,
) -> AsyncIterator[GatedScope]:
    """Release every registered gate and drain every owned task on the way out.

    A gated test parks the provider on a teardown the implementation shields on
    purpose, so the usual ``asyncio.wait_for`` hang guards are not hard bounds:
    an assertion that fails before the release line would leave the suite stuck.
    Releasing in a ``finally`` makes the failure report itself.

    The leaked-task check runs only when the body succeeded -- an assertion
    failure is the more useful report, and cancelling on the way out is exactly
    what would make the check lie.
    """
    scope = GatedScope()
    succeeded = False
    try:
        yield scope
        succeeded = True
    finally:
        await scope.release()
    if succeeded and before is not None:
        # Async-generator finalizers scheduled by the SDK's receive chain need
        # one turn to retire before the delta is meaningful.
        await asyncio.sleep(0)
        assert leaked_tasks(before) == []


async def settle(turns: int = 12) -> None:
    """Let the loop run ``turns`` times without advancing the clock.

    Used after firing a signal, so "the implementation did not react" is a real
    observation rather than a race the test happened to win. Deliberately not a
    timed sleep: nothing here may depend on wall-clock duration.
    """
    for _ in range(turns):
        await asyncio.sleep(0)


@contextlib.contextmanager
def trace_mcp_removal(trace: list[str]) -> Iterator[None]:
    """Record ``_remove_mcp_config`` in ``trace`` instead of unlinking."""
    with patch(
        f"{_PROVIDER}._remove_mcp_config",
        side_effect=lambda path: trace.append("remove_mcp_config"),
    ):
        yield
