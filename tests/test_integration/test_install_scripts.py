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
import textwrap
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


def _install_uv_shim(sandbox: Sandbox) -> tuple[Path, dict[str, str]]:
    """Install a ``uv`` shim that fakes a lock-error on the first install.

    Returns ``(shim_dir, extra_env)``. Callers must:

    * Prepend ``shim_dir`` to ``PATH`` for the ``install.ps1`` invocation,
      so PowerShell resolves bare ``uv`` to the shim's ``uv.bat`` first.
    * Merge ``extra_env`` into the install script's environment so the
      shim can find the real ``uv`` and persist its attempt counter.

    The shim intercepts ``uv tool install --force ...`` calls. On the
    first such call, it writes a canned lock-error to stderr (matching
    two of ``Test-LockError``'s needles) and exits non-zero. On all
    subsequent calls — and on every other ``uv`` subcommand
    (``tool dir``, ``tool update-shell``, etc.) — it ``exec``s the real
    ``uv`` unchanged. The result: ``install.ps1``'s first
    ``Invoke-UvInstall`` returns the canned lock-error, ``Test-LockError``
    matches, ``Move-ConductorToolDirAside`` runs (no real lock — succeeds),
    and the retried install hits the real ``uv`` (succeeds).

    Why this approach (Option B from issue #174): an earlier draft used a
    real Win32 file-handle lock, but no share-mode combination satisfies
    both required invariants. ``FILE_SHARE_READ``-only triggers
    ``Test-LockError`` but blocks the parent-directory rename (NTFS
    requires ``FILE_SHARE_DELETE`` on every open child handle for
    ``MoveFileExW`` on the parent to succeed). Adding ``FILE_SHARE_DELETE``
    lets uv's POSIX-semantics unlink (Rust ≥ 1.66 uses
    ``FILE_DISPOSITION_FLAG_POSIX_SEMANTICS``) immediately remove the
    file from the directory listing, so the lock never surfaces. See the
    PR description for #177 for the full investigation.

    The shim approach trades fidelity-to-real-Windows-locks for a
    deterministic test of the install.ps1 control flow — which is the
    actually load-bearing invariant the test should protect.
    """
    real_uv = shutil.which("uv")
    if not real_uv:
        raise RuntimeError("could not locate `uv` on PATH for shim setup")

    shim_dir = sandbox.root / "uv-shim"
    shim_dir.mkdir()
    state_file = sandbox.root / ".uv-shim-state"

    shim_py = shim_dir / "uv-shim.py"
    shim_py.write_text(
        textwrap.dedent(
            """
            import os
            import subprocess
            import sys
            from pathlib import Path

            real_uv = os.environ['CONDUCTOR_TEST_REAL_UV']
            state = Path(os.environ['CONDUCTOR_TEST_SHIM_STATE'])

            args = sys.argv[1:]
            is_install_force = (
                len(args) >= 3
                and args[0] == 'tool'
                and args[1] == 'install'
                and '--force' in args[2:]
            )

            if is_install_force:
                attempt = int(state.read_text().strip()) if state.exists() else 0
                attempt += 1
                state.write_text(str(attempt))
                if attempt == 1:
                    # Canned message that matches two Test-LockError needles
                    # in install.ps1 ('failed to remove directory' and
                    # 'used by another process'). Modeled after a real uv
                    # error from CI run #25672191042.
                    sys.stderr.write(
                        'error: failed to remove directory '
                        '`C:\\\\fake\\\\conductor-cli\\\\Scripts`: '
                        'The process cannot access the file because it is '
                        'being used by another process. (os error 32)\\n'
                    )
                    sys.stderr.flush()
                    sys.exit(2)

            # Defer to the real uv with the same args, cwd, env, and stdio.
            sys.exit(subprocess.run([real_uv, *args]).returncode)
            """
        ).strip(),
        encoding="utf-8",
    )

    # uv.bat: PowerShell's bare-command resolution finds .bat via PATHEXT.
    # Use the test runner's sys.executable (absolute path) so the shim
    # doesn't depend on `python` being on PATH inside install.ps1's env.
    shim_bat = shim_dir / "uv.bat"
    shim_bat.write_text(
        f'@echo off\r\n"{sys.executable}" "{shim_py}" %*\r\n',
        encoding="utf-8",
    )

    extra_env = {
        "CONDUCTOR_TEST_REAL_UV": real_uv,
        "CONDUCTOR_TEST_SHIM_STATE": str(state_file),
    }
    return shim_dir, extra_env


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
    assert sandbox.python_exe.exists(), (
        f"seeded venv missing expected python.exe at {sandbox.python_exe}"
    )

    shim_dir, shim_env = _install_uv_shim(sandbox)
    extra_env = {
        **shim_env,
        # Prepend shim_dir to PATH so PowerShell's `uv` resolution finds
        # the shim's uv.bat (via PATHEXT) before the real uv on the runner.
        "PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", ""),
    }

    result = run_install_script(sandbox, source=wheels.new, force=True, extra_env=extra_env)

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
