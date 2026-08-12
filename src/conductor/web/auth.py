"""Origin/Host validation and token auth for the web dashboard (issue #397).

A leaf module (imports only ``conductor.rundir`` + stdlib + Starlette) so
both :mod:`conductor.web.server` and :mod:`conductor.web.replay` can share
it, and so the CLI (``cli/app.py``, ``cli/gate.py``, ``cli/guide.py``) can
resolve the same token without importing the web server.

Three layers, none of them individually load-bearing:

1. :class:`OriginHostGuard` — a pure-ASGI middleware (not Starlette's
   ``BaseHTTPMiddleware`` / ``@app.middleware("http")``, neither of which
   ever sees WebSocket scopes) enforcing Origin/Host validation on every
   HTTP and WebSocket request, then token auth on mutating HTTP routes and
   the WebSocket handshake.
2. A per-run token, minted automatically so the protected configuration is
   the default. ``CONDUCTOR_GATE_TOKEN`` overrides it when set (the
   documented escape hatch predating this change).
3. A token *file* at ``~/.conductor/runs/dashboard-<port>.token`` (mode
   ``0600``) so a CLI invocation with no explicit ``--token`` and no env
   var set can still discover the running dashboard's token.

Read-only routes (``GET /api/state``, ``/api/info``, ``/api/logs``,
``/api/gate-status``, ``/api/files/*``, the replay app) are protected by
Origin/Host only — no token. ``GET /`` also needs no token to *view*: it
injects the token into the page for the *frontend* to present on
subsequent mutating requests and the WebSocket handshake.
"""

from __future__ import annotations

import hmac
import os
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from conductor import rundir

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

# Env var extending the origin allowlist for local dev servers (e.g. Vite's
# ``http://localhost:5173``). Comma-separated list of full origins
# (``scheme://host:port``). Nothing is disabled by setting this — every
# request still needs a matching Host and a valid token.
_ALLOW_ORIGINS_ENV = "CONDUCTOR_WEB_ALLOW_ORIGINS"

# Same env var the pre-existing gate-token check already used. Preserved
# here so setting it continues to override the per-run minted token.
_GATE_TOKEN_ENV = "CONDUCTOR_GATE_TOKEN"

_DEFAULT_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


# --------------------------------------------------------------------------- #
# Token minting, resolution and file storage
# --------------------------------------------------------------------------- #


def mint_token() -> str:
    """Return a fresh, high-entropy per-run token."""
    return secrets.token_urlsafe(32)


def resolve_expected_token(minted: str) -> str:
    """Return the token that requests must present.

    ``CONDUCTOR_GATE_TOKEN`` preserves the documented override; otherwise
    the automatically-minted per-run token is used.

    Args:
        minted: The token minted for this run (see :func:`mint_token`).

    Returns:
        The token value requests are checked against.
    """
    return os.environ.get(_GATE_TOKEN_ENV) or minted


def token_file_path(port: int) -> Path:
    """Return the path of the token file for a dashboard bound to ``port``."""
    return rundir.runs_dir() / f"dashboard-{port}.token"


def write_token_file(port: int, token: str) -> Path:
    """Write the dashboard token file for ``port``, mode ``0600``.

    Written atomically (temp file + ``os.replace``), mirroring
    ``cli/pid.py::write_pid_file``'s reasoning: a reader must never observe
    a partially-written token.

    Args:
        port: The bound dashboard port.
        token: The token to write.

    Returns:
        The path of the written file.
    """
    path = token_file_path(port)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def read_token_file(port: int) -> str | None:
    """Read the dashboard token file for ``port``, or None if absent/unreadable."""
    try:
        return token_file_path(port).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None


def remove_token_file(port: int) -> None:
    """Remove the dashboard token file for ``port``, if present."""
    token_file_path(port).unlink(missing_ok=True)


def resolve_cli_token(port: int, token: str | None) -> str | None:
    """Resolve the token a CLI command should present, in precedence order.

    Order: an explicit ``--token`` flag, then ``CONDUCTOR_GATE_TOKEN``, then
    the token file for ``port``.

    Args:
        port: The dashboard port the request targets.
        token: The value of an explicit ``--token`` flag, if any.

    Returns:
        The resolved token, or None if none of the three sources has one.
    """
    return token or os.environ.get(_GATE_TOKEN_ENV) or read_token_file(port)


# --------------------------------------------------------------------------- #
# Origin / Host validation
# --------------------------------------------------------------------------- #


