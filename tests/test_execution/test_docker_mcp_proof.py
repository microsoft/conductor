"""Negative controls for daemon allocation proof and environment separation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest

from conductor.execution.docker_mcp import ContainerOwner
from conductor.execution.errors import ExecutionSpecError
from conductor.execution.types import McpServerSpec, RunSpec

from .test_docker_mcp import FakeRealm


async def test_target_cannot_validate_before_launch_or_launch_after_close(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec
) -> None:
    # Requirement: unpublished targets cannot admit calls or launch after their owner closes.
    backend, log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    with pytest.raises(ExecutionSpecError, match="launch is not active"):
        await target.validate_started()
    await target.aclose()
    with pytest.raises(ExecutionSpecError, match="already consumed"):
        target.mark_launch_attempted()
    assert not any(json.loads(line)["argv"][0] == "run" for line in log.read_text().splitlines())
    await backend.finalize_run(lease, "failed")
    assert json.loads(store.read_text())["containers"] == {}


@pytest.mark.parametrize(
    ("rc", "message", "absent"),
    [
        (1, "no such container: target", True),
        (1, "Error: No such object: target", True),
        (1, "error: no such object: target", True),
        (1, "Error response from daemon: No such container: target", True),
        (1, "error: no such object: other-target", False),
        (1, "lookup target: no such host", False),
        (-124, "error: no such object: target", False),
        (1, "error: no such object: target\npermission denied", False),
    ],
)
async def test_absence_parser_requires_object_specific_terminal_response(
    rc: int, message: str, absent: bool
) -> None:
    # Requirement: actual Docker CLI absence forms prove only the queried object's absence.
    async def cli(argv: Sequence[str], *, timeout: float | None = 300) -> tuple[int, str, str]:
        assert tuple(argv) == ("inspect", "--format", "{{json .}}", "target")
        return rc, "", message

    owner = ContainerOwner(cli, "target", {})
    if absent:
        assert await owner.inspect("target") is None
    else:
        with pytest.raises(ExecutionSpecError, match="inspection unconfirmed"):
            await owner.inspect("target")


@pytest.mark.parametrize("attempted", [False, True])
async def test_absence_requires_either_no_attempt_or_known_original_id(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec, attempted: bool
) -> None:
    # Requirement: no launch is safe; after a launch only known-ID absence is terminal proof.
    backend, log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    if attempted:
        target.mark_launch_attempted()
        await backend._run_docker(target.args)
        await target.validate_started()
        state = json.loads(store.read_text())
        del state["containers"][target.identity]
        store.write_text(json.dumps(state))
    await target.aclose()
    await backend.finalize_run(lease, "succeeded")
    assert json.loads(store.read_text())["containers"] == {}
    assert json.loads(store.read_text())["volumes"] == {}
    rows = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert not any(row[:2] == ["rm", "-f"] and row[-1] == target.identity for row in rows)


@pytest.mark.parametrize("fault", ["id", "name", "labels", "status"])
async def test_malformed_daemon_metadata_never_authorizes_removal(
    fake_realm: FakeRealm,
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    fault: Literal["id", "name", "labels", "status"],
) -> None:
    # Requirement: successful inspect exit alone cannot authorize removal of unverifiable data.
    backend, _log, store = fake_realm
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    state = json.loads(store.read_text())
    container = state["containers"][target.identity]
    match fault:
        case "id":
            container["Id"] = "short"
        case "name":
            container["Name"] = "/unrelated"
        case "labels":
            container["Config"]["Labels"] = {"io.conductor.managed": "true"}
        case "status":
            container["State"]["Status"] = 7
        case unreachable:
            assert_never(unreachable)
    store.write_text(json.dumps(state))
    with pytest.raises(ExecutionSpecError):
        await target.validate_started()
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await target.aclose()
    with pytest.raises(ExecutionSpecError, match="unconfirmed"):
        await backend.finalize_run(lease, "failed")
    assert json.loads(store.read_text()) == state


@pytest.mark.parametrize("inherit", [False, True])
async def test_environment_preserves_operator_snapshot_and_payload_overrides(
    fake_realm: FakeRealm, mcp_run: RunSpec, mcp_spec: McpServerSpec, inherit: bool
) -> None:
    # Requirement: delivery cannot redirect Docker/SSH/proxy helpers or strip ambient sources.
    backend, log, _store = fake_realm
    backend._cli_env.update(
        {
            "DOCKER_HOST": "operator-daemon",
            "SSH_AUTH_SOCK": "/operator-agent",
            "HTTPS_PROXY": "operator-proxy",
            "SOURCE_SECRET": "ambient-source",
        }
    )
    operator = dict(backend._cli_env)
    spec = replace(
        mcp_spec,
        inherit_control_environment=inherit,
        env={"DOCKER_HOST": "payload-daemon", "DELIVERY": "payload-only"},
    )
    lease = await backend.prepare_run(mcp_run)
    target = await backend.open_mcp_stdio(spec, lease)
    path = Path(target.args[target.args.index("--env-file") + 1])
    payload = dict(line.split("=", 1) for line in path.read_text().splitlines())
    assert payload["DOCKER_HOST"] == "payload-daemon"
    assert payload["DELIVERY"] == "payload-only"
    assert ("SOURCE_SECRET" in payload) is inherit
    assert target.env == operator == backend._cli_env
    target.mark_launch_attempted()
    await backend._run_docker(target.args)
    await target.validate_started()
    row = next(
        json.loads(line)
        for line in log.read_text().splitlines()
        if json.loads(line)["argv"][0] == "run"
    )
    assert row["env"] == operator
    assert "payload-only" not in json.dumps(row["argv"])
    await target.aclose()
    await backend.finalize_run(lease, "succeeded")
