"""End-to-end tests for ``install.ps1`` (Windows) and ``install.sh`` (POSIX).

These tests build versioned wheels of ``conductor-cli``, install them into
isolated ``UV_TOOL_DIR`` sandboxes via the install scripts, and verify the
resulting binaries respond correctly.

Run with::

    uv run pytest -m install_scripts -v

Excluded from the default ``make test`` run.

The tests are deliberately Windows-focused because the upgrade reliability
problems they exercise are Windows-specific (file locking on a venv whose
``python.exe`` is the running interpreter). The POSIX tests still run as a
parity sanity check.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from .install_scripts_helpers import (
    INSTALL_PS1,
    IS_WINDOWS,
    InstallResult,
    Sandbox,
    WheelPair,
    get_installed_version,
    run_install_script,
    seed_install,
)

pytestmark = pytest.mark.install_scripts


# ---------------------------------------------------------------------------
# Helpers local to this module
# ---------------------------------------------------------------------------


def _assert_install_ok(result: InstallResult, version: str) -> None:
    assert result.returncode == 0, f"install script failed:\n{result.combined}"
    # The script prints "✓ Verified: conductor v0.0.2 …" or similar; tolerate
    # cases where post-install verify fell back to a warning.
    assert "Conductor" in (result.stdout + result.stderr) or "conductor" in (
        result.stdout + result.stderr
    )


def _spawn_long_running_conductor(sandbox: Sandbox) -> subprocess.Popen:
    """Spawn a long-running process inside the sandbox venv that locks files.

    Importing ``conductor`` loads the package and its DLLs, so the venv's
    ``python.exe`` plus its loaded ``python3xx.dll`` are locked for the
    lifetime of the process — exactly the scenario that fails in production.

    Uses ``-I`` (isolated mode) so the source tree at the repo cwd is not
    accidentally picked up over the installed package.
    """
    code = "import conductor; import time; time.sleep(120)"
    return subprocess.Popen(
        [str(sandbox.python_exe), "-I", "-c", code],
        env=sandbox.env(),
        cwd=str(sandbox.root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fresh_install(sandbox: Sandbox, wheels: WheelPair) -> None:
    """Install into an empty sandbox; verify version is reported correctly."""
    result = run_install_script(sandbox, source=wheels.new)
    _assert_install_ok(result, "0.0.2")
    assert sandbox.conductor_exe.exists(), (
        f"conductor exe not at {sandbox.conductor_exe} after install:\n{result.combined}"
    )
    version = get_installed_version(sandbox)
    assert version == "0.0.2", f"expected 0.0.2, got {version!r}\n{result.combined}"


def test_upgrade_clean(sandbox: Sandbox, wheels: WheelPair) -> None:
    """Seed an old install; upgrade via the install script; verify new version."""
    seed_install(sandbox, wheels.old)
    assert get_installed_version(sandbox) == "0.0.1"

    result = run_install_script(sandbox, source=wheels.new)
    _assert_install_ok(result, "0.0.2")
    assert get_installed_version(sandbox) == "0.0.2", (
        f"upgrade did not produce 0.0.2:\n{result.combined}"
    )


def test_upgrade_clears_stale_old_files(sandbox: Sandbox, wheels: WheelPair) -> None:
    """Stale ``*.exe.old`` files from prior failed updates must not block install."""
    seed_install(sandbox, wheels.old)

    if IS_WINDOWS:
        scripts = sandbox.tool_dir / "conductor-cli" / "Scripts"
    else:
        scripts = sandbox.tool_dir / "conductor-cli" / "bin"
    stale = scripts / "conductor.exe.old"
    stale.write_bytes(b"stale")
    assert stale.exists()

    result = run_install_script(sandbox, source=wheels.new)
    _assert_install_ok(result, "0.0.2")
    assert get_installed_version(sandbox) == "0.0.2"
    # Stale file should be gone after install
    assert not stale.exists(), f"stale .old file survived install:\n{result.combined}"


@pytest.mark.skipif(not IS_WINDOWS, reason="file-lock fallback is Windows-specific")
def test_upgrade_with_running_process_uses_rename_fallback(
    sandbox: Sandbox, wheels: WheelPair
) -> None:
    """Upgrade while a venv-internal process holds file locks.

    Spawns a child Python from the seeded venv that imports ``conductor`` and
    sleeps. This locks ``python.exe`` and ``python3xx.dll`` inside the
    sandbox's ``Scripts`` dir — exactly the situation that breaks
    ``conductor update`` in production. Must succeed via the rename-fallback.

    Uses ``-Force`` to skip the running-process safety check (the running
    process is intentional for this test) so the script actually attempts
    the install.
    """
    seed_install(sandbox, wheels.old)
    proc = _spawn_long_running_conductor(sandbox)
    try:
        # Give it time to fully load the DLLs
        time.sleep(2.0)
        result = run_install_script(sandbox, source=wheels.new, force=True)
    finally:
        _kill(proc)

    _assert_install_ok(result, "0.0.2")
    # After fallback, the freshly installed conductor should report 0.0.2.
    # Note: the running child process still holds the OLD venv (now renamed
    # aside), but a fresh invocation hits the new venv.
    version = get_installed_version(sandbox)
    assert version == "0.0.2", f"expected 0.0.2, got {version!r}\n{result.combined}"


def test_running_process_auto_stop_kills_and_continues(sandbox: Sandbox, wheels: WheelPair) -> None:
    """``--auto-stop`` must stop other conductor processes and proceed.

    Spawns the real ``conductor.exe`` (not python.exe) so it shows up under
    ``Get-CimInstance Win32_Process -Filter "Name = 'conductor.exe'"``.
    Uses ``conductor run`` with a workflow containing an unconditional human
    gate so the process hangs on stdin; with ``--auto-stop`` (and no
    ``--force``) the install script detects the running process, stops it,
    and proceeds to a successful install.
    """
    if not IS_WINDOWS:
        pytest.skip("running-process detection only wired for Windows in this test")

    seed_install(sandbox, wheels.old)

    # A minimal workflow that immediately hits a human gate (waiting on stdin).
    wf = sandbox.root / "wait.yaml"
    wf.write_text(
        "name: wait\n"
        "agents:\n"
        "  - name: pause\n"
        "    type: human_gate\n"
        "    prompt: 'paused'\n"
        "    options: ['continue']\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [str(sandbox.conductor_exe), "run", str(wf)],
        env=sandbox.env(),
        cwd=str(sandbox.root),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(3.0)  # let it boot and reach the gate
        # With --auto-stop (and no --force), the install script kills the
        # running conductor and proceeds. Verify the install ultimately
        # succeeds.
        result = run_install_script(sandbox, source=wheels.new, force=False, auto_stop=True)
    finally:
        _kill(proc)

    _assert_install_ok(result, "0.0.2")
    assert get_installed_version(sandbox) == "0.0.2", result.combined


@pytest.mark.skipif(not IS_WINDOWS, reason="PowerShell required to test irm | iex parse path")
def test_install_ps1_parses_via_iex_pipeline() -> None:
    """``install.ps1`` must parse cleanly when delivered via ``irm | iex``.

    The README install command is::

        irm https://aka.ms/conductor/install.ps1 | iex

    ``Invoke-RestMethod`` returns the body as a single string. If the file
    starts with a UTF-8 BOM (``EF BB BF``), the BOM survives as ``U+FEFF``
    at index 0 and PowerShell's in-memory parser (``Invoke-Expression`` /
    ``[ScriptBlock]::Create``) chokes on the first real token after the
    comment header, producing::

        Unexpected attribute 'CmdletBinding'.

    The existing tests in this module all use ``powershell.exe -File
    install.ps1``, which loads the script via PowerShell's *file* loader
    and handles the BOM differently from the in-memory ``iex`` parser —
    so they don't catch the BOM regression. This test does, by reading
    the script bytes ourselves (mirroring what ``irm`` does) and feeding
    them to ``[ScriptBlock]::Create``, which is what ``iex`` uses
    internally to parse the input. It does **not** execute the script,
    so it's safe and fast.
    """
    # Use a here-string that interpolates the script path; ``[ScriptBlock]::Create``
    # is the same parse path ``Invoke-Expression`` takes internally and will
    # surface the BOM-induced parse error if one regresses.
    # Escape any ``'`` in the path per PowerShell single-quoted-string rules
    # (a single quote is doubled) so paths containing one don't break the
    # generated -Command snippet.
    escaped_path = str(INSTALL_PS1).replace("'", "''")
    ps_script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$content = [System.IO.File]::ReadAllText('{escaped_path}'); "
        "try { "
        "  [void][ScriptBlock]::Create($content); "
        "  Write-Output 'PARSE_OK' "
        "} catch { "
        '  Write-Error "PARSE_FAIL: $_"; '
        "  exit 1 "
        "}"
    )
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps_script,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0 and "PARSE_OK" in proc.stdout, (
        "install.ps1 failed to parse via the `irm | iex` code path "
        "(`[ScriptBlock]::Create`). This usually means the file has a "
        "leading UTF-8 BOM (EF BB BF) that breaks `irm <url> | iex`. "
        f"\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
