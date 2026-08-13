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
import hashlib
import json
import logging
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import SecretStr
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


def _validate_execute_request(
    request: AcaExecuteRequest, *, allowed_base_urls: tuple[str, ...] | None
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
    if request.inner_provider != "copilot":
        raise ProviderError(
            f"aca runner: unsupported inner_provider {request.inner_provider!r}; "
            "the MVP runner only drives 'copilot'.",
            provider_name="aca",
            is_retryable=False,
        )
    _check_stdio_binaries(request.mcp_servers)
    check_inner_provider_settings(
        request.inner_provider_settings, allowed_base_urls=allowed_base_urls
    )
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
        last_call_input_tokens=output.last_call_input_tokens,
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
        # Epic E8 (aca_protocol.py's `_redact_inner_provider_secrets`) wraps
        # known credential keys in `SecretStr` on every `AcaExecuteRequest`
        # construction path, including this model's own FastAPI request
        # parsing — so `inner_provider_settings` may now hold `SecretStr`
        # values here. `json.dumps(..., default=str)` would otherwise call
        # `str()` on them, which is the same masked "**********" for every
        # distinct credential — collapsing different requests' distinct
        # credentials onto the same cache key and wrongly reusing a stale
        # provider built with a *different* credential. Unwrap to the real
        # secret value first so the key still reflects the actual credential.
        unwrapped_settings = (
            {
                key: value.get_secret_value() if isinstance(value, SecretStr) else value
                for key, value in inner_provider_settings.items()
            }
            if inner_provider_settings is not None
            else None
        )
        canonical = json.dumps(
            {
                "mcp_servers": mcp_servers,
                "inner_provider_settings": unwrapped_settings,
                "tool_output": tool_output,
            },
            sort_keys=True,
            default=str,
        )
        # Review fix: `self._key` (below) is a long-lived instance attribute,
        # not a local that goes out of scope after this call — retaining the
        # canonical JSON verbatim would keep the plaintext credential resident
        # in process memory for the cache's whole lifetime (readable by
        # anything with introspection access to this object, e.g. a debugger
        # or a future logging call that reprs the cache). Hash it instead so
        # only a one-way digest is ever stored; distinct credentials still
        # produce distinct keys (cache correctness is preserved) but neither
        # plaintext value is recoverable from `self._key`.
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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

            # `inner_provider_settings` carries the credential forwarded by the
            # host's `AcaRuntimeProvider._resolve_inner_provider_settings`
            # (epic E8, DD4): either BYOK `base_url`/`api_key`/`bearer_token`
            # (the existing Copilot custom-routing fields, unchanged), or a
            # `github_token` for Copilot-capacity auth. `github_token` isn't a
            # `ProviderSettings` field (it authenticates the SDK client itself,
            # not per-session model routing), so it's popped out here and
            # forwarded to `CopilotProvider`'s own `github_token` param (E9),
            # which in turn passes it in memory on each `create_session` /
            # `resume_session` call (see `CopilotProvider._apply_github_token`)
            # rather than at `CopilotClient` construction. The remaining dict
            # (if any) still builds the BYOK `ProviderSettings`, unchanged.
            remaining_settings = dict(inner_provider_settings) if inner_provider_settings else {}
            github_token_value = remaining_settings.pop("github_token", None)
            github_token = (
                github_token_value.get_secret_value()
                if isinstance(github_token_value, SecretStr)
                else github_token_value
            )
            provider_settings = (
                ProviderSettings(name="copilot", **remaining_settings)
                if remaining_settings
                else None
            )
            tool_output_config = ToolOutputConfig(**tool_output) if tool_output else None
            self._provider = CopilotProvider(
                mcp_servers=mcp_servers,
                provider_settings=provider_settings,
                tool_output=tool_output_config,
                github_token=github_token,
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
    provider: CopilotProvider, agent: AgentDef, payload: AcaExecuteRequest
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
                payload.context,
                payload.rendered_prompt,
                payload.tools,
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

    The transport-token gate and `base_url` allowlist (issue #396) are
    resolved **once at startup**, closing over them for the lifetime of the
    app — tests that need a different value set/clear the env var via
    `monkeypatch` before calling `create_app()`.
    """
    provider_cache = _InnerProviderCache()
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
        """
        return {
            "ready": True,
            "conductor_version": _conductor_version,
            "runner_version": RUNNER_VERSION,
            "auth_required": runner_token is not None,
            "auth_token_present": http_request.headers.get(RUNNER_TOKEN_HEADER) is not None,
        }

    @app.post("/execute")
    async def execute_endpoint(
        payload: AcaExecuteRequest,
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
        provider. Note FastAPI validates the `AcaExecuteRequest` body
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
                content={"error": {"message": "aca runner: missing or invalid runner auth token"}},
            )

        try:
            agent = _validate_execute_request(payload, allowed_base_urls=allowed_base_urls)
            provider = await provider_cache.get(
                mcp_servers=payload.mcp_servers,
                inner_provider_settings=payload.inner_provider_settings,
                tool_output=payload.tool_output,
            )
        except (ProviderError, PydanticValidationError) as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})

        return StreamingResponse(
            _stream_execute(provider, agent, payload), media_type="application/x-ndjson"
        )

    return app
