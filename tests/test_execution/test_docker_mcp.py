"""Direct target ownership against the existing executable fake Docker CLI."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.errors import ExecutionSpecError, WorkspaceAttachError
from conductor.execution.types import (
    McpServerSpec,
    RunSpec,
)

type FakeRealm = tuple[DockerRunnerBackend, Path, Path]


async def test_open_mcp_builds_shared_workspace_target(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec
) -> None:
    # Requirement: the target carries profile/cwd/volume and private payload, preserving CLI env.
    backend, log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    row = next(
        json.loads(line)
        for line in log.read_text().splitlines()
        if "--env-file" in json.loads(line)["argv"]
    )
    create = row["argv"]
    identifier = json.loads(store.read_text())["containers"][target.identity]["Id"]
    assert target.args == ("start", "--attach", "--interactive", identifier)
    assert target.cwd is None and target.env == backend._cli_env == row["env"]
    assert "TOP_SECRET" not in repr(target) + repr(mcp_spec) + repr(create)
    assert create[0] == "create" and "--interactive" in create and "--rm" not in create
    assert create[create.index("-w") + 1] == "/workspace/main/nested"
    assert create[create.index("-v") + 1] == "conductor-ws-mcp-test:/workspace"
    assert row["env_file_content"] == "ONLY_PAYLOAD=TOP_SECRET\n"
    assert sys.platform == "win32" or row["env_file_mode"] == 0o600
    env_file = Path(create[create.index("--env-file") + 1])
    assert not env_file.exists()
    target.mark_launch_attempted()
    assert (await backend._run_docker(target.args))[0] == 0
    await target.validate_started()
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
    assert state["containers"] == {}
    await backend.finalize_run(lease, "succeeded")
    assert json.loads(store.read_text())["containers"] == {}
    assert json.loads(store.read_text())["volumes"] == {}


async def test_aclose_is_idempotent(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec
) -> None:
    # Requirement: even a peer that ignores EOF is removed once, by ID, on concurrent close.
    backend, log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    backend._cli_env["FAKE_DOCKER_SCENARIO"] = "ignores-eof"
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    await target.validate_started()
    peer = json.loads(store.read_text())["containers"][target.identity]
    assert peer["State"]["Status"] == "running"
    identifier = peer["Id"]
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
async def test_sweep_preserves_replacement_after_confirmed_target_removal(
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
    await target.aclose()
    state["containers"][target.identity]["Id"] = "b" * 64
    if change == "foreign":
        state["containers"][target.identity]["Config"]["Labels"] = {}
    store.write_text(json.dumps(state))
    await backend.finalize_run(lease, "failed", retain=True)
    remaining = json.loads(store.read_text())
    assert remaining["containers"][target.identity]["Id"] == "b" * 64
    assert "conductor-ws-mcp-test" in remaining["volumes"]


async def test_volume_loss_after_spawn_refuses_without_adoption(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec
) -> None:
    # Requirement: unverified volume ownership preserves foreign storage, not a proven-owned target.
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
    with pytest.raises(WorkspaceAttachError, match="missing or label mismatch"):
        await target.validate_started()
    await target.aclose()
    await backend.finalize_run(lease, "failed")
    final = json.loads(store.read_text())
    assert final["containers"] == {} and final["volumes"] == state["volumes"]
    assert payload.read_bytes() == b"shared"
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert rows.count(["rm", "-f", owned["Id"]]) == 1
    assert ["rm", "-f", target.identity] not in rows
    refused_at = len(rows)
    with pytest.raises(ExecutionSpecError):
        await backend.open_mcp_stdio(mcp_spec, lease)
    assert len(log.read_text().splitlines()) == refused_at


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
    rows = (
        [json.loads(line)["argv"] for line in log.read_text().splitlines()] if log.exists() else []
    )
    assert not any(row[0] == "start" or "--env-file" in row for row in rows)
    await backend.finalize_run(lease, "failed")
    if change in {"argv", "execution"}:
        assert not store.exists()  # Rejected before lazy workspace creation.
    else:
        assert json.loads(store.read_text())["containers"] == {}
