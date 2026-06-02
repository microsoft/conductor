"""Test-only ``uv`` shim used by ``test_install_scripts.py``.

Copied into a per-test directory by ``_install_uv_shim``; the directory
is prepended to ``PATH`` so ``install.ps1``'s bare ``uv`` invocations
resolve to the shim's ``uv.bat`` (which exec's this script).

Behavior: intercepts ``uv tool install --force ...`` and, on the first
such call, writes a canned lock-error to stderr and exits non-zero.
Every other call (``tool dir``, ``tool update-shell``, the retried
install, etc.) is forwarded to the real ``uv`` unchanged.

Stateful via the file referenced by ``CONDUCTOR_TEST_SHIM_STATE``;
the real ``uv`` path is read from ``CONDUCTOR_TEST_REAL_UV`` (captured
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

args = sys.argv[1:]
is_install_force = args[:2] == ["tool", "install"] and "--force" in args[2:]

if is_install_force:
    attempt = int(state.read_text()) if state.exists() else 0
    attempt += 1
    state.write_text(str(attempt))
    if attempt == 1:
        # Canned message that matches two Test-LockError needles in
        # install.ps1 ('failed to remove directory' and 'used by another
        # process'). Modeled after a real uv error from CI run
        # https://github.com/microsoft/conductor/actions/runs/25672191042.
        sys.stderr.write(
            "error: failed to remove directory "
            "`C:\\fake\\conductor-cli\\Scripts`: "
            "The process cannot access the file because it is "
            "being used by another process. (os error 32)\n"
        )
        sys.stderr.flush()
        sys.exit(2)

# Defer to the real uv with the same args, cwd, env, and stdio.
sys.exit(subprocess.run([real_uv, *args]).returncode)
