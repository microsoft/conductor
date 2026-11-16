"""Real-daemon integration lane for the Docker runner backend.

These tests exercise ``DockerRunnerBackend`` and the engine's Docker wiring
against a genuine Docker daemon. They are marked ``docker_integration`` and
excluded from the default test run (``make test`` / CI matrix); the dedicated
CI job and ``-m docker_integration`` select them explicitly.

Skip discipline: every test depends on the ``docker_daemon`` fixture, which
probes ``docker info`` once per module and skips with a clear reason when no
daemon is available. A test that fails against a live daemon always FAILS --
only the absence of the daemon itself may produce a skip.

Local development: push the branch first, then build the base image with::

    docker build --build-arg CONDUCTOR_VERSION=<pushed-sha> \
      -t conductor-agent-realm:ci docker/aca-runner

For a fork, also pass `--build-arg CONDUCTOR_REPOSITORY=https://github.com/<owner>/conductor.git`.
The image must hold the same commit the host runs: a runner built from an older
commit fails the runner protocol check once ``main`` has changed it.

Set
``CONDUCTOR_TEST_RUNNER_IMAGE=conductor-agent-realm:ci`` before running this module. The
CI lane installs the commit it checked out (``github.sha``), which for a pull request is
GitHub's merge commit in the base repository, so the image matches the tested code even
when the PR branch predates a change on ``main``.
For an unpublished checkout, omit CONDUCTOR_TEST_RUNNER_IMAGE; the fixture
builds fixtures/runner_image/Dockerfile.local with ``pip install /src`` from
this checkout. That local variant does not prove the git-ref CI lane.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import textwrap
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import Any, TextIO
from unittest import mock
from unittest.mock import MagicMock
from uuid import uuid4

import anyio
import httpx
import psutil
import pytest
from anyio.abc import ByteReceiveStream
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from conductor.config.environment import resolve_environment
from conductor.config.loader import load_config
from conductor.engine import workflow as workflow_module
from conductor.engine.checkpoint import CheckpointManager, CheckpointTrigger
from conductor.engine.context import WorkflowContext
from conductor.engine.limits import LimitEnforcer
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ExecutionError
from conductor.execution.docker import (
    _AUXILIARY_TIMEOUT_SECONDS,
    _STAGING_COPY_MODE,
    DockerRunnerBackend,
)
from conductor.execution.types import (
    AgentSpec,
    BundleRef,
    CommandSpec,
    ResolvedExecutionSpec,
    RunOutcome,
    RunSpec,
)

# Pinned digest, resolved 2026-10-01 from docker.io/library/busybox:latest via
# `docker pull busybox:latest` + `docker inspect --format '{{index .RepoDigests 0}}'`
# on Docker Engine 29.7.2. The CI job pre-pulls this exact reference so the
# lane never depends on a moving tag for the reproducibility cases.
PINNED_BUSYBOX = "busybox@sha256:fd7dc98638c8e305f4dc34e979f1c0fdfdcaeb0fbf8fcff77ae834b6da3d7e6e"
# Tag form: the frictionless authoring case (main e2e path).
BUSYBOX_TAG = "busybox:latest"

pytestmark = pytest.mark.docker_integration

_CLI_TIMEOUT_SECONDS = 180.0


@pytest.fixture(scope="module")
def runner_image(
    docker_daemon: str, tmp_path_factory: pytest.TempPathFactory
) -> Generator[str, None, None]:
    """Build the test-only provider layer over the CI's SHA-pinned runner."""
    del docker_daemon
    base = os.environ.get("CONDUCTOR_TEST_RUNNER_IMAGE")
    image = _unique("agent-fixture")
    receipt_path = tmp_path_factory.getbasetemp() / "runner-image.json"
    receipt: dict[str, JsonValue] = {"tag": image, "registered": True}
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    fixture_dir = Path(__file__).parent / "fixtures" / "runner_image"
    if base:
        command = [
            "docker",
            "build",
            "-t",
            image,
            "--build-arg",
            f"BASE_IMAGE={base}",
            str(fixture_dir),
        ]
    else:
        command = [
            "docker",
            "build",
            "-t",
            image,
            "-f",
            str(fixture_dir / "Dockerfile.local"),
            str(Path(__file__).parents[2]),
        ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    receipt["build_exit"] = result.returncode
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert result.returncode == 0, (
        f"build test runner from {base or 'local context'}: {result.stderr}"
    )
    inspected = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stderr
    receipt["metadata"] = json.loads(inspected.stdout)
    try:
        yield image
    finally:
        removed = subprocess.run(
            ["docker", "image", "rm", image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        receipt["remove_exit"] = removed.returncode
        receipt["removal_output"] = removed.stdout
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        assert removed.returncode == 0, removed.stderr


@pytest.fixture(scope="module")
def docker_daemon() -> str:
    """Probe the daemon once per module; skip the whole lane when absent.

    The probe answers only "does a daemon exist" -- it never decides whether
    an individual test passed or failed."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"Docker daemon unavailable (docker info failed: {exc})")
    if result.returncode != 0:
        pytest.skip(f"Docker daemon unavailable: {result.stderr.strip()}")
    return result.stdout.strip()


async def _docker(
    *args: str, stdin: bytes | None = None, timeout: float = _CLI_TIMEOUT_SECONDS
) -> tuple[int, str, str]:
    """Run one Docker CLI command and return (rc, stdout, stderr)."""
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
    assert process.returncode is not None
    return (
        process.returncode,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


async def _remove_container(name: str) -> None:
    with contextlib.suppress(Exception):
        await _docker("rm", "-f", name)


async def _remove_volume(name: str) -> None:
    with contextlib.suppress(Exception):
        await _docker("volume", "rm", "-f", name)


async def _assert_gone(name: str) -> None:
    rc, stdout, _stderr = await _docker("ps", "-aq", "--filter", f"name={name}")
    assert rc == 0
    assert not [line for line in stdout.split() if line], f"container {name} still exists"


async def _workspace_volume_ids() -> set[str]:
    rc, stdout, _stderr = await _docker(
        "volume", "ls", "--format", "{{.Name}}", "--filter", "name=conductor-ws-"
    )
    assert rc == 0
    return set(stdout.split())


def _unique(suffix: str) -> str:
    return f"conductor-itest-{suffix}-{uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_spike1_trailing_dot_copy_into_created_container(
    docker_daemon: str, tmp_path: Path
) -> None:
    # Requirement (SPIKE-1, integration lane): `docker cp <dir>/.` into a
    # created-but-never-started container populates the named-volume root on
    # the pinned busybox digest -- the staging primitive DockerRunnerBackend
    # relies on. Re-verified here on every lane run, not just at spike time.
    del docker_daemon  # the fixture's job (daemon probe + skip) is done
    volume = _unique("spike1-vol")
    stage = _unique("spike1-stage")
    reader = _unique("spike1-read")
    source = tmp_path / "src"
    source.mkdir()
    (source / "payload.txt").write_text("spike-data\n", encoding="utf-8")
    try:
        rc, _stdout, stderr = await _docker("volume", "create", volume)
        assert rc == 0, stderr
        rc, _stdout, stderr = await _docker(
            "create", "--name", stage, "-v", f"{volume}:/workspace", PINNED_BUSYBOX
        )
        assert rc == 0, stderr
        rc, _stdout, stderr = await _docker("cp", f"{source}/.", f"{stage}:/workspace")
        assert rc == 0, stderr
        rc, _stdout, stderr = await _docker(
            "create",
            "--name",
            reader,
            "-v",
            f"{volume}:/workspace",
            PINNED_BUSYBOX,
            "cat",
            "/workspace/payload.txt",
        )
        assert rc == 0, stderr
        rc, stdout, stderr = await _docker("start", "--attach", reader)
        assert rc == 0, stderr
        assert stdout == "spike-data\n"
    finally:
        await _remove_container(stage)
        await _remove_container(reader)
        await _remove_volume(volume)


@pytest.mark.asyncio
async def test_spike2_non_root_writability_matrix_and_staging_mode(
    docker_daemon: str, tmp_path: Path
) -> None:
    # Requirement (SPIKE-2, integration lane): the ordered non-root
    # writability candidate matrix holds on the pinned busybox digest --
    # candidate (i) plain `docker cp` + scratch `--user 65532:65532` fails,
    # candidate (ii) `docker cp -a` + scratch `--user` succeeds -- and the
    # mechanism pinned in docker.py's _STAGING_COPY_MODE must not drift from
    # that spike result. If the constant changes without a fresh spike, this
    # test fails rather than letting the staging mechanism change silently.
    del docker_daemon
    assert _STAGING_COPY_MODE == "archive-to-container-user", (
        "_STAGING_COPY_MODE drifted from the spike finding recorded in "
        "docs/design/docker-backend.md (candidate 2: docker cp -a + scratch "
        "--user). Re-run the spike before changing the mechanism constant."
    )
    source = tmp_path / "src"
    source.mkdir()
    (source / "payload.txt").write_text("candidate\n", encoding="utf-8")

    async def _write_probe(volume: str, label: str) -> tuple[int, str]:
        writer = _unique(f"spike2-write-{label}")
        try:
            rc, _stdout, stderr = await _docker(
                "create",
                "--name",
                writer,
                "--user",
                "65532:65532",
                "-v",
                f"{volume}:/workspace",
                PINNED_BUSYBOX,
                "sh",
                "-c",
                "printf writable >> /workspace/payload.txt && cat /workspace/payload.txt",
            )
            assert rc == 0, stderr
            rc, stdout, stderr = await _docker("start", "--attach", writer)
            return rc, stdout + stderr
        finally:
            await _remove_container(writer)

    results: dict[str, tuple[int, str]] = {}
    try:
        # Candidate (i): plain docker cp + scratch --user -- must FAIL.
        volume = _unique("spike2-c1")
        stage = _unique("spike2-c1-stage")
        try:
            await _docker("volume", "create", volume)
            await _docker(
                "create",
                "--name",
                stage,
                "--user",
                "65532:65532",
                "-v",
                f"{volume}:/workspace",
                PINNED_BUSYBOX,
            )
            rc, _stdout, stderr = await _docker("cp", f"{source}/.", f"{stage}:/workspace")
            assert rc == 0, stderr
            results["1-plain-cp"] = await _write_probe(volume, "c1")
        finally:
            await _remove_container(stage)
            await _remove_volume(volume)
        assert results["1-plain-cp"][0] != 0, (
            f"candidate 1 unexpectedly writable: {results['1-plain-cp'][1]!r}"
        )

        # Candidate (ii): docker cp -a + scratch --user -- must SUCCEED. This
        # is the chosen mechanism, so the payload must show the appended text.
        volume = _unique("spike2-c2")
        stage = _unique("spike2-c2-stage")
        try:
            await _docker("volume", "create", volume)
            await _docker(
                "create",
                "--name",
                stage,
                "--user",
                "65532:65532",
                "-v",
                f"{volume}:/workspace",
                PINNED_BUSYBOX,
            )
            rc, _stdout, stderr = await _docker("cp", "-a", f"{source}/.", f"{stage}:/workspace")
            assert rc == 0, stderr
            results["2-archive-cp"] = await _write_probe(volume, "c2")
        finally:
            await _remove_container(stage)
            await _remove_volume(volume)
        rc2, out2 = results["2-archive-cp"]
        assert rc2 == 0 and "writable" in out2, f"candidate 2 failed: rc={rc2} out={out2!r}"

        # Candidate (iii): POSIX chmod -R a+rwX staging + plain cp -- recorded
        # for the matrix; the ordered spike stops at the first success, so
        # this candidate must complete but does not drive the mechanism.
        chmod_source = tmp_path / "src-chmod"
        chmod_source.mkdir()
        (chmod_source / "payload.txt").write_text("candidate\n", encoding="utf-8")
        os.chmod(chmod_source / "payload.txt", 0o666)
        volume = _unique("spike2-c3")
        stage = _unique("spike2-c3-stage")
        try:
            await _docker("volume", "create", volume)
            await _docker(
                "create",
                "--name",
                stage,
                "--user",
                "65532:65532",
                "-v",
                f"{volume}:/workspace",
                PINNED_BUSYBOX,
            )
            rc, _stdout, stderr = await _docker("cp", f"{chmod_source}/.", f"{stage}:/workspace")
            assert rc == 0, stderr
            results["3-chmod-cp"] = await _write_probe(volume, "c3")
        finally:
            await _remove_container(stage)
            await _remove_volume(volume)
        rc3, out3 = results["3-chmod-cp"]
        assert rc3 == 0 and "writable" in out3, (
            f"candidate 3 failed on this daemon: rc={rc3} out={out3!r}"
        )
    finally:
        # The matrix result is part of the lane's evidence trail.
        print(f"SPIKE-2 matrix on {PINNED_BUSYBOX}: {results}")


@pytest.mark.asyncio
async def test_spike3_remote_daemon_e2e(
    docker_daemon: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement (SPIKE-3): the backend works against a REMOTE daemon
    # (DOCKER_HOST), not only the local socket -- the backend shells out to
    # the Docker CLI, so a remote daemon is exercised through the CLI's own
    # transport. Opt-in via CONDUCTOR_TEST_REMOTE_DOCKER_HOST; without it the
    # test skips with this reason.
    del docker_daemon
    remote = os.environ.get("CONDUCTOR_TEST_REMOTE_DOCKER_HOST")
    if not remote:
        pytest.skip(
            "CONDUCTOR_TEST_REMOTE_DOCKER_HOST is not set; the remote-daemon "
            "e2e is opt-in (e.g. tcp://docker-host:2376)"
        )
    monkeypatch.setenv("DOCKER_HOST", remote)
    # Requirement: the backend snapshots its environment at construction, so
    # DOCKER_HOST must be set before the backend is built.
    backend = DockerRunnerBackend("docker")
    lease = await backend.prepare_run(RunSpec(run_id=_unique("remote")[:32]))
    try:
        result = await backend.run_command(
            CommandSpec(
                command="echo",
                args=("remote-ok",),
                execution=ResolvedExecutionSpec(image=PINNED_BUSYBOX),
            ),
            lease,
        )
        assert result.outcome == "completed", f"remote run failed: {result.start_error}"
        assert result.exit_code == 0
        assert result.stdout.strip() == "remote-ok"
    finally:
        await backend.finalize_run(lease, "succeeded")
        rc, stdout, _stderr = await _docker(
            "volume",
            "ls",
            "--format",
            "{{.Name}}",
            "--filter",
            f"name=conductor-ws-{lease.lease_id}",
        )
        assert rc == 0
        assert not stdout.strip(), f"remote workspace volume leaked: {stdout.strip()}"


def _write_docker_e2e_workflow(
    tmp_path: Path,
    *,
    name: str,
    image: str,
    step_fields: str,
) -> Path:
    """Write a provider-free script-only workflow + docker environment document.

    The workflow name doubles as the bundle sentinel: the in-container step
    greps for it in /workspace/main/<name>.yaml, proving the command ran
    against the staged bundle closure.
    """
    environment_dir = tmp_path / ".conductor" / "environments"
    environment_dir.mkdir(parents=True)
    (environment_dir / "itest.yaml").write_text(
        textwrap.dedent(
            f"""\
            default: local
            profiles:
              local:
                backend: local
              container:
                backend: docker
                docker:
                  image: {image}
            """
        ),
        encoding="utf-8",
    )
    workflow = tmp_path / f"{name}.yaml"
    workflow.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: __NAME__
              entry_point: probe
              runtime:
                provider: copilot
            agents:
              - name: probe
                type: script
            __STEP_FIELDS__
                execution:
                  profile: container
                routes:
                  - to: $end
            output:
              result: "{{ probe.output.stdout }}"
            """
        )
        .replace("__NAME__", name)
        .replace("__STEP_FIELDS__", textwrap.indent(step_fields.rstrip(), "    ")),
        encoding="utf-8",
    )
    return workflow


async def _run_docker_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str, image: str, step_fields: str
) -> dict[str, Any]:
    """Run one script step on a docker profile through the real engine."""
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    workflow = _write_docker_e2e_workflow(tmp_path, name=name, image=image, step_fields=step_fields)
    environment = resolve_environment("itest", workflow_dir=workflow.parent)
    engine = WorkflowEngine(
        load_config(workflow),
        MagicMock(),
        workflow_path=workflow,
        execution_environment=environment,
    )
    return await engine.run({})


