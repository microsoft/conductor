"""Wire protocol between ``AcaRuntimeProvider`` (host) and the in-sandbox
``conductor-agent-runner`` (issue #284, epic E4 — not yet built).

These Pydantic models are the shared contract for the single streaming
``POST {pool_endpoint}/execute?identifier=<id>&api-version=<v>`` request
(Branch S, DD3) described in the design's *API Contracts* section:
``agent`` / ``rendered_prompt`` / ``tools`` / ``mcp_servers`` / ``context`` on
the way in; one NDJSON event frame per line on the way out, terminated by a
``result`` frame.

Two fields are additive beyond the literal API Contracts example, resolving
open questions the design left for this epic:

- ``AcaExecuteRequest.inner_provider`` / ``inner_provider_settings`` — DD4
  credential precedence (epic E8; originally OQ#6's MVP stopgap). The runner
  has no interactive OAuth flow, so the host forwards one narrowly-scoped
  credential per call that the runner uses to construct
  ``ProviderSettings(name=inner_provider, **inner_provider_settings)`` for its
  inner ``CopilotProvider`` — reusing the existing custom-routing
  ``bearer_token`` path (``copilot.py:_resolve_sdk_provider_config``) instead
  of inventing a new auth mechanism. Precedence mirrors the Copilot CLI's own
  auth resolution: when ``COPILOT_PROVIDER_BASE_URL`` is set on the host, this
  is ``{"base_url": ..., "api_key": ..., "bearer_token": ...}`` (BYOK,
  unchanged from the stopgap); otherwise it is ``{"github_token": ...}``, a
  GitHub token sourced from ``COPILOT_GITHUB_TOKEN`` → ``GH_TOKEN`` →
  ``GITHUB_TOKEN`` so the sandbox's inner runtime authenticates against
  GitHub Copilot's own model routing (the operator's Copilot capacity). The
  host (``AcaRuntimeProvider._resolve_inner_provider_settings``) raises
  ``ProviderError`` rather than sending a request when neither resolves — no
  silently unauthenticated sandbox run. Every secret value in this dict
  (``api_key`` / ``bearer_token`` / ``github_token``) is a ``SecretStr``
  instance, not a plain ``str`` — pydantic still recognizes and redacts it in
  ``AcaExecuteRequest.model_dump()`` / ``repr()``; only
  ``AcaRuntimeProvider._wire_body`` unwraps it to plaintext, immediately
  before the request bytes leave the process. Review fix:
  ``AcaExecuteRequest._redact_inner_provider_secrets`` (a ``field_validator``)
  enforces this ``SecretStr`` wrapping on every *validated* construction path
  — not just ``AcaRuntimeProvider``'s own direct-construction call site, but
  also a plain dict/JSON payload validated via ``model_validate``/
  ``model_validate_json`` (e.g. the runner's own FastAPI request parsing),
  which would otherwise retain a plaintext credential string. This does not
  cover ``model_construct()`` or ``model_copy(update=...)``, both of which
  bypass validators by design; neither is used to build this model anywhere
  in this codebase.
- ``AcaExecuteRequest.tool_output`` / ``AcaResultData.cache_read_tokens`` /
  ``AcaResultData.cache_write_tokens`` — review fix: these were captured
  host-side (``ToolOutputConfig`` on the provider; Claude-style prompt-cache
  counters on ``AgentOutput``) but never crossed the wire, so the runner's
  inner provider had no way to honor the workflow's tool-output-size policy,
  and any cache-token counts the inner SDK reported were silently dropped
  instead of reaching ``AgentOutput.cache_read_tokens`` /
  ``cache_write_tokens``.
- ``AcaAgentPayload.retry`` / ``AcaAgentPayload.context_tier`` — review fix:
  the per-agent ``RetryPolicy`` and resolved ``context_tier`` literal are
  read directly off ``AgentDef`` by the inner ``CopilotProvider.execute()``
  (not passed as separate provider constructor args, unlike
  ``reasoning_effort``), so dropping them from the payload silently
  disabled per-agent retry and long-context routing for every ``aca``-backed
  agent. Both travel as already-resolved plain values (a dumped
  ``RetryPolicy`` dict / a concrete ``ContextTier`` literal) — the host has
  already rendered any ``{{ ... }}`` template by the time ``execute()`` is
  called (see ``conductor.providers.context_tier``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

# `inner_provider_settings` keys that carry a credential (as opposed to
# `base_url`, which is not secret). Kept in one place so the redaction
# validator below and any future field additions stay in sync.
_INNER_PROVIDER_SECRET_KEYS = ("api_key", "bearer_token", "github_token")


class AcaAgentPayload(BaseModel):
    """The subset of ``AgentDef`` the runner needs to reconstruct the inner agent.

    Deliberately narrower than the full ``AgentDef`` — only the fields that
    change inner-provider behavior are forwarded. Routing, dependency
    (``input:``), and validator configuration all stay host-side (they operate
    on ``AgentOutput``, which the runner already returns).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """Agent name — used by the runner for log/event attribution only."""

    model: str | None = None
    """Model identifier, already resolved (Jinja-rendered) on the host."""

    system_prompt: str | None = None
    """System message, already resolved on the host."""

    output: dict[str, Any] | None = None
    """Agent's ``output:`` schema (``OutputField`` dumped to plain dicts)."""

    max_agent_iterations: int | None = None
    """Maximum tool-use iterations for this execution."""

    max_session_seconds: float | None = None
    """Runner-enforced wall-clock guard for this execution (see DD3 caveat:
    the pool's own ``Timed`` lifecycle does not enforce this)."""

    reasoning_effort: str | None = None
    """Resolved reasoning-effort level (``low``/``medium``/``high``/``xhigh``/
    ``max``), already merged from per-agent + workflow-wide defaults on the
    host via ``resolve_reasoning_effort``. ``None`` means no reasoning
    parameter should be sent to the inner SDK."""

    working_dir: str | None = None
    """Container-relative working directory (``SandboxConfig.working_dir``).
    Never a host path — interpreted by the runner as a path inside the
    session filesystem."""

    retry: dict[str, Any] | None = None
    """Per-agent ``RetryPolicy`` (``agent.retry``), dumped to a plain dict.

    ``None`` when the agent has no ``retry:`` block, in which case the inner
    ``CopilotProvider`` falls back to its own default retry config — the
    same behavior as an on-host ``copilot`` agent with no ``retry:``."""

    context_tier: str | None = None
    """Resolved ``context_tier`` literal (``agent.context_tier``), already
    rendered from any ``{{ ... }}`` template on the host. ``None`` means no
    per-agent override — the inner provider uses its own default."""


