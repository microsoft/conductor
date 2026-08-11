"""Provider & environment diagnostics for ``conductor doctor``.

Keyless, Typer-free data-gathering layer behind the ``conductor doctor``
command (issue #274). It answers "is my setup healthy?" without running a
workflow: which providers are installed, whether they can connect, what
models they expose, plus Conductor version / update status and configured
registries.

Design contract:

* **Never raises.** Every probe degrades gracefully — a missing SDK, an
  unreadable config file, or a failing connection is captured as data, not
  an exception. Callers can render whatever was gathered.
* **Offline by default.** No provider is instantiated and no backend is
  contacted unless ``check=True`` (connection probes) or ``list_models=True``
  (which implies a check). The only default network touch is the GitHub
  Releases update check in :func:`gather_env`, which is cache-first, uses a
  short timeout, fails silently, and honors ``CONDUCTOR_NO_UPDATE_CHECK``.
* **No secrets.** Credential environment variables are reported by
  *presence only* — their values are never read into the report.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from conductor import __version__
from conductor.providers.capabilities import get_capabilities, known_provider_names

if TYPE_CHECKING:
    from conductor.providers.factory import ProviderType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

Section = Literal["env", "providers", "registries"]
"""A ``conductor doctor`` output section."""

ALL_SECTIONS: tuple[Section, ...] = ("env", "providers", "registries")
"""Default set of sections rendered when no positional ``SECTION`` is given."""

PricingSource = Literal["provider", "table", "none"]
"""How a model's per-Mtok rates were resolved (see #386)."""

# Provider names that are known to the schema/factory but not yet implemented.
# Surfaced as an informational note, not an error.
_NOT_IMPLEMENTED: frozenset[str] = frozenset({"openai-agents"})

# One-shot latch so a systemically-raising get_model_pricing hook logs once
# per process rather than once per model (mirrors
# WorkflowEngine._pricing_hook_failed_warned in engine/workflow.py).
_pricing_hook_failed_warned = False


@dataclass(frozen=True)
class _CredentialSpec:
    """Static credential metadata for a provider's offline diagnostic.

    Only the *presence* of each var in ``env_vars`` is ever reported (never
    its value). ``optional_auth_note`` collapses two related facts into one
    field so they can't drift apart: ``None`` means the provider genuinely
    requires one of ``env_vars``; a non-``None`` string means the provider
    has a credential path outside these env vars (e.g. an on-disk CLI
    login), so an all-absent credentials cell for it is expected — not a
    misconfiguration — and the string is that path, surfaced in the
    rendered report so the absence doesn't read as "provider is broken"
    (issue #319). See ``optional`` for the derived boolean.
    """

    env_vars: tuple[str, ...] = ()
    optional_auth_note: str | None = None

    @property
    def optional(self) -> bool:
        """Whether every var in ``env_vars`` is an optional override."""
        return self.optional_auth_note is not None


