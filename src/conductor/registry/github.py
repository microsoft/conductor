"""Helpers for fetching files, tags, and directory contents from GitHub repos.

Supports both public and private repos. Authentication is resolved
automatically via the ``gh`` CLI (``gh auth token --hostname github.com``)
when available.
"""

from __future__ import annotations

import logging
import subprocess
from urllib.parse import quote

import httpx

from conductor.registry.errors import RegistryError, RegistryNotFoundError

logger = logging.getLogger(__name__)

GITHUB_RAW_BASE = "https://raw.githubusercontent.com"
GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0

_HEADERS = {"User-Agent": "conductor-cli"}
_API_HEADERS = {
    **_HEADERS,
    "Accept": "application/vnd.github.v3+json",
}


def _get_auth_token() -> str | None:
    """Get a github.com token regardless of the caller's ``GH_HOST``.

    Returns:
        A token string, or ``None`` if the ``gh`` CLI is not available or
        not authenticated.
    """
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["gh", "auth", "token", "--hostname", "github.com"],
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
        raise RegistryNotFoundError(
            f"{context}: not found (404). Check that the repository exists and the ref is valid.",
            suggestion=(
                "If this is a private repo, ensure 'gh auth login --hostname github.com' "
                "has been run."
            ),
        )
    if status in (403, 429):
        raise RegistryError(
            f"{context}: HTTP {status}. GitHub API rate limit may be exceeded. Try again later."
        )
    raise RegistryError(f"{context}: HTTP {status}")


def fetch_file(owner: str, repo: str, path: str, ref: str = "main") -> bytes:
    """Fetch a single file from a GitHub repo at a given ref.

    Uses raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>. ``path`` is
    percent-encoded (preserving ``/`` separators) before being interpolated
    into the URL, so a repo-relative path containing ``#``, ``?``, or a
    literal ``%`` is requested — and cached — as the file it actually names,
    rather than being truncated at a URL fragment/query delimiter or
    misinterpreted as an existing percent-escape.

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
    url = f"{GITHUB_RAW_BASE}/{owner}/{repo}/{ref}/{quote(path, safe='/')}"
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


_MAX_TAGS = 1000


def list_tags(owner: str, repo: str) -> list[str]:
    """List all git tags for a repository in GitHub's commit-date order.

    Uses GET /repos/{owner}/{repo}/tags from the GitHub REST API.
    Follows ``Link: <...>; rel="next"`` headers to paginate, capping at
    ``_MAX_TAGS`` (1000) tags total to prevent runaway loops.

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        List of tag name strings in GitHub's commit-date order (newest commit
        first). For semver ordering of these tags, use
        :func:`conductor.registry.version_resolver.sort_tags`.

    Raises:
        RegistryError: If the API request fails.
    """
    url: str | None = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/tags?per_page=100"
    tags: list[str] = []
    while url is not None and len(tags) < _MAX_TAGS:
        try:
            response = httpx.get(
                url,
                headers=_build_headers(api=True),
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise RegistryError(f"Timeout listing tags for {owner}/{repo}") from exc
        except httpx.HTTPError as exc:
            raise RegistryError(f"HTTP error listing tags for {owner}/{repo}: {exc}") from exc

        _raise_for_status(response, context=f"Listing tags for {owner}/{repo}")
        tags.extend(tag["name"] for tag in response.json())

        next_link = response.links.get("next")
        url = next_link["url"] if next_link else None

    return tags[:_MAX_TAGS]


def get_default_branch(owner: str, repo: str) -> str:
    """Get the default branch name for a GitHub repository.

    Uses GET /repos/{owner}/{repo} from the GitHub REST API and returns
    the ``default_branch`` field.

    Args:
        owner: Repository owner.
        repo: Repository name.

    Returns:
        Default branch name (e.g. "main" or "master").

    Raises:
        RegistryError: If the request fails or the repo is not found.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
    try:
        response = httpx.get(
            url, headers=_build_headers(api=True), timeout=DEFAULT_TIMEOUT, follow_redirects=True
        )
    except httpx.TimeoutException as exc:
        raise RegistryError(f"Timeout fetching default branch for {owner}/{repo}") from exc
    except httpx.HTTPError as exc:
        raise RegistryError(
            f"HTTP error fetching default branch for {owner}/{repo}: {exc}"
        ) from exc

    _raise_for_status(response, context=f"Fetching default branch for {owner}/{repo}")
    return response.json()["default_branch"]


