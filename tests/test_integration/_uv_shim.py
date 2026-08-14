"""Test-only ``uv`` shim used by ``test_install_scripts.py``.

Copied into a per-test directory by ``_install_uv_shim``; the directory
is prepended to ``PATH`` so the install scripts' bare ``uv`` invocations
resolve to the shim (``uv.bat`` on Windows, ``uv`` on POSIX).

Behavior is selected by ``CONDUCTOR_TEST_SHIM_MODE``:

``lock-once`` (default)
    On the *first* ``uv tool install --force ...`` call, write a canned
    Windows file-lock error to stderr and exit non-zero. Every later
    call is forwarded to the real ``uv``, so the install script's retry
    succeeds.

``network-always``
    On *every* ``uv tool install --force ...`` call, write a canned
    blocked-package-index error to stderr and exit non-zero. Used to
    prove the install scripts classify the failure and stop retrying,
    instead of burning the full backoff schedule on a failure that
    cannot heal.

Every other call (``tool dir``, ``tool update-shell``, the retried
install, etc.) is forwarded to the real ``uv`` unchanged.

Stateful via the file referenced by ``CONDUCTOR_TEST_SHIM_STATE``, which
holds the number of intercepted ``tool install --force`` attempts; the
real ``uv`` path is read from ``CONDUCTOR_TEST_REAL_UV`` (captured
by the test before ``shim_dir`` was prepended to PATH so this script
never accidentally calls itself).

Filename starts with ``_`` so pytest does not collect it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

real_uv = os.environ["CONDUCTOR_TEST_REAL_UV"]
state = Path(os.environ["CONDUCTOR_TEST_SHIM_STATE"])
mode = os.environ.get("CONDUCTOR_TEST_SHIM_MODE", "lock-once")

# Canned message that matches two Test-LockError needles in install.ps1
# ('failed to remove directory' and 'used by another process'). Modeled
# after a real uv error from CI run
# https://github.com/microsoft/conductor/actions/runs/25672191042.
LOCK_ERROR = (
    "error: failed to remove directory "
    "`C:\\fake\\conductor-cli\\Scripts`: "
    "The process cannot access the file because it is "
    "being used by another process. (os error 32)\n"
)

# Canned message matching the network-block needles in both install
# scripts. Modeled after uv hitting a proxy that returns 403 for the
# public index, which is what a managed device with a package-registry
# block actually produces.
NETWORK_ERROR = (
    "error: Failed to fetch: `https://pypi.org/simple/httpx/`\n"
    "  Caused by: HTTP status client error (403 Forbidden) for url "
    "(https://pypi.org/simple/httpx/)\n"
)

args = sys.argv[1:]
is_install_force = args[:2] == ["tool", "install"] and "--force" in args[2:]

if is_install_force:
    attempt = int(state.read_text()) if state.exists() else 0
    attempt += 1
    state.write_text(str(attempt))
    if mode == "network-always":
        sys.stderr.write(NETWORK_ERROR)
        sys.stderr.flush()
        sys.exit(2)
    if mode == "lock-once" and attempt == 1:
        sys.stderr.write(LOCK_ERROR)
        sys.stderr.flush()
        sys.exit(2)

# Defer to the real uv with the same args, cwd, env, and stdio.
sys.exit(subprocess.run([real_uv, *args]).returncode)
