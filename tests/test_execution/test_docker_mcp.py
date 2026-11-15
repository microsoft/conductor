"""Direct target ownership against the existing executable fake Docker CLI."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import (
    McpServerSpec,
    RunSpec,
    WorkspaceIdentity,
)

type FakeRealm = tuple[DockerRunnerBackend, Path, Path]


async def test_open_mcp_builds_shared_workspace_target(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec
) -> None:
    # Requirement: the target carries profile/cwd/volume and private payload, preserving CLI env.
    backend, log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    env_file = Path(target.args[target.args.index("--env-file") + 1])
    assert target.env == backend._cli_env
    assert "TOP_SECRET" not in repr(target) + repr(mcp_spec) + repr(target.args)
    assert target.cwd is None and target.args[:3] == ("run", "--rm", "-i")
    assert target.args[target.args.index("-w") + 1] == "/workspace/main/nested"
    assert target.args[target.args.index("-v") + 1] == "conductor-ws-mcp-test:/workspace"
    assert env_file.read_text() == "ONLY_PAYLOAD=TOP_SECRET\n"
    if sys.platform != "win32":
        assert env_file.stat().st_mode & 0o777 == 0o600
    target.mark_launch_attempted()
    assert (await backend._run_docker(target.args))[0] == 0
    await target.validate_started()
    row = next(
        json.loads(line)
        for line in log.read_text().splitlines()
        if json.loads(line)["argv"][0] == "run"
    )
    assert row["env"] == target.env
    assert row["env_file_content"] == "ONLY_PAYLOAD=TOP_SECRET\n"
    for flag in (
        "--init",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "--cpus",
        "--memory",
        "--pids-limit",
        "--network",
        "--user",
    ):
        assert flag in row["argv"]
    await target.aclose()
    assert not env_file.exists()
    state = json.loads(store.read_text())
    assert target.identity not in state["containers"]
    assert len(state["containers"]) == 1  # The run guard survives individual target close.
    await backend.finalize_run(lease, "succeeded")
    assert json.loads(store.read_text())["containers"] == {}
    assert json.loads(store.read_text())["volumes"] == {}


async def test_aclose_is_idempotent(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec
) -> None:
    # Requirement: concurrent/repeated close removes one immutable ID exactly once.
    backend, log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    await target.validate_started()
    identifier = json.loads(store.read_text())["containers"][target.identity]["Id"]
    await asyncio.gather(target.aclose(), target.aclose())
    await target.aclose()
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert rows.count(["rm", "-f", identifier]) == 1
    assert ["rm", "-f", target.identity] not in rows
    await backend.finalize_run(lease, "succeeded")


async def test_missing_lease_refuses_without_daemon_contact(
    fake_realm: FakeRealm, mcp_spec: McpServerSpec
) -> None:
    # Requirement: an unleased request cannot allocate a target or silently fall back.
    backend, log, _store = fake_realm
    with pytest.raises(ExecutionSpecError, match="workspace lease"):
        await backend.open_mcp_stdio(mcp_spec, None)
    assert not log.exists()


@pytest.mark.parametrize("change", ["foreign", "same_labels"])
async def test_replacement_is_preserved_and_blocks_finalize(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec, change: str
) -> None:
    # Requirement: neither foreign labels nor recycled owned labels authorize replacement removal.
    backend, _log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    await target.validate_started()
    state = json.loads(store.read_text())
    state["containers"][target.identity]["Id"] = "b" * 64
    if change == "foreign":
        state["containers"][target.identity]["Config"]["Labels"] = {}
    store.write_text(json.dumps(state))
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await target.aclose()
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await backend.finalize_run(lease, "failed")
    remaining = json.loads(store.read_text())
    assert remaining["containers"][target.identity]["Id"] == "b" * 64
    assert "conductor-ws-mcp-test" in remaining["volumes"]


async def test_volume_loss_after_spawn_refuses_without_adoption(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec
) -> None:
    # Requirement: unverified volume ownership preserves the guard, not a proven-owned target.
    backend, log, store = fake_realm
    workspace = store.parent / "workspace"
    backend._cli_env["FAKE_DOCKER_WORKSPACE_ROOT"] = str(workspace)
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    state = json.loads(store.read_text())
    payload = workspace / "conductor-ws-mcp-test/main/payload"
    assert payload.read_bytes() == b"shared"
    state["volumes"]["conductor-ws-mcp-test"] = {}
    store.write_text(json.dumps(state))
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    owned = json.loads(store.read_text())["containers"][target.identity]
    with pytest.raises(ExecutionSpecError, match="missing or label mismatch"):
        await target.validate_started()
    await target.aclose()
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await backend.finalize_run(lease, "failed")
    assert json.loads(store.read_text()) == state
    assert payload.read_bytes() == b"shared"
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert rows.count(["rm", "-f", owned["Id"]]) == 1
    assert ["rm", "-f", target.identity] not in rows
    refused_at = len(rows)
    with pytest.raises(ExecutionSpecError):
        await backend.open_mcp_stdio(mcp_spec, lease)
    assert len(log.read_text().splitlines()) == refused_at


async def test_delayed_daemon_absence_is_not_creation_proof(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec
) -> None:
    # Requirement: an unproven create preserves the exact fence and seals all new consumers.
    from conductor.execution.errors import WorkspaceAttachError

    backend, log, store = fake_realm
    workspace = store.parent / "workspace"
    backend._cli_env["FAKE_DOCKER_WORKSPACE_ROOT"] = str(workspace)
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    protected = json.loads(store.read_text())
    payload = workspace / "conductor-ws-mcp-test/main/payload"
    assert payload.read_bytes() == b"shared"
    target.mark_launch_attempted()
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await target.aclose()
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await backend.finalize_run(lease, "failed")
    assert json.loads(store.read_text()) == protected
    refused_at = len(log.read_text().splitlines())
    second = DockerRunnerBackend("docker")
    with pytest.raises(WorkspaceAttachError) as failure:
        await second.attach_run(
            mcp_run, WorkspaceIdentity("docker", lease.lease_id, lease.incarnation)
        )
    assert failure.value.__cause__ is None
    for value in ("TOP_SECRET", lease.lease_id, lease.incarnation, target.identity):
        assert value not in str(failure.value)
    assert json.loads(store.read_text()) == protected
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()[refused_at:]]
    assert [row[0] for row in rows] == ["image", "create"]
    refused_at = len(log.read_text().splitlines())
    with pytest.raises(ExecutionSpecError):
        target.mark_launch_attempted()
    with pytest.raises(ExecutionSpecError):
        await backend.open_mcp_stdio(mcp_spec, lease)
    assert len(log.read_text().splitlines()) == refused_at
    # Model the original daemon request committing late, without SDK admission or adoption.
    await backend._run_docker(
        target.args[: target.args.index("--env-file")]
        + target.args[target.args.index("--env-file") + 2 :]
    )
    remaining = json.loads(store.read_text())
    late = remaining["containers"].pop(target.identity)
    assert late["Config"]["Labels"] == backend._labels(lease, "mcp")
    assert remaining == protected
    assert payload.read_bytes() == b"shared"
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await target.aclose()
    assert json.loads(store.read_text())["containers"][target.identity] == late


@pytest.mark.parametrize("change", ["argv", "env", "cwd", "execution"])
async def test_malformed_input_does_not_allocate_target(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec, change: str
) -> None:
    # Requirement: malformed inputs never become launchable targets or disclose payload values.
    backend, log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    variants = {
        "argv": replace(mcp_spec, argv=()),
        "env": replace(mcp_spec, env={"X": "secret\n"}),
        "cwd": replace(mcp_spec, working_dir="../outside"),
        "execution": replace(mcp_spec, execution=None),
    }
    with pytest.raises(ExecutionSpecError) as failure:
        await backend.open_mcp_stdio(variants[change], lease)
    assert "secret" not in str(failure.value)
    assert not any(json.loads(line)["argv"][0] == "run" for line in log.read_text().splitlines())
    await backend.finalize_run(lease, "failed")
    assert json.loads(store.read_text())["containers"] == {}


async def test_uncooperative_cleanup_keeps_original_deadline(
    fake_realm: FakeRealm,
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: an uncooperative cleanup stays owned and uncertainty never becomes success.
    backend, _log, store = fake_realm
    original = backend._run_docker
    entered, release = asyncio.Event(), asyncio.Event()
    identifier = ""

    async def held_remove(
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        timeout: float | None = 300.0,
        diagnostics: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        if tuple(argv) == ("rm", "-f", identifier):
            entered.set()
            await release.wait()
        return await original(
            argv, stdin_bytes=stdin_bytes, timeout=timeout, diagnostics=diagnostics
        )

    monkeypatch.setattr(backend, "_run_docker", held_remove)
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    await target.validate_started()
    identifier = json.loads(store.read_text())["containers"][target.identity]["Id"]
    mcp = backend._lease_states[backend._lease_key(lease)].mcp
    assert mcp is not None
    from conductor.execution.docker_mcp import join_before

    async def expired_observation(task: asyncio.Task[None], _deadline: float) -> bool:
        await asyncio.wait_for(entered.wait(), 5)
        return await join_before(task, asyncio.get_running_loop().time() - 1)

    monkeypatch.setattr("conductor.execution.docker.join_before", expired_observation)
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await target.aclose()
    release.set()
    await asyncio.gather(*mcp.target_cleanups, return_exceptions=True)
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await target.aclose()
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await backend.finalize_run(lease, "failed")
    assert "conductor-ws-mcp-test" in json.loads(store.read_text())["volumes"]