# Per-provider credential environment variables and their offline-diagnostic
# semantics. See each entry's ``optional_auth_note`` for *why* that provider's
# vars are optional overrides rather than hard requirements.
_CREDENTIAL_SPECS: dict[str, _CredentialSpec] = {
    "copilot": _CredentialSpec(
        env_vars=(
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "COPILOT_PROVIDER_API_KEY",
            "COPILOT_PROVIDER_BEARER_TOKEN",
            "COPILOT_PROVIDER_RUNTIME_TOKEN",
        ),
        optional_auth_note=(
            "authenticates via GitHub/Copilot CLI login; env vars are optional overrides"
        ),
    ),
    "claude": _CredentialSpec(env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")),
    "claude-agent-sdk": _CredentialSpec(
        env_vars=("ANTHROPIC_API_KEY",),
        optional_auth_note=(
            "authenticates via `claude login`; ANTHROPIC_API_KEY is an optional override"
        ),
    ),
    "openai": _CredentialSpec(env_vars=("OPENAI_API_KEY",)),
    "hermes": _CredentialSpec(),
    "openai-agents": _CredentialSpec(),
}

# Update-check opt-out env var (mirrors cli/update.py so diagnostics does not
# depend on a private symbol there).
_UPDATE_DISABLE_ENV = "CONDUCTOR_NO_UPDATE_CHECK"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialEnvVar:
    """Presence of a single credential environment variable (value never read)."""

    name: str
    present: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {"name": self.name, "present": self.present}


@dataclass(frozen=True)
class ModelDiagnostic:
    """Diagnostic snapshot of a single model's reasoning-effort and
    context-window capabilities (issue #301).

    Frozen to match its sibling value objects (:class:`CredentialEnvVar`,
    :class:`~conductor.providers.base.ModelCapabilityInfo`) — every instance
    is fully constructed in one step by :func:`_build_model_diagnostics` and
    never mutated afterward.

    Every capability field mirrors :class:`~conductor.providers.base.ModelCapabilityInfo`
    and is independently optional — a provider may know a model's token
    limits but not its reasoning-effort support, or vice versa. ``None``
    means "unknown"; an empty ``supported_reasoning_efforts`` list means
    "known to support none" (e.g. a non-thinking Claude model) — the two
    are deliberately distinct.
    """

    id: str
    supported_reasoning_efforts: list[str] | None = None
    default_reasoning_effort: str | None = None
    max_prompt_tokens: int | None = None
    max_output_tokens: int | None = None
    max_context_window_tokens: int | None = None
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    pricing_source: PricingSource | None = None
    """How ``input_per_mtok``/``output_per_mtok`` were resolved (see #386):
    ``"provider"`` (live :meth:`~conductor.providers.base.AgentProvider.get_model_pricing`
    hook), ``"table"`` (static ``DEFAULT_PRICING`` fallback), ``"none"``
    (genuinely unpriced), or ``None`` (pricing resolution itself failed —
    distinct from the determined ``"none"``)."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "id": self.id,
            "supported_reasoning_efforts": self.supported_reasoning_efforts,
            "default_reasoning_effort": self.default_reasoning_effort,
            "max_prompt_tokens": self.max_prompt_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_context_window_tokens": self.max_context_window_tokens,
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "pricing_source": self.pricing_source,
        }


@dataclass
class ProviderDiagnostic:
    """Diagnostic snapshot for a single provider."""

    name: str
    installed: bool
    implemented: bool
    tier: str | None
    credential_env_vars: list[CredentialEnvVar] = field(default_factory=list)
    credentials_optional: bool = False
    checked: bool = False
    connection_ok: bool | None = None
    connection_error: str | None = None
    connection_note: str | None = None
    models: list[ModelDiagnostic] | None = None
    models_error: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "name": self.name,
            "installed": self.installed,
            "implemented": self.implemented,
            "tier": self.tier,
            "credential_env_vars": [c.to_dict() for c in self.credential_env_vars],
            "credentials_optional": self.credentials_optional,
            "checked": self.checked,
            "connection_ok": self.connection_ok,
            "connection_error": self.connection_error,
            "connection_note": self.connection_note,
            "models": [m.to_dict() for m in self.models] if self.models is not None else None,
            "models_error": self.models_error,
            "note": self.note,
        }


@dataclass
class EnvDiagnostic:
    """Diagnostic snapshot of the Conductor install and host environment."""

    conductor_version: str
    python_version: str
    platform: str
    update_checked: bool
    update_available: bool | None
    latest_version: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "conductor_version": self.conductor_version,
            "python_version": self.python_version,
            "platform": self.platform,
            "update_checked": self.update_checked,
            "update_available": self.update_available,
            "latest_version": self.latest_version,
        }


@dataclass
class RegistryInfo:
    """A single configured registry entry."""

    name: str
    type: str
    source: str
    is_default: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "name": self.name,
            "type": self.type,
            "source": self.source,
            "is_default": self.is_default,
        }


@dataclass
class RegistryDiagnostic:
    """Diagnostic snapshot of configured workflow registries."""

    default: str | None
    registries: list[RegistryInfo] = field(default_factory=list)
    error: str | None = None
    """Set when the registries config could not be loaded (e.g. malformed
    TOML). Distinguishes a load *failure* from a genuinely empty config so
    ``doctor`` surfaces the problem instead of reporting "no registries"."""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "default": self.default,
            "registries": [r.to_dict() for r in self.registries],
            "error": self.error,
        }


@dataclass
class DoctorReport:
    """Aggregated diagnostics. Sections not requested are left as ``None``."""

    env: EnvDiagnostic | None = None
    providers: list[ProviderDiagnostic] | None = None
    registries: RegistryDiagnostic | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation, omitting sections not gathered."""
        out: dict[str, Any] = {}
        if self.env is not None:
            out["env"] = self.env.to_dict()
        if self.providers is not None:
            out["providers"] = [p.to_dict() for p in self.providers]
        if self.registries is not None:
            out["registries"] = self.registries.to_dict()
        return out


# ---------------------------------------------------------------------------
# Small helpers (never raise)
# ---------------------------------------------------------------------------


def _format_error(exc: BaseException) -> str:
    """Render an exception as a compact one-line string for the report."""
    msg = str(exc).strip()
    return msg if msg else type(exc).__name__


def _sdk_available(name: str) -> bool:
    """Return the provider's SDK-availability flag, or ``False`` on any error."""
    try:
        if name == "copilot":
            from conductor.providers.copilot import COPILOT_SDK_AVAILABLE

            return COPILOT_SDK_AVAILABLE
        if name == "claude":
            from conductor.providers.claude import ANTHROPIC_SDK_AVAILABLE

            return ANTHROPIC_SDK_AVAILABLE
        if name == "claude-agent-sdk":
            from conductor.providers.claude_agent_sdk import CLAUDE_AGENT_SDK_AVAILABLE

            return CLAUDE_AGENT_SDK_AVAILABLE
        if name == "openai":
            from conductor.providers.openai import OPENAI_SDK_AVAILABLE

            return OPENAI_SDK_AVAILABLE
        if name == "hermes":
            from conductor.providers.hermes import HERMES_SDK_AVAILABLE

            return HERMES_SDK_AVAILABLE
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return False
    return False


def _provider_tier(name: str) -> str | None:
    """Return the provider's stability tier, or ``None`` when undeterminable."""
    try:
        return get_capabilities(name).tier
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return None


def _credential_env_vars(spec: _CredentialSpec) -> list[CredentialEnvVar]:
    """Return presence flags for the provider's credential env vars."""
    return [CredentialEnvVar(var, bool(os.environ.get(var))) for var in spec.env_vars]


def _update_check_disabled() -> bool:
    """Return ``True`` if the user opted out of update checks via env var."""
    val = os.environ.get(_UPDATE_DISABLE_ENV, "").strip().lower()
    return val in {"1", "true", "yes"}


def _check_update() -> tuple[bool, bool | None, str | None]:
    """Determine update availability (cache-first, silent, best-effort).

    Returns:
        A ``(checked, available, latest_version)`` tuple.
        ``checked`` is ``False`` when the check was skipped via
        ``CONDUCTOR_NO_UPDATE_CHECK``. When ``checked`` is ``True`` but the
        result could not be determined (offline / parse failure),
        ``available`` is ``None`` and ``latest_version`` is ``None``.
    """
    if _update_check_disabled():
        return False, None, None
    try:
        from conductor.cli.update import (
            fetch_latest_version,
            is_newer,
            read_cache,
            write_cache,
        )

        cached = read_cache()
        if cached is not None:
            remote = cached.get("version", "")
        else:
            result = fetch_latest_version()
            if result is None:
                return True, None, None
            remote, tag_name, url = result
            # Persisting the fetched version is best-effort: a non-writable
            # HOME (common in CI) must NOT discard an already-successful
            # fetch and misreport "offline".
            with contextlib.suppress(Exception):
                write_cache(remote, tag_name, url)
        if not remote:
            return True, None, None
        return True, is_newer(remote, __version__), remote
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return True, None, None


# ---------------------------------------------------------------------------
# Gather functions
# ---------------------------------------------------------------------------


def gather_env() -> EnvDiagnostic:
    """Gather Conductor version, host, and update-availability diagnostics."""
    checked, available, latest = _check_update()
    return EnvDiagnostic(
        conductor_version=__version__,
        python_version=platform.python_version(),
        platform=platform.platform(),
        update_checked=checked,
        update_available=available,
        latest_version=latest,
    )


def gather_registries() -> RegistryDiagnostic:
    """Gather configured workflow registries (never raises).

    A load failure (e.g. malformed ``registries.toml``) is captured in the
    returned ``error`` field rather than swallowed — a corrupt config must be
    surfaced, not reported as "no registries configured".
    """
    try:
        from conductor.registry.config import load_config

        config = load_config()
    except Exception as e:  # noqa: BLE001 - diagnostics must never raise
        return RegistryDiagnostic(default=None, registries=[], error=_format_error(e))

    registries = [
        RegistryInfo(
            name=reg_name,
            type=str(entry.type),
            source=entry.source,
            is_default=(reg_name == config.default),
        )
        for reg_name, entry in config.registries.items()
    ]
    return RegistryDiagnostic(default=config.default, registries=registries)


async def _resolve_model_pricing(
    provider: Any, model_id: str
) -> tuple[float | None, float | None, PricingSource | None]:
    """Resolve a model's per-Mtok rates and where they came from (never raises).

    Mirrors the cost-resolution chain in ``engine/pricing.py::get_pricing``
    (workflow override → provider hook → ``DEFAULT_PRICING`` → ``None``),
    minus the workflow override — ``doctor`` has no workflow context.

    Note ``get_pricing`` may resolve via its versioned-suffix fuzzy path and
    log a one-time warning (see #137). Any such warning is invisible during
    ``doctor`` because ``gather`` runs under ``_suppressed_logging``
    (``cli/doctor.py``) whenever pricing is resolved. The module-level
    fuzzy-match latch IS still mutated by that call — logging suppression
    has no effect on it — but that is harmless only because ``doctor`` is a
    standalone, short-lived CLI process (``gather`` has exactly one caller)
    that never shares a process with a workflow run.

    Returns:
        A ``(input_per_mtok, output_per_mtok, source)`` tuple. ``source`` is
        ``"provider"``, ``"table"``, or ``"none"`` when resolution completed
        (with rates ``None`` in the ``"none"`` case), or ``None`` when
        resolution itself failed — distinct from the determined ``"none"``.
    """
    from conductor.engine.pricing import get_pricing

    global _pricing_hook_failed_warned

    hook_failed = False
    provider_pricing = None
    try:
        provider_pricing = await provider.get_model_pricing(model_id)
    except Exception as e:  # noqa: BLE001 - diagnostics must never raise
        hook_failed = True
        logger.debug("Failed to resolve provider pricing for %r: %s", model_id, e)
        # A raising hook usually means a systemic break (SDK auth /
        # model-listing failure) rather than a per-model fluke, and doctor's
        # `_suppressed_logging` disables this logger entirely — so warn
        # once here too, matching the same one-shot warning
        # engine/workflow.py emits for the identical exception.
        if not _pricing_hook_failed_warned:
            _pricing_hook_failed_warned = True
            logger.warning(
                "Provider pricing hook failed for model %r (%s); live pricing is "
                "unavailable and rates will fall back to the static table or show "
                "as unresolvable. Further failures are logged at debug level.",
                model_id,
                e,
            )

    if provider_pricing is not None:
        return provider_pricing.input_per_mtok, provider_pricing.output_per_mtok, "provider"

    table_pricing = get_pricing(model_id)
    if table_pricing is not None:
        return table_pricing.input_per_mtok, table_pricing.output_per_mtok, "table"

    if hook_failed:
        return None, None, None

    return None, None, "none"


async def _build_model_diagnostics(provider: Any, model_ids: list[str]) -> list[ModelDiagnostic]:
    """Build a :class:`ModelDiagnostic` per model id (never raises).

    Calls ``provider.get_model_capabilities(model_id)`` for each id. A
    per-model failure degrades that model to id-only (all capability fields
    ``None``) rather than dropping it from the list or failing the whole
    ``--models`` probe — one bad model must not hide the rest.

    The ``try`` wraps both the call *and* the read of its result: a
    misbehaving provider that returns something other than a genuine
    ``ModelCapabilityInfo`` (or ``None``) must degrade only that one model,
    not raise out of the loop and silently discard every already-built
    entry for models processed earlier in this same list.

    Pricing (see #386) is resolved independently via :func:`_resolve_model_pricing`
    for every model, including one whose capabilities call failed — a
    degraded capabilities row still gets its pricing columns.
    """
    result: list[ModelDiagnostic] = []
    for model_id in model_ids:
        input_per_mtok, output_per_mtok, pricing_source = await _resolve_model_pricing(
            provider, model_id
        )
        pricing_fields: dict[str, Any] = {
            "input_per_mtok": input_per_mtok,
            "output_per_mtok": output_per_mtok,
            "pricing_source": pricing_source,
        }
        try:
            caps = await provider.get_model_capabilities(model_id)
            # ModelCapabilityInfo.to_dict() keys mirror ModelDiagnostic's
            # capability fields exactly, so it doubles as the kwargs for
            # constructing this model's diagnostic. The try wraps both the
            # call and the construction below: a misbehaving provider whose
            # return value lacks to_dict() (or isn't None) is caught below
            # and degrades to id-only, same as any other failure.
            fields = caps.to_dict() if caps is not None else {}
            result.append(ModelDiagnostic(id=model_id, **pricing_fields, **fields))
        except Exception as e:  # noqa: BLE001 - diagnostics must never raise
            logger.debug("Failed to get model capabilities for %r: %s", model_id, e)
            result.append(ModelDiagnostic(id=model_id, **pricing_fields))
    return result


async def gather_provider(
    name: str,
    *,
    check: bool = False,
    list_models: bool = False,
) -> ProviderDiagnostic:
    """Gather diagnostics for a single provider (never raises).

    Offline fields (``installed`` / ``tier`` / credential presence) are
    always populated. When ``check`` (or ``list_models``, which implies a
    check) is set and the provider is implemented and installed, the
    provider is constructed and ``validate_connection()`` is called; with
    ``list_models`` its ``list_models()`` is also queried.

    Args:
        name: Provider name (e.g. ``"copilot"``).
        check: Instantiate the provider and probe ``validate_connection()``.
        list_models: Also enumerate available models (implies ``check``).

    Returns:
        A fully-populated :class:`ProviderDiagnostic`.
    """
    implemented = name not in _NOT_IMPLEMENTED
    installed = _sdk_available(name) if implemented else False
    spec = _CREDENTIAL_SPECS.get(name, _CredentialSpec())

    diag = ProviderDiagnostic(
        name=name,
        installed=installed,
        implemented=implemented,
        tier=_provider_tier(name),
        credential_env_vars=_credential_env_vars(spec),
        credentials_optional=spec.optional,
        # "not yet implemented" always wins over a provider's own credential
        # note — there is nothing useful to say about a provider's auth path
        # when it can't be instantiated at all.
        note="not yet implemented" if not implemented else spec.optional_auth_note,
    )

    do_check = check or list_models
    if not do_check or not implemented:
        return diag

    diag.checked = True
    if not installed:
        diag.connection_ok = False
        diag.connection_error = "SDK not installed"
        return diag

    from conductor.providers.factory import create_provider

    provider = None
    try:
        provider = await create_provider(cast("ProviderType", name), validate=False)
    except Exception as e:  # noqa: BLE001 - diagnostics must never raise
        diag.connection_ok = False
        diag.connection_error = _format_error(e)
        return diag

    try:
        try:
            diag.connection_ok = bool(await provider.validate_connection())
            # Some providers (e.g. claude, issue #455) return True for a
            # merely inconclusive probe (an endpoint that doesn't implement
            # model listing). connection_note carries that caveat so a
            # renderer doesn't claim "connected" when nothing was verified.
            # Restricted to str: an AsyncMock/Mock-based test double without
            # this attribute set auto-creates a truthy Mock via getattr(),
            # which is not a real note.
            note = getattr(provider, "_connection_probe_note", None)
            diag.connection_note = note if isinstance(note, str) else None
        except Exception as e:  # noqa: BLE001 - diagnostics must never raise
            diag.connection_ok = False
            diag.connection_error = _format_error(e)

        # Gate on a verified (not merely truthy) connection: an inconclusive
        # probe means models.list() already failed once, so calling
        # list_models() here would just be a second guaranteed-failing
        # round-trip.
        if list_models and diag.connection_ok and diag.connection_note is None:
            try:
                model_ids = await provider.list_models()
                diag.models = (
                    await _build_model_diagnostics(provider, model_ids)
                    if model_ids is not None
                    else None
                )
            except Exception as e:  # noqa: BLE001 - diagnostics must never raise
                diag.models_error = _format_error(e)
    finally:
        with contextlib.suppress(Exception):
            await provider.close()

    return diag


async def gather(
    *,
    sections: tuple[Section, ...] = ALL_SECTIONS,
    provider: str | None = None,
    check: bool = False,
    list_models: bool = False,
) -> DoctorReport:
    """Gather a full :class:`DoctorReport` for the requested sections.

    Args:
        sections: Which sections to include. Defaults to all.
        provider: When set, scope the ``providers`` section to this one name.
        check: Probe provider connections (``providers`` section only).
        list_models: Enumerate provider models (implies ``check``).

    Returns:
        A :class:`DoctorReport`; sections not requested remain ``None``.
    """
    report = DoctorReport()

    if "env" in sections:
        report.env = gather_env()

    if "providers" in sections:
        names = [provider] if provider is not None else list(known_provider_names())
        report.providers = [
            await gather_provider(pname, check=check, list_models=list_models) for pname in names
        ]

    if "registries" in sections:
        report.registries = gather_registries()

    return report


__all__ = [
    "ALL_SECTIONS",
    "CredentialEnvVar",
    "DoctorReport",
    "EnvDiagnostic",
    "ModelDiagnostic",
    "ProviderDiagnostic",
    "RegistryDiagnostic",
    "RegistryInfo",
    "Section",
    "gather",
    "gather_env",
    "gather_provider",
    "gather_registries",
]
