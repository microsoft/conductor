"""Update check and self-upgrade utilities for Conductor CLI.

This module provides:
- Cache-based update checking against the GitHub Releases API
- Semantic version comparison (including pre-release detection)
- A one-line Rich hint when a newer version is available
- A ``run_update()`` function that self-upgrades via ``uv tool install``

The cache file lives at ``~/.conductor/update-check.json`` and is refreshed
every 24 hours.  Network requests use a 2-second timeout and fail silently
so they never block the CLI.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from conductor import __version__

logger = logging.getLogger(__name__)

_CACHE_FILE_NAME = "update-check.json"
_CACHE_TTL_SECONDS = 86_400  # 24 hours
_API_URL = "https://api.github.com/repos/microsoft/conductor/releases/latest"
_FETCH_TIMEOUT_SECONDS = 2
_REPO_GIT_URL = "https://github.com/microsoft/conductor.git"
_RELEASE_DL_URL = "https://github.com/microsoft/conductor/releases/download"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def get_cache_path() -> Path:
    """Return the path to the update-check cache file.

    Returns:
        ``~/.conductor/update-check.json``
    """
    return Path.home() / ".conductor" / _CACHE_FILE_NAME


def read_cache() -> dict | None:
    """Read and return cached update-check data, or ``None`` if stale/missing.

    The cache is considered valid when:
    - The file exists and contains valid JSON
    - It has a ``checked_at`` ISO-8601 timestamp
    - The timestamp is less than ``_CACHE_TTL_SECONDS`` old

    Returns:
        A dict with ``tag_name``, ``version``, ``url``, and ``checked_at`` keys,
        or ``None`` if the cache is missing, expired, or invalid.
    """
    cache_path = get_cache_path()
    try:
        data = json.loads(cache_path.read_text())
        checked_at = datetime.fromisoformat(data["checked_at"])
        age = (datetime.now(UTC) - checked_at).total_seconds()
        if age > _CACHE_TTL_SECONDS:
            return None
        return data
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def write_cache(version: str, tag_name: str, url: str) -> None:
    """Write update-check data to the cache file.

    Args:
        version: The latest version string (without leading ``v``).
        tag_name: The raw tag name from the GitHub Release (e.g. ``v0.3.0``).
        url: The ``html_url`` of the release page.
    """
    cache_path = get_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "version": version,
        "tag_name": tag_name,
        "url": url,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    cache_path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a version string into a comparable tuple of integers.

    Leading ``v`` is stripped and any pre-release suffix (after ``-``) is
    removed before splitting on ``.``.

    Args:
        version_str: A version like ``"0.1.0"``, ``"v0.2.0"``, or
            ``"0.3.0-beta.1"``.

    Returns:
        A tuple of integers, e.g. ``(0, 3, 0)``.
    """
    v = version_str.lstrip("v")
    # Remove pre-release suffix
    v = v.split("-", 1)[0]
    return tuple(int(part) for part in v.split("."))


def has_prerelease(version_str: str) -> bool:
    """Return ``True`` if *version_str* contains a pre-release suffix.

    A pre-release suffix is anything after a ``-`` in the version string
    (after stripping a leading ``v``).

    Args:
        version_str: A version string such as ``"0.3.0-beta.1"``.

    Returns:
        ``True`` if the version has a pre-release component.
    """
    v = version_str.lstrip("v")
    return "-" in v


def is_newer(remote: str, local: str) -> bool:
    """Return ``True`` if *remote* is newer than *local*.

    Comparison is based on the numeric portion (via :func:`parse_version`).
    If the numeric tuples are equal but *local* has a pre-release suffix
    and *remote* does not, *remote* is considered newer (pre-release →
    release upgrade).

    Args:
        remote: The remote version string.
        local: The locally installed version string.

    Returns:
        ``True`` if an upgrade is available.
    """
    remote_tuple = parse_version(remote)
    local_tuple = parse_version(local)

    if remote_tuple > local_tuple:
        return True
    # Pre-release → release upgrade
    return remote_tuple == local_tuple and has_prerelease(local) and not has_prerelease(remote)


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------