def resolve_ref_to_sha(owner: str, repo: str, ref: str) -> str:
    """Resolve a git ref (branch, tag, or short SHA) to a full commit SHA.

    Uses GET /repos/{owner}/{repo}/commits/{ref} which accepts any kind of
    ref — branch names, tag names, or full/short SHAs — and returns the
    commit metadata. We return the full ``sha`` field.

    Args:
        owner: Repository owner.
        repo: Repository name.
        ref: A branch name, tag name, or commit SHA (full or short).

    Returns:
        The full 40-character commit SHA.

    Raises:
        RegistryError: If the ref cannot be resolved or request fails.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{ref}"
    try:
        response = httpx.get(
            url, headers=_build_headers(api=True), timeout=DEFAULT_TIMEOUT, follow_redirects=True
        )
    except httpx.TimeoutException as exc:
        raise RegistryError(f"Timeout resolving ref {ref} for {owner}/{repo}") from exc
    except httpx.HTTPError as exc:
        raise RegistryError(f"HTTP error resolving ref {ref} for {owner}/{repo}: {exc}") from exc

    _raise_for_status(response, context=f"Resolving ref {ref} for {owner}/{repo}")
    return response.json()["sha"]


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


# Git tree blob modes. A blob's `mode` field (not its `type`, which is only
# "blob"/"tree"/"commit") is what distinguishes a regular file from an
# executable file, a symlink, or a submodule/gitlink.
_MODE_REGULAR_FILE = "100644"
_MODE_EXECUTABLE_FILE = "100755"
_MODE_SYMLINK = "120000"
_REGULAR_FILE_MODES = frozenset({_MODE_REGULAR_FILE, _MODE_EXECUTABLE_FILE})


def _get_git_tree(owner: str, repo: str, tree_sha: str, *, context: str) -> list[dict]:
    """Fetch one non-recursive Git Trees API listing and validate its shape.

    Uses GET /repos/{owner}/{repo}/git/trees/{tree_sha} (no ``recursive``
    query param — the caller walks subdirectories itself via an explicit
    stack, see :func:`list_files_recursive`). This is deliberately not the
    Contents API, which caps a single directory listing at 1,000 entries;
    the Git Trees API has no such per-directory limit, though GitHub may
    still ``truncated: true`` an individual response for pathological
    directories (huge fan-out), which is treated as a hard failure below
    rather than a silent partial listing.

    Args:
        owner: Repository owner.
        repo: Repository name.
        tree_sha: A tree SHA (or a commit SHA, which resolves to its root
            tree) to list the immediate contents of.
        context: Human-readable description of what is being listed, used
            in error messages.

    Returns:
        A list of validated entry dicts, each with ``path``, ``mode``,
        ``type``, and ``sha`` (``sha`` may be ``None`` for a blob entry
        missing it, though GitHub always includes it in practice).

    Raises:
        RegistryError: On request failure, a non-JSON/non-object response,
            a missing or malformed ``tree`` array, a malformed entry, or a
            truncated response.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{tree_sha}"
    try:
        response = httpx.get(
            url, headers=_build_headers(api=True), timeout=DEFAULT_TIMEOUT, follow_redirects=True
        )
    except httpx.TimeoutException as exc:
        raise RegistryError(f"Timeout {context}") from exc
    except httpx.HTTPError as exc:
        raise RegistryError(f"HTTP error {context}: {exc}") from exc

    _raise_for_status(response, context=context)

    try:
        data = response.json()
    except ValueError as exc:
        raise RegistryError(
            f"Malformed response {context}: response body is not valid JSON"
        ) from exc

    if not isinstance(data, dict):
        raise RegistryError(f"Malformed response {context}: expected a JSON object")

    if data.get("truncated"):
        raise RegistryError(
            f"{context}: GitHub truncated this tree listing because it has too many entries. "
            "Refusing to proceed with a partial listing.",
            suggestion=(
                "Move the workflow to a directory with fewer nested files, or split large "
                "subdirectories out of the workflow's containing directory."
            ),
        )

    raw_entries = data.get("tree")
    if not isinstance(raw_entries, list):
        raise RegistryError(f"Malformed response {context}: missing or invalid 'tree' array")

    entries: list[dict] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise RegistryError(f"Malformed response {context}: tree entry is not an object")
        path = raw_entry.get("path")
        mode = raw_entry.get("mode")
        entry_type = raw_entry.get("type")
        entry_sha = raw_entry.get("sha")
        if not isinstance(path, str) or not path:
            raise RegistryError(f"Malformed response {context}: tree entry missing 'path'")
        if not isinstance(mode, str) or not mode:
            raise RegistryError(f"Malformed response {context}: tree entry missing 'mode'")
        if not isinstance(entry_type, str) or not entry_type:
            raise RegistryError(f"Malformed response {context}: tree entry missing 'type'")
        if entry_type == "tree" and not isinstance(entry_sha, str):
            raise RegistryError(
                f"Malformed response {context}: directory entry {path!r} missing 'sha'"
            )
        entries.append({"path": path, "mode": mode, "type": entry_type, "sha": entry_sha})
    return entries