@pytest.mark.asyncio
async def test_e2e_tag_form_image_runs_step_in_container(
    docker_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement (main frictionless check): a TAG-form image in the docker
    # profile runs a script step in a real container -- the step reads a file
    # that exists only inside the staged bundle closure, proving both the
    # container execution and the /workspace/main staging layout. After
    # finalize, the run-scoped workspace volume must be gone.
    del docker_daemon
    volumes_before = await _workspace_volume_ids()
    result = await _run_docker_e2e(
        tmp_path,
        monkeypatch,
        name="e2e-tag",
        image=BUSYBOX_TAG,
        step_fields=(
            "command: sh\n"
            'args: [-c, "grep -q e2e-tag /workspace/main/e2e-tag.yaml && echo IN-CONTAINER"]'
        ),
    )
    assert result["result"].strip() == "IN-CONTAINER"
    volumes_after = await _workspace_volume_ids()
    assert volumes_after == volumes_before, (
        f"workspace volume leaked: before={volumes_before} after={volumes_after}"
    )


@pytest.mark.asyncio
async def test_e2e_digest_pinned_image_reproducibility(
    docker_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement (reproducibility case): the digest-pinned busybox reference
    # runs the identical flow, so the lane never depends on what the moving
    # :latest tag happens to be for correctness assertions.
    del docker_daemon
    volumes_before = await _workspace_volume_ids()
    result = await _run_docker_e2e(
        tmp_path,
        monkeypatch,
        name="e2e-digest",
        image=PINNED_BUSYBOX,
        step_fields=(
            "command: sh\n"
            'args: [-c, "grep -q e2e-digest /workspace/main/e2e-digest.yaml'
            ' && echo IN-CONTAINER"]'
        ),
    )
    assert result["result"].strip() == "IN-CONTAINER"
    volumes_after = await _workspace_volume_ids()
    assert volumes_after == volumes_before, (
        f"workspace volume leaked: before={volumes_before} after={volumes_after}"
    )


@pytest.mark.asyncio
async def test_e2e_authored_command_overrides_image_entrypoint(
    docker_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: an image ENTRYPOINT cannot turn the authored executable into its first arg.
    del docker_daemon
    image = _unique("entrypoint")
    try:
        rc, _stdout, stderr = await _docker(
            "build",
            "-t",
            image,
            "-",
            stdin=f'FROM {PINNED_BUSYBOX}\nENTRYPOINT ["sh"]\n'.encode(),
        )
        assert rc == 0, stderr
        result = await _run_docker_e2e(
            tmp_path,
            monkeypatch,
            name="e2e-entrypoint",
            image=image,
            step_fields='command: sh\nargs: [-c, "echo ENTRYPOINT-OVERRIDDEN"]',
        )
        assert result["result"].strip() == "ENTRYPOINT-OVERRIDDEN"
    finally:
        await _docker("image", "rm", "-f", image)


@pytest.mark.asyncio
async def test_e2e_staging_image_without_command_defaults(
    docker_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: staging works for images without CMD or ENTRYPOINT when a binary exists.
    del docker_daemon
    image = _unique("no-defaults")
    try:
        rc, _stdout, stderr = await _docker(
            "build",
            "-t",
            image,
            "-",
            stdin=f"FROM {PINNED_BUSYBOX}\nENTRYPOINT []\nCMD []\n".encode(),
        )
        assert rc == 0, stderr
        rc, stdout, stderr = await _docker(
            "image",
            "inspect",
            "--format",
            "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}",
            image,
        )
        assert rc == 0, stderr
        entrypoint, command = stdout.strip().split("|")
        assert entrypoint in {"null", "[]"} and command in {"null", "[]"}
        result = await _run_docker_e2e(
            tmp_path,
            monkeypatch,
            name="e2e-no-defaults",
            image=image,
            step_fields='command: sh\nargs: [-c, "echo NO-DEFAULTS-OK"]',
        )
        assert result["result"].strip() == "NO-DEFAULTS-OK"
    finally:
        await _docker("image", "rm", "-f", image)


@pytest.mark.asyncio
async def test_e2e_stdin_payload_reaches_container_command(
    docker_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement (stdin E2E): a script step whose command reads stdin (`cat`)
    # receives the `stdin:` bytes end-to-end -- this exercises the
    # `--interactive` create + `start --attach` plumbing against the real
    # daemon, not just the canned fake CLI.
    del docker_daemon
    result = await _run_docker_e2e(
        tmp_path,
        monkeypatch,
        name="e2e-stdin",
        image=PINNED_BUSYBOX,
        step_fields='command: cat\nstdin: "hello-from-stdin-payload"',
    )
    assert result["result"] == "hello-from-stdin-payload"


@pytest.mark.asyncio
async def test_e2e_step_env_value_reaches_container(
    docker_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement (env E2E): the container sees the VALUE of a step `env:`
    # entry -- the value travels in the Docker CLI process environment with
    # only the NAME in argv (the argv-shape half is pinned daemon-free in
    # test_docker_backend.py), and the workload output matches the authored
    # value exactly.
    del docker_daemon
    result = await _run_docker_e2e(
        tmp_path,
        monkeypatch,
        name="e2e-env",
        image=PINNED_BUSYBOX,
        step_fields=(
            "command: sh\n"
            "args: [-c, 'printf %s \"$CONDUCTOR_ITEST_VALUE\"']\n"
            "env:\n"
            "  CONDUCTOR_ITEST_VALUE: visible-in-container"
        ),
    )
    assert result["result"] == "visible-in-container"


@pytest.mark.asyncio
async def test_e2e_timed_out_script_cleans_up_container_and_volume(
    docker_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement (cancellation/timeout case): a long-sleep script step with a
    # per-command timeout surfaces as a timed-out ExecutionError, the
    # container is removed (docker ps grep-negation), and engine finalization
    # still removes the run-scoped workspace volume.
    del docker_daemon
    volumes_before = await _workspace_volume_ids()
    with pytest.raises(ExecutionError, match="timed out"):
        await _run_docker_e2e(
            tmp_path,
            monkeypatch,
            name="e2e-timeout",
            image=PINNED_BUSYBOX,
            step_fields="command: sleep\nargs: ['30']\ntimeout: 2",
        )
    # Requirement: no conductor-managed container from this run survives; the
    # timed-out workload container is removed by run_command's own cleanup.
    rc, stdout, _stderr = await _docker("ps", "-aq", "--filter", "label=io.conductor.managed=true")
    assert rc == 0
    assert not stdout.strip(), f"conductor containers leaked: {stdout.strip()}"
    volumes_after = await _workspace_volume_ids()
    assert volumes_after == volumes_before, (
        f"workspace volume leaked: before={volumes_before} after={volumes_after}"
    )


def _agent_bundle(tmp_path: Path) -> tuple[BundleRef, Path]:
    """Publish a minimal skill topology into the actual Docker staging layout."""
    store = tmp_path / "bundle"
    (store / "tree/main").mkdir(parents=True)
    staged_skill = store / "tree/skills/review/SKILL.md"
    staged_skill.parent.mkdir(parents=True)
    text = "---\nname: review\ndescription: Runtime skill\n---\nSKILL_FROM_BUNDLE"
    staged_skill.write_text(text, encoding="utf-8")
    skill = tmp_path / "skills/review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(text, encoding="utf-8")
    (store / "bundle.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "logical_path": "tree/skills/review/SKILL.md",
                        "digest": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                    }
                ],
                "skills_topology": {"review": "tree/skills/review/SKILL.md"},
                "plugins_topology": {},
            }
        ),
        encoding="utf-8",
    )
    return (
        BundleRef(
            digest="sha256:realm-test",
            store_path=str(store),
            source_roots=((str(tmp_path), "main"),),
            agent_paths=((str(skill), "skills/review"),),
        ),
        skill,
    )


async def _assert_realm_resources_gone(run_id: str) -> None:
    for argv in (
        ("ps", "-aq", "--filter", f"label=io.conductor.run_id={run_id}"),
        ("volume", "ls", "-q", "--filter", f"label=io.conductor.run_id={run_id}"),
    ):
        rc, stdout, stderr = await _docker(*argv)
        assert rc == 0, stderr
        assert not stdout.strip(), f"orphaned Docker resource for {run_id}: {stdout}"


@pytest.mark.asyncio
async def test_agent_runner_reuses_container_and_reads_staged_skill_and_script_output(
    docker_daemon: str, runner_image: str, tmp_path: Path
) -> None:
    # Requirement: script and agent share a volume; the agent opens the staged skill twice
    # on the same persistent runner, and finalization leaves no labeled resources.
    del docker_daemon
    run_id = _unique("agent-volume")
    bundle, skill = _agent_bundle(tmp_path)
    backend = DockerRunnerBackend("docker")
    lease = await backend.prepare_run(RunSpec(run_id=run_id, bundle=bundle))
    try:
        script = await backend.run_command(
            CommandSpec(
                command="sh",
                args=("-c", "printf ARTIFACT_FROM_SCRIPT > /workspace/main/artifact.txt"),
                execution=ResolvedExecutionSpec(image=PINNED_BUSYBOX),
                inherit_control_environment=False,
            ),
            lease,
        )
        assert script.outcome == "completed" and script.exit_code == 0, (
            script.start_error,
            script.stderr,
        )
        spec = AgentSpec(
            name="probe",
            execution_id="first",
            model_provider="copilot",
            model=None,
            rendered_prompt="read:",
            execution=ResolvedExecutionSpec(image=runner_image),
            skill_directories=(str(skill),),
        )
        first = await backend.run_agent(spec, lease)
        runners = await _docker(
            "ps",
            "-q",
            "--filter",
            f"label=io.conductor.run_id={run_id}",
            "--filter",
            "label=io.conductor.resource=runner",
        )
        assert runners[0] == 0 and len(runners[1].splitlines()) == 1
        runner = runners[1].strip()
        rc, health, stderr = await _docker(
            "exec", runner, "python", "-m", "conductor.runner.bridge", "--health"
        )
        assert rc == 0, stderr
        assert json.loads(health)["protocol_version"] == 2
        second = await backend.run_agent(
            AgentSpec(
                name="probe",
                execution_id="second",
                model_provider="copilot",
                model=None,
                rendered_prompt="read:",
                execution=ResolvedExecutionSpec(image=runner_image),
                skill_directories=(str(skill),),
            ),
            lease,
        )
        assert first.content == second.content
        assert "ARTIFACT_FROM_SCRIPT" in first.content["answer"]
        assert "SKILL_FROM_BUNDLE" in first.content["answer"]
        assert (await _docker("ps", "-q", "--filter", f"id={runner}"))[1].strip() == runner
    finally:
        await backend.finalize_run(lease, "succeeded")
    await _assert_realm_resources_gone(run_id)


@pytest.mark.asyncio
async def test_agent_interrupt_and_cancel_remove_owned_resources(
    docker_daemon: str, runner_image: str, tmp_path: Path
) -> None:
    # Requirement: the bridge interrupt returns a partial result; outer cancellation
    # invalidates its runner and a label-filtered sweep finds no orphaned resources.
    del docker_daemon
    run_id = _unique("agent-cancel")
    bundle, _skill = _agent_bundle(tmp_path)
    backend = DockerRunnerBackend("docker")
    lease = await backend.prepare_run(RunSpec(run_id=run_id, bundle=bundle))
    signal = asyncio.Event()
    streamed = asyncio.Event()
    spec = AgentSpec(
        name="probe",
        execution_id="interrupt-me",
        model_provider="copilot",
        model=None,
        rendered_prompt="interrupt:",
        execution=ResolvedExecutionSpec(image=runner_image),
    )
    try:
        task = asyncio.create_task(
            backend.run_agent(
                spec, lease, interrupt_signal=signal, on_event=lambda _kind, _data: streamed.set()
            )
        )
        await asyncio.wait_for(streamed.wait(), 30)
        signal.set()
        result = await asyncio.wait_for(task, 30)
        assert result.partial is True and result.content == {"answer": "interrupted"}

        streamed.clear()
        task = asyncio.create_task(
            backend.run_agent(
                AgentSpec(
                    name="probe",
                    execution_id="cancel-me",
                    model_provider="copilot",
                    model=None,
                    rendered_prompt="interrupt:",
                    execution=ResolvedExecutionSpec(image=runner_image),
                ),
                lease,
                on_event=lambda _kind, _data: streamed.set(),
            )
        )
        await asyncio.wait_for(streamed.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await backend.finalize_run(lease, "cancelled")
    await _assert_realm_resources_gone(run_id)


@pytest.mark.asyncio
async def test_agent_for_each_calls_overlap_across_exclusive_runners(
    docker_daemon: str,
    runner_image: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: two for_each agents overlap on separate runner containers
    # while sharing the same staged workspace volume.
    del docker_daemon
    from conductor.providers.copilot import CopilotProvider

    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "fixture-credential")
    environment_dir = tmp_path / ".conductor/environments"
    environment_dir.mkdir(parents=True)
    (environment_dir / "itest.yaml").write_text(
        "default: local\nprofiles:\n  local:\n    backend: local\n"
        "  container:\n    backend: docker\n    docker:\n"
        f"      image: {PINNED_BUSYBOX}\n      runner_image: {runner_image}\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "overlap.yaml"
    workflow.write_text(
        "workflow:\n  name: overlap\n  entry_point: loop\n"
        "  runtime:\n    provider: copilot\n"
        "agents: []\nfor_each:\n  - name: loop\n    type: for_each\n"
        "    source: workflow.input.items\n    as: item\n    max_concurrent: 2\n"
        "    agent:\n      name: worker\n      prompt: 'overlap: {{ item }}'\n"
        "      output:\n        answer:\n          type: string\n"
        "      execution:\n        profile: container\n"
        "    routes:\n      - to: $end\n"
        "output:\n  count: '{{ loop.outputs | length }}'\n",
        encoding="utf-8",
    )
    emitter = WorkflowEventEmitter()
    events: list[WorkflowEvent] = []
    emitter.subscribe(events.append)
    engine = WorkflowEngine(
        load_config(workflow),
        CopilotProvider(mock_handler=lambda *_: {"answer": "HOST_FALLBACK"}),
        workflow_path=workflow,
        execution_environment=resolve_environment("itest", workflow_dir=tmp_path),
        event_emitter=emitter,
    )
    volumes_before = await _workspace_volume_ids()
    result = await asyncio.wait_for(engine.run({"items": ["a", "b"]}), 60)
    assert result["count"] == 2
    completed = [event for event in events if event.type == "for_each_item_completed"]
    assert len(completed) == 2
    assert all("HOST_FALLBACK" not in str(event.data) for event in completed)
    assert await _workspace_volume_ids() == volumes_before
    rc, containers, stderr = await _docker(
        "ps", "-aq", "--filter", "label=io.conductor.resource=runner"
    )
    assert rc == 0 and not containers.strip(), stderr


@pytest.mark.asyncio
async def test_agent_realm_works_on_remote_docker_host(
    docker_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a remote daemon needs neither a published runner port nor a host bind mount.
    del docker_daemon
    remote = os.environ.get("CONDUCTOR_TEST_REMOTE_DOCKER_HOST")
    if not remote:
        pytest.skip("CONDUCTOR_TEST_REMOTE_DOCKER_HOST is not set")
    monkeypatch.setenv("DOCKER_HOST", remote)
    base = os.environ.get("CONDUCTOR_TEST_REMOTE_RUNNER_IMAGE", "conductor-agent-realm:ci")
    image = _unique("remote-agent-image")
    fixture_dir = Path(__file__).parent / "fixtures/runner_image"
    rc, _stdout, stderr = await _docker(
        "build",
        "-t",
        image,
        "--build-arg",
        f"BASE_IMAGE={base}",
        str(fixture_dir),
        timeout=300,
    )
    assert rc == 0, stderr
    run_id = _unique("remote-agent")
    bundle, _skill = _agent_bundle(tmp_path)
    backend = DockerRunnerBackend("docker")
    lease = await backend.prepare_run(RunSpec(run_id=run_id, bundle=bundle))
    try:
        result = await backend.run_agent(
            AgentSpec(
                name="probe",
                execution_id="remote",
                model_provider="copilot",
                model=None,
                rendered_prompt="read:",
                execution=ResolvedExecutionSpec(image=image),
            ),
            lease,
        )
        assert result.content == {"answer": "|"}
    finally:
        await backend.finalize_run(lease, "succeeded")
        await _docker("image", "rm", "-f", image)
    await _assert_realm_resources_gone(run_id)


@pytest.mark.asyncio
async def test_retained_failure_then_resumed_success_removes_volume(
    docker_daemon: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: resume preserves generated and modified bundle files exactly, then cleans up.
    del docker_daemon
    workflow = _write_docker_e2e_workflow(
        tmp_path,
        name="retained-daemon",
        image=PINNED_BUSYBOX,
        step_fields=(
            "command: sh\n"
            "args: [-c, 'if [ -f /workspace/generated.txt ]; then "
            "cat /workspace/generated.txt /workspace/main/retained-daemon.yaml; "
            'else printf "generated-before-resume\\n" > /workspace/generated.txt; '
            'printf "modified-bundle-before-resume\\n" > '
            "/workspace/main/retained-daemon.yaml; fi']"
        ),
    )
    workflow.write_text(
        workflow.read_text(encoding="utf-8").replace(
            "  entry_point: probe",
            "  entry_point: probe\n  workspace:\n    persistence: on-failure",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    environment = resolve_environment("itest", workflow_dir=workflow.parent)
    run_id = _unique("retained")
    volume = f"conductor-ws-{run_id}"
    try:
        engine = WorkflowEngine(
            load_config(workflow),
            None,
            workflow_path=workflow,
            execution_environment=environment,
            run_context=workflow_module.RunContext(run_id=run_id),
        )
        with (
            mock.patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path),
            mock.patch.object(
                engine.context, "store", side_effect=RuntimeError("after docker dispatch")
            ),
            pytest.raises(RuntimeError, match="after docker dispatch"),
        ):
            await engine.run({})
        assert engine._last_checkpoint_path is not None
        checkpoint = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
        assert checkpoint.workspace is not None
        assert checkpoint.workspace["executed_backends"] == ["docker"]
        rc, _stdout, stderr = await _docker("volume", "inspect", volume)
        assert rc == 0, stderr

        resumed = WorkflowEngine(
            load_config(workflow),
            None,
            workflow_path=workflow,
            execution_environment=environment,
            run_context=workflow_module.RunContext(run_id=run_id),
        )
        resumed.set_context(WorkflowContext.from_dict(checkpoint.context))
        resumed.set_limits(
            LimitEnforcer.from_dict(
                checkpoint.limits,
                timeout_seconds=resumed.config.workflow.limits.timeout_seconds,
                budget_usd=resumed.config.workflow.limits.budget_usd,
                budget_mode=resumed.config.workflow.limits.budget_mode,
            )
        )
        result = await resumed.resume(checkpoint.current_agent, checkpoint=checkpoint)
        assert result["result"] == ("generated-before-resume\nmodified-bundle-before-resume\n")
        rc, _stdout, stderr = await _docker("volume", "inspect", volume)
        assert rc != 0 and "no such volume" in stderr.lower()
    finally:
        await _remove_volume(volume)


class _LiveConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    Labels: dict[str, str]


class _LiveMount(BaseModel):
    model_config = ConfigDict(frozen=True)
    Name: str | None = None
    Destination: str
    Type: str


class _LiveContainer(BaseModel):
    model_config = ConfigDict(frozen=True)
    Id: str
    Name: str
    Config: _LiveConfig
    State: dict[str, JsonValue]
    Mounts: list[_LiveMount]


class _LiveVolume(BaseModel):
    model_config = ConfigDict(frozen=True)
    Name: str
    Labels: dict[str, str] | None
    CreatedAt: str


class _LiveMcpCli:
    """Own one CLI scenario, its private homes, and exact daemon resource receipts."""

    def __init__(self, root: Path, image: str) -> None:
        self.root = root
        self.image = image
        self.run_id = _unique("mcp")
        self.volume = f"conductor-ws-{self.run_id}"
        self.workflow = root / "live.yaml"
        self.environment = root / "environment.yaml"
        self.commands: list[dict[str, str | int]] = []
        self.dispositions: list[dict[str, str | int]] = []
        self.hooks: dict[str, tuple[str, int]] = {}
        self.owned_containers: dict[str, _LiveContainer] = {}
        self.workspace_proof: _LiveVolume | None = None
        self.qa_replacement: tuple[_LiveVolume, str] | None = None

    def configure(
        self, mode: str = "cooperative", *, resume: bool = False, mcp_only: bool = False
    ) -> None:
        """Author a real workflow without changing product admission or transport code."""
        self.environment.write_text(
            json.dumps(
                {
                    "default": "container",
                    "profiles": {
                        "container": {
                            "backend": "docker",
                            "docker": {"image": self.image, "network": "none"},
                        }
                    },
                    "secrets": {"token": {"source": {"env": "TASK8_CANARY"}, "allow": ["mcp"]}},
                }
            ),
            encoding="utf-8",
        )
        agents: list[dict[str, JsonValue]] = [
            {
                "name": "setup",
                "type": "script",
                "working_dir": ".",
                "command": "sh",
                "args": ["-c", "mkdir generated; printf 'seed\\n' > generated/value.txt"],
                "routes": [{"to": "first"}],
            },
            *[
                {
                    "name": name,
                    "type": "mcp",
                    "server": "peer",
                    "tool": "mutate",
                    "arguments": {"path": "value.txt"},
                    "timeout": 2 if mode in {"timeout", "uncooperative"} else 30,
                    "routes": [{"to": target}],
                }
                for name, target in (("first", "second"), ("second", "observe"))
            ],
            {
                "name": "observe",
                "type": "script",
                "working_dir": "generated",
                "command": "python",
                "args": [
                    "-S",
                    "-c",
                    "from pathlib import Path; print(Path('value.txt').read_text(), end='')",
                ],
                "routes": [{"to": "$end"}],
            },
        ]
        if resume:
            agents[2]["routes"] = [{"to": "retry"}]
            agents.insert(
                3,
                {
                    "name": "retry",
                    "type": "mcp",
                    "server": "peer",
                    "tool": "mutate",
                    "arguments": {"path": "value.txt", "fail_once": True},
                    "routes": [{"to": "observe"}],
                },
            )
        if mcp_only:
            agents = agents[1:2]
            agents[0]["routes"] = [{"to": "$end"}]
            # This file exists even in a silently auto-created replacement.
            # Omitting post-spawn volume validation would therefore complete
            # the tool exchange, rather than fail incidentally on a missing file.
            agents[0]["arguments"] = {"path": "/workspace/peer-history.jsonl"}
        self.workflow.write_text(
            json.dumps(
                {
                    "workflow": {
                        "name": "live-mcp",
                        "entry_point": "first" if mcp_only else "setup",
                        "workspace": {"persistence": "durable"},
                        "runtime": {
                            "working_dir": "." if mcp_only else "generated",
                            "mcp_servers": {
                                "peer": {
                                    "command": "python",
                                    "args": [
                                        "-S",
                                        "-c",
                                        (
                                            Path(__file__).parents[1]
                                            / "test_mcp/fixtures/live_docker_stdio.py"
                                        ).read_text(encoding="utf-8"),
                                    ],
                                    "env": {"PEER_MODE": mode},
                                    "secrets": [
                                        {
                                            "ref": "token",
                                            "scope": "mcp",
                                            "delivery": {"env": "PAYLOAD_TOKEN"},
                                        }
                                    ],
                                }
                            },
                        },
                    },
                    "agents": agents,
                    "output": {"result": "{{ observe.output.stdout }}"},
                }
            ),
            encoding="utf-8",
        )

    async def cli(self, arguments: list[str], *, home: str = "controller-a") -> int:
        """Execute the operator CLI in a fresh isolated home, preserving Docker context."""
        directory = self.root / home
        directory.mkdir(exist_ok=True)
        temp = directory / "tmp"
        temp.mkdir(exist_ok=True)
        environment = {
            **os.environ,
            "HOME": str(directory),
            "USERPROFILE": str(directory),
            "CONDUCTOR_HOME": str(directory / ".conductor"),
            "TMPDIR": str(temp),
            "TEMP": str(temp),
            "TMP": str(temp),
            "CONDUCTOR_RUN_ID": self.run_id,
            "TASK8_CANARY": "task8-private-canary-94623",
        }
        entrypoint = ["-m", "conductor"]
        if home in self.hooks:
            hook, port = self.hooks[home]
            environment["TASK8_QA_HOOK"] = hook
            environment["TASK8_QA_PORT"] = str(port)
            environment["TASK8_QA_RECEIPT"] = str(directory / "qa-hooks.jsonl")
            entrypoint = [
                "-c",
                f"import runpy; runpy.run_path({str(Path(__file__))!r})['_mcp_qa_controller']()",
            ]
        command = [
            sys.executable,
            "-P",
            *entrypoint,
            *arguments,
            "--environment",
            str(self.environment),
            "--no-interactive",
            "--skip-gates",
            "--log-file",
            str(directory / "operator.log"),
        ]
        index = len(self.commands)
        receipt: dict[str, str | int] = {
            "command": " ".join(command),
            "exit": "pending",
            "home": home,
            "run_id": self.run_id,
            "expected_volume": self.volume,
            "image": self.image,
        }
        self.commands.append(receipt)
        (self.root / "commands.json").write_text(json.dumps(self.commands), encoding="utf-8")

        async def drain(stream: ByteReceiveStream, suffix: str) -> None:
            with (self.root / f"cli-{index}.{suffix}").open("wb") as sink:
                async for chunk in stream:
                    sink.write(chunk)

        with anyio.fail_after(180):
            process = await anyio.open_process(
                command, env=environment, cwd=self.root, stdin=subprocess.DEVNULL
            )
            try:
                async with process:
                    receipt["pid"] = process.pid
                    (self.root / "commands.json").write_text(
                        json.dumps(self.commands), encoding="utf-8"
                    )
                    assert process.stdout is not None and process.stderr is not None
                    async with anyio.create_task_group() as group:
                        group.start_soon(drain, process.stdout, "stdout")
                        group.start_soon(drain, process.stderr, "stderr")
                        result = await process.wait()
            finally:
                assert process.returncode is not None
                receipt["exit"] = process.returncode
                (self.root / "commands.json").write_text(
                    json.dumps(self.commands), encoding="utf-8"
                )
        return result

    async def containers(self) -> list[_LiveContainer]:
        """Inspect this run's resources; label filtering alone never authorizes disposal."""
        rc, output, error = await _docker(
            "ps", "-aq", "--filter", f"label=io.conductor.run_id={self.run_id}"
        )
        assert rc == 0, error
        if not output.strip():
            return []
        rc, output, error = await _docker("inspect", *output.split())
        assert rc == 0, error
        containers = TypeAdapter(list[_LiveContainer]).validate_json(output)
        proof = self.workspace_proof
        if proof is None:
            rc, volume_json, error = await _docker("volume", "inspect", self.volume)
            assert rc == 0, error
            proof = TypeAdapter(list[_LiveVolume]).validate_json(volume_json)[0]
            self.workspace_proof = proof
        assert proof.Labels is not None
        for item in containers:
            labels = item.Config.Labels
            assert labels["io.conductor.managed"] == "true"
            assert labels["io.conductor.run_id"] == self.run_id
            assert labels["io.conductor.workspace"] == self.run_id
            assert labels["io.conductor.incarnation"] == (proof.Labels["io.conductor.incarnation"])
            assert labels["io.conductor.resource"] in {
                "mcp-fence",
                "mcp",
                "scratch",
                "exec",
                "runner",
            }
            for registered in self.owned_containers.values():
                if registered.Name == item.Name:
                    assert registered.Id == item.Id
                    assert registered.Config.Labels == labels
            self.owned_containers[item.Id] = item
        return containers

    def checkpoint(self) -> Path:
        """Locate a real failure checkpoint rather than fabricating resume metadata."""
        return next(
            path
            for path in (self.root / "controller-a").rglob("*.json")
            if "current_agent" in json.loads(path.read_text())
        )

    async def read_volume(self, path: str) -> str:
        """Read through a separately registered QA reader, without starting the guard."""
        return await self.volume_command(("cat", path))

    async def volume_command(self, argv: tuple[str, ...]) -> str:
        """Run a QA command only over an already verified, owned volume."""
        rc, output, error = await _docker("volume", "inspect", self.volume)
        assert rc == 0, error
        volume = TypeAdapter(list[_LiveVolume]).validate_json(output)[0]
        assert volume.Labels is not None
        assert volume.Labels["io.conductor.run_id"] == self.run_id
        if self.workspace_proof is None:
            self.workspace_proof = volume
        assert volume == self.workspace_proof
        name = _unique("qa-reader")
        allocation_path = self.root / "qa-reader-allocations.jsonl"
        with allocation_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "name": name,
                        "volume": self.volume,
                        "image": self.image,
                        "phase": "registered",
                    }
                )
                + "\n"
            )
        rc, identifier, error = await _docker(
            "create",
            "--name",
            name,
            "-v",
            f"{self.volume}:/workspace",
            "--entrypoint",
            argv[0],
            self.image,
            *argv[1:],
        )
        assert rc == 0, error
        identifier = identifier.strip()
        with allocation_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"name": name, "id": identifier, "phase": "created"}) + "\n")
        try:
            rc, output, error = await _docker("start", "-a", identifier)
            assert rc == 0, error
            return output
        finally:
            rc, _output, error = await _docker("rm", "-f", identifier)
            assert rc == 0, error
            self.dispositions.append({"kind": "qa-reader", "id": identifier, "remove_exit": rc})
            (self.root / "qa-reader-receipts.json").write_text(
                json.dumps(self.dispositions), encoding="utf-8"
            )

    async def dispose(self) -> None:
        """Dispose QA resources separately, after every CLI process has been joined."""
        native: list[int] = []
        qa_controllers = {os.getpid(), *(parent.pid for parent in psutil.Process().parents())}
        for process in psutil.process_iter(["pid", "cmdline"]):
            argv = process.info["cmdline"] or []
            if process.pid not in qa_controllers and any(
                str(self.root) in argument or argument == f"io.conductor.run_id={self.run_id}"
                for argument in argv
            ):
                native.append(process.pid)
        (self.root / "qa-native-terminal.json").write_text(
            json.dumps(
                {
                    "live_owned_pids": native,
                    "commands": self.commands,
                    "qa_launcher_pids": sorted(qa_controllers),
                }
            ),
            encoding="utf-8",
        )
        assert native == [], "QA disposal requires terminal CLI/native/SDK/staging owners"
        inventory = await self.containers()
        (self.root / "qa-before-disposal.json").write_text(
            json.dumps([item.model_dump() for item in inventory]), encoding="utf-8"
        )
        for metadata in inventory:
            registered = self.owned_containers[metadata.Id]
            assert metadata.Config.Labels == registered.Config.Labels
            assert metadata.Config.Labels["io.conductor.run_id"] == self.run_id
            assert metadata.Name.startswith("/conductor-")
            identifier = metadata.Id
            assert len(identifier) == 64
            rc, _output, error = await _docker("rm", "-f", identifier)
            assert rc == 0, error
            self.dispositions.append({"kind": "container", "id": identifier, "remove_exit": rc})
        rc, output, error = await _docker("volume", "inspect", self.volume)
        if rc == 0:
            metadata = TypeAdapter(list[_LiveVolume]).validate_json(output)[0]
            if metadata.Labels is None:
                if self.qa_replacement is None:
                    self.dispositions.append(
                        {
                            "kind": "unverified-volume",
                            "name": self.volume,
                            "created": metadata.CreatedAt,
                            "disposition": "preserved; no adoption or removal",
                        }
                    )
                else:
                    expected, creator_id = self.qa_replacement
                    assert metadata == expected and len(creator_id) == 64
                    rc, _output, error = await _docker("volume", "rm", self.volume)
                    assert rc == 0, error
                    self.dispositions.append(
                        {
                            "kind": "qa-replacement-disposal",
                            "name": self.volume,
                            "created": metadata.CreatedAt,
                            "creator_id": creator_id,
                            "product_ownership": "unverified; never adopted",
                            "remove_exit": rc,
                        }
                    )
            else:
                assert metadata.Labels["io.conductor.run_id"] == self.run_id
                rc, _output, error = await _docker("volume", "rm", self.volume)
                assert rc == 0, error
                self.dispositions.append(
                    {
                        "kind": "volume",
                        "name": self.volume,
                        "created": metadata.CreatedAt,
                        "incarnation": metadata.Labels["io.conductor.incarnation"],
                        "remove_exit": rc,
                    }
                )
        else:
            assert "no such volume" in error.lower()
        (self.root / "qa-disposal.json").write_text(json.dumps(self.dispositions), encoding="utf-8")


@pytest.fixture
def live_mcp(tmp_path: Path, runner_image: str) -> _LiveMcpCli:
    return _LiveMcpCli(tmp_path, runner_image)


async def test_mcp_cli_relative_round_trip_reuse_and_retained_resume(
    live_mcp: _LiveMcpCli,
) -> None:
    # Requirement: real CLI calls share the script-created cwd/volume; resume starts a new peer.
    live_mcp.configure(resume=True)
    try:
        assert await live_mcp.cli(["run", str(live_mcp.workflow)]) == 1
        history = [
            json.loads(line)
            for line in (await live_mcp.read_volume("/workspace/peer-history.jsonl")).splitlines()
        ]
        calls = [entry for entry in history if entry["event"] == "call"]
        assert len(calls) == 3 and len({entry["container"] for entry in calls}) == 1
        assert all(entry["cwd"] == "/workspace/main/generated" for entry in calls)
        assert not (live_mcp.root / "generated").exists()
        assert await live_mcp.containers() == []
        checkpoints = list((live_mcp.root / "controller-a").rglob("*.json"))
        checkpoint = next(
            path
            for path in checkpoints
            if json.loads(path.read_text()).get("current_agent") == "retry"
        )
        assert await live_mcp.cli(["resume", "--from", str(checkpoint)], home="controller-b") == 0
        history = [
            json.loads(line)
            for line in (await live_mcp.read_volume("/workspace/peer-history.jsonl")).splitlines()
        ]
        calls = [entry for entry in history if entry["event"] == "call"]
        assert len(calls) == 4 and calls[-1]["container"] != calls[0]["container"]
        assert await live_mcp.read_volume("/workspace/main/generated/value.txt") == (
            "seed\nmcp\nmcp\nmcp\n"
        )
        assert await live_mcp.containers() == []
        assert json.loads((live_mcp.root / "cli-1.stdout").read_text())["result"] == (
            "seed\nmcp\nmcp\nmcp\n"
        )
    finally:
        await live_mcp.dispose()


class _CliPause:
    """An event-driven QA controller channel; no time-based synchronization."""

    def __init__(self, root: Path) -> None:
        self.receipt = root / "qa-ports.jsonl"
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(90)
        self.port = self.listener.getsockname()[1]
        self.peers: list[socket.socket] = []
        with self.receipt.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"port": self.port, "disposition": "registered"}) + "\n")

    async def event(self) -> tuple[socket.socket, dict[str, JsonValue]]:
        with anyio.fail_after(90):
            peer, _address = await anyio.to_thread.run_sync(self.listener.accept)
            self.peers.append(peer)
            peer.settimeout(90)
            with peer.makefile("rb") as stream:
                payload = await anyio.to_thread.run_sync(stream.readline)
        return peer, json.loads(payload)

    def close(self) -> None:
        for peer in self.peers:
            peer.close()
        self.listener.close()
        with self.receipt.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"port": self.port, "disposition": "closed"}) + "\n")