def fetch_latest_version() -> tuple[str, str, str] | None:
    """Fetch the latest release from GitHub.

    Sends a GET request to the GitHub Releases API with a 2-second timeout.
    Any network or parsing error is caught and ``None`` is returned so the
    CLI is never blocked.

    Returns:
        A 3-tuple ``(version, tag_name, html_url)`` on success, or ``None``
        on any error.  *version* has the leading ``v`` stripped.
    """
    try:
        req = urllib.request.Request(
            _API_URL,
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode())

        tag_name: str = data["tag_name"]
        html_url: str = data["html_url"]
        version = tag_name.lstrip("v")
        return version, tag_name, html_url
    except Exception:  # noqa: BLE001
        logger.debug("Failed to fetch latest version", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Hint display
# ---------------------------------------------------------------------------


def check_for_update_hint(console: Console) -> None:
    """Print a one-line update hint if a newer version is available.

    The hint is suppressed when:
    - stderr is not a TTY
    - The CLI is in ``SILENT`` mode
    - The invoked subcommand is ``update``

    Cache is read first; if the cache is stale or missing, a network fetch
    is performed and the result is cached.

    Args:
        console: The Rich console (stderr) used for output.
    """
    # Guard: non-TTY
    if not console.is_terminal:
        return

    # Guard: silent mode
    from conductor.cli.app import ConsoleVerbosity, console_verbosity

    if console_verbosity.get() == ConsoleVerbosity.SILENT:
        return

    # Guard: 'update' subcommand
    if _is_update_subcommand():
        return

    # Try cache first
    cached = read_cache()
    if cached is not None:
        remote_version = cached.get("version", "")
        if is_newer(remote_version, __version__):
            _print_hint(console, remote_version)
        return

    # Cache miss — fetch from network
    result = fetch_latest_version()
    if result is None:
        return

    version, tag_name, url = result
    write_cache(version, tag_name, url)

    if is_newer(version, __version__):
        _print_hint(console, version)


def _is_update_subcommand() -> bool:
    """Return ``True`` if the CLI was invoked with the ``update`` subcommand."""
    args = sys.argv[1:]
    # Skip global options to find the subcommand
    for arg in args:
        if not arg.startswith("-"):
            return arg == "update"
    return False


def _print_hint(console: Console, remote_version: str) -> None:
    """Print the update-available hint line.

    Args:
        console: Rich console for output.
        remote_version: The newer version available.
    """
    console.print(
        f"💡 Conductor v{remote_version} available "
        f"(you have v{__version__}). "
        f"Run [bold]'conductor update'[/bold] to upgrade.",
        style="yellow",
    )


# ---------------------------------------------------------------------------
# Self-upgrade
# ---------------------------------------------------------------------------


def _get_conductor_exe() -> Path | None:
    """Return the path to the ``conductor`` executable, or ``None`` if not found.

    Uses :func:`shutil.which` to locate the executable on ``$PATH``.

    Returns:
        A :class:`Path` to the executable, or ``None``.
    """
    which = shutil.which("conductor")
    return Path(which) if which else None


def run_update(console: Console) -> None:
    """Fetch the latest version and self-upgrade via ``uv tool install``.

    This always bypasses the cache and fetches from the network.  On success
    the cache file is deleted so the next invocation will re-check cleanly.

    The upgrade pins transitive dependencies using a constraints file
    published with each GitHub Release, verified via SHA-256 checksum.

    Args:
        console: Rich console for output.
    """
    console.print("[bold]Checking for updates…[/bold]")

    result = fetch_latest_version()
    if result is None:
        console.print("[bold red]Error:[/bold red] Could not reach GitHub to check for updates.")
        return

    version, tag_name, _url = result
    current = __version__

    if not is_newer(version, current):
        console.print(f"[green]Already up to date[/green] (v{current}).")
        return

    console.print(f"Upgrading Conductor: v{current} → v{version}")

    install_url = f"git+{_REPO_GIT_URL}@{tag_name}"

    # Download constraints file and verify checksum
    constraints_path = _download_constraints(tag_name, console)

    cmd = ["uv", "tool", "install", "--force", install_url]
    if constraints_path:
        cmd.extend(["-c", str(constraints_path)])

    # On Windows, rename our exe out of the way so uv can write the new one.
    # Windows locks running executables but allows renaming them.
    renamed_exes: list[tuple[Path, Path]] = []
    if sys.platform == "win32":
        renamed_exes = _rename_windows_exes()

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603

        if proc.returncode == 0:
            console.print(f"[green]Successfully upgraded to v{version}[/green]")
            cache_path = get_cache_path()
            cache_path.unlink(missing_ok=True)
        elif sys.platform == "win32" and "Failed to install entrypoint" in (proc.stderr or ""):
            # On Windows, uv may fail to copy the entrypoint because the running
            # executable is locked.  The package itself was installed successfully.
            console.print(f"[green]Successfully upgraded to v{version}[/green]")
            console.print(
                "[dim]Note: restart your terminal for the update to take full effect.[/dim]"
            )
            cache_path = get_cache_path()
            cache_path.unlink(missing_ok=True)
        else:
            console.print(f"[bold red]Upgrade failed[/bold red] (exit code {proc.returncode})")
            if proc.stderr:
                console.print(f"[dim]{proc.stderr.strip()}[/dim]")
            # On Windows, restore the original exe(s) if uv failed
            for orig, backup in renamed_exes:
                if backup.exists() and not orig.exists():
                    with contextlib.suppress(OSError):
                        backup.rename(orig)
    finally:
        # Clean up temp constraints file
        if constraints_path:
            with contextlib.suppress(OSError):
                constraints_path.unlink()
                constraints_path.parent.rmdir()


def _rename_windows_exes() -> list[tuple[Path, Path]]:
    """Rename conductor executables on Windows so ``uv`` can overwrite them.

    Windows locks running executables, preventing overwrite.  Renaming is
    still allowed, so we move them out of the way before ``uv tool install``.

    We target both the executable found on ``PATH`` (the one currently running)
    and the standard ``uv`` entrypoint location at ``~/.local/bin``, deduplicating
    by resolved path.

    Returns:
        A list of ``(original_path, backup_path)`` tuples for later restoration.
    """
    renamed: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    candidates: list[Path] = []

    # 1. The exe on PATH (the one currently running)
    exe_from_which = _get_conductor_exe()
    if exe_from_which:
        candidates.append(exe_from_which)

    # 2. The standard uv entrypoint location
    uv_bin_exe = Path.home() / ".local" / "bin" / "conductor.exe"
    candidates.append(uv_bin_exe)

    for exe_path in candidates:
        if not exe_path.exists():
            continue

        # Deduplicate by resolved path (case-insensitive on Windows)
        try:
            key = str(exe_path.resolve()).lower()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)

        old_path = exe_path.with_suffix(".exe.old")
        try:
            # replace() overwrites an existing .old file, unlike rename() which
            # fails on Windows when the destination already exists (e.g. from a
            # previous interrupted update).
            exe_path.replace(old_path)
            renamed.append((exe_path, old_path))
        except OSError:
            pass  # rename failed; proceed, uv will report the error

    return renamed


