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

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .install_scripts_helpers import (
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


_SHIM_SOURCE = Path(__file__).parent / "_uv_shim.py"


def _install_uv_shim(sandbox: Sandbox) -> dict[str, str]:
    """Install a ``uv`` shim that fakes a lock-error on the first install.

    Returns an ``extra_env`` mapping that callers should merge into the
    ``install.ps1`` invocation's environment. The mapping prepends the
    shim's directory to ``PATH`` (so PowerShell's bare-``uv`` resolution
    finds ``uv.bat`` first), captures the real ``uv`` path for the shim
    to defer to, and points the shim at a per-sandbox state file.

    The shim itself lives in ``_uv_shim.py`` next to this file; see that
    module's docstring for behavior. The shim intercepts only the first
    ``uv tool install --force`` call and forwards every other ``uv``
    invocation to the real binary.

    Why this approach (Option B from issue #174): no synthetic Win32 file
    handle can simultaneously trigger ``Test-LockError`` AND let
    ``Move-ConductorToolDirAside`` succeed against modern uv on Windows.
    Without ``FILE_SHARE_DELETE``, NTFS blocks the parent-directory
    rename; with it, Rust ≥ 1.66's POSIX-semantics unlinks bypass the
    lock entirely. See PR #177 for the full investigation. The shim
    trades fidelity-to-real-Windows-locks for a deterministic test of
    install.ps1's control flow — which is the actually load-bearing
    invariant the test should protect.
    """
    real_uv = shutil.which("uv")
    if not real_uv:
        raise RuntimeError("could not locate `uv` on PATH for shim setup")

    shim_dir = sandbox.root / "uv-shim"
    shim_dir.mkdir()
    shim_py = shim_dir / "_uv_shim.py"
    shutil.copy(_SHIM_SOURCE, shim_py)

    # uv.bat: PowerShell's bare-command resolution finds .bat via PATHEXT.
    # Use the test runner's sys.executable (absolute path) so the shim
    # works regardless of what's on PATH inside install.ps1's env.
    (shim_dir / "uv.bat").write_text(
        f'@echo off\r\n"{sys.executable}" "{shim_py}" %*\r\n',
        encoding="utf-8",
    )

    return {
        "PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", ""),
        "CONDUCTOR_TEST_REAL_UV": real_uv,
        "CONDUCTOR_TEST_SHIM_STATE": str(shim_dir / "state"),
    }


def _kill(proc: subprocess.Popen) -> None:
    """Best-effort kill + reap. Surfaces leaks to stderr instead of swallowing."""
    try:
        proc.kill()
    except OSError as exc:
        print(f"WARNING: kill(pid={proc.pid}) failed: {exc!r}", file=sys.stderr)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print(
            f"WARNING: subprocess pid={proc.pid} did not exit within 10s after kill; "
            f"may leak a file handle into pytest tmp_path teardown",
            file=sys.stderr,
        )


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


@pytest.mark.skipif(not IS_WINDOWS, reason="install.ps1 rename-fallback is Windows-specific")
def test_upgrade_with_running_process_uses_rename_fallback(
    sandbox: Sandbox, wheels: WheelPair
) -> None:
    """Verify the install.ps1 rename-fallback control flow end-to-end.

    Installs a ``uv`` shim (see ``_install_uv_shim``) that intercepts the
    first ``uv tool install --force`` call and returns a canned lock-error
    matching ``Test-LockError``'s needles. ``install.ps1`` must then:

    1. Detect the lock error and log ``"Install blocked by a file lock"``.
    2. Call ``Move-ConductorToolDirAside`` and log
       ``"Moved existing install to <path>"`` once the rename succeeds.
    3. Retry ``uv tool install --force`` — which now hits the real ``uv``
       (the shim only fakes attempt #1) and installs into a fresh
       ``conductor-cli`` directory.
    4. Report success and verify the new version responds.

    All three assertions below are load-bearing — see issue #174 for what
    happens when they're missing (the test passes whenever ``uv tool
    install --force`` happens to succeed on the first attempt, silently
    masking regressions in ``Test-LockError`` or
    ``Move-ConductorToolDirAside``).

    Uses ``-Force`` to skip the running-process safety check; the shim
    deliberately produces only the lock-error diagnostic and isn't a
    ``conductor.exe`` process so wouldn't trip that check anyway.
    """
    seed_install(sandbox, wheels.old)

    result = run_install_script(
        sandbox, source=wheels.new, force=True, extra_env=_install_uv_shim(sandbox)
    )

    _assert_install_ok(result, "0.0.2")
    version = get_installed_version(sandbox)
    assert version == "0.0.2", f"expected 0.0.2, got {version!r}\n{result.combined}"

    # Load-bearing assertions for issue #174 — these prove the fallback
    # ran end-to-end. Without them, this test would pass even if
    # Test-LockError or Move-ConductorToolDirAside silently regressed.
    assert "Install blocked by a file lock" in result.combined, (
        "rename-fallback did not trigger; install.ps1 didn't recognize the "
        "shim's canned lock-error as a lock-shaped failure. Did "
        f"Test-LockError's needle list change?\n{result.combined}"
    )
    assert "Moved existing install to" in result.combined, (
        "Move-ConductorToolDirAside did not log the renamed-aside path; "
        f"the rename either failed or the success log was removed:\n{result.combined}"
    )


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
