"""FastAPI app implementing the `conductor-agent-runner` remote runtime.

`conductor-agent-runner` (built from ``docker/aca-runner/Dockerfile``) is the
reference remote agent runtime. It hosts the selected model provider behind
the wire contract shared with the host-side
:class:`~conductor.providers.aca.AcaRuntimeProvider` and defined in
:mod:`conductor.runner.protocol`:

- ``GET /health`` — readiness + Conductor/runner/protocol version, so
  ``validate_connection()`` can detect host/runner version skew.
- ``POST /execute`` — deserializes a
  :class:`~conductor.runner.protocol.RunnerAgentRequest`, runs the inner
  ``AgentProvider.execute()``, and streams the result back as
  ``application/x-ndjson``: one ``{"type": ..., "data": ...}`` line per SDK
  event, terminated by a ``result`` (or ``error``) frame.

- ``POST /interrupt`` — signals a named in-flight agent invocation; its
  partial result uses the existing terminal result frame.

The runner-side ``max_session_seconds`` wall-clock guard remains a follow-up.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic import ValidationError as PydanticValidationError

from conductor import __version__ as _conductor_version
from conductor.aca_runner.auth import (
    RUNNER_TOKEN_HEADER,
    check_inner_provider_settings,
    resolve_allowed_base_urls,
    resolve_runner_token,
    token_gate,
)
from conductor.config.schema import (
    AgentDef,
    OutputField,
    ProviderSettings,
    ReasoningConfig,
    RetryPolicy,
    ToolOutputConfig,
)
from conductor.exceptions import ProviderError
from conductor.providers.base import AgentProvider
from conductor.providers.claude import ClaudeProvider
from conductor.providers.copilot import CopilotProvider
from conductor.providers.openai import OpenAIProvider
from conductor.redaction import RunRedactor
from conductor.runner.protocol import (
    RUNNER_PROTOCOL_VERSION,
    RunnerAgentPayload,
    RunnerAgentRequest,
    RunnerAgentResult,
)

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


class _InterruptRequest(BaseModel):
    execution_id: str = Field(min_length=1)


def _frame(event_type: str, data: dict[str, Any]) -> bytes:
    """Serialize one NDJSON line: ``{"type": ..., "data": ...}\\n``.

    ``default=str`` is a last-resort fallback for any non-JSON-native value
    an SDK event might carry (e.g. a Path) — never raise out of the stream
    over a single malformed event.
    """
    return (json.dumps({"type": event_type, "data": data}, default=str) + "\n").encode("utf-8")


def _build_agent(payload: RunnerAgentPayload) -> AgentDef:
    """Reconstruct the minimal `AgentDef` the inner `CopilotProvider` needs.

    Only the fields `RunnerAgentPayload` actually carries are set — routing,
    dependency (`input:`), and validator configuration stay host-side (see
    `RunnerAgentPayload`'s docstring). `working_dir` is forwarded as-is: it is
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
        timeout_seconds=None,
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
            "runner: declared stdio MCP server binary not found in the runner "
            f"image: {'; '.join(missing)}.",
            suggestion=(
                "Extend the conductor-agent-runner base image (`FROM "
                "conductor-agent-runner:<tag>`) to install the missing binary, or "
                "remove the server from runtime.mcp_servers."
            ),
            provider_name="aca",
            is_retryable=False,
        )


def _validate_execute_request(
    request: RunnerAgentRequest, *, allowed_base_urls: tuple[str, ...] | None
) -> AgentDef:
    """Pre-flight checks run before the streaming response is opened.

    Anything detectable synchronously (unsupported inner provider, a missing
    stdio binary, an invalid agent payload, an out-of-allowlist
    `inner_provider_settings` key or `base_url` — issue #396) is surfaced as
    a non-2xx JSON response — mirroring ``AcaRuntimeProvider._error_from_response``
    on the host side — rather than as a mid-stream ``error`` frame, since
    none of these failures depend on actually starting the inner SDK call.

    Returns the reconstructed `AgentDef` (review fix) so the caller can reuse
    it in `_stream_execute` instead of re-running (and re-risking a
    mid-stream failure from) `_build_agent` a second time after the response
    has already started streaming.
    """
    if request.inner_provider not in ("copilot", "openai", "claude"):
        raise ProviderError(
            f"runner: unsupported inner_provider {request.inner_provider!r}; "
            "supported providers: copilot, openai, claude. "
            "Other providers require a follow-up implementation.",
            provider_name="aca",
            is_retryable=False,
        )
    _check_stdio_binaries(request.mcp_servers)
    check_inner_provider_settings(
        request.inner_provider_settings, allowed_base_urls=allowed_base_urls
    )
    return _build_agent(request.agent)


