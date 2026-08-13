"""Transport-token gate and request-body narrowing for the runner (issue #396).

A small leaf module (stdlib + Pydantic + `conductor.web.auth` +
`conductor.providers.aca_protocol`), matching the repository's convention of
single-purpose leaves (`rundir.py`, `console.py`, `duration.py`,
`web/auth.py`). Keeps `server.py` focused on the wire contract while giving
this policy a directly unit-testable surface. `RUNNER_TOKEN_HEADER` itself is
defined in `aca_protocol.py` (a genuine leaf both sides already import) and
re-exported here, so the runner (`server.py`) and the host provider
(`providers/aca.py`) share the same header name without either side dragging
the other's dependency tree in.

Five independent hardening layers, none individually load-bearing (mirroring
`web/auth.py`'s own framing for issue #397):

1. Bind loopback by default (see `aca_runner/__main__.py`) — narrows the
   attack surface for an ad-hoc local run; the container image sets
   `ACA_RUNNER_HOST=0.0.0.0` explicitly, so it is unaffected.
2. An optional transport-token gate on `/execute`, via
   `RUNNER_TOKEN_HEADER`, opt-in via `ACA_RUNNER_AUTH_TOKEN`. `/health`
   stays unauthenticated — the image `HEALTHCHECK` sends no header at all.
3. `/health` reports `auth_required` / `auth_token_present` (presence, never
   validity) so an operator can detect a gateway that strips custom headers
   before relying on the gate.
4. A four-key allowlist on `inner_provider_settings` (`check_inner_provider_settings`)
   plus an optional `base_url` allowlist (`ACA_RUNNER_ALLOWED_BASE_URLS`).
5. `identifier` remains gateway routing metadata, not a caller-authentication
   signal — deliberately not enforced here; see `server.py`'s endpoint
   docstrings.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import SecretStr

from conductor.exceptions import ProviderError
from conductor.providers.aca_protocol import RUNNER_TOKEN_HEADER
from conductor.web.auth import constant_time_match

__all__ = [
    "RUNNER_TOKEN_HEADER",
    "ALLOWED_INNER_PROVIDER_SETTINGS_KEYS",
    "resolve_runner_token",
    "resolve_allowed_base_urls",
    "check_inner_provider_settings",
    "token_gate",
]

# The four `inner_provider_settings` keys `AcaRuntimeProvider
# ._resolve_inner_provider_settings` actually produces: the BYOK branch
# returns `base_url`/`api_key`/`bearer_token`, the default branch returns
# `github_token`. Anything else (`runtime_url`, `headers`, `type`,
# `wire_api`, `azure`, ...) would let a caller repoint the inner Copilot
# session at an arbitrary external runtime or inject arbitrary HTTP headers
# — neither of which any `base_url` allowlist alone would catch.
ALLOWED_INNER_PROVIDER_SETTINGS_KEYS = frozenset(
    {"base_url", "api_key", "bearer_token", "github_token"}
)


def _clean_env(name: str) -> str | None:
    """Read an environment variable, normalizing unset/whitespace-only values to `None`.

    Deliberately duplicated from `providers/aca.py::_clean_env` rather than
    imported: importing `providers.aca` would drag `httpx`/`azure-identity`
    into the runner's startup path, which must stay lightweight (it runs
    in-container, not on the host doing the AAD/httpx work).
    """
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def resolve_runner_token() -> str | None:
    """Return the configured runner auth token, or `None` when the gate is off.

    Reads `ACA_RUNNER_AUTH_TOKEN` (opt-in — see module docstring). Unset,
    empty, and whitespace-only values are all treated as "not configured".
    """
    return _clean_env("ACA_RUNNER_AUTH_TOKEN")


def resolve_allowed_base_urls() -> tuple[str, ...] | None:
    """Return the configured `base_url` allowlist, or `None` for "no restriction".

    Reads `ACA_RUNNER_ALLOWED_BASE_URLS` as a comma-separated list; each
    entry is stripped and empty entries are dropped. `None` (the env var
    unset, or set to only whitespace/commas) means every `base_url` is
    admitted — this is a narrowing control, not a default-deny one.
    """
    raw = os.environ.get("ACA_RUNNER_ALLOWED_BASE_URLS")
    if raw is None:
        return None
    entries = tuple(entry.strip() for entry in raw.split(",") if entry.strip())
    return entries or None


def _unwrap(value: Any) -> Any:
    """Unwrap a `SecretStr`-wrapped value to plain text for comparison.

    `AcaExecuteRequest._redact_inner_provider_secrets` wraps known credential
    keys in `SecretStr` before the runner ever sees them; `base_url` is not a
    secret key, so it is never wrapped here, but it also arrives typed as
    `Any` — a caller can send any JSON value, not necessarily a `str` — so
    this unwrap is applied uniformly rather than assuming the shape.
    """
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def check_inner_provider_settings(
    settings: dict[str, Any] | None,
    *,
    allowed_base_urls: tuple[str, ...] | None,
) -> None:
    """Reject any `inner_provider_settings` key/value the host doesn't send.

    Raises `ProviderError` naming the offending key(s) for anything outside
    `ALLOWED_INNER_PROVIDER_SETTINGS_KEYS` — this is what closes off
    `runtime_url` (repoint the inner Copilot session at an arbitrary
    external runtime) and `headers` (inject arbitrary HTTP headers), which
    no `base_url` allowlist alone would catch. When `allowed_base_urls` is
    given, a present `base_url` must exactly match one of its entries after
    stripping a trailing `/` from both sides (so a trailing-slash mismatch
    isn't a false rejection).

    Args:
        settings: The request's `inner_provider_settings`, or `None`.
        allowed_base_urls: The configured allowlist, or `None` for no
            restriction (see `resolve_allowed_base_urls`).

    Raises:
        ProviderError: When an unrecognized key is present, or `base_url` is
            set but not in `allowed_base_urls`.
    """
    if not settings:
        return

    disallowed = sorted(set(settings) - ALLOWED_INNER_PROVIDER_SETTINGS_KEYS)
    if disallowed:
        raise ProviderError(
            f"aca runner: unsupported inner_provider_settings key(s): {', '.join(disallowed)}.",
            suggestion=(
                "The runner only accepts "
                f"{sorted(ALLOWED_INNER_PROVIDER_SETTINGS_KEYS)} in "
                "inner_provider_settings."
            ),
            provider_name="aca",
            is_retryable=False,
        )

    base_url = _unwrap(settings.get("base_url"))
    if base_url is not None and not isinstance(base_url, str):
        raise ProviderError(
            f"aca runner: inner_provider_settings.base_url must be a string, "
            f"got {type(base_url).__name__}.",
            suggestion="Send base_url as a JSON string, or omit it.",
            provider_name="aca",
            is_retryable=False,
        )

    if allowed_base_urls is None:
        return
    if base_url is None:
        return
    normalized = base_url.rstrip("/")
    normalized_allowed = {entry.rstrip("/") for entry in allowed_base_urls}
    if normalized not in normalized_allowed:
        raise ProviderError(
            f"aca runner: base_url {base_url!r} is not in the configured allowlist.",
            suggestion=("Add the base_url to ACA_RUNNER_ALLOWED_BASE_URLS on the runner pool."),
            provider_name="aca",
            is_retryable=False,
        )


def token_gate(presented: str | None, expected: str | None) -> bool:
    """Return whether `presented` satisfies the runner's transport-token gate.

    When `expected` is `None` (the gate is disabled — `ACA_RUNNER_AUTH_TOKEN`
    unset), every request passes, preserving the zero-configuration default.
    Otherwise delegates to `conductor.web.auth.constant_time_match`, which
    encodes with `surrogatepass` so a non-ASCII presented value cannot raise
    instead of simply failing the comparison.

    Args:
        presented: The value of the `X-Conductor-Runner-Token` header, or
            `None` if absent.
        expected: The configured token, or `None` if the gate is disabled.

    Returns:
        `True` when the gate is disabled or `presented` matches `expected`.
    """
    if expected is None:
        return True
    return constant_time_match(presented, expected)