def _resolve_directory_tree_sha(owner: str, repo: str, commit_sha: str, directory: str) -> str:
    """Walk from a commit's root tree down to the tree SHA for *directory*.

    ``GET /repos/{owner}/{repo}/git/trees/{commit_sha}`` resolves a commit
    SHA to its root tree automatically, so the walk starts there and
    descends one path segment at a time using each segment's own ``tree``
    entry — never the Contents API, and never a full-repository recursive
    listing when the workflow occupies a smaller subtree.

    Args:
        owner: Repository owner.
        repo: Repository name.
        commit_sha: The pinned commit SHA to resolve the tree from.
        directory: Repo-relative directory path (``""`` or ``"."`` for the
            repository root).

    Returns:
        The tree SHA for *directory*.

    Raises:
        RegistryNotFoundError: If any path segment does not exist or is not
            a directory.
        RegistryError: On any other listing failure.
    """
    segments = (
        [s for s in directory.strip("/").split("/") if s] if directory not in ("", ".") else []
    )

    current_tree_sha = commit_sha
    walked = ""
    for segment in segments:
        entries = _get_git_tree(
            owner,
            repo,
            current_tree_sha,
            context=f"Listing directory '{walked or '.'}' in {owner}/{repo} at {commit_sha}",
        )
        match = next((e for e in entries if e["path"] == segment and e["type"] == "tree"), None)
        if match is None:
            raise RegistryNotFoundError(
                f"Directory '{directory}' not found in {owner}/{repo} at {commit_sha} "
                f"(missing segment '{segment}')",
                suggestion="Check the workflow's path in the registry index.",
            )
        current_tree_sha = match["sha"]
        walked = f"{walked}/{segment}" if walked else segment
    return current_tree_sha


def list_files_recursive(owner: str, repo: str, directory: str, ref: str) -> list[str]:
    """Recursively list every regular file under *directory* at a pinned ref.

    Uses the Git Trees API with an explicit stack for iterative traversal
    rather than a single ``recursive=1`` request, so a directory this large
    fetches one small listing per subdirectory instead of one response that
    GitHub could truncate for the whole subtree at once — and so only the
    workflow's containing subtree is walked, never the entire repository,
    when that subtree is smaller than the repo as a whole.

    Symlinks (blob mode ``120000``) and submodules/gitlinks (entry type
    ``"commit"``) are excluded from the result and logged; they are not
    followed or downloaded.

    Args:
        owner: Repository owner.
        repo: Repository name.
        directory: Repo-relative directory to walk (``""`` or ``"."`` for
            the repository root).
        ref: A resolved, immutable commit SHA — traversal is pinned to this
            single commit's tree identities throughout, never re-resolved.

    Returns:
        A deterministic (sorted), repository-relative list of regular file
        paths (both ``100644`` and ``100755`` blob modes), rooted at the
        repository root (not *directory*) — e.g. listing directory
        ``"workflows/foo"`` returns paths like
        ``"workflows/foo/prompts/plan.md"``.

    Raises:
        RegistryNotFoundError: If *directory* does not exist.
        RegistryError: On any listing failure, malformed response, or a
            truncated tree — never a partial success.
    """
    directory_norm = directory.strip("/")
    if directory_norm == ".":
        directory_norm = ""
    label = directory_norm or "."

    try:
        root_tree_sha = _resolve_directory_tree_sha(owner, repo, ref, directory_norm)
    except RegistryNotFoundError:
        raise
    except RegistryError as exc:
        raise RegistryError(
            f"Failed to locate directory '{label}' in {owner}/{repo} at {ref}: {exc}"
        ) from exc

    results: list[str] = []
    # Stack of (repo-relative directory prefix, tree sha) to explore. The
    # prefix already includes `directory_norm` so returned paths are
    # repository-relative, not directory-relative.
    stack: list[tuple[str, str]] = [(directory_norm, root_tree_sha)]
    while stack:
        prefix, tree_sha = stack.pop()
        entries = _get_git_tree(
            owner,
            repo,
            tree_sha,
            context=f"Listing directory '{prefix or '.'}' in {owner}/{repo} at {ref}",
        )
        for entry in entries:
            entry_path = f"{prefix}/{entry['path']}" if prefix else entry["path"]
            if entry["type"] == "tree":
                stack.append((entry_path, entry["sha"]))
            elif entry["type"] == "blob":
                if entry["mode"] in _REGULAR_FILE_MODES:
                    results.append(entry_path)
                elif entry["mode"] == _MODE_SYMLINK:
                    logger.warning(
                        "Skipping symlink %r in %s/%s at %s (symlinks are not followed)",
                        entry_path,
                        owner,
                        repo,
                        ref,
                    )
                else:
                    logger.warning(
                        "Skipping blob %r with unexpected mode %r in %s/%s at %s",
                        entry_path,
                        entry["mode"],
                        owner,
                        repo,
                        ref,
                    )
            elif entry["type"] == "commit":
                logger.warning(
                    "Skipping submodule %r in %s/%s at %s (submodules are not followed)",
                    entry_path,
                    owner,
                    repo,
                    ref,
                )
            # Any other entry type (unexpected) is ignored rather than
            # treated as a fatal shape error — the required fields were
            # already validated in _get_git_tree.
    return sorted(results)


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