def _mcp_qa_controller() -> None:
    """Run the real CLI with narrow QA pauses at existing native-owner boundaries."""
    import mcp.client.stdio as sdk_stdio

    from conductor.cli.app import app
    from conductor.engine.execution_resolution import ExecutionResolverSession
    from conductor.engine.mcp_lifecycle import McpRunLifecycle

    mode = os.environ["TASK8_QA_HOOK"]
    port = int(os.environ["TASK8_QA_PORT"])
    used = False
    holding = False

    def record(event: str) -> None:
        with Path(os.environ["TASK8_QA_RECEIPT"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")

    def notify(event: str, deadline: float | None, checkpoint: Path | None = None) -> None:
        record(event)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as channel:
            channel.sendall(
                json.dumps(
                    {
                        "event": event,
                        "deadline": deadline,
                        "checkpoint": str(checkpoint) if checkpoint else None,
                    }
                ).encode()
                + b"\n"
            )

    async def pause(phase: str) -> None:
        nonlocal used, holding
        if used:
            return
        used = holding = True
        record(phase)
        async with await anyio.connect_tcp("127.0.0.1", port) as channel:
            await channel.send(json.dumps({"event": phase, "pid": os.getpid()}).encode() + b"\n")
            # Deliberately model an owner that ignores cancellation. The actual
            # root deadline stays intact and its uncertainty must fence resume.
            with anyio.CancelScope(shield=True):
                cancelled = False
                while True:
                    try:
                        assert await channel.receive(1) == b"R"
                        break
                    except anyio.get_cancelled_exc_class():
                        if not cancelled:
                            record("ignored-cancellation")
                            cancelled = True
                        continue
        record("released")
        holding = False

    original_run = DockerRunnerBackend._run_docker

    async def run_docker(
        self: DockerRunnerBackend,
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        timeout: float | None = _AUXILIARY_TIMEOUT_SECONDS,
        diagnostics: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        # The QA wrapper must preserve the existing control boundary's complete signature.
        guard_create = argv[0] == "create" and any(
            str(item).startswith("conductor-mcp-fence-") for item in argv
        )
        if (mode == "stage" and tuple(argv[:2]) == ("cp", "-a")) or (
            mode == "acquire-before" and guard_create
        ):
            await pause(mode)
        result = await original_run(
            self, argv, stdin_bytes=stdin_bytes, timeout=timeout, diagnostics=diagnostics
        )
        if (
            mode == "sdk"
            and argv[0] == "inspect"
            and result[0] == 0
            and str(argv[-1]).startswith("conductor-mcp-")
            and not str(argv[-1]).startswith("conductor-mcp-fence-")
        ):
            # Observe the real daemon response without retaining Config.Env,
            # which contains the deliberately secret-bearing container payload.
            metadata = _LiveContainer.model_validate_json(result[1])
            path = Path(os.environ["TASK8_QA_RECEIPT"]).with_name("qa-targets.jsonl")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(metadata.model_dump_json() + "\n")
        if mode == "acquire-after" and guard_create:
            await pause(mode)
        return result

    DockerRunnerBackend._run_docker = run_docker
    original_spawn = sdk_stdio._create_platform_compatible_process

    async def spawn(
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        errlog: TextIO = sys.stderr,
        cwd: Path | str | None = None,
    ) -> anyio.abc.Process | sdk_stdio.FallbackProcess:
        # The SDK owns this callback signature; changing it would change the tested launch.
        if mode == "sdk":
            await pause("sdk")
        return await original_spawn(command, args, env, errlog, cwd)

    sdk_stdio._create_platform_compatible_process = spawn
    original_teardown = ExecutionResolverSession.begin_teardown

    def teardown(self: ExecutionResolverSession, outcome: RunOutcome, *, retain: bool) -> None:
        original_teardown(self, outcome, retain=retain)
        if holding:
            notify("sealed", self.cleanup_deadline)

    ExecutionResolverSession.begin_teardown = teardown
    original_uncertain = McpRunLifecycle.mark_uncertain

    def uncertain(self: McpRunLifecycle) -> None:
        first = not self.uncertain
        original_uncertain(self)
        if holding and first:
            notify("uncertain", self.deadline)

    McpRunLifecycle.mark_uncertain = uncertain
    original_dispatch = ExecutionResolverSession.record_backend_execution

    def dispatch(self: ExecutionResolverSession, name: str, *, step: str | None = None) -> None:
        original_dispatch(self, name, step=step)
        path = Path(os.environ["TASK8_QA_RECEIPT"]).with_name("qa-dispatch.jsonl")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "backends": sorted(self.executed_backends()),
                        "consumers": sorted(self._file_consumers.get(name, set())),
                    }
                )
                + "\n"
            )

    ExecutionResolverSession.record_backend_execution = dispatch
    original_checkpoint = WorkflowEngine._write_checkpoint

    def checkpoint(
        self: WorkflowEngine, error: BaseException | None, trigger: CheckpointTrigger
    ) -> Path | None:
        path = original_checkpoint(self, error, trigger)
        if path is not None and holding:
            notify("checkpoint", self._execution_session.cleanup_deadline, path)
        return path

    WorkflowEngine._write_checkpoint = checkpoint
    app()