def _mcp_servers_for_request(request: RunnerAgentRequest) -> dict[str, Any] | None:
    """Copy stdio configurations and add only this call's MCP spawn environment."""
    servers = request.mcp_servers
    overlay = request.env_overlay
    if not overlay:
        return servers
    if not servers or not any(
        isinstance(config, dict) and config.get("type", "stdio") == "stdio"
        for config in servers.values()
    ):
        raise ProviderError(
            "runner: env_overlay requires at least one stdio MCP server in this request",
            provider_name="aca",
            is_retryable=False,
        )
    values = {
        name: value.get_secret_value() if isinstance(value, SecretStr) else value
        for name, value in overlay.items()
    }
    return {
        name: {
            **config,
            "env": {**(config.get("env") or {}), **values},
        }
        if isinstance(config, dict) and config.get("type", "stdio") == "stdio"
        else config
        for name, config in servers.items()
    }


def _result_frame_data(output: AgentOutput, session_seconds: float) -> dict[str, Any]:
    """Build the terminal `result` frame payload (E4-T2, incl. `session_seconds`).

    `session_seconds` is a field on `RunnerAgentResult` (added by E6, which
    parses it into `AgentOutput.session_seconds` on the host side).
    """
    payload = RunnerAgentResult(
        content=output.content,
        model=output.model,
        input_tokens=output.input_tokens,
        output_tokens=output.output_tokens,
        cache_read_tokens=output.cache_read_tokens,
        cache_write_tokens=output.cache_write_tokens,
        last_call_input_tokens=output.last_call_input_tokens,
        partial=output.partial,
        session_seconds=session_seconds,
    ).model_dump(mode="json")
    return payload


@dataclass
class _ProviderEntry:
    provider: AgentProvider
    active: int = 0


