"""Helpers for the install-script integration tests.

These are pytest fixtures and utilities that build versioned wheels of
``conductor-cli`` and drive ``install.ps1`` / ``install.sh`` against
isolated ``UV_TOOL_DIR`` sandboxes so the user's real install is never
touched.

Tests using these fixtures are gated behind ``-m install_scripts`` (see
``pyproject.toml``) and excluded from the default ``make test`` run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_PS1 = REPO_ROOT / "install.ps1"
INSTALL_SH = REPO_ROOT / "install.sh"

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Wheel building
# ---------------------------------------------------------------------------


def _stamp_pyproject_version(pyproject: Path, version: str) -> None:
    """Rewrite the top-level ``version = "..."`` line in pyproject.toml."""
    text = pyproject.read_text(encoding="utf-8")
    new_lines: list[str] = []
    replaced = False
    for line in text.splitlines():
        if not replaced and line.startswith("version") and "=" in line:
            new_lines.append(f'version = "{version}"')
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        raise RuntimeError(f"Could not find version line in {pyproject}")
    pyproject.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _copy_source_tree(src: Path, dst: Path) -> None:
    """Copy the source tree to ``dst`` excluding heavy/irrelevant dirs."""
    ignore = shutil.ignore_patterns(
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        "*.egg-info",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "htmlcov",
        "tests",  # tests aren't needed for the wheel
    )
    shutil.copytree(src, dst, ignore=ignore)


def build_versioned_wheel(version: str, out_dir: Path, work_root: Path) -> Path:
    """Build a wheel of conductor-cli stamped with ``version``.

    Args:
        version: PEP 440 version string (e.g. ``"0.0.1"``).
        out_dir: Directory the built wheel is copied into.
        work_root: Tmp dir used as a working area for the source copy.

    Returns:
        Absolute path to the built wheel file.
    """
    work = work_root / f"src-{version}"
    _copy_source_tree(REPO_ROOT, work)
    _stamp_pyproject_version(work / "pyproject.toml", version)

    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"uv build failed for {version}:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    # uv prints the wheel name; just look for the matching file
    wheels = sorted(out_dir.glob(f"conductor_cli-{version}*.whl"))
    if not wheels:
        raise RuntimeError(f"No wheel found for {version} in {out_dir}: {list(out_dir.iterdir())}")
    return wheels[-1]


# ---------------------------------------------------------------------------
# Sandbox + install drivers
# ---------------------------------------------------------------------------


@dataclass
class Sandbox:
    """An isolated uv tool installation environment."""

    root: Path
    tool_dir: Path  # UV_TOOL_DIR
    bin_dir: Path  # UV_TOOL_BIN_DIR
    cache_dir: Path  # UV_CACHE_DIR (shared across tests in a session)

    @property
    def conductor_exe(self) -> Path:
        if IS_WINDOWS:
            return self.tool_dir / "conductor-cli" / "Scripts" / "conductor.exe"
        return self.tool_dir / "conductor-cli" / "bin" / "conductor"

    @property
    def python_exe(self) -> Path:
        if IS_WINDOWS:
            return self.tool_dir / "conductor-cli" / "Scripts" / "python.exe"
        return self.tool_dir / "conductor-cli" / "bin" / "python"

    def env(self, extra: dict | None = None) -> dict:
        e = os.environ.copy()
        e["UV_TOOL_DIR"] = str(self.tool_dir)
        e["UV_TOOL_BIN_DIR"] = str(self.bin_dir)
        e["UV_CACHE_DIR"] = str(self.cache_dir)
        # Don't let uv touch the user's PATH registry from inside tests.
        e["UV_NO_MODIFY_PATH"] = "1"
        if extra:
            e.update({k: str(v) for k, v in extra.items()})
        return e


@dataclass
class InstallResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return f"--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}\n"


def seed_install(sandbox: Sandbox, wheel: Path) -> None:
    """Install a wheel into the sandbox via ``uv tool install --force``."""
    proc = subprocess.run(
        ["uv", "tool", "install", "--force", str(wheel)],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"seed_install failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    if not sandbox.conductor_exe.exists():
        raise RuntimeError(
            f"seed_install completed but conductor exe missing at {sandbox.conductor_exe}"
        )


def run_install_script(
    sandbox: Sandbox,
    *,
    source: Path | str,
    auto_stop: bool = True,
    force: bool = False,
    extra_env: dict | None = None,
    timeout: int = 600,
) -> InstallResult:
    """Drive install.ps1 (Windows) or install.sh (POSIX) against the sandbox.

    Always passes the source via ``--source`` / ``-Source`` and runs in
    ``--auto-stop`` mode unless overridden so any conductor process the test
    leaves behind gets reaped automatically.
    """
    extra_env = dict(extra_env or {})
    # Optional belt-and-braces: tests can opt-out of `uv tool update-shell`
    # to avoid touching the user's profile. The script itself respects
    # UV_NO_MODIFY_PATH=1 set in Sandbox.env(), but we also support a hook.
    extra_env.setdefault("CONDUCTOR_INSTALL_SKIP_PATH_UPDATE", "1")

    if IS_WINDOWS:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALL_PS1),
            "-Source",
            str(source),
        ]
        if auto_stop:
            cmd.append("-AutoStop")
        if force:
            cmd.append("-Force")
    else:
        cmd = ["sh", str(INSTALL_SH), "--source", str(source)]
        if auto_stop:
            cmd.append("--auto-stop")
        if force:
            cmd.append("--force")

    proc = subprocess.run(
        cmd,
        env=sandbox.env(extra_env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return InstallResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def get_installed_version(sandbox: Sandbox) -> str | None:
    """Run ``conductor --version`` from the sandbox and return the version."""
    if not sandbox.conductor_exe.exists():
        return None
    proc = subprocess.run(
        [str(sandbox.conductor_exe), "--version"],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return None
    # Output looks like "Conductor v0.0.1" or similar
    for token in (proc.stdout + proc.stderr).split():
        cleaned = token.lstrip("vV").strip().rstrip(",.")
        if cleaned and cleaned[0].isdigit() and "." in cleaned:
            return cleaned
    return None


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def shared_uv_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A session-scoped uv cache so deps are downloaded once across all tests."""
    return tmp_path_factory.mktemp("uv-cache")


@dataclass
class WheelPair:
    old: Path  # version 0.0.1
    new: Path  # version 0.0.2


@pytest.fixture(scope="session")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> WheelPair:
    """Build conductor-cli wheels at versions 0.0.1 (old) and 0.0.2 (new)."""
    work_root = tmp_path_factory.mktemp("wheel-build")
    out_dir = tmp_path_factory.mktemp("wheels-out")
    old = build_versioned_wheel("0.0.1", out_dir, work_root)
    new = build_versioned_wheel("0.0.2", out_dir, work_root)
    return WheelPair(old=old, new=new)


@pytest.fixture()
def sandbox(tmp_path: Path, shared_uv_cache: Path) -> Sandbox:
    """A per-test isolated uv tool sandbox."""
    tool_dir = tmp_path / "uv-tools"
    bin_dir = tmp_path / "uv-bin"
    tool_dir.mkdir()
    bin_dir.mkdir()
    return Sandbox(root=tmp_path, tool_dir=tool_dir, bin_dir=bin_dir, cache_dir=shared_uv_cache)