@pytest.mark.parametrize("winner", ["first", "second"])
async def test_mcp_cli_atomic_guard_acquisition_between_independent_homes(
    live_mcp: _LiveMcpCli,
    winner: str,
) -> None:
    # Requirement: exactly one controller acquires the deterministic daemon guard before dispatch.
    live_mcp.configure(resume=True)
    first = _CliPause(live_mcp.root)
    second = _CliPause(live_mcp.root)
    results: dict[str, int] = {}
    done = {"race-a": anyio.Event(), "race-b": anyio.Event()}

    async def resume(home: str) -> None:
        results[home] = await live_mcp.cli(
            ["resume", "--from", str(live_mcp.checkpoint())], home=home
        )
        done[home].set()

    try:
        assert await live_mcp.cli(["run", str(live_mcp.workflow)]) == 1
        live_mcp.hooks["race-a"] = (
            "acquire-after" if winner == "first" else "acquire-before",
            first.port,
        )
        async with anyio.create_task_group() as group:
            group.start_soon(resume, "race-a")
            held_a, event = await first.event()
            assert event["event"] == live_mcp.hooks["race-a"][0]
            if winner == "first":
                await resume("race-b")
                assert results["race-b"] == 1
                guards = await live_mcp.containers()
                held_a.sendall(b"R")
            else:
                live_mcp.hooks["race-b"] = ("sdk", second.port)
                group.start_soon(resume, "race-b")
                held_b, event = await second.event()
                assert event["event"] == "sdk"
                guards = await live_mcp.containers()
                held_a.sendall(b"R")
                # The loser must reach its terminal CLI result while the
                # winner still holds the guard, before releasing its SDK.
                with anyio.fail_after(30):
                    await done["race-a"].wait()
                assert results["race-a"] == 1
                held_b.sendall(b"R")
        assert len(guards) == 1
        assert guards[0].Config.Labels["io.conductor.resource"] == "mcp-fence"
        assert guards[0].State["Status"] == "created" and guards[0].Mounts == []
        assert results == {
            "race-a": 0 if winner == "first" else 1,
            "race-b": 1 if winner == "first" else 0,
        }
        assert await live_mcp.containers() == []
        (live_mcp.root / "product-race.json").write_text(
            json.dumps({"winner": winner, "outcomes": results, "guard": guards[0].model_dump()}),
            encoding="utf-8",
        )
    finally:
        first.close()
        second.close()
        await live_mcp.dispose()


