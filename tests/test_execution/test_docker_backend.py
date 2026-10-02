"""Contract tests for DockerRunnerBackend using a daemon-free fake CLI."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from typing import TypedDict, cast

import pytest

from conductor.execution.docker import (
    DockerRunnerBackend,
    _copy_tree_preserving_symlinks,
    _map_working_dir,
)
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import BundleRef, CommandSpec, ResolvedExecutionSpec, RunSpec


def _write_fake(bin_dir: Path) -> str:
    helper = Path(__file__).with_name("docker_fake.py")
    if sys.platform == "win32":
        wrapper = bin_dir / "docker.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{helper}" %*\n', encoding="utf-8")
    else:
        wrapper = bin_dir / "docker"
        wrapper.write_text(
            f"#!{sys.executable}\nexec(open({str(helper)!r}).read())\n", encoding="utf-8"
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return wrapper.name


@pytest.fixture
def fake_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DockerRunnerBackend, Path]:
    """Return an isolated backend and its JSONL invocation log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = _write_fake(bin_dir)
    log = tmp_path / "docker.jsonl"
    state = tmp_path / "state.json"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    return DockerRunnerBackend(binary), log


class _Record(TypedDict):
    argv: list[str]
    env: dict[str, str]
    stdin: str


def _records(log: Path) -> list[_Record]:
    return [
        cast(_Record, json.loads(line)) for line in log.read_text(encoding="utf-8").splitlines()
    ]


async def _lease(backend: DockerRunnerBackend, *, bundle: BundleRef | None = None):
    return await backend.prepare_run(RunSpec(run_id="run_ABC-1", bundle=bundle))


def _minimal() -> ResolvedExecutionSpec:
    return ResolvedExecutionSpec(image="alpine:3.20")


@pytest.mark.asyncio
async def test_capabilities_and_prepare_have_no_io(tmp_path: Path) -> None:
    # Requirement: construction, capabilities, and lease preparation never touch Docker.
    backend = DockerRunnerBackend(str(tmp_path / "missing"))
    capabilities = backend.capabilities()
    lease = await backend.prepare_run(RunSpec(run_id="run-one"))
    assert (capabilities.batch, capabilities.sessions) == (True, False)
    assert (capabilities.shared_workspace, capabilities.snapshots) == (True, False)
    assert lease.backend == "docker" and lease.lease_id == "run-one"
    assert len(lease.incarnation) == 12


@pytest.mark.asyncio
async def test_missing_payload_is_spec_error(fake_docker: tuple[DockerRunnerBackend, Path]) -> None:
    # Requirement: backend/payload inconsistency is a leaf specification error.
    backend, _log = fake_docker
    lease = await _lease(backend)
    with pytest.raises(ExecutionSpecError, match="execution specification"):
        await backend.run_command(CommandSpec(command="true"), lease)


@pytest.mark.asyncio
async def test_missing_cli_is_command_not_found(tmp_path: Path) -> None:
    # Requirement: an unresolved Docker CLI is represented as command_not_found data.
    backend = DockerRunnerBackend(str(tmp_path / "missing-docker"))
    lease = await _lease(backend)
    result = await backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    assert result.outcome == "command_not_found"
    assert result.resolved_command == str(tmp_path / "missing-docker")