class AcaExecuteRequest(BaseModel):
    """Body of ``POST {pool_endpoint}/execute?identifier=<id>&api-version=<v>``."""

    model_config = ConfigDict(extra="forbid")

    agent: AcaAgentPayload
    rendered_prompt: str
    tools: list[str] | None = None
    """Per-agent tool allowlist. ``None`` = all workflow tools, ``[]`` = none."""

    mcp_servers: dict[str, Any] | None = None
    """Full ``runtime.mcp_servers`` definitions (not just names) — the
    runner-image contract requires stdio binaries to already be baked into
    the image; remote (HTTP/SSE) servers require pool egress."""

    context: dict[str, Any] = Field(default_factory=dict)
    """Accumulated workflow context needed to reconstruct the agent's view."""

    inner_provider: str = "copilot"
    """SDK the runner should drive. MVP: ``"copilot"`` only."""

    inner_provider_settings: dict[str, Any] | None = None
    """DD4 credential precedence (epic E8) — see module docstring. Either
    BYOK settings (``base_url`` + optional ``api_key``/``bearer_token``) or a
    single ``github_token`` field for Copilot-capacity auth. The host raises
    ``ProviderError`` rather than sending a request with this unset, so in
    practice this field is never ``None`` on the wire. Secret values are
    ``SecretStr`` instances (redacted in ``model_dump``/``repr``); the host
    unwraps them to plaintext only in its dedicated wire-serialization step
    (``AcaRuntimeProvider._wire_body``).

    The type is a loosely-typed ``dict[str, Any]`` (not a dedicated
    sub-model) because the runner side (epic E4) treats it as opaque
    ``ProviderSettings`` constructor kwargs. ``_redact_inner_provider_secrets``
    below still guarantees redaction on every *validated* construction path
    — not just ``AcaRuntimeProvider``'s own direct-construction call site —
    by wrapping any plain-``str`` value under a known credential key in
    ``SecretStr`` immediately after validation. (Field validators only run
    on validated construction; ``model_construct()``/``model_copy(update=...)``
    bypass them and are not used to build this model anywhere in this
    codebase.)"""

    @field_validator("inner_provider_settings", mode="after")
    @classmethod
    def _redact_inner_provider_secrets(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Coerce known credential keys to ``SecretStr`` regardless of how
        this model was validated (review fix).

        ``AcaRuntimeProvider._resolve_inner_provider_settings`` already
        returns these fields pre-wrapped in ``SecretStr`` for the host's own
        direct-construction call site, so `model_dump`/`repr` already
        redact them there. But `dict[str, Any]` performs no coercion of its
        own — constructing this model via `model_validate`/
        `model_validate_json` on a plain dict/JSON payload (e.g. the
        runner's own FastAPI request parsing) would otherwise retain
        plaintext credential strings, silently defeating that redaction.
        Values that are already ``SecretStr`` (not a ``str`` subclass) pass
        through unchanged, so this is idempotent across repeated
        validation.

        Like all field validators, this only runs on *validated*
        construction (``__init__``/``model_validate``/``model_validate_json``).
        ``model_construct()`` (skips validation entirely) and
        ``model_copy(update=...)`` (assigns the update dict directly, no
        validator re-run) both bypass it — neither is used to build this
        model anywhere in this codebase, so this is a documented scope
        limit, not a gap in current call sites.
        """
        if value is None:
            return value
        return {
            key: SecretStr(raw)
            if key in _INNER_PROVIDER_SECRET_KEYS and isinstance(raw, str)
            else raw
            for key, raw in value.items()
        }

    tool_output: dict[str, Any] | None = None
    """``runtime.tool_output`` (``ToolOutputConfig``), dumped to a plain dict.

    Forwarded so the runner's inner provider applies the same per-result MCP
    tool-output size limit (``max_chars``/``spill_to_file``/``spill_dir``) the
    host would have applied for an on-host provider. ``None`` when unset."""


class AcaEventFrame(BaseModel):
    """One line of the ``application/x-ndjson`` event stream (non-terminal).

    Event types reuse Conductor's own vocabulary (``agent_turn_start``,
    ``agent_message``, ``agent_tool_start``, ...) so the host can relay
    ``(type, data)`` verbatim to ``event_callback`` with no translation.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class AcaResultData(BaseModel):
    """Payload of the terminal ``result`` frame, parsed into ``AgentOutput``."""

    model_config = ConfigDict(extra="ignore")

    content: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    """Tokens read from cache (Claude-style prompt caching), when the inner
    provider reports it."""

    cache_write_tokens: int | None = None
    """Tokens written to cache (Claude-style prompt caching), when the inner
    provider reports it."""

    session_seconds: float | None = None
    """Sandbox wall-clock time for this execution, as measured by the runner
    (issue #284, FR7). Parsed into ``AgentOutput.session_seconds`` so the host
    engine can record it as a distinct usage row, separate from token cost."""

    partial: bool = False


class AcaErrorData(BaseModel):
    """Payload of a terminal ``error`` frame, or a non-2xx HTTP error body.

    Field names mirror ACA's own management-API error shape (``code`` /
    ``message`` / ``traceId``) so host-side ``ProviderError`` messages can
    surface the same diagnostic identifiers an operator would use with
    Azure support.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message: str = "aca runner reported an error"
    code: str | None = None
    trace_id: str | None = Field(default=None, alias="traceId")