async def test_mcp_cli_post_staging_sdk_spawn_rejects_lost_volume_replacement(
    live_mcp: _LiveMcpCli,
) -> None:
    # Requirement: the guard has no mount; validate_started rejects an auto-created volume.
    live_mcp.configure(mcp_only=True)
    pause = _CliPause(live_mcp.root)
    live_mcp.hooks["controller-a"] = ("sdk", pause.port)
    result: list[int] = []

    async def run() -> None:
        result.append(await live_mcp.cli(["run", str(live_mcp.workflow)]))

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(run)
            held, event = await pause.event()
            assert event["event"] == "sdk"
            guards = await live_mcp.containers()
            assert len(guards) == 1 and guards[0].Mounts == []
            assert guards[0].State["Status"] == "created"
            rc, output, error = await _docker("volume", "inspect", live_mcp.volume)
            assert rc == 0, error
            original = TypeAdapter(list[_LiveVolume]).validate_json(output)[0]
            assert original.Labels is not None
            assert original.Labels["io.conductor.run_id"] == live_mcp.run_id
            rc, _output, error = await _docker("volume", "rm", live_mcp.volume)
            assert rc == 0, error
            live_mcp.dispositions.append(
                {
                    "kind": "qa-loss-injection",
                    "name": original.Name,
                    "created": original.CreatedAt,
                    "incarnation": original.Labels["io.conductor.incarnation"],
                    "remove_exit": rc,
                }
            )
            (live_mcp.root / "qa-loss-injection.json").write_text(
                json.dumps(
                    {
                        "original": original.model_dump(),
                        "original_remove_exit": rc,
                        "registered_replacement_name": live_mcp.volume,
                        "guard_id": guards[0].Id,
                        "controller_pid": event["pid"],
                    }
                ),
                encoding="utf-8",
            )
            held.sendall(b"R")
        assert result == [1]
        rc, output, error = await _docker("volume", "inspect", live_mcp.volume)
        assert rc == 0, error
        replacement = TypeAdapter(list[_LiveVolume]).validate_json(output)[0]
        assert replacement.Labels is None
        target_path = live_mcp.root / "controller-a/qa-targets.jsonl"
        target = _LiveContainer.model_validate_json(target_path.read_text().splitlines()[0])
        assert target.Config.Labels["io.conductor.run_id"] == live_mcp.run_id
        assert (
            target.Config.Labels["io.conductor.incarnation"]
            == (original.Labels["io.conductor.incarnation"])
        )
        assert any(
            mount.Type == "volume"
            and mount.Name == replacement.Name
            and mount.Destination == "/workspace"
            for mount in target.Mounts
        )
        # Product ownership remains refused. Independent QA ownership comes
        # from this run's exact creator ID and its observed replacement mount.
        live_mcp.qa_replacement = (replacement, target.Id)
        inventory = await live_mcp.containers()
        # Validation refusal is not owner uncertainty: the SDK/target cleanup
        # completed, so removing the verified guard is permitted.
        assert inventory == []
        assert (
            await live_mcp.cli(
                ["resume", "--from", str(live_mcp.checkpoint())], home="controller-b"
            )
            == 1
        )
        rc, output, error = await _docker("volume", "inspect", live_mcp.volume)
        assert rc == 0, error
        assert TypeAdapter(list[_LiveVolume]).validate_json(output)[0] == replacement
        event_files = list((live_mcp.root / "controller-a").rglob("*.events.jsonl"))
        events = [
            json.loads(line) for path in event_files for line in path.read_text().splitlines()
        ]
        assert "mcp_completed" not in [event["type"] for event in events]
        assert not any(
            event["type"] == "agent_started" and event["data"]["agent_name"] == "observe"
            for event in events
        )
        (live_mcp.root / "product-replacement-refusal.json").write_text(
            json.dumps(
                {
                    "original": original.model_dump(),
                    "replacement": replacement.model_dump(),
                    "guard": guards[0].model_dump(),
                    "replacement_creator": target.model_dump(),
                    "outcome": "startup rejected; unverified replacement left untouched",
                }
            ),
            encoding="utf-8",
        )
    finally:
        pause.close()
        await live_mcp.dispose()


