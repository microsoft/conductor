"""CLI reporting requirements for Docker execution profiles."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.engine.run_manifest import BACKEND_CAPABILITY_PROVIDERS
from conductor.execution.types import RunnerCapabilities

runner = CliRunner()


class _DockerCapability:
    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=True,
            sessions=False,
            shared_workspace=False,
            snapshots=False,
        )


@pytest.fixture(autouse=True)
def _docker_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(BACKEND_CAPABILITY_PROVIDERS, "docker", _DockerCapability())


def _write_workflow(root: Path, *, profile: str | None = "container") -> Path:
    execution = f"\n    execution:\n      profile: {profile}" if profile is not None else ""
    path = root / "workflow.yaml"
    path.write_text(
        f"""\
workflow:
  name: validate-docker
  entry_point: run
agents:
  - name: run
    type: script{execution}
    command: "true"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    return path


def _write_environment(root: Path, body: str) -> None:
    directory = root / ".conductor" / "environments"
    directory.mkdir(parents=True)
    (directory / "demo.yaml").write_text(body, encoding="utf-8")


def _invoke(path: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["validate", str(path), *args], color=False)
    return result.exit_code, result.output


def test_explicit_environment_reports_authored_docker_details(tmp_path: Path) -> None:
    # Requirement: the report prints every Docker profile field without daemon lookup.
    _write_environment(
        tmp_path,
        """\
default: container
profiles:
  container:
    backend: docker
    docker:
      image: python@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      platform: linux/amd64
      network: none
      user: 1000:1000
      init: true
      read_only: true
      cap_drop_all: true
      no_new_privileges: true
      tmpfs: 512m
      resources:
        cpu: 2
        memory: 4g
        pids: 512
""",
    )
    code, output = _invoke(_write_workflow(tmp_path), "--environment", "demo")

    assert code == 0
    expected_details = {
        "Docker Profile: container": None,
        "Image": "python@sha256:",
        "Platform": "linux/amd64",
        "Network": "none",
        "User": "1000:1000",
        "Init": "true",
        "Read-only": "true",
        "Cap-drop-all": "true",
        "No-new-privileges": "true",
        "Tmpfs": "512m",
        "CPU": "2.0",
        "Memory": "4g",
        "PIDs": "512",
    }
    for label, value in expected_details.items():
        assert label in output
        if value is not None:
            assert value in output
    assert "Docker runs require a complete bundle closure" in output
    assert "conductor bundle build primes" in output
    assert "the cache" in output
    assert "pin this image by digest" not in output


def test_omitted_values_render_defaults_and_mutable_tag_advice(tmp_path: Path) -> None:
    # Requirement: omitted daemon-owned values are labelled, never inferred.
    _write_environment(
        tmp_path,
        """\
default: container
profiles:
  container:
    backend: docker
    docker:
      image: busybox:latest
""",
    )
    code, output = _invoke(_write_workflow(tmp_path), "--environment", "demo")

    assert code == 0
    assert output.count("Docker default") >= 3
    assert output.count("—") >= 3
    assert "pin this image by digest" in output
    assert "reproducible CI" in output


def test_bare_local_validate_is_byte_identical_with_docker_environment(tmp_path: Path) -> None:
    # Requirement: a profile-free local workflow produces no Docker report or discovery noise.
    workflow = _write_workflow(tmp_path, profile=None)
    before_code, before = _invoke(workflow)
    _write_environment(
        tmp_path,
        """\
default: container
profiles:
  container:
    backend: docker
    docker:
      image: busybox:latest
""",
    )
    with patch("conductor.config.environment.discover_all_environments") as discover:
        after_code, after = _invoke(workflow)

    assert before_code == after_code == 0
    discover.assert_not_called()
    assert after == before
    assert "Docker" not in after


def test_bare_validate_prints_mixed_warning_from_discovery(tmp_path: Path) -> None:
    # Requirement: bare validation surfaces mixed local/Docker snapshot semantics.
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
workflow:
  name: mixed
  entry_point: host
agents:
  - name: host
    type: script
    execution:
      profile: local
    command: "true"
    routes:
      - to: box
  - name: box
    type: script
    execution:
      profile: container
    command: "true"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    _write_environment(
        tmp_path,
        """\
profiles:
  local:
    backend: local
  container:
    backend: docker
    docker:
      image: busybox:latest
""",
    )
    code, output = _invoke(workflow)
    assert code == 0
    assert "mixes local and docker script backends" in output
    assert "bundle snapshot collected before the run" in output
