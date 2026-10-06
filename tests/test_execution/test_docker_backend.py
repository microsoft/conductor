"""Contract tests for DockerRunnerBackend using a daemon-free fake CLI."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TypedDict, cast

import pytest

from conductor.execution.docker import (
    _CLI_TIMEOUT_RC,
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
    env_file_content: str
    env_file_mode: int


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
    # Requirement: secrets travel only through a protected temporary container env file.
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
    assert argv[-4:] == ["--entrypoint", "printf", "alpine:3.20", "ok"]
    assert "--env" not in argv
    assert "--env-file" in argv
    assert secret not in json.dumps(argv)
    assert create["env_file_content"] == f"TOKEN={secret}\n"
    assert "TOKEN" not in create["env"]
    assert not Path(argv[argv.index("--env-file") + 1]).exists()
    if sys.platform != "win32":
        assert create["env_file_mode"] == 0o600
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
async def test_create_entrypoint_replaces_image_defaults(
    fake_docker: tuple[DockerRunnerBackend, Path],
) -> None:
    # Requirement: the authored executable precedes the image as --entrypoint, never as an arg.
    backend, log = fake_docker
    lease = await _lease(backend)
    result = await backend.run_command(
        CommandSpec(command="python3", args=("-c", "print(1)"), execution=_minimal()), lease
    )
    assert result.outcome == "completed"
    argv = next(row["argv"] for row in _records(log) if row["argv"][0] == "create")
    assert argv[argv.index("--entrypoint") + 1 :] == [
        "python3",
        "alpine:3.20",
        "-c",
        "print(1)",
    ]
    assert argv.count("python3") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("variable", ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "HOME"])
async def test_payload_docker_settings_do_not_change_cli_environment(
    fake_docker: tuple[DockerRunnerBackend, Path], variable: str
) -> None:
    # Requirement: Docker client settings supplied as payload never redirect any Docker CLI call.
    backend, log = fake_docker
    control = backend._cli_env.get(variable)
    lease = await _lease(backend)
    payload = f"payload-{variable}"
    result = await backend.run_command(
        CommandSpec(command="true", env={variable: payload}, execution=_minimal()), lease
    )
    assert result.outcome == "completed"
    await backend.finalize_run(lease, "succeeded")
    rows = _records(log)
    assert rows
    assert all(row["env"].get(variable) == control for row in rows)
    assert all(payload not in json.dumps(row["argv"]) for row in rows)
    create = next(row for row in rows if row["argv"][0] == "create")
    assert f"{variable}={payload}\n" in create["env_file_content"]
    assert not Path(create["argv"][create["argv"].index("--env-file") + 1]).exists()


@pytest.mark.asyncio
async def test_container_environment_without_inheritance(
    fake_docker: tuple[DockerRunnerBackend, Path],
) -> None:
    # Requirement: disabling control inheritance passes only declared variables to the container.
    backend, log = fake_docker
    lease = await _lease(backend)
    result = await backend.run_command(
        CommandSpec(
            command="true",
            env={"ONLY_PAYLOAD": "value"},
            inherit_control_environment=False,
            execution=_minimal(),
        ),
        lease,
    )
    assert result.outcome == "completed"
    create = next(row for row in _records(log) if row["argv"][0] == "create")
    assert create["env_file_content"] == "ONLY_PAYLOAD=value\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("BAD=NAME", "secret"),
        ("#COMMENTED", "secret"),
        ("BAD\nNAME", "secret"),
        ("TOKEN", "secret\nsecond-line"),
        ("TOKEN", "secret\x00tail"),
    ],
)
async def test_invalid_container_environment_fails_without_disclosing_value(
    fake_docker: tuple[DockerRunnerBackend, Path], variable: str, value: str
) -> None:
    # Requirement: env-file entries the line format cannot represent fail before
    # create without leaking values.
    backend, log = fake_docker
    lease = await _lease(backend)
    result = await backend.run_command(
        CommandSpec(
            command="true",
            env={variable: value},
            inherit_control_environment=False,
            execution=_minimal(),
        ),
        lease,
    )
    assert result.outcome == "start_failed"
    assert result.start_error is not None
    assert variable.split("\n")[0].split("=")[0] in result.start_error.message
    assert "secret" not in result.start_error.message
    assert not any(row["argv"][0] == "create" for row in _records(log))


@pytest.mark.asyncio
async def test_representable_non_identifier_names_pass_to_env_file(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: env names the env-file line format can represent (e.g. the
    # Windows host's "CommonProgramFiles(x86)") reach the container through the
    # inherited host snapshot even though they are not shell identifiers.
    _backend, log = fake_docker
    # The backend snapshots os.environ at construction, so the variable must be
    # set before the backend is built — the same pattern as the scenario tests below.
    monkeypatch.setenv("CommonProgramFiles(x86)", r"C:\Program Files (x86)")
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend)
    result = await backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    assert result.outcome == "completed"
    create = next(row for row in _records(log) if row["argv"][0] == "create")
    # The host env is case-insensitive on Windows: os.environ may hand the
    # snapshot back in a different case, so compare the whole line case-insensitively.
    lines = {line.casefold() for line in create["env_file_content"].splitlines()}
    assert r"commonprogramfiles(x86)=c:\program files (x86)" in lines


@pytest.mark.asyncio
async def test_create_failure_removes_container_env_file(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: failed docker create removes its temporary env file before returning.
    _backend, log = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_SCENARIO", "createfail")
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend)
    result = await backend.run_command(
        CommandSpec(command="true", env={"TOKEN": "private"}, execution=_minimal()), lease
    )
    assert result.outcome == "start_failed"
    create = next(row for row in _records(log) if row["argv"][0] == "create")
    assert not Path(create["argv"][create["argv"].index("--env-file") + 1]).exists()


@pytest.mark.asyncio
async def test_cancel_create_removes_container_env_file(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Requirement: cancelling docker create removes the env file after its subprocess is reaped.
    _backend, log = fake_docker
    marker = tmp_path / "creating"
    monkeypatch.setenv("FAKE_DOCKER_DELAY_COMMAND", "create --name conductor-run_")
    monkeypatch.setenv("FAKE_DOCKER_DELAY_MARKER", str(marker))
    monkeypatch.setenv("FAKE_DOCKER_DELAY", "30")
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend)
    task = asyncio.create_task(
        backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    )
    for _ in range(300):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    create = next(row for row in _records(log) if row["argv"][0] == "create")
    assert not Path(create["argv"][create["argv"].index("--env-file") + 1]).exists()


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


@pytest.mark.asyncio
async def test_cancel_during_final_removal_drains_before_propagating(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: cancellation at final rm waits for removal and never returns completed data.
    backend, _log = fake_docker
    lease = await _lease(backend)
    original = backend._run_docker
    removing = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def delayed_rm(argv: Sequence[str], **kwargs: object) -> tuple[int, str, str]:
        if argv[:2] == ("rm", "-f"):
            removing.set()
            await release.wait()
            result = await original(argv)
            finished.set()
            return result
        return await original(argv)

    monkeypatch.setattr(backend, "_run_docker", delayed_rm)
    task = asyncio.create_task(
        backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    )
    await asyncio.wait_for(removing.wait(), 10)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_timeout_with_unconfirmed_termination_warns_and_preserves_outcome(
    fake_docker: tuple[DockerRunnerBackend, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement: failed kill and rm leave timeout data intact and identify the live container.
    _backend, log = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_SCENARIO", "termination-fails")
    monkeypatch.setenv("FAKE_DOCKER_DELAY_COMMAND", "start --attach")
    monkeypatch.setenv("FAKE_DOCKER_DELAY", "30")
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend)
    result = await backend.run_command(
        CommandSpec(command="sleep", timeout=0.05, execution=_minimal()), lease
    )
    name = next(row["argv"][-1] for row in _records(log) if row["argv"][0] == "start")
    assert result.outcome == "timed_out"
    assert "kill denied" in caplog.text and "remove denied" in caplog.text
    assert f"container={name}" in caplog.text
    assert f"docker rm -f {name}" in caplog.text
    assert name in json.loads(Path(os.environ["FAKE_DOCKER_STATE"]).read_text())["containers"]
    assert sum(row["argv"][:2] == ["rm", "-f"] for row in _records(log)) >= 2


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
    # Requirement: the bundle tree is copied once via archive-preserving trailing-dot cp.
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


@pytest.mark.asyncio
async def test_scratch_create_has_placeholder_entrypoint(
    fake_docker: tuple[DockerRunnerBackend, Path], tmp_path: Path
) -> None:
    # Requirement: scratch creation does not require image CMD or ENTRYPOINT defaults.
    backend, log = fake_docker
    store = tmp_path / "store"
    (store / "tree/main").mkdir(parents=True)
    (store / "tree/main/file.txt").write_text("payload", encoding="utf-8")
    lease = await _lease(backend, bundle=BundleRef("sha256:test", str(store)))
    result = await backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    assert result.outcome == "completed"
    scratch = next(
        row["argv"]
        for row in _records(log)
        if row["argv"][0] == "create"
        and row["argv"][row["argv"].index("--name") + 1].startswith("conductor-stage-")
    )
    assert scratch[-3:] == ["--entrypoint", "/conductor-staging-placeholder", "alpine:3.20"]


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


def test_staging_copies_every_bundle_namespace(tmp_path: Path) -> None:
    # Requirement: local roots retain registry children, plugins, skills, and additional roots.
    tree = tmp_path / "store/tree"
    paths = (
        "main/workflow.yaml",
        "roots/00/asset.txt",
        "registry/team/aaaaaaaaaaaa/child.yaml",
        "plugins/helper/plugin.json",
        "skills/review/SKILL.md",
    )
    for relative in paths:
        file = tree / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(relative, encoding="utf-8")
    staging = tmp_path / "staging"
    DockerRunnerBackend._build_staging_tree(BundleRef("sha256:test", str(tree.parent)), staging)
    for relative in paths:
        assert (staging / relative).read_text(encoding="utf-8") == relative


def test_staging_rejects_missing_tree(tmp_path: Path) -> None:
    # Requirement: missing bundle trees fail independently of the optional main namespace.
    with pytest.raises(ExecutionSpecError, match="bundle tree does not exist"):
        DockerRunnerBackend._build_staging_tree(
            BundleRef("sha256:test", str(tmp_path)), tmp_path / "staging"
        )


@pytest.mark.asyncio
async def test_registry_root_without_main_stages_and_maps_working_dir(
    fake_docker: tuple[DockerRunnerBackend, Path], tmp_path: Path
) -> None:
    # Requirement: a registry-root bundle without main stages and maps relative cwd safely.
    backend, log = fake_docker
    store = tmp_path / "store"
    root = "registry/team/aaaaaaaaaaaa"
    workflow = store / "tree" / root / "workflow.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("workflow", encoding="utf-8")
    lease = await _lease(backend, bundle=BundleRef("sha256:test", str(store), root=root))
    result = await backend.run_command(
        CommandSpec(command="true", working_dir="scripts", execution=_minimal()), lease
    )
    assert result.outcome == "completed"
    create = next(
        row["argv"]
        for row in _records(log)
        if row["argv"][0] == "create"
        and "--entrypoint" in row["argv"]
        and row["argv"][row["argv"].index("--entrypoint") + 1] == "true"
    )
    assert create[create.index("-w") + 1] == "/workspace/registry/team/aaaaaaaaaaaa/scripts"
    staging = tmp_path / "staging"
    DockerRunnerBackend._build_staging_tree(BundleRef("sha256:test", str(store), root), staging)
    assert (staging / root / "workflow.yaml").read_text(encoding="utf-8") == "workflow"
    with pytest.raises(ExecutionSpecError, match="escape"):
        _map_working_dir("../escape", root)


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
@pytest.mark.parametrize(
    ("scenario", "detail"),
    [
        ("inspect-error", "Cannot connect to the Docker daemon"),
        ("inspect-no-such-host", "no such host"),
        ("inspect-malformed", "malformed labels"),
        ("inspect-nondict", "non-dict labels"),
    ],
)
async def test_volume_inspect_failure_aborts_staging(
    fake_docker: tuple[DockerRunnerBackend, Path],
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    detail: str,
) -> None:
    # Requirement: unverifiable volume labels stop staging instead of creating a replacement.
    _backend, log = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_SCENARIO", scenario)
    backend = DockerRunnerBackend("docker")
    lease = await _lease(backend)
    result = await backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    assert result.outcome == "start_failed"
    assert result.start_error is not None
    assert f"Docker volume conductor-ws-{lease.lease_id}" in result.start_error.message
    assert detail in result.start_error.message
    assert [row["argv"][:2] for row in _records(log)] == [["volume", "inspect"]]


@pytest.mark.asyncio
async def test_volume_inspect_timeout_with_absence_text_is_not_missing(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a CLI timeout cannot confirm absence even if its stderr mentions "no such".
    backend, _log = fake_docker
    original = backend._run_docker

    async def timed_out_inspect(
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        timeout: float | None = 300.0,
        diagnostics: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        if argv[:2] == ["volume", "inspect"]:
            return _CLI_TIMEOUT_RC, "", "no such volume (CLI timed out)"
        return await original(
            argv, stdin_bytes=stdin_bytes, timeout=timeout, diagnostics=diagnostics
        )

    monkeypatch.setattr(backend, "_run_docker", timed_out_inspect)
    lease = await _lease(backend)
    result = await backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    assert result.outcome == "start_failed"
    assert result.start_error is not None
    assert "no such volume (CLI timed out)" in result.start_error.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "detail"),
    [
        ("inspect-error", "Cannot connect to the Docker daemon"),
        ("inspect-no-such-host", "no such host"),
        ("inspect-malformed", "malformed labels"),
        ("inspect-nondict", "non-dict labels"),
    ],
)
async def test_volume_inspect_failure_warns_on_finalize(
    fake_docker: tuple[DockerRunnerBackend, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
    detail: str,
) -> None:
    # Requirement: unverifiable volume labels retain storage and emit a cleanup warning.
    backend, log = fake_docker
    lease = await _lease(backend)
    result = await backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    assert result.outcome == "completed"
    monkeypatch.setenv("FAKE_DOCKER_SCENARIO", scenario)
    backend._cli_env = dict(os.environ)
    await backend.finalize_run(lease, "succeeded")
    assert f"Docker cleanup failed for run_id={lease.lease_id}" in caplog.text
    assert detail in caplog.text
    assert not any(row["argv"][:3] == ["volume", "rm", "-f"] for row in _records(log))
    state = json.loads(Path(os.environ["FAKE_DOCKER_STATE"]).read_text(encoding="utf-8"))
    assert backend._volume_name(lease) in state["volumes"]


@pytest.mark.asyncio
async def test_volume_inspect_timeout_warns_on_finalize(
    fake_docker: tuple[DockerRunnerBackend, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement: a timed-out volume inspection leaves storage intact and warns on cleanup.
    backend, log = fake_docker
    lease = await _lease(backend)
    result = await backend.run_command(CommandSpec(command="true", execution=_minimal()), lease)
    assert result.outcome == "completed"
    original = backend._run_docker

    async def timed_out_inspect(
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        timeout: float | None = 300.0,
        diagnostics: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        if argv[:2] == ["volume", "inspect"]:
            return _CLI_TIMEOUT_RC, "", "Docker CLI command timed out"
        return await original(
            argv, stdin_bytes=stdin_bytes, timeout=timeout, diagnostics=diagnostics
        )

    monkeypatch.setattr(backend, "_run_docker", timed_out_inspect)
    await backend.finalize_run(lease, "succeeded")
    assert "Docker cleanup failed" in caplog.text
    assert "Docker CLI command timed out" in caplog.text
    assert not any(row["argv"][:3] == ["volume", "rm", "-f"] for row in _records(log))


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
async def test_finalize_during_scratch_removal_cleans_host_staging(
    fake_docker: tuple[DockerRunnerBackend, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Requirement: cancelling staging at scratch rm drains cleanup and removes its host tree.
    _backend, log = fake_docker
    store = tmp_path / "store"
    (store / "tree/main").mkdir(parents=True)
    (store / "tree/main/file.txt").write_text("payload", encoding="utf-8")
    marker = tmp_path / "scratch-removal-started"
    monkeypatch.setenv("FAKE_DOCKER_DELAY_COMMAND", "rm -f conductor-stage-")
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
    source = next(row["argv"][2] for row in _records(log) if row["argv"][0] == "cp")
    staging = Path(source.removesuffix("/."))
    assert staging.exists()
    await backend.finalize_run(lease, "cancelled")
    with pytest.raises(asyncio.CancelledError):
        await command
    assert not staging.exists()


@pytest.mark.asyncio
async def test_cancel_finalize_drains_resources_and_propagates(
    fake_docker: tuple[DockerRunnerBackend, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: caller cancellation during finalization waits for owned cleanup, then propagates.
    backend, _log = fake_docker
    lease = await _lease(backend)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def delayed_resources(_lease: object) -> None:
        entered.set()
        await release.wait()
        finished.set()

    monkeypatch.setattr(backend, "_finalize_resources", delayed_resources)
    task = asyncio.create_task(backend.finalize_run(lease, "cancelled"))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert finished.is_set()


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
    # Requirement: the exec (non-scratch) create uses the rendered command as
    # entrypoint, with only args after the image reference.
    create = next(
        argv for argv in commands if argv[0] == "create" and "--name" in argv and "echo" in argv
    )
    assert create[-4:] == ["--entrypoint", "echo", "alpine:3.20", "hello-from-container"]
    assert any(argv[0] == "start" for argv in commands)