@pytest.mark.parametrize("phase", ["stage", "sdk"])
async def test_mcp_cli_delayed_owner_stop_uncertainty_fences_independent_resume(
    live_mcp: _LiveMcpCli,
    phase: str,
) -> None:
    # Requirement: empty MCP inventory cannot release the guard of a pending native owner.
    live_mcp.configure()
    pause = _CliPause(live_mcp.root)
    web_reservation = _CliPause(live_mcp.root)
    web_port = web_reservation.port
    web_reservation.close()
    live_mcp.hooks["controller-a"] = (phase, pause.port)
    outcomes: list[int] = []

    async def run() -> None:
        outcomes.append(
            await live_mcp.cli(
                ["run", str(live_mcp.workflow), "--web", "--web-port", str(web_port)]
            )
        )

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(run)
            held, event = await pause.event()
            assert event["event"] == phase
            token = (
                live_mcp.root / "controller-a/.conductor/runs" / f"dashboard-{web_port}.token"
            ).read_text()
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                headers = {"Authorization": f"Bearer {token}"}
                for action in ("stop", "kill"):
                    response = await client.post(
                        f"http://127.0.0.1:{web_port}/api/{action}", headers=headers, json={}
                    )
                    assert response.status_code == 200
            messages: list[dict[str, JsonValue]] = []
            while True:
                _channel, message = await pause.event()
                messages.append(message)
                if message["event"] == "checkpoint":
                    checkpoint_path = message["checkpoint"]
                    assert isinstance(checkpoint_path, str)
                    break
            assert "uncertain" in [message["event"] for message in messages]
            deadlines = {message["deadline"] for message in messages}
            assert len(deadlines) == 1 and None not in deadlines
            inventory = await live_mcp.containers()
            guards = [
                item
                for item in inventory
                if item.Config.Labels["io.conductor.resource"] == "mcp-fence"
            ]
            assert len(guards) == 1
            assert guards[0].Mounts == [] and guards[0].State["Status"] == "created"
            assert not any(
                item.Config.Labels["io.conductor.resource"] == "mcp" for item in inventory
            )
            rc, original, error = await _docker("volume", "inspect", live_mcp.volume)
            assert rc == 0, error
            assert (
                await live_mcp.cli(["resume", "--from", checkpoint_path], home="controller-b") == 1
            )
            after = await live_mcp.containers()
            assert guards[0].Id in {item.Id for item in after}
            assert not any(item.Config.Labels["io.conductor.resource"] == "mcp" for item in after)
            rc, current, error = await _docker("volume", "inspect", live_mcp.volume)
            assert rc == 0, error
            assert json.loads(current) == json.loads(original)
            (live_mcp.root / "product-delayed-refusal.json").write_text(
                json.dumps(
                    {
                        "phase": phase,
                        "guard": guards[0].model_dump(),
                        "volume": json.loads(original),
                        "events": messages,
                        "controller_b_exit": 1,
                        "controller_a_pending_at_refusal": not outcomes,
                    }
                ),
                encoding="utf-8",
            )
            assert not outcomes
            held.sendall(b"R")
        assert outcomes == [1]
    finally:
        pause.close()
        await live_mcp.dispose()


