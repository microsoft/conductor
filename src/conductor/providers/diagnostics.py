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

# Provider names that are known to the schema/factory but not yet implemented.
# Surfaced as an informational note, not an error.
_NOT_IMPLEMENTED: frozenset[str] = frozenset({"openai-agents"})

# Per-provider credential environment variables whose *presence* (never
# value) is reported in the offline diagnostic. Copilot authenticates via
# the GitHub/Copilot CLI login on disk, so its GitHub-token vars are
# best-effort hints rather than hard requirements.
_CREDENTIAL_ENV_VARS: dict[str, tuple[str, ...]] = {
    "copilot": (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_BEARER_TOKEN",
    ),
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "claude-agent-sdk": ("ANTHROPIC_API_KEY",),
    "hermes": (),
    "openai-agents": (),
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


@dataclass
class ModelDiagnostic:
    """Diagnostic snapshot of a single model's reasoning-effort and
    context-window capabilities (issue #301).

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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "id": self.id,
            "supported_reasoning_efforts": self.supported_reasoning_efforts,
            "default_reasoning_effort": self.default_reasoning_effort,
            "max_prompt_tokens": self.max_prompt_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_context_window_tokens": self.max_context_window_tokens,
        }


@dataclass
class ProviderDiagnostic:
    """Diagnostic snapshot for a single provider."""

    name: str
    installed: bool
    implemented: bool
    tier: str | None
    credential_env_vars: list[CredentialEnvVar] = field(default_factory=list)
    checked: bool = False
    connection_ok: bool | None = None
    connection_error: str | None = None
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
            "checked": self.checked,
            "connection_ok": self.connection_ok,
            "connection_error": self.connection_error,
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


def _credential_env_vars(name: str) -> list[CredentialEnvVar]:
    """Return presence flags for the provider's credential env vars."""
    return [
        CredentialEnvVar(var, bool(os.environ.get(var)))
        for var in _CREDENTIAL_ENV_VARS.get(name, ())
    ]


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


async def _build_model_diagnostics(provider: Any, model_ids: list[str]) -> list[ModelDiagnostic]:
    """Build a :class:`ModelDiagnostic` per model id (never raises).

    Calls ``provider.get_model_capabilities(model_id)`` for each id. A
    per-model failure degrades that model to id-only (all capability fields
    ``None``) rather than dropping it from the list or failing the whole
    ``--models`` probe — one bad model must not hide the rest.
    """
    result: list[ModelDiagnostic] = []
    for model_id in model_ids:
        caps = None
        try:
            caps = await provider.get_model_capabilities(model_id)
        except Exception as e:  # noqa: BLE001 - diagnostics must never raise
            logger.debug("Failed to get model capabilities for %r: %s", model_id, e)
        if caps is None:
            result.append(ModelDiagnostic(id=model_id))
        else:
            result.append(
                ModelDiagnostic(
                    id=model_id,
                    supported_reasoning_efforts=caps.supported_reasoning_efforts,
                    default_reasoning_effort=caps.default_reasoning_effort,
                    max_prompt_tokens=caps.max_prompt_tokens,
                    max_output_tokens=caps.max_output_tokens,
                    max_context_window_tokens=caps.max_context_window_tokens,
                )
            )
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

    diag = ProviderDiagnostic(
        name=name,
        installed=installed,
        implemented=implemented,
        tier=_provider_tier(name),
        credential_env_vars=_credential_env_vars(name),
        note=None if implemented else "not yet implemented",
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
        except Exception as e:  # noqa: BLE001 - diagnostics must never raise
            diag.connection_ok = False
            diag.connection_error = _format_error(e)

        if list_models and diag.connection_ok:
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