@pytest.mark.asyncio
async def test_minimal_create_argv_and_secret_env(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: default create argv stays minimal and secrets travel only in process env.
    backend, log = fake_docker
    lease = await _lease(backend)
    secret = "top-secret-value"
    result = await backend.run_command(
        CommandSpec(
            command="printf",
            args=("ok",),
            env={"TOKEN": secret},
            inherit_control_environment=False,
            execution=_minimal(),
            name="step",
        ),
        lease,
    )
    assert result.outcome == "completed" and result.exit_code == 0
    create = next(row for row in _records(log) if row["argv"][0] == "create")
    argv = create["argv"]
    assert argv[-3:] == ["alpine:3.20", "printf", "ok"]
    assert [argv[index + 1] for index, value in enumerate(argv) if value == "--env"] == ["TOKEN"]
    assert secret not in json.dumps(argv)
    assert create["env"]["TOKEN"] == secret
    for forbidden in (
        "--init",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "--network",
        "--platform",
        "--user",
        "--cpus",
        "--memory",
        "--pids-limit",
        "--interactive",
    ):
        assert forbidden not in argv
    assert not any(value == "run" or value == "--rm" for value in argv)


@pytest.mark.asyncio
async def test_fully_hardened_create_and_stdin(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: every explicit hardening/resource option maps to its Docker create flag.
    backend, log = fake_docker
    lease = await _lease(backend)
    execution = ResolvedExecutionSpec(
        image="alpine:3.20",
        platform="linux/amd64",
        network="host",
        user="root",
        init=True,
        read_only=True,
        cap_drop_all=True,
        no_new_privileges=True,
        tmpfs="512m",
        cpu=1.5,
        memory="2g",
        pids=128,
    )
    result = await backend.run_command(
        CommandSpec(command="cat", stdin=b"payload", execution=execution), lease
    )
    assert result.outcome == "completed"
    records = _records(log)
    create = next(row for row in records if row["argv"][0] == "create")
    argv = create["argv"]
    for expected in (
        "--init",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "--network",
        "--platform",
        "--user",
        "--cpus",
        "--memory",
        "--pids-limit",
        "--interactive",
    ):
        assert expected in argv
    start = next(row for row in records if row["argv"][0] == "start")
    assert "--interactive" in start["argv"] and start["stdin"] == "payload"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("pullfail", "start_failed"), ("createfail", "start_failed"), ("startfail", "start_failed")],
)
async def test_infrastructure_failures(
    fake_docker: tuple[DockerRunnerBackend, Path],
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected: str,
) -> None:
    # Requirement: daemon, pull, create, and start failures are start_failed outcomes.
    backend, _log = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_SCENARIO", scenario)
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend)
    result = await backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    assert result.outcome == expected
    assert result.start_error is not None


@pytest.mark.asyncio
async def test_nonzero_container_exit_is_completed(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a started container's non-zero command exit remains routable completed data.
    _backend, _log = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_EXIT_CODE", "42")
    monkeypatch.setenv("FAKE_DOCKER_START_RC", "42")
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend)
    result = await backend.run_command(CommandSpec(command="false", execution=_minimal()), lease)
    assert result.outcome == "completed" and result.exit_code == 42


@pytest.mark.asyncio
async def test_timeout_kills_and_removes(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Requirement: timeout invokes Docker kill then rm and returns timed_out.
    _backend, log = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_DELAY_COMMAND", "start --attach")
    monkeypatch.setenv("FAKE_DOCKER_DELAY", "30")
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend)
    result = await backend.run_command(
        CommandSpec(command="sleep", timeout=0.05, execution=_minimal()), lease
    )
    assert result.outcome == "timed_out"
    commands = [row["argv"][0] for row in _records(log)]
    assert "kill" in commands and "rm" in commands


@pytest.mark.asyncio
async def test_cancel_propagates_after_cleanup(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Requirement: cancellation remains cancellation after shielded kill/rm cleanup.
    _backend, log = fake_docker
    marker = tmp_path / "started"
    monkeypatch.setenv("FAKE_DOCKER_DELAY_COMMAND", "start --attach")
    monkeypatch.setenv("FAKE_DOCKER_DELAY_MARKER", str(marker))
    monkeypatch.setenv("FAKE_DOCKER_DELAY", "30")
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend)
    task = asyncio.create_task(
        backend.run_command(CommandSpec(command="sleep", execution=_minimal()), lease)
    )
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    commands = [row["argv"][0] for row in _records(log)]
    assert "kill" in commands and "rm" in commands


@pytest.mark.parametrize(
    ("value", "mapped"),
    [
        (None, "/workspace"),
        (".", "/workspace/main"),
        ("src/pkg", "/workspace/main/src/pkg"),
        ("/app", "/app"),
    ],
)
def test_working_dir_mapping(value: str | None, mapped: str) -> None:
    # Requirement: relative paths map below main while POSIX absolute paths remain verbatim.
    assert _map_working_dir(value) == mapped