@pytest.mark.parametrize("ownership", ["matching", "mismatched"])
async def test_mcp_cli_orphan_refusal_preserves_exact_existing_container(
    live_mcp: _LiveMcpCli,
    ownership: str,
) -> None:
    # Requirement: surviving MCP containers refuse attach, including unverified ownership.
    live_mcp.configure(resume=True)
    orphan_id: str | None = None
    orphan_labels: dict[str, str] = {}
    try:
        assert await live_mcp.cli(["run", str(live_mcp.workflow)]) == 1
        rc, output, error = await _docker("volume", "inspect", live_mcp.volume)
        assert rc == 0, error
        volume = TypeAdapter(list[_LiveVolume]).validate_json(output)[0]
        assert volume.Labels is not None
        orphan_labels = {
            key: value for key, value in volume.Labels.items() if key != "io.conductor.retention"
        } | {"io.conductor.resource": "mcp"}
        if ownership == "mismatched":
            orphan_labels["io.conductor.incarnation"] = "qa-different-incarnation"
        name = f"conductor-mcp-{live_mcp.run_id[:8]}-{uuid4().hex}"
        argv = ["create", "--name", name]
        for key, value in orphan_labels.items():
            argv.extend(("--label", f"{key}={value}"))
        rc, output, error = await _docker(
            *argv, "--entrypoint", "/qa-orphan-never-started", live_mcp.image
        )
        assert rc == 0, error
        orphan_id = output.strip()
        assert len(orphan_id) == 64
        before = await live_mcp.read_volume("/workspace/peer-history.jsonl")
        assert (
            await live_mcp.cli(
                ["resume", "--from", str(live_mcp.checkpoint())], home="controller-b"
            )
            == 1
        )
        rc, output, error = await _docker("inspect", orphan_id)
        assert rc == 0, error
        orphan = TypeAdapter(list[_LiveContainer]).validate_json(output)[0]
        assert orphan.Id == orphan_id and orphan.Config.Labels == orphan_labels
        assert orphan.State["Status"] == "created" and orphan.Mounts == []
        assert await live_mcp.read_volume("/workspace/peer-history.jsonl") == before
        (live_mcp.root / "product-orphan-refusal.json").write_text(
            json.dumps(
                {
                    "orphan": orphan.model_dump(),
                    "controller_b_exit": 1,
                    "ownership": ownership,
                    "volume": volume.model_dump(),
                }
            ),
            encoding="utf-8",
        )
    finally:
        # Separate QA disposal: both CLI calls have joined. This fixture-created
        # container is not adopted by product cleanup, even in the mismatch row.
        if orphan_id is not None:
            rc, output, error = await _docker("inspect", orphan_id)
            assert rc == 0, error
            orphan = TypeAdapter(list[_LiveContainer]).validate_json(output)[0]
            assert orphan.Id == orphan_id and orphan.Config.Labels == orphan_labels
            rc, _output, error = await _docker("rm", orphan_id)
            assert rc == 0, error
            live_mcp.dispositions.append({"kind": "qa-orphan", "id": orphan_id, "remove_exit": rc})
        await live_mcp.dispose()


