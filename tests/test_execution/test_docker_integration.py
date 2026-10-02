"""Real-daemon integration lane for the Docker runner backend.

These tests exercise ``DockerRunnerBackend`` and the engine's Docker wiring
against a genuine Docker daemon. They are marked ``docker_integration`` and
excluded from the default test run (``make test`` / CI matrix); the dedicated
CI job and ``-m docker_integration`` select them explicitly.

Skip discipline: every test depends on the ``docker_daemon`` fixture, which
probes ``docker info`` once per module and skips with a clear reason when no
daemon is available. A test that fails against a live daemon always FAILS --
only the absence of the daemon itself may produce a skip.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from conductor.config.environment import resolve_environment
from conductor.config.loader import load_config
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ExecutionError
from conductor.execution.docker import _STAGING_COPY_MODE, DockerRunnerBackend
from conductor.execution.types import CommandSpec, ResolvedExecutionSpec, RunSpec

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