@pytest.mark.parametrize("value", [" ", "../escape", r"dir\child", "C:/repo", r"\\server\share"])
def test_working_dir_refusals(value: str) -> None:
    # Requirement: empty, escaping, backslash, drive, and UNC values fail before create.
    with pytest.raises(ExecutionSpecError, match="working_dir"):
        _map_working_dir(value)


@pytest.mark.asyncio
async def test_staging_layout_archive_copy_and_reentry(
    fake_docker: tuple[DockerRunnerBackend, Path], tmp_path: Path
) -> None:
    # Requirement: bundle main/roots are copied once via archive-preserving trailing-dot cp.
    backend, log = fake_docker
    store = tmp_path / "store"
    (store / "tree/main").mkdir(parents=True)
    (store / "tree/main/file.txt").write_text("main", encoding="utf-8")
    (store / "tree/roots/00").mkdir(parents=True)
    (store / "tree/roots/00/root.txt").write_text("root", encoding="utf-8")
    lease = await _lease(backend, bundle=BundleRef("sha256:test", str(store)))
    spec = CommandSpec(command="true", execution=replace(_minimal(), user="65532:65532"))
    await asyncio.gather(backend.run_command(spec, lease), backend.run_command(spec, lease))
    records = _records(log)
    cp_rows = [row for row in records if row["argv"][0] == "cp"]
    assert len(cp_rows) == 1
    assert cp_rows[0]["argv"][1] == "-a"
    assert cp_rows[0]["argv"][2].endswith("/.")
    assert cp_rows[0]["argv"][3].endswith(":/workspace")
    assert sum(row["argv"][:2] == ["volume", "create"] for row in records) == 1


def test_symlink_walker_preserves_internal_and_rejects_escape(tmp_path: Path) -> None:
    # Requirement: staged links remain links and cannot escape their declared source root.
    source = tmp_path / "source"
    source.mkdir()
    (source / "target").write_text("value", encoding="utf-8")
    (source / "inside").symlink_to("target")
    destination = tmp_path / "destination"
    _copy_tree_preserving_symlinks(source, destination)
    assert (destination / "inside").is_symlink()
    assert os.readlink(destination / "inside") == "target"
    (source / "escape").symlink_to("../outside")
    with pytest.raises(ExecutionSpecError, match="escapes"):
        _copy_tree_preserving_symlinks(source, tmp_path / "rejected")


def test_staging_walker_preserves_main_to_additional_root_link(tmp_path: Path) -> None:
    # Requirement: relocated main-to-root links remain valid in the mirrored volume topology.
    tree = tmp_path / "tree"
    main = tree / "main"
    root = tree / "roots/00"
    main.mkdir(parents=True)
    root.mkdir(parents=True)
    (root / "shared.txt").write_text("shared", encoding="utf-8")
    # Native separators keep the source link traversable on every host; the
    # staged copy must preserve the stored target verbatim, so the staged
    # readlink is pinned against the source link's own stored target rather
    # than a literal (a host's symlink readback spelling is not portable).
    target = os.path.normpath("../roots/00/shared.txt")
    (main / "shared").symlink_to(target)
    staging = tmp_path / "staging"
    DockerRunnerBackend._build_staging_tree(BundleRef("sha256:test", str(tmp_path)), staging)
    assert (staging / "main/shared").is_symlink()
    assert os.readlink(staging / "main/shared") == os.readlink(main / "shared")
    assert (staging / "main/shared").read_text(encoding="utf-8") == "shared"


def test_staging_walker_preserves_forward_slash_link_without_following_it(
    tmp_path: Path,
) -> None:
    # Requirement: a Linux-authored relative link (forward-slash target) is
    # staged verbatim even where the host cannot traverse such a link
    # (Windows stat-through fails with WinError 123) -- the walker resolves
    # the target lexically and must never os.stat through the link object.
    tree = tmp_path / "tree"
    main = tree / "main"
    root = tree / "roots/00"
    main.mkdir(parents=True)
    root.mkdir(parents=True)
    (root / "shared.txt").write_text("shared", encoding="utf-8")
    (main / "shared").symlink_to("../roots/00/shared.txt")
    staging = tmp_path / "staging"
    DockerRunnerBackend._build_staging_tree(BundleRef("sha256:test", str(tmp_path)), staging)
    assert (staging / "main/shared").is_symlink()
    assert os.readlink(staging / "main/shared") == os.readlink(main / "shared")
    if sys.platform != "win32":
        assert (staging / "main/shared").read_text(encoding="utf-8") == "shared"