def _parse_host_header(value: str) -> tuple[str, str | None]:
    """Split a ``Host`` header into ``(name, port)``, handling IPv6 brackets.

    Args:
        value: The raw ``Host`` header value (e.g. ``127.0.0.1:8080``,
            ``[::1]:8080``, or a bare name with no port).

    Returns:
        A ``(name, port)`` tuple. ``port`` is None when the header carries
        no port.
    """
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        name = value[: end + 1]
        rest = value[end + 1 :]
        if rest.startswith(":"):
            return name, rest[1:]
        return name, None
    if ":" in value:
        name, _, port = value.rpartition(":")
        return name, port
    return value, None


def _host_name_candidates(bound_host: str) -> set[str]:
    """Return the set of hostnames considered "this machine" for ``bound_host``.

    Always includes the loopback aliases a browser or CLI tool might use
    (``127.0.0.1``, ``localhost``, ``::1``/``[::1]``), plus the configured
    bind host itself (bracketed if it looks like a raw IPv6 literal).
    """
    candidates = {"127.0.0.1", "localhost", "[::1]"}
    if bound_host and bound_host not in ("0.0.0.0", "::", ""):
        if ":" in bound_host and not bound_host.startswith("["):
            candidates.add(f"[{bound_host}]")
        else:
            candidates.add(bound_host)
    return candidates


def allowed_hosts(bound_host: str, bound_port: int) -> set[str]:
    """Return the set of acceptable ``Host`` header values (``name:port``)."""
    return {f"{name}:{bound_port}" for name in _host_name_candidates(bound_host)}


def _extra_allowed_origins() -> set[str]:
    """Return the origins added via ``CONDUCTOR_WEB_ALLOW_ORIGINS`` (dev escape hatch)."""
    raw = os.environ.get(_ALLOW_ORIGINS_ENV, "")
    return {o.strip() for o in raw.split(",") if o.strip()}


def allowed_origins(bound_host: str, bound_port: int) -> set[str]:
    """Return the set of acceptable ``Origin`` header values.

    Includes ``http://<name>:<port>`` for every loopback alias and the
    configured bind host, plus anything declared via
    ``CONDUCTOR_WEB_ALLOW_ORIGINS``.
    """
    origins = {f"http://{name}:{bound_port}" for name in _host_name_candidates(bound_host)}
    origins |= _extra_allowed_origins()
    return origins


def check_origin_host(headers: Mapping[str, str], bound_host: str, bound_port: int) -> str | None:
    """Validate the ``Host`` and (if present) ``Origin`` headers.

    ``Host`` is required and must name this machine; its port must match
    ``bound_port`` unless ``bound_port`` is falsy (still unresolved — the
    narrow startup window before the server socket has bound).

    A present ``Origin`` must match ``bound_port``'s ``http://`` origin (or
    an entry in ``CONDUCTOR_WEB_ALLOW_ORIGINS``). An **absent** ``Origin``
    is allowed: httpx, curl, and ``conductor gate respond`` send none, and
    the "no extra setup" acceptance criterion requires that path to keep
    working.

    Args:
        headers: Request headers (case-insensitive mapping, e.g. Starlette's
            ``Headers``).
        bound_host: The host the dashboard is bound to.
        bound_port: The resolved dashboard port, or a falsy value if not
            yet known.

    Returns:
        None if the request passes, otherwise a human-readable reason.
    """
    host = headers.get("host")
    if not host:
        return "Missing Host header"

    name, port_str = _parse_host_header(host)
    if name not in _host_name_candidates(bound_host):
        return f"Host '{host}' is not allowed"
    if bound_port and port_str != str(bound_port):
        return f"Host '{host}' is not allowed"

    origin = headers.get("origin")
    if origin:
        if origin in _extra_allowed_origins():
            return None
        if bound_port:
            if origin not in {
                f"http://{n}:{bound_port}" for n in _host_name_candidates(bound_host)
            }:
                return f"Origin '{origin}' is not allowed"
        else:
            split = urlsplit(origin)
            origin_name = split.hostname or ""
            if split.port is not None and (":" in origin_name and not origin_name.startswith("[")):
                origin_name = f"[{origin_name}]"
            if origin_name not in _host_name_candidates(bound_host):
                return f"Origin '{origin}' is not allowed"

    return None


# --------------------------------------------------------------------------- #
# Token extraction from a request/connection
# --------------------------------------------------------------------------- #