def _download_constraints(tag_name: str, console: Console) -> Path | None:
    """Download and verify the constraints file for a release.

    Args:
        tag_name: The release tag (e.g. ``v0.3.0``).
        console: Rich console for status output.

    Returns:
        Path to the downloaded constraints file, or ``None`` if unavailable.
    """
    constraints_url = f"{_RELEASE_DL_URL}/{tag_name}/constraints.txt"
    checksum_url = f"{_RELEASE_DL_URL}/{tag_name}/constraints.txt.sha256"

    tmpdir = Path(tempfile.mkdtemp(prefix="conductor-update-"))
    constraints_path = tmpdir / "constraints.txt"

    try:
        # Download constraints file
        req = urllib.request.Request(constraints_url)
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            constraints_path.write_bytes(resp.read())

        # Download checksum
        req = urllib.request.Request(checksum_url)
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            checksum_content = resp.read().decode().strip()
        expected_hash = checksum_content.split()[0]

        # Verify
        actual_hash = hashlib.sha256(constraints_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            console.print(
                "[bold red]Error:[/bold red] Constraints file checksum mismatch — "
                "skipping constraints."
            )
            with contextlib.suppress(OSError):
                constraints_path.unlink()
                tmpdir.rmdir()
            return None

        console.print("[dim]Constraints verified ✓[/dim]")
        return constraints_path

    except Exception:  # noqa: BLE001
        logger.debug("Failed to download constraints file", exc_info=True)
        console.print(
            "[dim]Constraints file not available for this release, installing without.[/dim]"
        )
        with contextlib.suppress(OSError):
            constraints_path.unlink()
            tmpdir.rmdir()
        return None
