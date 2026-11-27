"""Exercise OCI transfer through the installed CLI and a real TLS registry."""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Final, NoReturn

import anyio
import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

pytestmark = pytest.mark.docker_integration

_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "oci-registry"
_IMAGE: Final = (
    "registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
)
_DIGEST: Final = r"sha256:[0-9a-f]{64}"
_ENTRYPOINT: Final = "tree/main/workflow.yaml"


def _run(
    command: list[str], env: Mapping[str, str], *, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def base_env(tmp_path: Path) -> Mapping[str, str]:
    """Allowlist subprocess environment before probing Docker or starting the CLI."""
    home = tmp_path / "docker-home"
    docker_config = home / ".docker"
    docker_config.mkdir(parents=True)
    return {
        **{name: os.environ[name] for name in ("PATH", "SYSTEMROOT") if name in os.environ},
        "HOME": str(home),
        "USERPROFILE": str(home),
        "DOCKER_CONFIG": str(docker_config),
        "TMPDIR": str(tmp_path),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "CONDUCTOR_HOME": str(home / ".conductor"),
        "CONDUCTOR_NO_UPDATE_CHECK": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
    }


def _docker_unavailable(reason: str) -> NoReturn:
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        pytest.fail(f"Docker daemon unavailable in CI: {reason}")
    pytest.skip(f"Docker daemon unavailable: {reason}")


async def _wait_for_registry_listener(command: list[str], env: Mapping[str, str]) -> None:
    # Docker replays startup output before following, so the listen event cannot be missed.
    async with await anyio.open_process(command, env=env, stderr=subprocess.STDOUT) as logs:
        assert logs.stdout is not None
        output = bytearray()
        try:
            with anyio.move_on_after(15):
                async for chunk in logs.stdout:
                    output.extend(chunk)
                    if b'msg="listening on [::]:5000, tls"' in output:
                        return
            pytest.fail(
                f"Registry did not report its TLS listener: {output.decode(errors='replace')}"
            )
        finally:
            with suppress(ProcessLookupError):
                logs.kill()


@pytest.fixture
def registry(base_env: Mapping[str, str]) -> Iterator[str]:
    """Start a loopback-only registry and remove only the container we created."""
    docker = which("docker", path=base_env.get("PATH", ""))
    if docker is None:
        return _docker_unavailable("Docker CLI not found")
    try:
        probe = _run([docker, "info", "--format", "{{.ServerVersion}}"], base_env, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _docker_unavailable(str(exc))
    if probe.returncode:
        _docker_unavailable(f"docker info exited {probe.returncode}: {probe.stderr.strip()}")

    created = _run(
        [
            docker,
            "create",
            "--publish",
            "127.0.0.1::5000",
            "--mount",
            f"type=bind,source={_FIXTURES},target=/test-fixtures,readonly",
            "--tmpfs",
            "/var/lib/registry",
            "--env",
            "REGISTRY_HTTP_TLS_CERTIFICATE=/test-fixtures/server.pem",
            "--env",
            "REGISTRY_HTTP_TLS_KEY=/test-fixtures/server.key",
            "--env",
            "REGISTRY_AUTH=htpasswd",
            "--env",
            "REGISTRY_AUTH_HTPASSWD_REALM=Conductor OCI test",
            "--env",
            "REGISTRY_AUTH_HTPASSWD_PATH=/test-fixtures/htpasswd",
            _IMAGE,
        ],
        base_env,
    )
    assert created.returncode == 0, created.stderr
    container = created.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", container), created.stdout
    try:
        started = _run([docker, "start", container], base_env)
        assert started.returncode == 0, started.stderr
        port = _run([docker, "port", container, "5000/tcp"], base_env)
        assert port.returncode == 0, port.stderr
        authority = port.stdout.strip()
        assert re.fullmatch(r"127\.0\.0\.1:[0-9]+", authority), port.stdout

        anyio.run(_wait_for_registry_listener, [docker, "logs", "--follow", container], base_env)
        context = ssl.create_default_context(cafile=str(_FIXTURES / "ca.pem"))
        with httpx.Client(verify=context, trust_env=False, timeout=1) as client:
            anonymous = client.get(f"https://{authority}/v2/")
        assert anonymous.status_code == 401, anonymous.text
        assert anonymous.headers["www-authenticate"].lower().startswith("basic ")
        yield authority
    finally:
        removed = _run([docker, "rm", "--force", "--volumes", container], base_env, timeout=30)
        assert removed.returncode == 0, (
            f"Could not remove owned container {container}: {removed.stderr}"
        )


def _cli_env(base_env: Mapping[str, str], home: Path, authority: str) -> Mapping[str, str]:
    home.mkdir()
    config = home / ".docker"
    config.mkdir()
    auth = base64.b64encode(b"conductor-test:conductor-oci-test-password").decode("ascii")
    (config / "config.json").write_text(
        json.dumps({"auths": {authority: {"auth": auth}}}), encoding="utf-8"
    )
    return {
        **{name: value for name, value in base_env.items() if name != "DOCKER_CONFIG"},
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CONDUCTOR_HOME": str(home / ".conductor"),
        "REQUESTS_CA_BUNDLE": str(_FIXTURES / "ca.pem"),
    }


def test_oci_bundle_round_trip_over_https_basic(
    tmp_path: Path, registry: str, base_env: Mapping[str, str]
) -> None:
    # Requirement: the real CLI transfers a verified bundle into a fresh CAS via TLS and Basic auth.
    workflow = tmp_path / "workflow.yaml"
    workflow.write_bytes((_FIXTURES.parent / "valid_simple.yaml").read_bytes())
    executable = Path(sys.executable).with_name("conductor.exe" if os.name == "nt" else "conductor")
    command = [str(executable), "bundle"]
    repository = f"oci://{registry}/test/runbundle"
    push_env = _cli_env(base_env, tmp_path / "push-home", registry)
    pull_env = _cli_env(base_env, tmp_path / "pull-home", registry)

    pushed = _run(
        [*command, "push", str(workflow), "--to", f"{repository}:round-trip", "--auth", "basic"],
        push_env,
    )
    assert pushed.returncode == 0, pushed.stdout + pushed.stderr
    manifest = re.search(
        rf"^OCI manifest: ({re.escape(repository)}@{_DIGEST})$", pushed.stdout, re.M
    )
    bundle = re.search(rf"^Bundle digest: ({_DIGEST})$", pushed.stdout, re.M)
    assert manifest and bundle, pushed.stdout
    assert f"Entrypoint: {_ENTRYPOINT}" in pushed.stdout.splitlines()

    pull_cache = Path(pull_env["CONDUCTOR_HOME"]) / "cache" / "bundles"
    assert not pull_cache.exists()
    pulled = _run([*command, "pull", manifest[1], "--auth", "basic"], pull_env)
    assert pulled.returncode == 0, pulled.stdout + pulled.stderr
    assert f"OCI manifest: {manifest[1]}" in pulled.stdout.splitlines()
    assert f"Bundle digest: {bundle[1]}" in pulled.stdout.splitlines()
    assert f"Entrypoint: {_ENTRYPOINT}" in pulled.stdout.splitlines()
    key = bundle[1].replace(":", "-")
    # Requirement: online pull prints the verified CAS path as its last stdout line.
    assert pulled.stdout.splitlines()[-1] == f"Store: {pull_cache / key}"
    assert (pull_cache / key / _ENTRYPOINT).read_bytes() == workflow.read_bytes()
