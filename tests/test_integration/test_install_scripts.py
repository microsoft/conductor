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
    # The script prints "[OK] Verified: conductor v0.0.2 ..." or similar; tolerate
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
