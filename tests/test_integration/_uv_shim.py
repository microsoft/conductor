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
    On *every* call, write a canned blocked-package-index error. Used to
    prove the install scripts classify the failure and stop retrying,
    instead of burning the full backoff schedule on a failure that
    cannot heal.

``network-after-first``
    An ordinary failure first, then the blocked-index error. Proves the
    early exit is evaluated on every attempt rather than only the first.

``lock-and-network-always``
    Output matching *both* the lock and index classifiers, on every
    call. Proves the lock fallback cannot starve the index classifier —
    the fallback runs once and the next attempt is still classified.

``other-always``
    An ordinary build failure with no network signature. The negative
    direction: the full retry schedule must still run and no index
    guidance may be printed.

``transient-then-other``
    A connection-level blip on attempt 2, an ordinary build failure
    otherwise. A blip must not cut the retry schedule short, and the run
    must be reported as whatever it *finally* failed on.

``git-host-always``
    A git-remote failure. uv words this the same way it words an index
    fetch failure, so this proves the two are told apart.

Every other call (``tool dir``, ``tool update-shell``, ...) is forwarded
to the real ``uv`` unchanged in every mode.

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

# An ordinary failure with no network signature at all. The negative
# control: the scripts must retry this on the full schedule and must not
# print index guidance for it.
OTHER_ERROR = (
    "error: Failed to build `pyaudio==0.2.14`\n"
    "  Caused by: src/pyaudio/device_api.c:9:10: fatal error: "
    "portaudio.h: No such file or directory\n"
)

# A connection-level blip. Retrying really can fix this, so it must not
# cut the retry schedule short even though it does name a network fault.
TRANSIENT_ERROR = (
    "error: Failed to download `httpx==0.28.1`\n  Caused by: connection reset by peer\n"
)

# uv's wording for an unreachable *git* remote. Note it says "failed to
# fetch" exactly as an index failure does -- which is why the scripts
# need a dedicated git check rather than relying on that needle.
GIT_HOST_ERROR = (
    "error: Git operation failed\n"
    "  Caused by: failed to fetch into: /home/u/.cache/uv/git-v0/db/4a951fc9\n"
    "  Caused by: failed to fetch branch or tag `v0.1.30`\n"
)

_MODES = {
    "lock-once",
    "network-always",
    "network-after-first",
    "lock-and-network-always",
    "other-always",
    "transient-then-other",
    "git-host-always",
}

args = sys.argv[1:]
is_install_force = args[:2] == ["tool", "install"] and "--force" in args[2:]

if mode not in _MODES:
    # Falling through to the real uv would run a genuine network install and
    # surface as a confusing "install should have failed" much later.
    sys.stderr.write(f"_uv_shim: unknown CONDUCTOR_TEST_SHIM_MODE {mode!r}\n")
    sys.exit(64)

if is_install_force:
    attempt = int(state.read_text()) if state.exists() else 0
    attempt += 1
    state.write_text(str(attempt))

    canned: str | None = None
    if mode == "network-always":
        canned = NETWORK_ERROR
    elif mode == "other-always":
        canned = OTHER_ERROR
    elif mode == "git-host-always":
        canned = GIT_HOST_ERROR
    elif mode == "lock-and-network-always":
        canned = LOCK_ERROR + NETWORK_ERROR
    elif mode == "network-after-first":
        canned = OTHER_ERROR if attempt == 1 else NETWORK_ERROR
    elif mode == "transient-then-other":
        canned = TRANSIENT_ERROR if attempt == 2 else OTHER_ERROR
    elif mode == "lock-once" and attempt == 1:
        canned = LOCK_ERROR

    if canned is not None:
        sys.stderr.write(canned)
        sys.stderr.flush()
        sys.exit(2)

# Defer to the real uv with the same args, cwd, env, and stdio.
sys.exit(subprocess.run([real_uv, *args]).returncode)
