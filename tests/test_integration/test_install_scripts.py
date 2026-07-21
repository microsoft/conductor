"""End-to-end tests for ``install.ps1`` (Windows) and ``install.sh`` (POSIX).

These tests build versioned wheels of ``conductor-cli``, install them into
isolated ``UV_TOOL_DIR`` sandboxes via the install scripts, and verify the
resulting binaries respond correctly.

Run with::

    uv run pytest -m install_scripts -v

Excluded from the default ``make test`` run, and auto-skipped by plain
``pytest`` / ``pytest -m "not performance"`` invocations (and CI's main test
job) via the ``tests/conftest.py`` collection hook — see issue #331. Without
that hook, these tests would run under any of those invocations; combined
with ``install.sh``'s ``find_running_conductor()`` scanning the whole *host*
process table, a test that opts into ``--auto-stop`` could SIGTERM-kill an
unrelated live ``conductor run --web-bg`` process. ``run_install_script()``
now defaults to ``auto_stop=False`` for the same reason.

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
    # The script prints "[OK] Verified: conductor v0.0.2 ..." or similar; tolerate
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


def _run_pwsh(script: str) -> subprocess.CompletedProcess[str]:
    """Run a PowerShell snippet via ``powershell.exe -Command`` (Windows-only).

    Sets the standard non-interactive flags so the snippet can't pop a prompt
    or load a profile. Returns the completed process; callers do their own
    assertion on returncode/stdout/stderr.
    """
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
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


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell-profile isolation check")
def test_install_does_not_touch_shell_profiles(
    sandbox: Sandbox, wheels: WheelPair, tmp_path: Path
) -> None:
    """The install script must not edit the user's shell profiles under test.

    Regression guard: ``install.sh`` once ran ``uv tool update-shell``
    unconditionally, which appended each test's throwaway
    ``UV_TOOL_BIN_DIR`` to the developer's real ``~/.zshenv`` and shadowed
    their actual conductor install with a stale ``v0.0.2`` test fixture
    (``uv tool update-shell`` deliberately edits the shell and ignores
    ``UV_NO_MODIFY_PATH``). The install scripts now honor
    ``CONDUCTOR_INSTALL_SKIP_PATH_UPDATE=1`` — set by default in
    :func:`run_install_script` — to skip that step.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = run_install_script(sandbox, source=wheels.new, extra_env={"HOME": str(fake_home)})
    _assert_install_ok(result, "0.0.2")

    assert "Skipping shell PATH update" in result.combined, (
        f"install script did not honor CONDUCTOR_INSTALL_SKIP_PATH_UPDATE:\n{result.combined}"
    )

    profiles = (".zshenv", ".zshrc", ".zprofile", ".bashrc", ".bash_profile", ".profile")
    touched = [name for name in profiles if (fake_home / name).exists()]
    assert not touched, (
        f"install script modified shell profiles {touched} despite skip hook:\n{result.combined}"
    )


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
    install.ps1``, which uses PowerShell's file loader. The file loader
    detects the BOM as an encoding sniff and *strips* it from the string
    before parsing.  ``Invoke-RestMethod``, by contrast, decodes the HTTP
    body without that special handling, so the U+FEFF survives as a
    literal character that ``iex`` / ``[ScriptBlock]::Create`` then sees
    at offset 0.  This test mirrors the ``irm`` path exactly by reading
    the raw bytes with ``ReadAllBytes`` and decoding via
    ``Encoding.UTF8.GetString`` (which preserves the BOM in the returned
    string), then handing the result to ``[ScriptBlock]::Create``, which
    is what ``Invoke-Expression`` uses internally to parse its input.
    It does **not** execute the script, so it's safe and fast.

    DO NOT swap ``ReadAllBytes`` + ``UTF8.GetString`` for ``ReadAllText``:
    the one-arg ``ReadAllText`` overload constructs a ``StreamReader`` with
    ``detectEncodingFromByteOrderMarks: true``, which silently *consumes*
    the BOM. That would make this test a tautology and miss the regression.
    """
    # Build the PowerShell snippet via a Python f-string, embedding the
    # script path inside a PowerShell single-quoted string. Escape any ``'``
    # in the path per PowerShell single-quoted-string rules (a single
    # quote is doubled) so paths containing one don't break the snippet.
    escaped_path = str(INSTALL_PS1).replace("'", "''")
    ps_script = (
        "$ErrorActionPreference = 'Stop'; "
        # ReadAllBytes + UTF8.GetString preserves a leading BOM as U+FEFF
        # in the resulting string, exactly as Invoke-RestMethod would.
        # See the docstring above -- do NOT replace this with ReadAllText.
        f"$bytes = [System.IO.File]::ReadAllBytes('{escaped_path}'); "
        "$content = [System.Text.Encoding]::UTF8.GetString($bytes); "
        "try { "
        "  [void][ScriptBlock]::Create($content); "
        "  Write-Output 'PARSE_OK' "
        "} catch { "
        '  Write-Error "PARSE_FAIL: $_"; '
        "  exit 1 "
        "}"
    )
    proc = _run_pwsh(ps_script)
    assert proc.returncode == 0 and "PARSE_OK" in proc.stdout, (
        "install.ps1 failed to parse via the `irm | iex` code path "
        "(`[ScriptBlock]::Create`). This usually means the file has a "
        "leading UTF-8 BOM (EF BB BF) that breaks `irm <url> | iex`. "
        f"\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


@pytest.mark.skipif(not IS_WINDOWS, reason="PowerShell required to test the test")
def test_iex_pipeline_test_actually_catches_bom_regression() -> None:
    """Test-the-test: prepending a UTF-8 BOM must make the parse path fail.

    This proves the harness in :func:`test_install_ps1_parses_via_iex_pipeline`
    actually catches a regressed BOM.  We read the (BOM-free) install script,
    prepend a BOM byte sequence in PowerShell, and assert that
    ``[ScriptBlock]::Create`` rejects the result.  If this test ever starts
    passing without raising, the parse-path test above has rotted into a
    tautology and must be re-checked.
    """
    escaped_path = str(INSTALL_PS1).replace("'", "''")
    ps_script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$bytes = [System.IO.File]::ReadAllBytes('{escaped_path}'); "
        "$content = [System.Text.Encoding]::UTF8.GetString($bytes); "
        # Prepend a literal U+FEFF, simulating a BOM that survived `irm`.
        "$withBom = [char]0xFEFF + $content; "
        "try { "
        "  [void][ScriptBlock]::Create($withBom); "
        "  Write-Output 'UNEXPECTED_PASS' "
        "} catch { "
        "  Write-Output 'EXPECTED_FAIL' "
        "}"
    )
    proc = _run_pwsh(ps_script)
    assert "EXPECTED_FAIL" in proc.stdout, (
        "Expected `[ScriptBlock]::Create` to reject install.ps1 when a BOM "
        "is prepended, but it did not. The parse-path test in this file is "
        "no longer protecting against the issue #175 regression. "
        f"\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