@pytest.mark.parametrize("mode", ["startup-secret", "call-secret"])
async def test_mcp_cli_secret_failure_is_visible_without_payload(
    live_mcp: _LiveMcpCli,
    mode: str,
) -> None:
    # Requirement: actual startup/call stderr is suppressed in console, events and diagnostics.
    live_mcp.configure(mode)
    try:
        assert await live_mcp.cli(["run", str(live_mcp.workflow)]) == 1
        history = [
            json.loads(line)
            for line in (await live_mcp.read_volume("/workspace/peer-history.jsonl")).splitlines()
        ]
        expected = "startup-stderr-emitted" if mode == "startup-secret" else "call-stderr-emitted"
        assert [entry["event"] for entry in history].count(expected) == 1
        assert [entry["event"] for entry in history].count("initialize") == 1
        assert [entry["event"] for entry in history].count("call") == (
            0 if mode == "startup-secret" else 1
        )
        console = (live_mcp.root / "cli-0.stdout").read_text() + (
            live_mcp.root / "cli-0.stderr"
        ).read_text()
        assert "failed" in console.lower() or "error" in console.lower()
        for path in live_mcp.root.rglob("*"):
            if path.is_file():
                assert b"task8-private-canary-94623" not in path.read_bytes(), str(path)
        inventory = await live_mcp.containers()
        if mode == "startup-secret":
            assert len(inventory) == 1
            assert inventory[0].Config.Labels["io.conductor.resource"] == "mcp-fence"
            assert inventory[0].Mounts == [] and inventory[0].State["Status"] == "created"
        else:
            assert inventory == []
        (live_mcp.root / "product-refusal.json").write_text(
            json.dumps(
                {
                    "containers": [item.model_dump() for item in inventory],
                    "retained_volume": live_mcp.volume,
                }
            ),
            encoding="utf-8",
        )
    finally:
        await live_mcp.dispose()


@pytest.mark.parametrize("mode", ["is-error", "transport", "timeout", "uncooperative"])
async def test_mcp_cli_is_error_transport_timeout_and_uncooperative_cleanup(
    live_mcp: _LiveMcpCli,
    mode: str,
) -> None:
    # Requirement: is_error reuses the peer; failing exchanges have no automatic retry.
    live_mcp.configure(mode)
    try:
        assert await live_mcp.cli(["run", str(live_mcp.workflow)]) == (
            0 if mode == "is-error" else 1
        )
        history = [
            json.loads(line)
            for line in (await live_mcp.read_volume("/workspace/peer-history.jsonl")).splitlines()
        ]
        calls = [entry for entry in history if entry["event"] == "call"]
        assert len(calls) == (2 if mode == "is-error" else 1)
        assert len({entry["container"] for entry in calls}) == 1
        inventory = await live_mcp.containers()
        assert inventory == []
        (live_mcp.root / "product-cleanup.json").write_text(
            json.dumps(
                {
                    "containers": [item.model_dump() for item in inventory],
                    "retained_volume": live_mcp.volume,
                }
            ),
            encoding="utf-8",
        )
    finally:
        await live_mcp.dispose()


async def test_mcp_only_start_failure_history_marker_loss_and_resume_refusal(
    live_mcp: _LiveMcpCli,
) -> None:
    # Requirement: initialize alone records Docker history; marker loss never restages mutations.
    live_mcp.configure("startup-secret", mcp_only=True)
    live_mcp.hooks["controller-a"] = ("observe", 0)
    try:
        assert await live_mcp.cli(["run", str(live_mcp.workflow)]) == 1
        checkpoint_path = next(
            path
            for path in (live_mcp.root / "controller-a").rglob("*.json")
            if json.loads(path.read_text()).get("current_agent") == "first"
        )
        checkpoint = CheckpointManager.load_checkpoint(checkpoint_path)
        assert checkpoint.workspace is not None
        assert checkpoint.workspace["executed_backends"] == ["docker"]
        assert checkpoint.context["execution_history"] == []
        dispatches = [
            json.loads(line)
            for line in (live_mcp.root / "controller-a/qa-dispatch.jsonl").read_text().splitlines()
        ]
        assert dispatches[-1] == {"backends": ["docker"], "consumers": ["first"]}
        assert await live_mcp.read_volume("/workspace/initialize-mutated.txt") == (
            "initialize-mutated\n"
        )
        before = await live_mcp.read_volume("/workspace/peer-history.jsonl")
        original_guard = await live_mcp.containers()
        assert len(original_guard) == 1
        assert original_guard[0].Config.Labels["io.conductor.resource"] == "mcp-fence"
        assert original_guard[0].Mounts == [] and original_guard[0].State["Status"] == "created"
        await live_mcp.volume_command(("rm", "/workspace/.conductor-staged"))
        assert (
            await live_mcp.cli(["resume", "--from", str(checkpoint_path)], home="controller-b") == 1
        )
        assert await live_mcp.read_volume("/workspace/peer-history.jsonl") == before
        assert await live_mcp.read_volume("/workspace/initialize-mutated.txt") == (
            "initialize-mutated\n"
        )
        inventory = await live_mcp.containers()
        # This follows the product resume attempt, not the QA marker removal:
        # resume must neither restage the bundle nor republish its marker.
        await live_mcp.volume_command(("sh", "-c", "test ! -e /workspace/.conductor-staged"))
        assert [(item.Id, item.Config.Labels) for item in inventory] == [
            (original_guard[0].Id, original_guard[0].Config.Labels)
        ]
        (live_mcp.root / "product-refusal.json").write_text(
            json.dumps(
                {
                    "history": checkpoint.workspace,
                    "guard": [item.model_dump() for item in inventory],
                    "retained_volume": live_mcp.volume,
                }
            ),
            encoding="utf-8",
        )
    finally:
        await live_mcp.dispose()
