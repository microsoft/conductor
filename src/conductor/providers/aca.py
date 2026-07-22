"""Azure Container Apps (ACA) dynamic-sessions runtime provider.

This module provides ``AcaRuntimeProvider`` — a thin host-side transport
shim implementing :class:`~conductor.providers.base.AgentProvider` that
delegates agent execution to an in-sandbox ``conductor-agent-runner``
process running inside an Azure Container Apps dynamic-sessions pool.

The library is an optional dependency — install with:
    pip install 'conductor-cli[aca]'
(pins ``azure-identity``, used to acquire a ``dynamicsessions.io`` bearer
token via ``DefaultAzureCredential``.)

The transport shim (epic E3, issue #284) derives a session ``identifier``
from ``identifier_scope`` (DD5), acquires a cached AAD bearer token, issues a
single streaming ``POST {pool_endpoint}/execute`` request (Branch S, DD3),
relays the runner's NDJSON event frames verbatim to ``event_callback``, and
parses the terminal ``result`` frame into :class:`AgentOutput`. The
in-sandbox ``conductor-agent-runner`` itself (epic E4) is not yet built —
this module implements only the host side of the contract defined in
:mod:`conductor.providers.aca_protocol`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

import httpx

from conductor.exceptions import ProviderError
from conductor.providers.aca_protocol import (
    AcaAgentPayload,
    AcaErrorData,
    AcaExecuteRequest,
    AcaResultData,
)
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.capabilities import ProviderCapabilities
from conductor.providers.reasoning import ReasoningEffort, resolve_reasoning_effort

if TYPE_CHECKING:
    from conductor.config.schema import AgentDef, ProviderSettings, ToolOutputConfig

logger = logging.getLogger(__name__)

# azure-identity ships DefaultAzureCredential, used to acquire a
# dynamicsessions.io bearer token for the Session Executor role (FR6). It is
# gated behind the `aca` extra (pyproject.toml) rather than a base
# dependency, mirroring the anthropic/hermes-agent/claude-agent-sdk optional
# SDK guards elsewhere in this package. The sync `DefaultAzureCredential` is
# kept as the availability-check import (existing E2 tests patch
# `AZURE_IDENTITY_AVAILABLE` directly); the async variant
# (`azure.identity.aio`) is what the provider actually calls at runtime, since
# blocking the event loop on a sync `get_token()` inside an async provider
# would stall every other concurrent agent.
try:
    from azure.identity import DefaultAzureCredential  # ty: ignore[unresolved-import]
    from azure.identity.aio import (  # ty: ignore[unresolved-import]
        DefaultAzureCredential as _AsyncDefaultAzureCredential,
    )

    AZURE_IDENTITY_AVAILABLE = True
except ImportError:
    AZURE_IDENTITY_AVAILABLE = False
    DefaultAzureCredential: Any = None
    _AsyncDefaultAzureCredential: Any = None


# Audience for the Session Executor role (FR6, Security Considerations).
_DYNAMICSESSIONS_SCOPE = "https://dynamicsessions.io/.default"

# Fallback management-API version when the workflow YAML doesn't pin one.
_DEFAULT_API_VERSION = "2025-07-01"

# ACA session identifiers must fit this bound (Data Flow: "truncated to
# ≤128 chars with a hash suffix").
_MAX_IDENTIFIER_LENGTH = 128

# Charset-normalization: anything outside lowercase-alnum-hyphen collapses to
# a single hyphen (Data Flow: "charset-normalized").
_IDENTIFIER_INVALID_RE = re.compile(r"[^a-z0-9-]+")

# Refresh the cached AAD token this many seconds before its reported expiry,
# so a request never starts with a token that expires mid-flight.
_TOKEN_REFRESH_MARGIN_SECONDS = 60.0


class AcaRuntimeProvider(AgentProvider):
    """Host-side transport shim for the ``aca`` (Azure Container Apps) provider.

    Owns no agentic logic itself: it derives a session ``identifier``,
    authenticates against the dynamic-sessions pool, and relays the
    in-sandbox runner's NDJSON event stream verbatim to ``event_callback``,
    parsing the terminal ``result`` frame into :class:`AgentOutput`.

    Requires the ``azure-identity`` package:
        pip install 'conductor-cli[aca]'

    Example:
        >>> provider = AcaRuntimeProvider(provider_settings=settings)
        >>> await provider.close()
    """

    CAPABILITIES = ProviderCapabilities(
        tier="experimental",
        # Full `runtime.mcp_servers` is forwarded to the runner, which wraps
        # a real `CopilotProvider` in-container (runner-image contract).
        mcp_tools=True,
        # Per-agent `tools:` allowlist is forwarded and enforced by the
        # inner SDK running inside the sandbox.
        workflow_tools_passthrough=True,
        # Branch S (single streaming request, #312) relays event frames
        # incrementally as the runner emits them.
        streaming_events=True,
        # The runner forwards reasoning frames from the inner provider.
        agent_reasoning_events=True,
        # The inner provider (Copilot) translates reasoning effort natively.
        reasoning_effort=("low", "medium", "high", "xhigh", "max"),
        # Inherits the real CopilotProvider's prompt-injection schema
        # enforcement — Copilot has no native JSON mode.
        structured_output="prompt_injection",
        # Real interrupt via the in-flight stream (Branch S); a best-effort
        # local hard-abort remains available as a fallback.
        interrupt=True,
        # Enforced by a runner-side wall-clock guard (the default `Timed`
        # lifecycle does not honor the pool's `maxAlivePeriodInSeconds`).
        max_session_seconds=True,
        # Sessions are ephemeral with no volume mount; `conductor resume`
        # re-runs the agent rather than restoring in-sandbox state.
        checkpoint_resume=False,
        # The runner returns token counts on the terminal result frame.
        usage_tracking=True,
        # Honest via the mandatory concurrency discriminator in the
        # identifier derivation (DD5).
        concurrent_safe=True,
        # Interpreted container-relative: a path inside the session
        # filesystem, never resolved against the host workflow directory.
        working_dir=True,
        upstream_pin="azure-identity>=1.19.0",
        maintainer=None,
    )

    def __init__(
        self,
        provider_settings: ProviderSettings,
        mcp_servers: dict[str, Any] | None = None,
        default_model: str | None = None,
        max_agent_iterations: int | None = None,
        default_reasoning_effort: ReasoningEffort | None = None,
        max_session_seconds: float | None = None,
        tool_output: ToolOutputConfig | None = None,
    ) -> None:
        """Initialize the ACA runtime provider.

        Args:
            provider_settings: Structured ``runtime.provider`` settings with
                ``name="aca"``. Carries ``pool_endpoint``, ``api_version``,
                ``inner_provider``, ``identifier_scope``, ``egress``,
                ``lifecycle``, and ``auth`` — the only place these
                ``aca``-only fields live (see :class:`ProviderSettings`).
            mcp_servers: MCP server configurations forwarded to the runner.
            default_model: Default model for agents that don't specify one.
            max_agent_iterations: Maximum tool-use iterations per execution.
            default_reasoning_effort: Workflow-wide default reasoning effort.
            max_session_seconds: Maximum wall-clock duration for agent
                sessions, enforced runner-side.
            tool_output: MCP tool result output-size configuration.

        Raises:
            ProviderError: If the ``azure-identity`` package is not
                installed.
        """
        if not AZURE_IDENTITY_AVAILABLE:
            raise ProviderError(
                "aca provider requires the azure-identity package",
                suggestion="Install with: uv add 'conductor-cli[aca]'",
            )

        self._provider_settings = provider_settings
        self._mcp_servers = mcp_servers
        self._default_model = default_model
        self._default_max_agent_iterations = max_agent_iterations
        self._default_reasoning_effort = default_reasoning_effort
        self._default_max_session_seconds = max_session_seconds
        self._tool_output_config = tool_output

        # Per-run random salt (E3-T1) mixed into every derived identifier so
        # two workflow runs never collide even for identical agent/context —
        # see `identifier_for` / Data Flow "generated with a cryptographic
        # salt". 4 bytes (8 hex chars) is ample entropy for a routing key
        # that is never guessable-security-critical on its own (the pool
        # endpoint + AAD auth is the actual access boundary).
        self._run_salt = secrets.token_hex(4)

        # Lazily constructed: an httpx client owns real sockets, and the AAD
        # credential owns its own transport, so neither is created until
        # first use (e.g. a `conductor validate` capability lookup never
        # touches either).
        self._http_client: httpx.AsyncClient | None = None
        self._credential: Any = None
        self._cached_token: str | None = None
        self._cached_token_expires_at: float = 0.0

        # `identifier_scope: "none"` needs a fresh workspace on *every* call
        # (Data Flow: "fresh workspace every execution, including retries");
        # this monotonic counter is what makes that scope diverge even for
        # otherwise-identical agent/context pairs.
        self._none_scope_counter = 0

        # In-flight registry keyed by the *logical* identifier returned from
        # `identifier_for` (review fix, OQ#1). Maps each logical identifier
        # to the set of concurrency *slot numbers* (not a count) currently
        # reserved for it, so `execute()` can append a discriminator only
        # when genuinely concurrent, and release exactly the slot it
        # acquired regardless of completion order — see
        # `_acquire_wire_identifier`.
        self._active_identifiers: dict[str, set[int]] = {}

    # ------------------------------------------------------------------
    # Identifier derivation (E3-T3, DD5, OQ#1)
    # ------------------------------------------------------------------

    def identifier_for(self, agent: AgentDef, context: dict[str, Any]) -> str:
        """Derive the *logical* ACA session identifier for this execution.

        Implements the Data Flow formula
        ``cond-{run_salt}-{scope_key}{concurrency_suffix}``: ``scope_key``
        comes from the effective ``identifier_scope`` (per-agent
        ``sandbox.identifier_scope`` override, else the workflow-wide
        default), and the concurrency discriminator is appended whenever a
        for-each loop signal (``_key`` when ``key_by`` is set, else
        ``_index``) is present in ``context``.

        This is the *reuse* key described by the Data Flow table — calling
        it twice for the same agent/context always returns the same string,
        which is what makes sequential re-executions (loop-backs, retries,
        sequential ``for_each`` iterations of a different agent under
        ``identifier_scope: workflow``) share one sandbox workspace.

        **OQ#1 decision.** The design's *Data Flow* mandates a discriminator
        for "any concurrent unit", but this function's only per-call signal
        is ``context`` — the for-each loop key, when present. A ``parallel``
        group carries no such signal at all, and scopes other than the
        default ``agent``/``item`` don't already vary ``scope_key`` by agent
        name, so relying on this function alone is not sufficient to keep
        concurrent siblings from colliding (e.g. two members of a `parallel`
        group under `identifier_scope: workflow` would otherwise resolve to
        the *same* logical identifier). Rather than change the `execute()`
        contract (ruled out by this epic's acceptance criteria),
        `execute()` layers a runtime in-flight registry on top of this
        logical identifier (`_acquire_wire_identifier`/
        `_release_wire_identifier`): it only diverges the identifier
        actually sent over the wire when another call sharing this same
        logical identifier is genuinely in flight *right now*, and reuses it
        otherwise — satisfying both halves of the Data Flow contract
        (sequential reuse, concurrent divergence) with no engine-visible
        change.

        The result is charset-normalized (lowercase alphanumeric + hyphen)
        and truncated to ``_MAX_IDENTIFIER_LENGTH`` chars with an
        unconditional hash suffix so two distinct raw identifiers never
        collide after normalization/truncation.
        """
        scope = self._resolve_identifier_scope(agent)
        scope_key = self._scope_key(scope, agent, context)
        parts = [f"cond-{self._run_salt}", scope_key]

        # `item` scope already folds the loop key into `scope_key`, so
        # appending it again as a discriminator would just duplicate it.
        if scope != "item":
            discriminator = self._concurrency_discriminator(context)
            if discriminator:
                parts.append(discriminator)

        if scope == "none":
            self._none_scope_counter += 1
            parts.append(str(self._none_scope_counter))

        return self._normalize_and_truncate("-".join(parts))

    def _acquire_wire_identifier(self, logical_id: str) -> tuple[str, int]:
        """Reserve the identifier actually sent to ACA for this call.

        Tracks the *set* of slot numbers currently reserved for
        `logical_id` in `self._active_identifiers` — not merely a count.
        Slot `0` always maps to `logical_id` unchanged (preserving
        sequential reuse); a call that arrives while another call sharing
        this same `logical_id` hasn't released its slot yet (i.e. genuinely
        concurrent, per OQ#1) is assigned the smallest unused slot number
        instead, so `concurrent_safe=True` stays honest even under
        `identifier_scope: workflow` (whose `scope_key` alone does not vary
        by agent name) and for `parallel`-group members (which never carry a
        `context` loop-key signal at all).

        Using a *count* here (rather than a set of reserved slots) would be
        unsafe under out-of-order completion: if call A reserves slot 0 and
        call B reserves slot 1 while A is still in flight, then A finishes
        and releases first, a naive count would let a subsequent call C
        collide with B's still-active slot 1 (i.e. `count == 1` again,
        producing the same `-conc1` suffix as B). Tracking the actual
        reserved slot numbers means C instead gets the smallest number *not
        in the active set* (slot 0, freed by A) — never colliding with B.

        Must be paired with a matching `_release_wire_identifier(logical_id,
        slot)` call (typically in a `finally` block) once the request this
        identifier was used for has completed, passing back the exact
        `slot` this call returned.
        """
        used_slots = self._active_identifiers.setdefault(logical_id, set())
        slot = 0
        while slot in used_slots:
            slot += 1
        used_slots.add(slot)
        if slot == 0:
            return logical_id, slot
        return self._normalize_and_truncate(f"{logical_id}-conc{slot}"), slot

    def _release_wire_identifier(self, logical_id: str, slot: int) -> None:
        """Release the specific `slot` reserved by `_acquire_wire_identifier`."""
        used_slots = self._active_identifiers.get(logical_id)
        if used_slots is None:
            return
        used_slots.discard(slot)
        if not used_slots:
            self._active_identifiers.pop(logical_id, None)

    def _resolve_identifier_scope(self, agent: AgentDef) -> str:
        """Per-agent ``sandbox.identifier_scope`` wins over the workflow default."""
        if agent.sandbox is not None and agent.sandbox.identifier_scope is not None:
            return agent.sandbox.identifier_scope
        return self._provider_settings.identifier_scope or "agent"

    def _scope_key(self, scope: str, agent: AgentDef, context: dict[str, Any]) -> str:
        if scope == "workflow":
            # Constant across the whole run — `run_salt` already makes this
            # unique per *run*; every agent in this workflow shares it.
            return "workflow"
        if scope == "item":
            item_key = context.get("_key", context.get("_index"))
            if item_key is None:
                # No active for-each loop context — degrade to per-agent
                # reuse rather than raising (an `identifier_scope: item`
                # agent that isn't inside a for_each is a config oddity,
                # not an execution error).
                return agent.name
            return str(item_key)
        # "agent" (default) and "none" both key off the agent name; "none"'s
        # per-call uniqueness comes from `_none_scope_counter` instead.
        return agent.name

    def _concurrency_discriminator(self, context: dict[str, Any]) -> str:
        """Mandatory concurrency discriminator (DD5) — see OQ#1 in `identifier_for`."""
        key = context.get("_key")
        if key is not None:
            return str(key)
        index = context.get("_index")
        if index is not None:
            return str(index)
        return ""

    def _normalize_and_truncate(self, raw: str) -> str:
        """Charset-normalize ``raw`` and bound it to the ACA identifier limit.

        The digest is computed from ``raw`` (*before* normalization) and
        appended **unconditionally**, not only when the body is too long.
        ``_IDENTIFIER_INVALID_RE`` collapses every run of non
        ``[a-z0-9-]`` characters to a single hyphen, which is lossy: two
        distinct raw identifiers that differ only in the characters being
        collapsed (e.g. agent names ``"foo_bar"`` and ``"foo.bar"``, or
        ``"foo bar"`` and ``"foo-bar"``) would otherwise normalize to the
        *same* string and collide on the ACA session identifier — silently
        merging two logically distinct sessions. Mixing in a hash of the
        pre-normalization input guarantees distinct raw inputs stay distinct
        after normalization, regardless of collapsing.
        """
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        normalized = _IDENTIFIER_INVALID_RE.sub("-", raw.lower()).strip("-")
        if not normalized:
            normalized = "cond"
        prefix_len = _MAX_IDENTIFIER_LENGTH - len(digest) - 1
        return f"{normalized[:prefix_len]}-{digest}"

    @property
    def _health_identifier(self) -> str:
        """Stable per-run identifier for `/health` probes.

        Every request the container-path-forwarding proxy forwards —
        `/health` included — requires an `identifier` query parameter so ACA
        knows which session to route to (auto-allocating one if it doesn't
        exist yet). `validate_connection()` has no `agent`/`context` to
        derive one from, so this uses a fixed, run-scoped identifier instead.
        """
        return self._normalize_and_truncate(f"cond-{self._run_salt}-health")

    # ------------------------------------------------------------------
    # AAD auth (E3-T4)
    # ------------------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            # `read=None`: the streaming `/execute` response can legitimately
            # stay open for the ~30-minute per-request cap measured by the
            # Phase 0 spike (DD3); the runner-side `max_session_seconds`
            # guard and ACA's own platform cap are what actually bound
            # duration. Short calls (`/health`, interrupt, session delete)
            # pass their own per-request timeout override.
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
            )
        return self._http_client

    async def _get_access_token(self) -> str:
        """Acquire (and cache) a ``dynamicsessions.io`` bearer token.

        Uses the async ``azure.identity.aio.DefaultAzureCredential`` so
        token acquisition never blocks the event loop (FR6).
        """
        now = time.time()
        if self._cached_token is not None and now < (
            self._cached_token_expires_at - _TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return self._cached_token
        if self._credential is None:
            self._credential = _AsyncDefaultAzureCredential()  # ty: ignore[call-non-callable]
        token = await self._credential.get_token(_DYNAMICSESSIONS_SCOPE)
        self._cached_token = token.token
        self._cached_token_expires_at = float(token.expires_on)
        return self._cached_token

    def _build_url(self, path: str) -> str:
        base = (self._provider_settings.pool_endpoint or "").rstrip("/")
        return f"{base}/{path}"

    @property
    def _api_version(self) -> str:
        return self._provider_settings.api_version or _DEFAULT_API_VERSION

    # ------------------------------------------------------------------
    # Request construction (OQ#6 credential stopgap)
    # ------------------------------------------------------------------

    def _resolve_inner_provider_settings(self) -> dict[str, Any] | None:
        """Phase 1 credential stopgap (OQ#6, DD4).

        The ``aca``-scoped ``ProviderSettings`` fields intentionally exclude
        ``bearer_token``/``api_key``/``base_url`` (those stay copilot-only,
        `_check_field_compatibility`), so this resolves the *same*
        environment variables the Copilot custom-routing resolver already
        reads (``copilot.py:_resolve_sdk_provider_config``) and forwards
        them verbatim so the runner can construct
        ``ProviderSettings(name=inner_provider, **inner_provider_settings)``
        for its inner ``CopilotProvider`` instead of attempting an
        impossible interactive OAuth flow inside a headless sandbox.

        Acceptable only for **trusted Phase 1 use** (DD4) — the plaintext
        credential travels in the request body to the runner. Returns
        ``None`` when nothing is configured, which is itself an accepted gap
        until the Phase 2 gateway (E8) removes the need for this mechanism.
        """
        base_url = os.environ.get("COPILOT_PROVIDER_BASE_URL")
        api_key = os.environ.get("COPILOT_PROVIDER_API_KEY")
        bearer_token = os.environ.get("COPILOT_PROVIDER_BEARER_TOKEN")
        if not (base_url or api_key or bearer_token):
            return None
        settings: dict[str, Any] = {}
        if base_url:
            settings["base_url"] = base_url
        if api_key:
            settings["api_key"] = api_key
        if bearer_token:
            settings["bearer_token"] = bearer_token
        return settings

    def _serialize_mcp_servers(self) -> dict[str, Any] | None:
        if not self._mcp_servers:
            return None
        result: dict[str, Any] = {}
        for name, cfg in self._mcp_servers.items():
            result[name] = cfg.model_dump(mode="json") if hasattr(cfg, "model_dump") else cfg
        return result

    def _build_request(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None,
    ) -> AcaExecuteRequest:
        reasoning_effort = resolve_reasoning_effort(agent, self._default_reasoning_effort)
        working_dir = agent.sandbox.working_dir if agent.sandbox is not None else None
        output_schema = (
            {name: field.model_dump(exclude_none=True) for name, field in agent.output.items()}
            if agent.output
            else None
        )
        agent_payload = AcaAgentPayload(
            name=agent.name,
            model=agent.model or self._default_model,
            system_prompt=agent.system_prompt,
            output=output_schema,
            max_agent_iterations=agent.max_agent_iterations or self._default_max_agent_iterations,
            max_session_seconds=agent.max_session_seconds or self._default_max_session_seconds,
            reasoning_effort=reasoning_effort,
            working_dir=working_dir,
        )
        return AcaExecuteRequest(
            agent=agent_payload,
            rendered_prompt=rendered_prompt,
            tools=tools,
            mcp_servers=self._serialize_mcp_servers(),
            context=context,
            inner_provider=self._provider_settings.inner_provider or "copilot",
            inner_provider_settings=self._resolve_inner_provider_settings(),
            tool_output=(
                self._tool_output_config.model_dump(mode="json")
                if self._tool_output_config is not None
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Error classification (E3-T5)
    # ------------------------------------------------------------------

    def _error_from_frame(self, data: dict[str, Any]) -> ProviderError:
        parsed = AcaErrorData.model_validate(data)
        return self._provider_error_from_parts(parsed)

    async def _error_from_response(self, response: httpx.Response) -> ProviderError:
        """Build a `ProviderError` from a non-2xx response body.

        Review fix: a response opened via `client.stream()` (the `/execute`
        path) has an unread body — calling `.json()` directly raises
        `httpx.ResponseNotRead` (an `httpx.HTTPError` subclass), which the
        `execute()` caller's `except httpx.HTTPError` clause then swallowed
        into a generic "transport error" message, discarding the real ACA
        `code`/`message`/`traceId`. `.aread()` buffers the body first; it is
        a no-op on a response that was already fully read (e.g. the
        non-streaming `client.get()`/`client.post()` callers), so this is
        safe for every call site.
        """
        await response.aread()
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        error_obj = body.get("error", body) if isinstance(body, dict) else {}
        parsed = (
            AcaErrorData.model_validate(error_obj)
            if isinstance(error_obj, dict)
            else AcaErrorData()
        )
        return self._provider_error_from_parts(parsed, status_code=response.status_code)

    def _provider_error_from_parts(
        self, error: AcaErrorData, status_code: int | None = None
    ) -> ProviderError:
        suggestion = None
        if error.code or error.trace_id:
            suggestion = f"ACA error code={error.code} traceId={error.trace_id}"
        return ProviderError(
            f"aca: {error.message}",
            suggestion=suggestion,
            status_code=status_code,
            provider_name="aca",
        )

    def _agent_output_from_result(self, data: dict[str, Any], *, interrupted: bool) -> AgentOutput:
        result = AcaResultData.model_validate(data)
        input_tokens = result.input_tokens
        output_tokens = result.output_tokens
        tokens_used = (
            (input_tokens or 0) + (output_tokens or 0)
            if input_tokens is not None or output_tokens is not None
            else None
        )
        return AgentOutput(
            content=result.content,
            raw_response=data,
            tokens_used=tokens_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_write_tokens,
            model=result.model,
            partial=result.partial or interrupted,
        )

    # ------------------------------------------------------------------
    # Streaming transport (E3-T5, Branch S)
    # ------------------------------------------------------------------

    def _stream_execute(
        self, url: str, params: dict[str, str], headers: dict[str, str], json_body: dict[str, Any]
    ):
        """Open the streaming ``/execute`` request.

        Isolated as its own method (rather than inlined into `execute`) so
        tests can substitute a fully-controlled fake response for
        deterministic interrupt-race testing without depending on real
        transport/event-loop scheduling.
        """
        client = self._ensure_client()
        return client.stream("POST", url, params=params, headers=headers, json=json_body)

    async def _send_interrupt(self, identifier: str) -> None:
        """POST a runner-defined interrupt signal for the in-flight session.

        This is a **runner-image contract**, not an ACA management-plane
        operation: per the container-path-forwarding proxy contract
        (`<POOL_MANAGEMENT_ENDPOINT>/<path>?identifier=<id>` forwards to
        `<TARGET_PORT>/<path>`), a top-level `/interrupt` path is proxied
        straight to the runner's own `/interrupt` handler for the session
        already routed by `identifier` — the same session currently
        streaming the `/execute` response — so the runner can signal its
        in-flight inner-provider call to stop (Branch S). Deliberately a
        sibling of `/execute`, not nested under it (`/execute/interrupt`
        would forward to a runner path that doesn't exist in this
        contract).
        """
        token = await self._get_access_token()
        client = self._ensure_client()
        response = await client.post(
            self._build_url("interrupt"),
            params={"identifier": identifier, "api-version": self._api_version},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        if response.status_code >= 400:
            raise await self._error_from_response(response)

    async def _stop_session(self, identifier: str) -> None:
        """Best-effort hard-abort fallback when the in-stream interrupt fails.

        Uses ACA's real Session Management **Delete** data-plane operation
        (`DELETE {endpoint}/session?identifier=<id>&api-version=<v>` —
        Microsoft Learn, Container Apps data-plane REST API), *not* the
        fictional `POST {endpoint}/stopSession` this used to call. Note that
        operation is documented as **"not supported for custom container
        session pools"** (which is what this provider always targets), so
        this call is expected to itself return an error for the MVP runner
        image — it is issued anyway, best-effort, in case that restriction
        is lifted for a future pool SKU, and its result never blocks the
        actual local abort: the caller always cancels its own read loop
        regardless of whether this call succeeds.
        """
        token = await self._get_access_token()
        client = self._ensure_client()
        response = await client.delete(
            self._build_url("session"),
            params={"identifier": identifier, "api-version": self._api_version},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        if response.status_code >= 400:
            raise await self._error_from_response(response)

    async def _read_frames(
        self,
        response: Any,
        interrupt_signal: asyncio.Event | None,
        identifier: str,
        event_callback: EventCallback | None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Consume the NDJSON stream, relaying frames and racing `interrupt_signal`.

        Only one line-read (``__anext__``) is ever in flight at a time.
        While not yet interrupted, each pending line read races against
        `interrupt_signal.wait()`; once interrupted, subsequent lines are
        awaited directly (E3-T6: fire the in-stream interrupt once, then
        drain toward the runner's resulting partial `result` frame).

        Returns ``(result_frame_data, interrupted)``. `result_frame_data` is
        ``None`` when the stream ended without a terminal frame (either a
        genuine premature close, or a hard local abort after the in-stream
        interrupt itself failed to send — see `_stop_session`).
        """
        interrupted = False
        result_data: dict[str, Any] | None = None
        line_iter = response.aiter_lines()
        next_line_task: asyncio.Task[Any] | None = None
        try:
            while True:
                if next_line_task is None:
                    next_line_task = asyncio.create_task(line_iter.__anext__())

                if interrupt_signal is not None and not interrupted:
                    interrupt_task = asyncio.create_task(interrupt_signal.wait())
                    done, _pending = await asyncio.wait(
                        {next_line_task, interrupt_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if interrupt_task in done:
                        interrupt_signal.clear()
                        interrupted = True
                        try:
                            await self._send_interrupt(identifier)
                        except Exception:
                            logger.warning(
                                "aca: in-stream interrupt failed for identifier=%s; "
                                "hard-aborting locally (best-effort session delete)",
                                identifier,
                            )
                            with contextlib.suppress(Exception):
                                await self._stop_session(identifier)
                            next_line_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await next_line_task
                            next_line_task = None
                            return None, True
                    else:
                        interrupt_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await interrupt_task

                try:
                    if not next_line_task.done():
                        await next_line_task
                    line = next_line_task.result()
                except StopAsyncIteration:
                    break
                finally:
                    next_line_task = None

                line = line.strip()
                if not line:
                    continue
                frame = json.loads(line)
                frame_type = frame.get("type")
                data = frame.get("data") or {}
                if frame_type == "result":
                    result_data = data
                    break
                if frame_type == "error":
                    raise self._error_from_frame(data)
                if event_callback is not None:
                    with contextlib.suppress(Exception):
                        event_callback(frame_type, data)
        finally:
            if next_line_task is not None and not next_line_task.done():
                next_line_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await next_line_task
        return result_data, interrupted

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
    ) -> AgentOutput:
        """Delegate execution to the in-sandbox runner over Branch S streaming.

        See module docstring / Data Flow. Raises `ProviderError` on any
        transport failure, non-2xx response, or runner-reported `error`
        frame; returns a partial `AgentOutput` when `interrupt_signal` fires
        and the runner's resulting `result` frame (if any) arrives before
        the stream otherwise ends.
        """
        logical_id = self.identifier_for(agent, context)
        # Reserve the wire identifier for the full lifetime of this request
        # (acquired before the request starts, released once it finishes —
        # success, error, or interrupt) so a genuinely concurrent sibling
        # sharing `logical_id` diverges while this call is in flight, and
        # reuses `logical_id` once it is free again (OQ#1; see
        # `identifier_for` / `_acquire_wire_identifier`). `slot` identifies
        # exactly which reservation this call holds, so release always frees
        # the right one even if calls sharing `logical_id` complete out of
        # order.
        identifier, slot = self._acquire_wire_identifier(logical_id)
        try:
            request = self._build_request(agent, context, rendered_prompt, tools)
            token = await self._get_access_token()
            url = self._build_url("execute")
            params = {"identifier": identifier, "api-version": self._api_version}
            headers = {"Authorization": f"Bearer {token}"}
            body = request.model_dump(mode="json")

            try:
                async with self._stream_execute(url, params, headers, body) as response:
                    if response.status_code >= 400:
                        raise await self._error_from_response(response)
                    result_data, interrupted = await self._read_frames(
                        response, interrupt_signal, identifier, event_callback
                    )
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"aca: transport error contacting runner at {url}: {exc}",
                    provider_name="aca",
                    is_retryable=True,
                ) from exc

            if result_data is None:
                if interrupted:
                    return AgentOutput(content={}, raw_response=None, partial=True)
                raise ProviderError(
                    "aca: runner stream ended without a terminal result frame",
                    provider_name="aca",
                    is_retryable=True,
                )
            return self._agent_output_from_result(result_data, interrupted=interrupted)
        finally:
            self._release_wire_identifier(logical_id, slot)

    # ------------------------------------------------------------------
    # validate_connection() / close() (E3-T6, E3-T1)
    # ------------------------------------------------------------------

    async def validate_connection(self) -> bool:
        """Lightweight management-plane + `/health` probe (skew check).

        Acquires an AAD token (proves the *Session Executor* role is
        reachable) then calls the pool's `/health` endpoint. Like every
        other request forwarded through the container-path-forwarding proxy,
        `/health` requires an `identifier` query parameter — ACA routes by
        identifier, auto-allocating a session if none exists yet — so this
        uses a dedicated, stable per-run health-check identifier
        (`_health_identifier`) rather than omitting it. A
        `conductor_version` mismatch between host and runner is logged as a
        warning, never raised — version skew is a compatibility hint, not a
        hard failure (mirrors the "safe degradation" convention of the other
        best-effort provider hooks in `AgentProvider`).
        """
        try:
            token = await self._get_access_token()
        except Exception as exc:
            raise ProviderError(
                f"aca: failed to acquire a dynamicsessions.io access token: {exc}",
                suggestion=(
                    "Verify DefaultAzureCredential can authenticate (az login, "
                    "managed identity, workload identity, etc.) and that the "
                    "identity has the Session Executor role on the pool."
                ),
                provider_name="aca",
                is_retryable=False,
            ) from exc

        client = self._ensure_client()
        url = self._build_url("health")
        try:
            response = await client.get(
                url,
                params={
                    "identifier": self._health_identifier,
                    "api-version": self._api_version,
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"aca: failed to reach pool health endpoint at {url}: {exc}",
                provider_name="aca",
                is_retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise await self._error_from_response(response)

        with contextlib.suppress(Exception):
            self._warn_on_version_skew(response.json())
        return True

    def _warn_on_version_skew(self, health: dict[str, Any]) -> None:
        runner_version = health.get("conductor_version") if isinstance(health, dict) else None
        if not runner_version:
            return
        from conductor import __version__ as host_version

        if runner_version != host_version:
            logger.warning(
                "aca: runner Conductor version %s differs from host version %s; "
                "behavior may differ between host and sandbox.",
                runner_version,
                host_version,
            )

    async def close(self) -> None:
        """Release the AAD credential and httpx client."""
        if self._credential is not None:
            with contextlib.suppress(Exception):
                await self._credential.close()
            self._credential = None
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
