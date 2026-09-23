"""Backend-neutral wire contract between the Conductor CLI host and a remote
agent runtime.

This module is the shared contract for the single streaming
``POST {endpoint}/execute?identifier=<id>`` request: ``agent`` /
``rendered_prompt`` / ``tools`` / ``mcp_servers`` / ``context`` on the way in;
one NDJSON event frame per line on the way out, terminated by a ``result``
frame (or an ``error`` frame / non-2xx error body).

Both sides of the wire install the one ``conductor`` package —
``docker/aca-runner/Dockerfile`` pins the runner image by git SHA, so the
host and the runner deploy independently and skew in both directions is a
normal operating condition, not an error.

Boundary vs ``conductor.execution``
-----------------------------------
:mod:`conductor.execution` is the **in-process** Python backend seam:
stdlib-only, frozen dataclass contracts, used when the agent runtime lives in
the same interpreter as the engine. This module is the **serialized** wire
contract for a *remote* runtime: Pydantic models that define the exact JSON
bodies and NDJSON frame shapes exchanged over HTTP.

Compatibility rules
-------------------
- Response-side models (``RunnerAgentResult``, ``RunnerErrorData``,
  ``RunnerEventFrame``, ``RunnerHealthResponse``) use ``extra="ignore"`` so a
  new host parses an old runner's payload and an old host ignores keys a new
  runner added — evolution is additive-only.
- Request-side models (``RunnerAgentPayload``, ``RunnerAgentRequest``) are
  strict ``extra="forbid"`` so a sender cannot smuggle undeclared fields past
  a receiver's validation.
- ``RUNNER_PROTOCOL_VERSION`` is advertised on ``/health``; bump it only on
  an incompatible change (see its docstring).

History
-------
Lifted de-ACA'd from ``conductor.providers.aca_protocol`` (issue #284); the
field names, aliases, defaults, validators, and ``ConfigDict`` modes are
byte-identical in meaning — only the class names and module identity changed
to make the contract backend-neutral. ``conductor-agent-runner`` (shipped via
``docker/aca-runner/Dockerfile``) is the reference remote runtime speaking
this contract.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

# The one definition of the transport-token header name (issue #396),
# imported by both the runner and the host transport adapter so the two
# sides of the transport-token gate can never drift apart.
RUNNER_TOKEN_HEADER = "X-Conductor-Runner-Token"

# Wire-protocol version advertised by the runner on /health (and understood
# by the host). Evolution of this contract is additive-only (response-side
# extra="ignore"); bump this constant ONLY on an incompatible change — a
# host and runner disagreeing on it is a compatibility warning, never a
# hard failure.
RUNNER_PROTOCOL_VERSION: int = 1

# `inner_provider_settings` keys that carry a credential (as opposed to
# `base_url`, which is not secret). Kept in one place so the redaction
# validator below and any future field additions stay in sync.
_INNER_PROVIDER_SECRET_KEYS = ("api_key", "bearer_token", "github_token")


class RunnerAgentPayload(BaseModel):
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
    """Runner-enforced wall-clock guard for this execution (the remote
    runtime's own lifecycle does not enforce this on the host's behalf)."""

    reasoning_effort: str | None = None
    """Resolved reasoning-effort level (``low``/``medium``/``high``/``xhigh``/
    ``max``), already merged from per-agent + workflow-wide defaults on the
    host via ``resolve_reasoning_effort``. ``None`` means no reasoning
    parameter should be sent to the inner SDK."""

    working_dir: str | None = None
    """Runtime-relative working directory. Never a host path — interpreted by
    the runner as a path inside the remote session filesystem."""

    retry: dict[str, Any] | None = None
    """Per-agent ``RetryPolicy`` (``agent.retry``), dumped to a plain dict.

    ``None`` when the agent has no ``retry:`` block, in which case the inner
    provider falls back to its own default retry config — the same behavior
    as an on-host agent with no ``retry:``."""

    context_tier: str | None = None
    """Resolved ``context_tier`` literal (``agent.context_tier``), already
    rendered from any ``{{ ... }}`` template on the host. ``None`` means no
    per-agent override — the inner provider uses its own default."""


class RunnerAgentRequest(BaseModel):
    """Body of ``POST {endpoint}/execute?identifier=<id>``.

    The envelope is backend-neutral, but the concrete dict of credential keys
    (``_INNER_PROVIDER_SECRET_KEYS``) is the vocabulary of today's single
    inner provider (Copilot); a new inner provider extends the list.
    """

    model_config = ConfigDict(extra="forbid")

    agent: RunnerAgentPayload
    rendered_prompt: str
    tools: list[str] | None = None
    """Per-agent tool allowlist. ``None`` = all workflow tools, ``[]`` = none."""

    mcp_servers: dict[str, Any] | None = None
    """Full ``runtime.mcp_servers`` definitions (not just names) — the
    runner-image contract requires stdio binaries to already be baked into
    the image; remote (HTTP/SSE) servers require runtime egress."""

    context: dict[str, Any] = Field(default_factory=dict)
    """Accumulated workflow context needed to reconstruct the agent's view."""

    inner_provider: str = "copilot"
    """SDK the runner should drive. MVP: ``"copilot"`` only."""

    inner_provider_settings: dict[str, Any] | None = None
    """Credential precedence — either BYOK settings (``base_url`` + optional
    ``api_key``/``bearer_token``) or a single ``github_token`` field for
    Copilot-capacity auth. The host raises ``ProviderError`` rather than
    sending a request with this unset, so in practice this field is never
    ``None`` on the wire. Secret values are ``SecretStr`` instances (redacted
    in ``model_dump``/``repr``); the host unwraps them to plaintext only in
    the dedicated wire-serialization step (:func:`request_to_wire_body`).

    The type is a loosely-typed ``dict[str, Any]`` (not a dedicated
    sub-model) because the runner side treats it as opaque
    ``ProviderSettings`` constructor kwargs. ``_redact_inner_provider_secrets``
    below still guarantees redaction on every *validated* construction path —
    not just the host's own direct-construction call site —
    by wrapping any plain-``str`` value under a known credential key in
    ``SecretStr`` immediately after validation. (Field validators only run
    on validated construction; ``model_construct()``/``model_copy(update=...)``
    bypass them and are not used to build this model anywhere in this
    codebase.)

    The runner rejects any key outside the four named above
    (``aca_runner/auth.py::ALLOWED_INNER_PROVIDER_SETTINGS_KEYS``) with a
    400, and optionally checks ``base_url`` against
    ``ACA_RUNNER_ALLOWED_BASE_URLS``. That allowlist is a separate control
    from ``_INNER_PROVIDER_SECRET_KEYS`` here: that list is about which
    values get redacted, not which keys are permitted at all — ``base_url``
    is permitted but not secret.
    """

    @field_validator("inner_provider_settings", mode="after")
    @classmethod
    def _redact_inner_provider_secrets(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Coerce known credential keys to ``SecretStr`` regardless of how
        this model was validated.

        The host's credential-resolution step already returns these fields
        pre-wrapped in ``SecretStr`` for its own direct-construction call
        site, so `model_dump`/`repr` already redact them there. But
        `dict[str, Any]` performs no coercion of its own — constructing this
        model via `model_validate`/`model_validate_json` on a plain dict/JSON
        payload (e.g. the runner's own FastAPI request parsing) would
        otherwise retain plaintext credential strings, silently defeating
        that redaction. Values that are already ``SecretStr`` (not a ``str``
        subclass) pass through unchanged, so this is idempotent across
        repeated validation.

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


class RunnerEventFrame(BaseModel):
    """One line of the ``application/x-ndjson`` event stream (non-terminal).

    Event types reuse Conductor's own vocabulary (``agent_turn_start``,
    ``agent_message``, ``agent_tool_start``, ...) so the host can relay
    ``(type, data)`` verbatim to ``event_callback`` with no translation.

    This is a *documenting* model of the frame shape — it is instantiated by
    neither side: the host parses frames with ``json.loads`` (see
    ``providers/aca.py::_read_frames``) and the runner serializes them with
    ``json.dumps`` (see ``aca_runner/server.py::_frame``). It exists as the
    formal description of the on-the-wire shape and as a validation anchor
    for tests.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class RunnerAgentResult(BaseModel):
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

    last_call_input_tokens: int | None = None
    """Prompt tokens of the most recent single API call (issue #412), when
    the inner provider reports it. Together with ``ConfigDict(extra="ignore")``
    and the ``None`` default, this keeps host/runner version skew
    bidirectionally compatible: a new host against an old runner gets
    ``None`` (bar hidden); an old host against a new runner ignores the
    extra key."""

    session_seconds: float | None = None
    """Remote wall-clock time for this execution, as measured by the runner
    (issue #284, FR7). Parsed into ``AgentOutput.session_seconds`` so the host
    engine can record it as a distinct usage row, separate from token cost."""

    partial: bool = False


class RunnerErrorData(BaseModel):
    """Payload of a terminal ``error`` frame, or a non-2xx HTTP error body.

    The neutral runner error shape: a human-readable ``message`` only.
    Backend-specific adapters may extend this model with their platform's
    diagnostic identifiers (e.g. an error ``code`` / ``traceId`` from a cloud
    front-end) — the ``extra="ignore"`` config keeps this base model
    parseable against such richer bodies.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message: str = "runner reported an error"


class RunnerHealthResponse(BaseModel):
    """Typed model of the runner's ``GET /health`` payload.

    Today the runner returns this as an ad-hoc dict (``aca_runner/server.py``);
    this model gives the host a typed, version-advertisement-aware parse.
    ``extra="ignore"`` keeps it parseable against an old runner (a missing
    ``protocol_version`` parses to ``None`` — the host skips the
    compatibility check for runners that predate version advertisement) and
    against a newer runner carrying keys this host does not know.
    """

    model_config = ConfigDict(extra="ignore")

    ready: bool
    conductor_version: str | None = None
    runner_version: str | None = None
    protocol_version: int | None = None
    auth_required: bool | None = None
    auth_token_present: bool | None = None


def request_to_wire_body(request: RunnerAgentRequest) -> dict[str, Any]:
    """Serialize `request` into the JSON body actually sent to the runner.

    ``request.model_dump(mode="json")`` alone keeps any ``SecretStr`` values
    held in ``inner_provider_settings`` redacted (``"**********"``) — the
    correct behavior for anything that logs, reprs, or otherwise dumps the
    request object itself (including the request's own ``__repr__``). This
    function is the one dedicated wire-serialization step that unwraps those
    secrets back to plaintext, reading them directly off
    ``request.inner_provider_settings`` (not off the already-redacted dump)
    immediately before the bytes leave the process — nowhere else in the
    codebase sees the plaintext.
    """
    body = request.model_dump(mode="json")
    if request.inner_provider_settings is not None:
        body["inner_provider_settings"] = {
            key: value.get_secret_value() if isinstance(value, SecretStr) else value
            for key, value in request.inner_provider_settings.items()
        }
    return body
