"""Helpers for fetching files, tags, and directory contents from GitHub repos.

Supports both public and private repos. Authentication is resolved
automatically via the ``gh`` CLI (``gh auth token``) when available.
"""

from __future__ import annotations

import subprocess

import httpx

from conductor.registry.errors import RegistryError

GITHUB_RAW_BASE = "https://raw.githubusercontent.com"
GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0

_HEADERS = {"User-Agent": "conductor-cli"}
_API_HEADERS = {
    **_HEADERS,
    "Accept": "application/vnd.github.v3+json",
}


def _get_auth_token() -> str | None:
    """Attempt to get a GitHub token from the ``gh`` CLI.

    Returns:
        A token string, or ``None`` if the ``gh`` CLI is not available or
        not authenticated.
    """
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _build_headers(*, api: bool = False) -> dict[str, str]:
    """Build request headers, adding auth if a token is available."""
    base = dict(_API_HEADERS if api else _HEADERS)
    token = _get_auth_token()
    if token:
        base["Authorization"] = f"Bearer {token}"
    return base


def _raise_for_status(response: httpx.Response, *, context: str) -> None:
    """Check response status and raise RegistryError with helpful messages."""
    if response.is_success:
        return
    status = response.status_code
    if status == 404:
        raise RegistryError(
            f"{context}: not found (404). Check that the repository exists and the ref is valid.",
            suggestion="If this is a private repo, ensure 'gh auth login' has been run.",
        )
    if status in (403, 429):
        raise RegistryError(
            f"{context}: HTTP {status}. GitHub API rate limit may be exceeded. Try again later."
        )
    raise RegistryError(f"{context}: HTTP {status}")


def fetch_file(owner: str, repo: str, path: str, ref: str = "main") -> bytes:
    """Fetch a single file from a GitHub repo at a given ref.

    Uses raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>.

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: File path within the repo.
        ref: Git ref — branch, tag, or commit SHA. Defaults to "main".

    Returns:
        Raw file content as bytes.

    Raises:
        RegistryError: If the file is not found (404) or request fails.
    """
    url = f"{GITHUB_RAW_BASE}/{owner}/{repo}/{ref}/{path}"
    try:
        response = httpx.get(
            url, headers=_build_headers(), timeout=DEFAULT_TIMEOUT, follow_redirects=True
        )
    except httpx.TimeoutException as exc:
        raise RegistryError(f"Timeout fetching {owner}/{repo}/{path} at ref {ref}") from exc
    except httpx.HTTPError as exc:
        raise RegistryError(f"HTTP error fetching {owner}/{repo}/{path}: {exc}") from exc

    _raise_for_status(response, context=f"Fetching {owner}/{repo}/{path} at ref {ref}")
    return response.content


def fetch_file_text(owner: str, repo: str, path: str, ref: str = "main") -> str:
    """Like fetch_file but returns decoded text (UTF-8).

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: File path within the repo.
        ref: Git ref — branch, tag, or commit SHA. Defaults to "main".

    Returns:
        File content decoded as UTF-8 text.

    Raises:
        RegistryError: If the file is not found (404) or request fails.
    """
    return fetch_file(owner, repo, path, ref).decode("utf-8")


def list_tags(owner: str, repo: str) -> list[str]:
    """List all git tags for a repository, newest first.

    Uses GET /repos/{owner}/{repo}/tags from the GitHub REST API.
    Returns just the tag names as strings.
    No pagination in v1 — returns first page (up to 100).

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        List of tag name strings, newest first.

    Raises:
        RegistryError: If the API request fails.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/tags"
    try:
        response = httpx.get(
            url, headers=_build_headers(api=True), timeout=DEFAULT_TIMEOUT, follow_redirects=True
        )
    except httpx.TimeoutException as exc:
        raise RegistryError(f"Timeout listing tags for {owner}/{repo}") from exc
    except httpx.HTTPError as exc:
        raise RegistryError(f"HTTP error listing tags for {owner}/{repo}: {exc}") from exc

    _raise_for_status(response, context=f"Listing tags for {owner}/{repo}")
    return [tag["name"] for tag in response.json()]


def list_directory(owner: str, repo: str, path: str, ref: str = "main") -> list[str]:
    """List files in a directory of a GitHub repo.

    Uses the Contents API:
    GET /repos/{owner}/{repo}/contents/{path}?ref={ref}

    Returns names of files only (not subdirectories).

    Args:
        owner: Repository owner.
        repo: Repository name.
        path: Directory path within the repo.
        ref: Git ref. Defaults to "main".

    Returns:
        List of filenames in the directory (files only, not subdirs).

    Raises:
        RegistryError: If the directory is not found or request fails.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    params = {"ref": ref}
    try:
        response = httpx.get(
            url,
            params=params,
            headers=_build_headers(api=True),
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.TimeoutException as exc:
        raise RegistryError(
            f"Timeout listing directory {owner}/{repo}/{path} at ref {ref}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RegistryError(f"HTTP error listing directory {owner}/{repo}/{path}: {exc}") from exc

    _raise_for_status(response, context=f"Listing directory {owner}/{repo}/{path} at ref {ref}")

    items = response.json()
    if not isinstance(items, list):
        raise RegistryError(
            f"Expected a directory at {owner}/{repo}/{path}, but got a single file."
        )
    return [item["name"] for item in items if item.get("type") == "file"]


def parse_github_source(source: str) -> tuple[str, str]:
    """Parse 'owner/repo' source string into (owner, repo) tuple.

    Args:
        source: A string in the format "owner/repo".

    Returns:
        Tuple of (owner, repo).

    Raises:
        RegistryError: If the source doesn't match expected format.
    """
    parts = source.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise RegistryError(f"Invalid GitHub source '{source}'. Expected format: 'owner/repo'.")
    return parts[0], parts[1]