class _InnerProviderCache:
    """Keep provider instances by complete config until idle eviction.

    Concurrent requests with different overlays cannot share an instance,
    nor can changing settings close a provider with an active execution.
    """

    _MAX_IDLE_ENTRIES = 16

    def __init__(self) -> None:
        self._entries: OrderedDict[str, _ProviderEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._idle = asyncio.Condition(self._lock)
        self._closing = False

    @staticmethod
    def _key_for(
        mcp_servers: dict[str, Any] | None,
        inner_provider_settings: dict[str, Any] | None,
        tool_output: dict[str, Any] | None,
        *,
        inner_provider: str = "copilot",
        env_overlay: dict[str, str] | None = None,
    ) -> str:
        def unwrap(value: Any) -> Any:
            if isinstance(value, SecretStr):
                return value.get_secret_value()
            if isinstance(value, dict):
                return {key: unwrap(item) for key, item in value.items()}
            return value

        canonical = json.dumps(
            {
                "inner_provider": inner_provider,
                "mcp_servers": unwrap(mcp_servers),
                "inner_provider_settings": unwrap(inner_provider_settings),
                "tool_output": tool_output,
                "env_overlay": unwrap(env_overlay),
            },
            sort_keys=True,
            default=str,
        )
        # Retain only a digest of credentials and MCP spawn environment.
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _construct_provider(
        inner_provider: str,
        mcp_servers: dict[str, Any] | None,
        inner_provider_settings: dict[str, Any] | None,
        tool_output: dict[str, Any] | None,
    ) -> AgentProvider:
        settings = dict(inner_provider_settings or {})
        github_token_value = settings.pop("github_token", None)
        if github_token_value is not None and inner_provider != "copilot":
            raise ProviderError(
                "runner: github_token is only supported by the copilot inner provider",
                provider_name="aca",
                is_retryable=False,
            )
        github_token = (
            github_token_value.get_secret_value()
            if isinstance(github_token_value, SecretStr)
            else github_token_value
        )
        provider_settings = (
            ProviderSettings.model_validate({"name": inner_provider, **settings})
            if settings
            else None
        )
        tool_config = ToolOutputConfig(**tool_output) if tool_output else None
        api_key = settings.get("api_key")
        api_key_text = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        factories = {
            "copilot": lambda: CopilotProvider(
                mcp_servers=mcp_servers,
                provider_settings=provider_settings,
                tool_output=tool_config,
                github_token=github_token,
            ),
            "openai": lambda: OpenAIProvider(
                api_key=api_key_text,
                base_url=settings.get("base_url"),
                mcp_servers=mcp_servers,
                tool_output=tool_config,
            ),
            "claude": lambda: ClaudeProvider(
                api_key=api_key_text,
                base_url=settings.get("base_url"),
                mcp_servers=mcp_servers,
                tool_output=tool_config,
            ),
        }
        return factories[inner_provider]()

    async def get(
        self,
        *,
        mcp_servers: dict[str, Any] | None,
        inner_provider_settings: dict[str, Any] | None,
        tool_output: dict[str, Any] | None,
        inner_provider: str = "copilot",
        env_overlay: dict[str, str] | None = None,
        reserve: bool = False,
    ) -> AgentProvider:
        key = self._key_for(
            mcp_servers,
            inner_provider_settings,
            tool_output,
            inner_provider=inner_provider,
            env_overlay=env_overlay,
        )
        async with self._lock:
            if self._closing:
                raise ProviderError("runner: provider cache is shutting down", provider_name="aca")
            entry = self._entries.get(key)
            if entry is None:
                entry = _ProviderEntry(
                    self._construct_provider(
                        inner_provider, mcp_servers, inner_provider_settings, tool_output
                    )
                )
                self._entries[key] = entry
            self._entries.move_to_end(key)
            if reserve:
                entry.active += 1
            await self._evict_idle(protected_key=key)
            return entry.provider

    async def _evict_idle(self, *, protected_key: str | None = None) -> None:
        while len(self._entries) > self._MAX_IDLE_ENTRIES:
            idle_key = next(
                (
                    key
                    for key, entry in self._entries.items()
                    if not entry.active and key != protected_key
                ),
                None,
            )
            if idle_key is None:
                return
            entry = self._entries.pop(idle_key)
            await entry.provider.close()

    async def release(self, provider: AgentProvider) -> None:
        async with self._lock:
            for entry in self._entries.values():
                if entry.provider is provider:
                    entry.active -= 1
                    break
            await self._evict_idle()
            self._idle.notify_all()

    async def close(self) -> None:
        async with self._idle:
            self._closing = True
            await self._idle.wait_for(
                lambda: all(entry.active == 0 for entry in self._entries.values())
            )
            for entry in self._entries.values():
                await entry.provider.close()
            self._entries.clear()


async def _stream_execute(
    provider: AgentProvider,
    agent: AgentDef,
    payload: RunnerAgentRequest,
    provider_cache: _InnerProviderCache,
    interrupt_signal: asyncio.Event | None,
    mark_terminal: Callable[[], None] | None = None,
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
    redactor = RunRedactor()
    redactor.register(
        value.get_secret_value() if isinstance(value, SecretStr) else value
        for value in (payload.env_overlay or {}).values()
    )
    redactor.register(
        value.get_secret_value()
        for value in (payload.inner_provider_settings or {}).values()
        if isinstance(value, SecretStr)
    )
    for server in (payload.mcp_servers or {}).values():
        if not isinstance(server, dict):
            continue
        environment = server.get("env")
        if isinstance(environment, dict):
            redactor.register(
                value.get_secret_value() if isinstance(value, SecretStr) else value
                for value in environment.values()
            )
        headers = server.get("headers")
        if server.get("type") in ("http", "sse") and isinstance(headers, dict):
            redactor.register(
                value.get_secret_value() if isinstance(value, SecretStr) else value
                for name, value in headers.items()
                if isinstance(name, str) and name.lower() == "authorization"
            )

    def emit(event_type: str, data: dict[str, Any]) -> None:
        queue.put_nowait(_frame(event_type, redactor.scrub(data)))

    async def run() -> None:
        start = time.monotonic()
        try:
            output = await provider.execute(
                agent,
                payload.context,
                payload.rendered_prompt,
                tools=payload.tools,
                event_callback=emit,
                interrupt_signal=interrupt_signal,
                skill_directories=payload.skill_directories,
                custom_agents=payload.custom_agents,
                extra_mcp_servers=None,
                suppress_mcp_servers=payload.mcp_servers is None,
            )
        except Exception as exc:  # broad: forwarded as an error frame, never swallowed
            logger.error("runner: execute failed for agent %r (%s)", agent.name, type(exc).__name__)
            frame = _frame("error", {"message": redactor.scrub(str(exc))})
            if mark_terminal is not None:
                mark_terminal()
            await queue.put(frame)
        else:
            session_seconds = time.monotonic() - start
            frame = _frame("result", redactor.scrub(_result_frame_data(output, session_seconds)))
            if mark_terminal is not None:
                mark_terminal()
            await queue.put(frame)
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
        try:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        finally:
            await asyncio.shield(provider_cache.release(provider))


def create_app() -> FastAPI:
    """Build the runner's FastAPI app.

    A factory (rather than a module-level singleton) so tests can construct
    a fresh app per test with `CopilotProvider` monkeypatched beforehand.

    The transport-token gate and `base_url` allowlist (issue #396) are
    resolved **once at startup**, closing over them for the lifetime of the
    app — tests that need a different value set/clear the env var via
    `monkeypatch` before calling `create_app()`.
    """
    provider_cache = _InnerProviderCache()
    active_executions: dict[str, asyncio.Event] = {}
    terminal_executions: set[str] = set()
    runner_token = resolve_runner_token()
    allowed_base_urls = resolve_allowed_base_urls()

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
        http_request: Request,
        identifier: str | None = None,
        api_version: str | None = Query(default=None, alias="api-version"),
    ) -> dict[str, Any]:
        """Readiness + version probe (E4-T1) for `validate_connection` skew checks.

        Deliberately unauthenticated (issue #396): the image's own
        `HEALTHCHECK` (`curl -fsS http://localhost:$ACA_RUNNER_PORT/health`)
        sends no header, so gating this endpoint would break it. Reports
        `auth_required` (whether the transport-token gate is configured) and
        `auth_token_present` (whether *a* token header arrived on **this**
        request — never whether it matched) so `validate_connection()` can
        warn when the two postures disagree (e.g. a gateway silently
        stripping the header). `identifier` is gateway routing metadata
        only — ACA routes by it, auto-allocating a session if none exists —
        and is never treated as a caller-authentication signal; the
        transport-token gate on `/execute` is the actual runner-side
        control.

        Realm adapters enable interrupt only with protocol v2 and the
        advertised interrupt feature. A v1/missing-feature handshake means
        interrupt=False: interrupt-requiring configurations must be refused
        before dispatch. Legacy ACA retains abort-read and its existing
        version-skew warning. The consuming adapters implement that policy.
        """
        return {
            "ready": True,
            "conductor_version": _conductor_version,
            "runner_version": RUNNER_VERSION,
            "protocol_version": RUNNER_PROTOCOL_VERSION,
            "features": ["interrupt"],
            "auth_required": runner_token is not None,
            "auth_token_present": http_request.headers.get(RUNNER_TOKEN_HEADER) is not None,
        }

    @app.post("/execute")
    async def execute_endpoint(
        payload: RunnerAgentRequest,
        http_request: Request,
        identifier: str | None = None,
        api_version: str | None = Query(default=None, alias="api-version"),
    ) -> Response:
        """Run one agent turn, streaming NDJSON event frames (E4-T2/T3/T4).

        Gated by the optional transport-token check (issue #396) before any
        application-level work: the header is checked first thing in the
        handler, so a missing/incorrect `X-Conductor-Runner-Token` header
        (when `ACA_RUNNER_AUTH_TOKEN` is configured) returns a plain 401 in
        the same `{"error": {"message": ...}}` envelope
        `AcaRuntimeProvider._error_from_response` already parses, and never
        runs `_validate_execute_request` or constructs the inner Copilot
        provider. Note FastAPI validates the `RunnerAgentRequest` body
        parameter *before* this handler runs, so a malformed body from an
        unauthenticated caller still returns FastAPI's own 422 rather than a
        401 — the gate protects execution, not the parser. `identifier`
        remains gateway routing metadata only, exactly as on `/health` —
        never inspected as an authentication signal.

        Review fix: agent reconstruction (`_build_agent`, via
        `_validate_execute_request`) and the provider-cache lookup both run
        *before* `StreamingResponse` is constructed, so a malformed agent
        payload (e.g. an invalid `context_tier` literal) or an unavailable
        inner provider surfaces as a clean 400 JSON body — the HTTP status
        line and headers are not sent until this block returns successfully,
        so nothing here can corrupt an already-started NDJSON stream.
        """
        presented = http_request.headers.get(RUNNER_TOKEN_HEADER)
        if not token_gate(presented, runner_token):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "runner: missing or invalid runner auth token"}},
            )

        try:
            agent = _validate_execute_request(payload, allowed_base_urls=allowed_base_urls)
            mcp_servers = _mcp_servers_for_request(payload)
            provider = await provider_cache.get(
                mcp_servers=mcp_servers,
                inner_provider_settings=payload.inner_provider_settings,
                tool_output=payload.tool_output,
                inner_provider=payload.inner_provider,
                env_overlay=payload.env_overlay,
                reserve=True,
            )
        except (ProviderError, PydanticValidationError) as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

        # Legacy ACA identifies an invocation by its gateway session identifier.
        execution_id = payload.execution_id or identifier
        if payload.execution_id is None and identifier is not None:
            # An ACA gateway identifier names a reusable session, not a
            # permanently unique invocation; completed sessions may run again.
            terminal_executions.discard(identifier)
        if execution_id is not None and (
            execution_id in active_executions or execution_id in terminal_executions
        ):
            await provider_cache.release(provider)
            return JSONResponse(
                status_code=409,
                content={"error": {"message": "runner: execution_id already used"}},
            )
        interrupt_signal = asyncio.Event() if execution_id is not None else None
        if execution_id is not None and interrupt_signal is not None:
            active_executions[execution_id] = interrupt_signal

        async def stream() -> AsyncIterator[bytes]:
            def mark_terminal() -> None:
                if execution_id is not None:
                    terminal_executions.add(execution_id)

            try:
                async for frame in _stream_execute(
                    provider, agent, payload, provider_cache, interrupt_signal, mark_terminal
                ):
                    yield frame
            finally:
                if execution_id is not None:
                    _ = active_executions.pop(execution_id, None)
                    terminal_executions.add(execution_id)

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
        )

    @app.post("/interrupt")
    async def interrupt_endpoint(
        http_request: Request,
        payload: _InterruptRequest | None = None,
        identifier: str | None = None,
        api_version: str | None = Query(default=None, alias="api-version"),
    ) -> Response:
        """Signal one agent without cancelling its streaming response.

        New callers supply execution_id in JSON. The legacy ACA gateway
        instead routes an empty POST by identifier to its active session.
        """
        presented = http_request.headers.get(RUNNER_TOKEN_HEADER)
        if not token_gate(presented, runner_token):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "runner: missing or invalid runner auth token"}},
            )
        execution_id = payload.execution_id if payload is not None else identifier
        if execution_id is None:
            return JSONResponse(
                status_code=422,
                content={"error": {"message": "runner: execution_id is required"}},
            )
        signal = active_executions.get(execution_id)
        if signal is None and execution_id not in terminal_executions:
            return JSONResponse(
                status_code=404,
                content={"error": {"message": "runner: execution_id not found"}},
            )
        if execution_id in terminal_executions or signal is None or signal.is_set():
            return JSONResponse(
                status_code=409,
                content={"error": {"message": "runner: execution already interrupted or terminal"}},
            )
        signal.set()
        return Response(status_code=200)

    return app
