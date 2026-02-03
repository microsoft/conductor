"""MCP OAuth authentication helpers.

This module provides functions to discover OAuth requirements for HTTP MCP servers
and fetch Azure AD tokens automatically, similar to how VS Code handles authentication.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.request
from typing import Any
from urllib.error import URLError


async def discover_oauth_requirements(url: str) -> dict[str, Any] | None:
    """Discover OAuth requirements for an HTTP MCP server.

    Checks for a .well-known/oauth-protected-resource endpoint to determine
    if the server requires OAuth authentication.

    Args:
        url: The base URL of the MCP server.

    Returns:
        OAuth metadata dict if the server requires OAuth, None otherwise.
        The dict contains 'resource', 'authorization_servers', and 'scopes_supported'.
    """
    # Ensure URL ends with /
    base_url = url.rstrip("/") + "/"
    well_known_url = f"{base_url}.well-known/oauth-protected-resource/"

    def fetch_metadata() -> dict[str, Any] | None:
        try:
            req = urllib.request.Request(well_known_url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, json.JSONDecodeError):
            pass
        return None

    return await asyncio.to_thread(fetch_metadata)


def get_azure_token(scope: str) -> str | None:
    """Get an Azure AD token using the Azure CLI.

    Args:
        scope: The OAuth scope to request (e.g., 'api://xxx/mcp-user').

    Returns:
        The access token string, or None if token acquisition fails.
    """
    try:
        result = subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--scope",
                scope,
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


async def get_mcp_oauth_headers(url: str, name: str) -> dict[str, str]:
    """Get OAuth headers for an HTTP MCP server if required.

    Discovers OAuth requirements and fetches an Azure AD token if needed.

    Args:
        url: The base URL of the MCP server.
        name: The name of the MCP server (for logging).

    Returns:
        Dict with Authorization header if OAuth is required and token
        acquisition succeeds, empty dict otherwise.
    """
    # Import here to avoid circular dependency
    from conductor.cli.run import verbose_log

    # Discover OAuth requirements
    oauth_metadata = await discover_oauth_requirements(url)
    if not oauth_metadata:
        return {}

    # Extract the scope from metadata
    scopes = oauth_metadata.get("scopes_supported", [])
    if not scopes:
        verbose_log(f"MCP server '{name}' requires OAuth but no scopes defined", style="yellow")
        return {}

    # Use the first scope (typically the main access scope)
    scope = scopes[0]
    verbose_log(f"MCP server '{name}' requires OAuth, fetching token for scope: {scope}")

    # Get Azure AD token
    token = await asyncio.to_thread(get_azure_token, scope)
    if not token:
        verbose_log(
            f"Failed to get Azure AD token for '{name}'. Run 'az login' first.", style="yellow"
        )
        return {}

    verbose_log(f"Successfully acquired OAuth token for '{name}'")
    return {"Authorization": f"Bearer {token}"}


async def resolve_mcp_server_auth(
    name: str,
    server_config: dict[str, Any],
) -> dict[str, Any]:
    """Resolve authentication for an HTTP/SSE MCP server.

    If the server requires OAuth and no Authorization header is provided,
    attempts to discover OAuth requirements and fetch a token.

    Args:
        name: The name of the MCP server.
        server_config: The server configuration dict.

    Returns:
        Updated server configuration with headers added if needed.
    """
    # Only process http/sse servers
    server_type = server_config.get("type", "stdio")
    if server_type not in ("http", "sse"):
        return server_config

    # Skip if Authorization header already provided
    headers = server_config.get("headers", {})
    if "Authorization" in headers or "authorization" in headers:
        return server_config

    url = server_config.get("url")
    if not url:
        return server_config

    # Try to get OAuth headers
    oauth_headers = await get_mcp_oauth_headers(url, name)
    if oauth_headers:
        # Merge OAuth headers with existing headers
        updated_config = server_config.copy()
        updated_config["headers"] = {**headers, **oauth_headers}
        return updated_config

    return server_config