@pytest.mark.asyncio
async def test_finalize_is_idempotent_and_backend_can_prepare_again(
    fake_docker: tuple[DockerRunnerBackend, Path],
) -> None:
    # Requirement: closing one incarnation does not close the reusable backend instance.
    backend, log = fake_docker
    first = await _lease(backend)
    spec = CommandSpec(command="true", execution=_minimal())
    await backend.run_command(spec, first)
    await backend.finalize_run(first, "succeeded")
    await backend.finalize_run(first, "succeeded")
    with pytest.raises(ExecutionSpecError, match="finalizing"):
        await backend.run_command(spec, first)
    second = await _lease(backend)
    await backend.run_command(spec, second)
    await backend.finalize_run(second, "succeeded")
    records = _records(log)
    assert sum(row["argv"][:2] == ["volume", "create"] for row in records) == 2


@pytest.mark.asyncio
async def test_incarnation_mismatch_recreates_volume(
    fake_docker: tuple[DockerRunnerBackend, Path],
) -> None:
    # Requirement: a same-run-id lease with a fresh incarnation fully restages stale state.
    backend, log = fake_docker
    first = await _lease(backend)
    await backend.run_command(CommandSpec(command="true", execution=_minimal()), first)
    second = await _lease(backend)
    await backend.run_command(CommandSpec(command="true", execution=_minimal()), second)
    records = _records(log)
    assert sum(row["argv"][:3] == ["volume", "rm", "-f"] for row in records) == 1
    assert sum(row["argv"][:2] == ["volume", "create"] for row in records) == 2


