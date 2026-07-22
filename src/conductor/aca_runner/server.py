"""FastAPI app implementing the `conductor-agent-runner` contract (epic E4).

Wraps a real ``CopilotProvider`` (the only supported ``inner_provider`` for
the MVP, per *DD2* / *Open Questions*) behind the wire contract shared with
the host-side :class:`~conductor.providers.aca.AcaRuntimeProvider`:

- ``GET /health`` — readiness + Conductor/runner version, so
  ``validate_connection()`` can detect host/runner version skew.
- ``POST /execute`` — deserializes an
  :class:`~conductor.providers.aca_protocol.AcaExecuteRequest`, runs the
  inner ``CopilotProvider.execute()``, and streams the result back as
  ``application/x-ndjson``: one ``{"type": ..., "data": ...}`` line per SDK
  event, terminated by a ``result`` (or ``error``) frame.

Not built by this epic (see the plan's Files Affected / task table): a
dedicated ``/interrupt`` endpoint (the host's in-stream interrupt currently
has nothing to land on inside this runner) and a runner-side
``max_session_seconds`` wall-clock guard (the capability is declared "as
runner-enforced" by E3, but no E4 task assigns building the guard itself).
Both are tracked as follow-up gaps rather than implemented here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError as PydanticValidationError

from conductor import __version__ as _conductor_version
from conductor.config.schema import (
    AgentDef,
    OutputField,
    ProviderSettings,
    ReasoningConfig,
    RetryPolicy,
    ToolOutputConfig,
)
from conductor.exceptions import ProviderError
from conductor.providers.aca_protocol import AcaAgentPayload, AcaExecuteRequest, AcaResultData
from conductor.providers.copilot import CopilotProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from conductor.providers.base import AgentOutput

logger = logging.getLogger(__name__)

# Runner package version — reported alongside `conductor_version` on
# `/health` so an operator can distinguish an out-of-date runner image from
# an out-of-date host Conductor install. Bumped independently of
# `conductor-cli` releases (the runner ships as a base image, not a wheel
# release train).
RUNNER_VERSION = "0.1.0"


def _frame(event_type: str, data: dict[str, Any]) -> bytes:
    """Serialize one NDJSON line: ``{"type": ..., "data": ...}\\n``.

    ``default=str`` is a last-resort fallback for any non-JSON-native value
    an SDK event might carry (e.g. a Path) — never raise out of the stream
    over a single malformed event.
    """
    return (json.dumps({"type": event_type, "data": data}, default=str) + "\n").encode("utf-8")


def _build_agent(payload: AcaAgentPayload) -> AgentDef:
    """Reconstruct the minimal `AgentDef` the inner `CopilotProvider` needs.

    Only the fields `AcaAgentPayload` actually carries are set — routing,
    dependency (`input:`), and validator configuration stay host-side (see
    `AcaAgentPayload`'s docstring). `working_dir` is forwarded as-is: it is
    already container-relative (the `sandbox.working_dir` semantics, not
    `agent.working_dir`'s host-path resolution), so no path resolution
    happens here — the container filesystem *is* the working directory.

    Raises:
        pydantic.ValidationError: If the payload carries a value `AgentDef`
            itself rejects (e.g. an invalid `context_tier` literal). Callers
            must validate this *before* opening the response stream so a
            malformed request surfaces as a clean 4xx rather than a broken
            mid-stream frame (review fix).
    """
    output = (
        {name: OutputField.model_validate(field) for name, field in payload.output.items()}
        if payload.output
        else None
    )
    reasoning = (
        ReasoningConfig(effort=payload.reasoning_effort) if payload.reasoning_effort else None
    )
    retry = RetryPolicy.model_validate(payload.retry) if payload.retry else None
    return AgentDef(
        name=payload.name,
        model=payload.model,
        system_prompt=payload.system_prompt,
        output=output,
        max_agent_iterations=payload.max_agent_iterations,
        max_session_seconds=payload.max_session_seconds,
        reasoning=reasoning,
        working_dir=payload.working_dir,
        retry=retry,
        context_tier=payload.context_tier,
    )


def _check_stdio_binaries(mcp_servers: dict[str, Any] | None) -> None:
    """Fail loudly when a declared stdio MCP server's binary is absent (E4-T3).

    Runner-image contract (design *API Contracts* / *Open Questions → MCP*):
    stdio MCP servers must be baked into the image. A declared-but-absent
    binary is a **runtime error** — the same failure mode as a missing
    binary on-host — never a silently dropped tool. Remote (``http``/``sse``)
    servers need no local binary and are skipped.
    """
    if not mcp_servers:
        return
    missing: list[str] = []
    for name, config in mcp_servers.items():
        if not isinstance(config, dict) or config.get("type", "stdio") != "stdio":
            continue
        command = config.get("command")
        if command and shutil.which(command) is None:
            missing.append(f"{name!r} (command={command!r})")
    if missing:
        raise ProviderError(
            "aca runner: declared stdio MCP server binary not found in the runner "
            f"image: {'; '.join(missing)}.",
            suggestion=(
                "Extend the conductor-agent-runner base image (`FROM "
                "conductor-agent-runner:<tag>`) to install the missing binary, or "
                "remove the server from runtime.mcp_servers."
            ),
            provider_name="aca",
            is_retryable=False,
        )


def _validate_execute_request(request: AcaExecuteRequest) -> AgentDef:
    """Pre-flight checks run before the streaming response is opened.

    Anything detectable synchronously (unsupported inner provider, a missing
    stdio binary, an invalid agent payload) is surfaced as a non-2xx JSON
    response — mirroring ``AcaRuntimeProvider._error_from_response`` on the
    host side — rather than as a mid-stream ``error`` frame, since none of
    these failures depend on actually starting the inner SDK call.

    Returns the reconstructed `AgentDef` (review fix) so the caller can reuse
    it in `_stream_execute` instead of re-running (and re-risking a
    mid-stream failure from) `_build_agent` a second time after the response
    has already started streaming.
    """
    if request.inner_provider != "copilot":
        raise ProviderError(
            f"aca runner: unsupported inner_provider {request.inner_provider!r}; "
            "the MVP runner only drives 'copilot'.",
            provider_name="aca",
            is_retryable=False,
        )
    _check_stdio_binaries(request.mcp_servers)
    return _build_agent(request.agent)


def _result_frame_data(output: AgentOutput, session_seconds: float) -> dict[str, Any]:
    """Build the terminal `result` frame payload (E4-T2, incl. `session_seconds`).

    `session_seconds` is a field on `AcaResultData` (added by E6, which parses
    it into `AgentOutput.session_seconds` on the host side).
    """
    payload = AcaResultData(
        content=output.content,
        model=output.model,
        input_tokens=output.input_tokens,
        output_tokens=output.output_tokens,
        cache_read_tokens=output.cache_read_tokens,
        cache_write_tokens=output.cache_write_tokens,
        partial=output.partial,
        session_seconds=session_seconds,
    ).model_dump(mode="json")
    return payload


class _InnerProviderCache:
    """Constructs/reuses the inner `CopilotProvider` across `/execute` calls.

    Reconstructing a `CopilotProvider` on every call would spawn a fresh
    nested `copilot` process per request. `mcp_servers` / `inner_provider_settings`
    / `tool_output` are only settable at construction time (unlike per-agent
    `tools:`, which `execute()` takes per-call), so this caches by those three
    fields and only rebuilds the provider when one of them actually changes
    between requests — closing the stale instance first.

    `get()` is guarded by an `asyncio.Lock` (review fix): concurrent
    `/execute` requests that land while the cached settings are changing
    would otherwise race on the read-check-close-rebuild sequence below —
    each concurrent caller sees the same stale `self._provider`/`self._key`,
    so more than one would call `close()` on the same instance (a double
    close) and/or construct a provider that never gets tracked (and thus
    never closed). The lock serializes the whole check-and-maybe-rebuild
    critical section so only one coroutine at a time can decide whether a
    rebuild is needed and perform it.
    """

    def __init__(self) -> None:
        self._provider: CopilotProvider | None = None
        self._key: str | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _key_for(
        mcp_servers: dict[str, Any] | None,
        inner_provider_settings: dict[str, Any] | None,
        tool_output: dict[str, Any] | None,
    ) -> str:
        return json.dumps(
            {
                "mcp_servers": mcp_servers,
                "inner_provider_settings": inner_provider_settings,
                "tool_output": tool_output,
            },
            sort_keys=True,
            default=str,
        )

    async def get(
        self,
        *,
        mcp_servers: dict[str, Any] | None,
        inner_provider_settings: dict[str, Any] | None,
        tool_output: dict[str, Any] | None,
    ) -> CopilotProvider:
        key = self._key_for(mcp_servers, inner_provider_settings, tool_output)
        async with self._lock:
            if self._provider is not None and key == self._key:
                return self._provider
            if self._provider is not None:
                await self._provider.close()

            # OQ#6 Phase 1 credential stopgap: `inner_provider_settings` carries
            # the plaintext bearer_token/api_key/base_url forwarded by the host's
            # `AcaRuntimeProvider._resolve_inner_provider_settings` (reusing the
            # existing Copilot custom-routing `bearer_token` path). Phase 2 (E8)
            # replaces this whole branch with a call to the credential gateway —
            # this is the seam that change would touch.
            provider_settings = (
                ProviderSettings(name="copilot", **inner_provider_settings)
                if inner_provider_settings
                else None
            )
            tool_output_config = ToolOutputConfig(**tool_output) if tool_output else None
            self._provider = CopilotProvider(
                mcp_servers=mcp_servers,
                provider_settings=provider_settings,
                tool_output=tool_output_config,
            )
            self._key = key
            return self._provider

    async def close(self) -> None:
        async with self._lock:
            if self._provider is not None:
                await self._provider.close()
                self._provider = None
                self._key = None


async def _stream_execute(
    provider: CopilotProvider, agent: AgentDef, request: AcaExecuteRequest
) -> AsyncIterator[bytes]:
    """Run the inner `execute()` call, yielding NDJSON frames as they arrive.

    `agent` is pre-built (and thus pre-validated) by the caller — see
    `_validate_execute_request` — so the only way this generator can fail is
    inside the inner SDK call itself, which is always caught and turned into
    a terminal ``error`` frame rather than propagating out of the stream.

    Event frames from `event_callback` and the terminal frame share one
    `asyncio.Queue` (FIFO, single event loop — no thread-safety concerns) so
    they are yielded in the exact order the inner provider produced them,
    ending in exactly one terminal ``result`` or ``error`` frame.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    sentinel = object()

    def emit(event_type: str, data: dict[str, Any]) -> None:
        queue.put_nowait(_frame(event_type, data))

    async def run() -> None:
        start = time.monotonic()
        try:
            output = await provider.execute(
                agent,
                request.context,
                request.rendered_prompt,
                request.tools,
                event_callback=emit,
            )
        except Exception as exc:  # broad: forwarded as an error frame, never swallowed
            logger.exception("aca runner: execute failed for agent %r", agent.name)
            await queue.put(_frame("error", {"message": str(exc)}))
        else:
            session_seconds = time.monotonic() - start
            await queue.put(_frame("result", _result_frame_data(output, session_seconds)))
        finally:
            await queue.put(sentinel)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(Exception):
            await task


def create_app() -> FastAPI:
    """Build the runner's FastAPI app.

    A factory (rather than a module-level singleton) so tests can construct
    a fresh app per test with `CopilotProvider` monkeypatched beforehand.
    """
    provider_cache = _InnerProviderCache()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await provider_cache.close()

    app = FastAPI(
        title="conductor-agent-runner",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health(
        identifier: str | None = None,
        api_version: str | None = Query(default=None, alias="api-version"),
    ) -> dict[str, Any]:
        """Readiness + version probe (E4-T1) for `validate_connection` skew checks."""
        return {
            "ready": True,
            "conductor_version": _conductor_version,
            "runner_version": RUNNER_VERSION,
        }

    @app.post("/execute")
    async def execute_endpoint(
        request: AcaExecuteRequest,
        identifier: str | None = None,
        api_version: str | None = Query(default=None, alias="api-version"),
    ) -> Response:
        """Run one agent turn, streaming NDJSON event frames (E4-T2/T3/T4).

        Review fix: agent reconstruction (`_build_agent`, via
        `_validate_execute_request`) and the provider-cache lookup both run
        *before* `StreamingResponse` is constructed, so a malformed agent
        payload (e.g. an invalid `context_tier` literal) or an unavailable
        inner provider surfaces as a clean 400 JSON body — the HTTP status
        line and headers are not sent until this block returns successfully,
        so nothing here can corrupt an already-started NDJSON stream.
        """
        try:
            agent = _validate_execute_request(request)
            provider = await provider_cache.get(
                mcp_servers=request.mcp_servers,
                inner_provider_settings=request.inner_provider_settings,
                tool_output=request.tool_output,
            )
        except (ProviderError, PydanticValidationError) as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

        return StreamingResponse(
            _stream_execute(provider, agent, request), media_type="application/x-ndjson"
        )

    return app