def token_from_scope(scope: Scope) -> str | None:
    """Extract a bearer token from an ASGI scope.

    Checks the ``Authorization: Bearer <token>`` header first, then the
    ``token`` query parameter — the latter exists because browsers cannot
    set arbitrary headers on a WebSocket handshake. Query-param exposure is
    acceptable here because both dashboard apps run uvicorn with
    ``log_level="warning"``, so access logs (which would otherwise capture
    the query string) are off.

    Args:
        scope: The ASGI scope (``http`` or ``websocket``).

    Returns:
        The presented token, or None if absent.
    """
    headers = Headers(scope=scope)
    auth = headers.get("authorization")
    if auth:
        scheme, _, presented = auth.partition(" ")
        if scheme.lower() == "bearer" and presented:
            return presented

    query_string = scope.get("query_string", b"")
    if query_string:
        params = parse_qs(query_string.decode("utf-8", errors="replace"))
        values = params.get("token")
        if values:
            return values[0]

    return None


def constant_time_match(presented: str | None, expected: str) -> bool:
    """Compare a presented token against the expected one in constant time."""
    if presented is None:
        return False
    return hmac.compare_digest(presented, expected)


# --------------------------------------------------------------------------- #
# ASGI middleware
# --------------------------------------------------------------------------- #


async def _send_json_error(send: Send, status_code: int, error: str) -> None:
    """Send a minimal JSON error response directly via raw ASGI messages.

    Deliberately not ``JSONResponse.__call__`` (which also accepts a
    ``scope``/``receive``): those aren't needed for a body-only response,
    and constructing raw messages avoids depending on Starlette's response
    internals inside a middleware that runs before routing.
    """
    body = JSONResponse({"error": error}, status_code=status_code)
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": body.raw_headers,
        }
    )
    await send({"type": "http.response.body", "body": body.body})


async def _reject_websocket(receive: Receive, send: Send, code: int = 1008) -> None:
    """Close a WebSocket handshake before ``accept()``.

    Receives the pending ``websocket.connect`` message (ASGI requires it be
    consumed) and replies with ``websocket.close`` instead of
    ``websocket.accept``, which surfaces to the browser as a failed
    handshake — the socket never reaches ``accept()``, so it can never send
    any message type.
    """
    await receive()
    await send({"type": "websocket.close", "code": code})


class OriginHostGuard:
    """Pure-ASGI middleware enforcing Origin/Host validation and token auth.

    Registered via ``app.add_middleware(OriginHostGuard, ...)`` rather than
    Starlette's ``BaseHTTPMiddleware`` (and therefore rather than
    ``@app.middleware("http")``), neither of which ever sees WebSocket
    scopes — a decorator-style middleware would leave ``/ws`` completely
    unguarded. Runs on every ``http``/``websocket`` scope; other scope
    types (``lifespan``) pass through untouched.

    Args:
        app: The wrapped ASGI application.
        get_bound: Callable returning ``(host, port)`` for the current bind.
            A callable rather than a fixed value because the port is
            unknown at app-construction time (``port=0`` resolves only
            after the socket binds).
        get_expected_token: Callable returning the token requests must
            present. Lazy for the same reason as ``get_bound``.
        protected_paths: HTTP paths that require token auth + a JSON
            ``Content-Type`` when the request method is mutating.
        websocket_paths: WebSocket paths that require token auth at the
            handshake.
        mutating_methods: HTTP methods considered mutating.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        get_bound: Callable[[], tuple[str, int]],
        get_expected_token: Callable[[], str],
        protected_paths: frozenset[str] = frozenset(),
        websocket_paths: frozenset[str] = frozenset(),
        mutating_methods: frozenset[str] = _DEFAULT_MUTATING_METHODS,
    ) -> None:
        self.app = app
        self._get_bound = get_bound
        self._get_expected_token = get_expected_token
        self._protected_paths = protected_paths
        self._websocket_paths = websocket_paths
        self._mutating_methods = mutating_methods

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        bound_host, bound_port = self._get_bound()
        reason = check_origin_host(headers, bound_host, bound_port)
        if reason is not None:
            if scope["type"] == "websocket":
                await _reject_websocket(receive, send)
            else:
                await _send_json_error(send, 403, reason)
            return

        path = scope["path"]

        if scope["type"] == "websocket":
            if path in self._websocket_paths:
                expected = self._get_expected_token()
                presented = token_from_scope(scope)
                if not constant_time_match(presented, expected):
                    await _reject_websocket(receive, send)
                    return
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        if path in self._protected_paths and method in self._mutating_methods:
            expected = self._get_expected_token()
            presented = token_from_scope(scope)
            if not constant_time_match(presented, expected):
                await _send_json_error(send, 403, "Invalid or missing token")
                return
            content_type = headers.get("content-type", "")
            if content_type.split(";")[0].strip().lower() != "application/json":
                await _send_json_error(send, 415, "Content-Type must be application/json")
                return

        await self.app(scope, receive, send)
