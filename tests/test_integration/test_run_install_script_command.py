"""Fast, unmarked unit tests for ``run_install_script()``'s command construction.

Unlike :mod:`tests.test_integration.test_install_scripts`, which drives the
real install scripts end-to-end behind the ``install_scripts`` pytest marker
(auto-skipped by default — see issue #331), these tests mock
``subprocess.run`` so they run in microseconds as part of the default
``make test`` / plain ``pytest`` suite.

The specific thing under test is ``run_install_script()``'s
``auto_stop``-to-flag mapping: whether ``--auto-stop`` / ``-AutoStop`` is
appended to the command line. This is the exact behavior issue #331's fix
depends on (``auto_stop`` now defaults to ``False`` so no call site
accidentally drives the install scripts' host-wide, process-killing
``--auto-stop`` path). Without a fast, always-run test for it, a future
change to :func:`run_install_script` could silently flip the default back
without anything catching it outside the (skipped-by-default) E2E suite.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .install_scripts_helpers import Sandbox, run_install_script


@pytest.fixture
def fake_sandbox(tmp_path: Path) -> Sandbox:
    """A ``Sandbox`` backed by non-existent paths — fine since we never touch disk."""
    return Sandbox(
        root=tmp_path,
        tool_dir=tmp_path / "uv-tools",
        bin_dir=tmp_path / "uv-bin",
        cache_dir=tmp_path / "uv-cache",
    )


def _captured_cmd(
    fake_sandbox: Sandbox, *, auto_stop: bool = False, force: bool = False
) -> list[str]:
    """Run ``run_install_script`` with ``subprocess.run`` mocked; return the built cmd."""
    fake_proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch(
        "tests.test_integration.install_scripts_helpers.subprocess.run",
        return_value=fake_proc,
    ) as mock_run:
        run_install_script(fake_sandbox, source="fake-wheel.whl", auto_stop=auto_stop, force=force)
    args, _kwargs = mock_run.call_args
    return args[0]


def test_auto_stop_flag_omitted_by_default(fake_sandbox: Sandbox) -> None:
    """Regression guard for issue #331: no flag means no host-wide process kill."""
    cmd = _captured_cmd(fake_sandbox)
    assert "--auto-stop" not in cmd
    assert "-AutoStop" not in cmd


def test_auto_stop_flag_included_when_explicitly_requested(fake_sandbox: Sandbox) -> None:
    """Tests that need the kill-and-continue behavior can still opt in explicitly."""
    cmd = _captured_cmd(fake_sandbox, auto_stop=True)
    assert "--auto-stop" in cmd or "-AutoStop" in cmd


def test_force_flag_omitted_by_default(fake_sandbox: Sandbox) -> None:
    cmd = _captured_cmd(fake_sandbox)
    assert "--force" not in cmd
    assert "-Force" not in cmd


def test_force_flag_included_when_explicitly_requested(fake_sandbox: Sandbox) -> None:
    cmd = _captured_cmd(fake_sandbox, force=True)
    assert "--force" in cmd or "-Force" in cmd