@pytest.mark.asyncio
async def test_finalize_refuses_spoofed_container_name(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: matching labels cannot authorize deletion of a non-Conductor name.
    backend, log = fake_docker
    lease = await _lease(backend)
    state_path = Path(os.environ["FAKE_DOCKER_STATE"])
    labels = backend._labels(lease, "exec")
    state = {
        "volumes": {},
        "containers": {
            "unrelated-service": {
                "Name": "/unrelated-service",
                "Config": {"Labels": labels},
                "State": {"ExitCode": 0},
            }
        },
        "volume_creates": 0,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    await backend.finalize_run(lease, "failed")
    remaining = json.loads(state_path.read_text(encoding="utf-8"))["containers"]
    assert "unrelated-service" in remaining
    assert not any(row["argv"][:3] == ["rm", "-f", "unrelated-service"] for row in _records(log))


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed", ["pull", "cp -a"])
async def test_finalize_cancels_inflight_staging_before_volume_cleanup(
    fake_docker: tuple[DockerRunnerBackend, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    delayed: str,
) -> None:
    # Requirement: finalize drains pull/cp staging before removing the run volume.
    _backend, log = fake_docker
    store = tmp_path / "store"
    (store / "tree/main").mkdir(parents=True)
    (store / "tree/main/file.txt").write_text("payload", encoding="utf-8")
    marker = tmp_path / "staging-started"
    monkeypatch.setenv("FAKE_DOCKER_SCENARIO", "pull" if delayed == "pull" else "")
    monkeypatch.setenv("FAKE_DOCKER_DELAY_COMMAND", delayed)
    monkeypatch.setenv("FAKE_DOCKER_DELAY_MARKER", str(marker))
    monkeypatch.setenv("FAKE_DOCKER_DELAY", "30")
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend, bundle=BundleRef("sha256:test", str(store)))
    command = asyncio.create_task(
        backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    )
    for _ in range(300):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()
    await backend.finalize_run(lease, "cancelled")
    with pytest.raises(asyncio.CancelledError):
        await command
    records = _records(log)
    volume_rm_index = next(
        index for index, row in enumerate(records) if row["argv"][:3] == ["volume", "rm", "-f"]
    )
    delayed_index = next(
        index for index, row in enumerate(records) if delayed in " ".join(row["argv"])
    )
    assert delayed_index < volume_rm_index


@pytest.mark.asyncio
async def test_command_arriving_during_finalize_is_rejected(
    fake_docker: tuple[DockerRunnerBackend, Path],
) -> None:
    # Requirement: finalize closes the lease before it begins resource discovery.
    backend, _log = fake_docker
    lease = await _lease(backend)
    await backend.finalize_run(lease, "succeeded")
    with pytest.raises(ExecutionSpecError, match="backend is finalizing"):
        await backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)


@pytest.mark.asyncio
async def test_executor_translates_missing_payload_to_configuration_error(
    fake_docker: tuple[DockerRunnerBackend, Path],
) -> None:
    # Requirement: a container backend receiving no execution payload raises the
    # leaf ExecutionSpecError, and ScriptExecutor translates it into the
    # engine/CLI ConfigurationError vocabulary with the leaf error as __cause__.
    from conductor.config.schema import ScriptStepDef
    from conductor.exceptions import ConfigurationError
    from conductor.executor.script import ScriptExecutor

    backend, _log = fake_docker
    executor = ScriptExecutor(backend)
    agent = ScriptStepDef(name="nopayload", command="true")

    with pytest.raises(ConfigurationError) as exc_info:
        await executor.execute(agent, {})

    assert isinstance(exc_info.value.__cause__, ExecutionSpecError)


@pytest.mark.asyncio
async def test_engine_routes_docker_step_through_fake_cli(
    fake_docker: tuple[DockerRunnerBackend, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Requirement: end-to-end wiring -- a script step with a docker profile is
    # routed through DockerRunnerBackend via the registered factory, the run
    # bundle is staged into the workspace volume, and the completed
    # CommandResult carries the container stdout back into the workflow output.
    import textwrap

    import conductor.engine.execution_resolution as execution_resolution_module
    from conductor.config.environment import (
        DockerProfileOptions,
        EnvironmentDocument,
        ProfileDefinition,
        ResolvedEnvironment,
    )
    from conductor.config.loader import load_config
    from conductor.engine.workflow import WorkflowEngine

    _backend, log = fake_docker
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    # Requirement: the fake CLI is canned -- it echoes FAKE_DOCKER_STDOUT from
    # the started container, which stands in for the real workload stdout.
    monkeypatch.setenv("FAKE_DOCKER_STDOUT", "hello-from-container")
    monkeypatch.setitem(
        execution_resolution_module.BACKEND_FACTORIES,
        "docker",
        lambda: DockerRunnerBackend("docker"),
    )
    document = EnvironmentDocument(
        default="local",
        profiles={
            "local": ProfileDefinition(backend="local"),
            "docker": ProfileDefinition(
                backend="docker",
                docker=DockerProfileOptions(image="alpine:3.20"),
            ),
        },
    )
    environment = ResolvedEnvironment(
        document=document,
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        textwrap.dedent(
            """\
            workflow:
              name: docker-e2e
              entry_point: run
            agents:
              - name: run
                type: script
                command: echo
                args: ["hello-from-container"]
                execution:
                  profile: docker
                routes:
                  - to: $end
            output:
              result: "{{ run.output.stdout }}"
            """
        ),
        encoding="utf-8",
    )

    engine = WorkflowEngine(
        load_config(workflow),
        workflow_path=workflow,
        execution_environment=environment,
    )

    result = await engine.run({})

    assert result == {"result": "hello-from-container"}
    commands = [row["argv"] for row in _records(log)]
    # Requirement: the docker CLI really drove the lifecycle -- workspace
    # volume created, bundle staged via archive cp, container created/started.
    assert any(argv[:2] == ["volume", "create"] for argv in commands)
    assert any(
        argv[0] == "cp" and argv[1] == "-a" and argv[3].endswith(":/workspace") for argv in commands
    )
    # Requirement: the exec (non-scratch) container create carries the rendered
    # command and args after the image reference.
    create = next(
        argv for argv in commands if argv[0] == "create" and "--name" in argv and "echo" in argv
    )
    assert create[-2:] == ["echo", "hello-from-container"]
    assert any(argv[0] == "start" for argv in commands)
