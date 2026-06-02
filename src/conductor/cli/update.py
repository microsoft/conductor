"""Update check and self-upgrade utilities for Conductor CLI.

This module provides:
- Cache-based update checking against the GitHub Releases API
- Semantic version comparison (including pre-release detection)
- A one-line Rich hint when a newer version is available
- A ``run_update()`` function that prints the install-script command, or with
  ``apply=True`` spawns the install script as a fully detached process and
  exits the current ``conductor`` so it releases its file locks

The cache file lives at ``~/.conductor/update-check.json`` and is refreshed
every 24 hours.  Network requests use a 2-second timeout and fail silently
so they never block the CLI.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
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

# Install-script entry points. Kept as module-level constants so a future
# redirect change is a one-line edit.
_INSTALL_PS1_URL = "https://aka.ms/conductor/install.ps1"
_INSTALL_SH_URL = "https://aka.ms/conductor/install.sh"

# Retry settings for `uv tool install` — mirrors install.ps1
_INSTALL_MAX_ATTEMPTS = 3
_INSTALL_RETRY_DELAY_SECONDS = 2


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


_DISABLE_ENV_VAR = "CONDUCTOR_NO_UPDATE_CHECK"
_HINT_SKIP_FLAGS = frozenset({"--help", "-h", "--version", "-v"})


def check_for_update_hint(console: Console) -> None:
    """Print a one-line update hint if a newer version is available.

    The hint is suppressed when:
    - stderr is not a TTY
    - The CLI is in ``SILENT`` mode
    - The invoked subcommand is ``update``
    - ``--help`` / ``-h`` / ``--version`` / ``-v`` was passed (these
      already produce focused output the user came for; a hint would be
      noise)
    - The ``CONDUCTOR_NO_UPDATE_CHECK`` environment variable is set to a
      truthy value (``1``, ``true``, ``yes``, case-insensitive). Useful
      for users who manage upgrades through a package manager and want
      to silence the nudge permanently.

    Cache is read first; if the cache is stale or missing, a network fetch
    is performed and the result is cached.

    Args:
        console: The Rich console (stderr) used for output.
    """
    # Guard: explicit opt-out via env var
    if _update_check_disabled():
        return

    # Guard: non-TTY
    if not console.is_terminal:
        return

    # Guard: silent mode
    from conductor.cli.app import ConsoleVerbosity, console_verbosity

    if console_verbosity.get() == ConsoleVerbosity.SILENT:
        return

    # Guard: 'update' subcommand or --help/--version
    if _is_update_subcommand() or _is_help_or_version_invocation():
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


def _update_check_disabled() -> bool:
    """Return ``True`` if the user opted out of update checks via env var."""
    val = os.environ.get(_DISABLE_ENV_VAR, "").strip().lower()
    return val in {"1", "true", "yes"}


def _is_update_subcommand() -> bool:
    """Return ``True`` if the CLI was invoked with the ``update`` subcommand."""
    args = sys.argv[1:]
    # Skip global options to find the subcommand
    for arg in args:
        if not arg.startswith("-"):
            return arg == "update"
    return False


def _is_help_or_version_invocation() -> bool:
    """Return ``True`` if any ``--help`` / ``--version`` style flag is present.

    These invocations already produce the focused output the user requested;
    sneaking an upgrade hint above (or below) it would obscure the answer.
    """
    return any(arg in _HINT_SKIP_FLAGS for arg in sys.argv[1:])


def _print_hint(console: Console, remote_version: str) -> None:
    """Print the update-available hint line.

    Args:
        console: Rich console for output.
        remote_version: The newer version available.
    """
    console.print(
        f"💡 Conductor v{remote_version} available "
        f"(you have v{__version__}). "
        f"Run [bold]'conductor update'[/bold] to see how, "
        f"or [bold]'conductor update --apply'[/bold] to upgrade in one step.",
        style="yellow",
    )


# ---------------------------------------------------------------------------
# Upgrade instructions (formerly self-upgrade)
# ---------------------------------------------------------------------------


def _install_command() -> str:
    """Return the OS-appropriate one-line install/upgrade command.

    The install script is the single, canonical upgrade path. ``conductor
    update`` no longer attempts to self-upgrade in-process because on
    Windows the running ``python.exe`` lives inside the venv that
    ``uv tool install --force`` is trying to delete, which makes the
    operation fundamentally impossible from within the running process.
    """
    if sys.platform == "win32":
        return f"irm {_INSTALL_PS1_URL} | iex"
    return f"curl -sSfL {_INSTALL_SH_URL} | sh"


def _spawn_installer_and_exit(console: Console) -> None:
    """Spawn the install script detached from this process, then exit.

    The current ``conductor`` process holds locks on the venv that the
    install script needs to recreate. Spawning the installer and exiting
    immediately gives the OS a chance to release those locks before
    ``uv tool install`` tries to delete the directory.

    On Windows the installer runs in a *new console window* (so the user
    can watch progress) and the current process exits with code 0.

    On POSIX the current process is replaced via :func:`os.execvp` so the
    installer inherits this terminal directly.

    The installer is invoked with ``CONDUCTOR_INSTALL_AUTO_STOP=1`` so any
    leftover conductor processes (including the brief race window during
    our exit) are reaped without blocking on a prompt.

    This function does not return; it raises ``SystemExit`` (Windows) or
    replaces the process (POSIX).
    """
    env = {**os.environ, "CONDUCTOR_INSTALL_AUTO_STOP": "1"}

    if sys.platform == "win32":
        ps_command = f"irm {_INSTALL_PS1_URL} | iex"
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps_command,
        ]
        # CREATE_NEW_CONSOLE gives the installer its own visible console
        # window so the user can watch progress. CREATE_BREAKAWAY_FROM_JOB
        # detaches the installer from any job object the parent is in,
        # which matters in CI runners (GitHub Actions, Azure Pipelines)
        # and some terminal hosts that kill all job members on close —
        # without it the installer can be terminated mid-upgrade.
        # Both fall back to literal Win32 values if Python lacks them.
        create_new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        create_breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)

        # Prefer breakaway, but the parent's job may forbid it
        # (JOB_OBJECT_LIMIT_BREAKAWAY_OK not set), in which case
        # CreateProcess returns ERROR_ACCESS_DENIED. Fall back to
        # plain CREATE_NEW_CONSOLE so we still spawn something.
        flag_attempts = (create_new_console | create_breakaway, create_new_console)
        spawned = False
        last_error: OSError | None = None
        for flags in flag_attempts:
            try:
                subprocess.Popen(
                    cmd,
                    creationflags=flags,
                    close_fds=True,
                    env=env,
                )
                spawned = True
                break
            except OSError as e:
                last_error = e
                continue

        if not spawned:
            assert last_error is not None
            console.print(f"[bold red]Could not spawn installer:[/bold red] {last_error}")
            console.print(
                f"Run this manually in a new shell:  [bold cyan]{_install_command()}[/bold cyan]"
            )
            raise SystemExit(1) from None

        console.print(
            "[green]Installer launched in a new console window.[/green] "
            "This conductor process will now exit so file locks release. "
            "Watch the new window for progress."
        )
        # Exit immediately so the venv we live in becomes deletable.
        raise SystemExit(0)

    # POSIX: no file-lock issue. Replace ourselves with the install script
    # so its output streams directly to this terminal.
    sh_command = f"curl -sSfL {_INSTALL_SH_URL} | sh"
    console.print(
        "[green]Replacing conductor with installer…[/green] (install script output follows)"
    )
    # os.execvpe replaces the current process image, so we need to flush
    # any buffered Rich output first.
    console.file.flush()
    try:
        os.execvpe("sh", ["sh", "-c", sh_command], env)
    except OSError as e:
        console.print(f"[bold red]Could not exec installer:[/bold red] {e}")
        console.print(f"Run this manually:  [bold cyan]{_install_command()}[/bold cyan]")
        raise SystemExit(1) from None


def run_update(console: Console, force: bool = False, apply: bool = False) -> None:
    """Check for a newer release and either print or run the install command.

    By default, this prints the OS-appropriate install-script one-liner so
    the user can paste it into a fresh shell. With ``apply=True`` the script
    is spawned as a fully detached process and the current ``conductor``
    process exits immediately so its file locks release — this avoids the
    "Access is denied" failure that in-process self-upgrade hits on Windows.

    Args:
        console: Rich console for output.
        force: Accepted for backward compatibility; currently unused
            (the install script handles its own safety checks).
        apply: If True, spawn the installer and exit instead of printing
            the command. The installer runs in a new console window on
            Windows and replaces the current process on POSIX.
    """
    del force  # accepted for backward compatibility; ignored

    console.print("[bold]Checking for updates…[/bold]")

    result = fetch_latest_version()
    if result is None:
        console.print("[bold red]Error:[/bold red] Could not reach GitHub to check for updates.")
        return

    version, _tag_name, _url = result
    current = __version__

    if not is_newer(version, current):
        console.print(f"[green]Already up to date[/green] (v{current}).")
        # Refresh the cache so the hint stops nagging.
        write_cache(version, _tag_name, _url)
        return

    console.print(f"[bold]Conductor v{version}[/bold] is available (you have v{current}).")
    console.print()

    if apply:
        # Hand off to the installer and exit; this call does not return.
        _spawn_installer_and_exit(console)
        return  # pragma: no cover - _spawn_installer_and_exit never returns

    cmd = _install_command()
    console.print("To upgrade, run this in a [bold]new shell[/bold] (not inside conductor):")
    console.print()
    console.print(f"  [bold cyan]{cmd}[/bold cyan]")
    console.print()
    console.print(
        "[dim]Or re-run with [bold]--apply[/bold] to launch the installer "
        "automatically (conductor will exit so file locks release).[/dim]"
    )
    console.print(
        "[dim]The install script handles file-lock safety, retries, and "
        "post-install verification. It is the single supported upgrade path "
        "on all platforms.[/dim]"
    )
