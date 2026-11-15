"""Fake Docker executable shared by realm and event-parity tests."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.types import BundleRef, McpServerSpec, ResolvedExecutionSpec, RunSpec


@pytest.fixture
def fake_realm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DockerRunnerBackend, Path, Path]:
    """Isolate Docker calls in a stateful fake CLI under this test's tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    helper = Path(__file__).with_name("docker_fake.py")
    if sys.platform == "win32":
        # Requirement: snapshot CMD's default prompt without replacing an operator's value.
        monkeypatch.setenv("PROMPT", os.environ.get("PROMPT", "$P$G"))
        binary = bin_dir / "docker.cmd"
        binary.write_text(f'@"{sys.executable}" "{helper}" %*\n', encoding="utf-8")
    else:
        binary = bin_dir / "docker"
        binary.write_text(
            f"#!{sys.executable}\nexec(open({str(helper)!r}).read())\n", encoding="utf-8"
        )
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.jsonl"
    state = tmp_path / "state.json"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    return DockerRunnerBackend(binary.name), log, state


@pytest.fixture
def mcp_run(tmp_path: Path) -> RunSpec:
    """Provide a fenced bundle without consulting a real Docker daemon."""
    tree = tmp_path / "bundle/tree/main"
    tree.mkdir(parents=True)
    (tree / "payload").write_text("shared", encoding="utf-8")
    return RunSpec(
        "mcp-test",
        bundle=BundleRef("sha256:mcp", str(tree.parent.parent)),
        mcp_fence_execution=ResolvedExecutionSpec("server:image"),
    )


@pytest.fixture
def mcp_spec() -> McpServerSpec:
    """Exercise the direct target's payload and full profile option boundary."""
    return McpServerSpec(
        "files",
        ("server", "--stdio"),
        {"ONLY_PAYLOAD": "TOP_SECRET"},
        working_dir="nested",
        execution=ResolvedExecutionSpec(
            "server:image",
            network="none",
            user="1000",
            init=True,
            read_only=True,
            cap_drop_all=True,
            no_new_privileges=True,
            tmpfs="16m",
            cpu=1.5,
            memory="128m",
            pids=32,
        ),
    )
