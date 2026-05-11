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

import contextlib
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


def _spawn_file_locker(sandbox: Sandbox, ready_file: Path) -> subprocess.Popen:
    """Hold ``Scripts/python.exe`` open so its directory cannot be cleanly removed.

    Spawns a small child process (the test runner's own Python) that uses
    Win32 ``CreateFileW`` to open ``Scripts/python.exe`` with **full share
    modes** (``FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE``).
    The handle stays alive for 300 s; the parent kills it in ``finally``.

    Why full share modes (and not the stricter ``FILE_SHARE_READ`` only)
    that an earlier draft used: this test must trigger the rename-fallback
    in ``install.ps1`` (``Move-ConductorToolDirAside``), which needs to be
    able to rename the ``conductor-cli`` directory aside *while a child
    file is still locked*. On NTFS, a parent directory can only be renamed
    when every open child handle was opened with ``FILE_SHARE_DELETE`` —
    the same rule the Windows image loader follows. The earlier draft
    (without ``FILE_SHARE_DELETE``) was strict enough to trigger
    ``Test-LockError`` but ALSO blocked the rename, so the fallback fired
    but couldn't succeed and the test failed in CI.

    What still creates a lock with full sharing: ``uv tool install --force``
    opens each file in ``Scripts/`` with DELETE access and marks it for
    delete via ``FILE_DISPOSITION_INFO``. Because this child still holds
    an open handle, the file enters "delete-pending" state — it remains
    listed in the directory until the last handle closes. Subsequent
    ``RemoveDirectory(Scripts/)`` then fails with
    ``ERROR_DIR_NOT_EMPTY``, which uv surfaces as
    ``"failed to remove directory ..."`` — matching one of
    ``Test-LockError``'s needles in ``install.ps1`` and triggering the
    rename-fallback. The rename then succeeds because the child handle
    permits ``FILE_SHARE_DELETE``.

    This faithfully mimics the production scenario the fallback was
    designed for: a running ``conductor.exe`` whose Windows image section
    pins ``Scripts/`` files (image loader uses full share modes — same
    semantics as this child).

    Why a child process and not in-process: in many uv installs the
    seeded ``Scripts/python.exe`` is a ~241 KB launcher whose real
    interpreter lives under ``%APPDATA%/uv/python/...`` (see issue #174),
    so a previous attempt that ran ``python.exe -c "import conductor"``
    didn't actually lock anything in ``Scripts/``. Using ``sys.executable``
    (the test runner's Python) plus an explicit ``CreateFileW`` sidesteps
    that pitfall — Windows enforces share modes per file regardless of
    which process opened the handle.

    The child writes ``ready_file`` once the handle is open, then sleeps;
    the caller polls ``ready_file`` to know when it's safe to invoke the
    install script.
    """
    target = str(sandbox.python_exe)
    code = textwrap.dedent(
        f"""
        import ctypes
        import ctypes.wintypes
        import sys
        import time
        from pathlib import Path

        target = {target!r}
        ready = Path({str(ready_file)!r})

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = (
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
        )

        share_mode = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        # NOTE: must read get_last_error() immediately after CreateFileW;
        # any intervening ctypes foreign-function call would clobber it.
        h = kernel32.CreateFileW(
            target, GENERIC_READ, share_mode, None, OPEN_EXISTING, 0, None
        )
        if not h or h == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            sys.stderr.write(f'CreateFileW({{target!r}}) failed: WinError {{err}}\\n')
            sys.exit(1)

        ready.write_text('locked')
        # Sleep well past the test's timeout; the parent kills us when done.
        time.sleep(300)
        """
    ).strip()
    return subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        env=sandbox.env(),
        cwd=str(sandbox.root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _drain_subprocess_output(proc: subprocess.Popen) -> tuple[str, str]:
    """Best-effort read of an exited (or about-to-be-killed) subprocess's pipes."""
    try:
        out, err = proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        out = err = f"<unavailable: {exc!r}>"
    return out or "", err or ""


def _wait_for_lock(proc: subprocess.Popen, ready_file: Path, timeout: float = 10.0) -> None:
    """Poll until the locker subprocess signals ready, or fail fast on its death.

    On early subprocess exit, raises ``AssertionError`` including the child's
    captured stdout/stderr (typically a ``CreateFileW failed: WinError ...``
    line) so the failure is immediately diagnosable in CI.

    On timeout, kills the still-running subprocess and includes whatever
    was captured before the kill — this catches "child stuck after
    CreateFileW returned but before write_text" cases (e.g. AV interference).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_file.exists():
            return
        if proc.poll() is not None:
            out, err = _drain_subprocess_output(proc)
            raise AssertionError(
                f"file-locker subprocess exited early (code {proc.returncode}):\n"
                f"--- stdout ---\n{out}\n--- stderr ---\n{err}\n"
            )
        time.sleep(0.05)
    # Timeout — kill the child so we can read its pipes.
    with contextlib.suppress(OSError):
        proc.kill()
    out, err = _drain_subprocess_output(proc)
    raise AssertionError(
        f"file-locker subprocess did not become ready within {timeout}s "
        f"(ready_file={ready_file}, exited={proc.poll() is not None}):\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}\n"
    )


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


@pytest.mark.skipif(not IS_WINDOWS, reason="file-lock fallback is Windows-specific")
def test_upgrade_with_running_process_uses_rename_fallback(
    sandbox: Sandbox, wheels: WheelPair
) -> None:
    """Upgrade while a child process holds an open handle on a file in ``Scripts/``.

    Uses ``_spawn_file_locker`` to keep ``Scripts/python.exe`` open with full
    share modes. ``uv tool install --force`` can mark the file for delete
    (it enters delete-pending state) but the directory removal then fails
    with ``ERROR_DIR_NOT_EMPTY`` — uv surfaces this as
    ``"failed to remove directory ..."``, which matches a ``Test-LockError``
    needle in ``install.ps1`` and triggers ``Move-ConductorToolDirAside``.
    The rename succeeds because the locker grants ``FILE_SHARE_DELETE``,
    and the retried install succeeds in a fresh ``conductor-cli`` directory.

    See ``_spawn_file_locker``'s docstring for why full share modes (vs. a
    stricter ``FILE_SHARE_READ``-only handle) are required: stricter
    sharing also blocks the parent rename, which would defeat the test.

    Beyond the install succeeding, this test asserts the fallback path
    actually ran by checking for both diagnostic messages in the log:

    * ``"Install blocked by a file lock"`` — emitted right before the rename
    * ``"Moved existing install to"`` — emitted after the rename succeeds

    Without those assertions (see issue #174) this test passes whenever
    ``uv tool install --force`` happens to succeed on the first attempt,
    silently masking regressions in ``Test-LockError`` or
    ``Move-ConductorToolDirAside``.

    Uses ``-Force`` to skip the running-process safety check (the locker
    isn't a ``conductor.exe`` process so it wouldn't trip that check anyway,
    but ``-Force`` keeps this test independent of that path).
    """
    seed_install(sandbox, wheels.old)
    assert sandbox.python_exe.exists(), (
        f"seeded venv missing expected python.exe at {sandbox.python_exe}"
    )
    ready_file = sandbox.root / ".lock-ready"
    proc = _spawn_file_locker(sandbox, ready_file)
    try:
        _wait_for_lock(proc, ready_file)
        result = run_install_script(sandbox, source=wheels.new, force=True)
    finally:
        _kill(proc)

    _assert_install_ok(result, "0.0.2")
    # After fallback, the freshly installed conductor should report 0.0.2.
    # Note: the locked python.exe is now under conductor-cli.old-<ts>/, but
    # a fresh invocation hits the new venv.
    version = get_installed_version(sandbox)
    assert version == "0.0.2", f"expected 0.0.2, got {version!r}\n{result.combined}"

    # Assert the rename-fallback actually ran — these are the load-bearing
    # checks for this test (see issue #174).
    assert "Install blocked by a file lock" in result.combined, (
        "rename-fallback path did not trigger; install succeeded on the first "
        f"attempt without the lock being detected:\n{result.combined}"
    )
    assert "Moved existing install to" in result.combined, (
        f"Move-ConductorToolDirAside did not log the renamed-aside path:\n{result.combined}"
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
