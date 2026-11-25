"""Installed helper startup independence and public package export compatibility."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest


@pytest.mark.parametrize("dependency", ["package_version", "workspace_backend"])
def test_installed_helper_does_not_initialize_unrelated_package_dependencies(
    tmp_path: Path, dependency: str
) -> None:
    # Requirement: native custody bootstrap needs neither package metadata nor workspace backend.
    guards = {
        "package_version": """\
import importlib.metadata
original = importlib.metadata.version
def version(name: str) -> str:
    assert name != "conductor-cli", "helper requested package metadata"
    return original(name)
importlib.metadata.version = version
""",
        "workspace_backend": """\
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType
class Guard(MetaPathFinder):
    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        assert fullname != "conductor.execution.local", "helper initialized workspace backend"
        return None
sys.meta_path.insert(0, Guard())
""",
    }
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(guards[dependency], encoding="utf-8")
    name = "conductor-local-scope.exe" if sys.platform == "win32" else "conductor-local-scope"
    installed = Path(sysconfig.get_path("scripts")) / name
    launch = tmp_path / "launch"
    launch.mkdir()
    helper = launch / name
    shutil.copyfile(installed, helper)
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    (launch / "descriptor.json").write_text('{"v": 0}', encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(guard), env.get("PYTHONPATH"))))

    result = subprocess.run(
        [str(helper)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 125, result.stderr.decode(errors="replace")
    assert result.stdout == b""
    assert result.stderr == (
        b"conductor-local-scope: infrastructure failure" + os.linesep.encode("ascii")
    )


def test_public_package_exports_preserve_real_values_and_identity(tmp_path: Path) -> None:
    # Requirement: deferred exports remain discoverable, retain real identity, and support star.
    program = """\
from importlib.metadata import version
import conductor
import conductor.execution as execution
assert set(conductor.__all__) <= set(dir(conductor))
assert set(execution.__all__) <= set(dir(execution))
from conductor import __version__
from conductor.execution import LocalRunnerBackend
from conductor.execution.local import LocalRunnerBackend as direct
assert isinstance(__version__, str) and __version__ == version("conductor-cli")
assert conductor.__version__ is __version__
assert LocalRunnerBackend is direct is execution.LocalRunnerBackend
root_exports = {}
execution_exports = {}
exec("from conductor import *", root_exports)
exec("from conductor.execution import *", execution_exports)
assert root_exports["__version__"] is __version__
assert execution_exports["LocalRunnerBackend"] is direct
for package in (conductor, execution):
    assert not hasattr(package, "not_a_public_export")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
