"""Fake Docker executable shared by realm and event-parity tests."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from conductor.execution.docker import DockerRunnerBackend


@pytest.fixture
def fake_realm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DockerRunnerBackend, Path, Path]:
    """Isolate Docker calls in a stateful fake CLI under this test's tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    helper = Path(__file__).with_name("docker_fake.py")
    if sys.platform == "win32":
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
