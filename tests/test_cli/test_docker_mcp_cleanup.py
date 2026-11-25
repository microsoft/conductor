"""Docker-only root cleanup through the real run and resume routes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket

from conductor.cli.run import resume_workflow_async, run_workflow_async
from conductor.engine.execution_resolution import BACKEND_FACTORIES
from conductor.exceptions import ExecutionError
from conductor.execution.types import McpServerSpec, McpStdioTarget, WorkspaceLease
from conductor.web.server import WebDashboard
from tests.test_cli.test_resume_command import _write_checkpoint
from tests.test_mcp.fixtures.docker_realm import create_realm


@pytest.fixture
def docker_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    realm = create_realm(tmp_path, monkeypatch)
    monkeypatch.setitem(BACKEND_FACTORIES, "docker", lambda: realm.backend)
    source = tmp_path / "source"
    source.mkdir()
    workflow = source / "calls.yaml"
    workflow.write_text(
        """workflow:
  name: calls
  entry_point: first
  runtime:
    mcp_servers:
      a: {command: peer}
      b: {command: peer}
agents:
  - {name: first, type: mcp, server: a, tool: receive, routes: [{to: second}]}
  - {name: second, type: mcp, server: b, tool: receive, routes: [{to: $end}]}
"""
    )
    environment = tmp_path / "environment.yaml"
    environment.write_text(
        "default: box\nprofiles: {box: {backend: docker, docker: {image: peer}}}"
    )
    return workflow, str(environment), realm


@pytest.mark.parametrize("resume", [False, True], ids=["run", "resume"])
async def test_dashboard_disconnects_after_unconfirmed_lease_release(
    docker_workflow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    # Requirement: a refused Docker disposal cannot skip the CLI-owned dashboard's shutdown.
    workflow, environment, realm = docker_workflow
    connection = AsyncMock(spec=WebSocket)

    async def start_offline(dashboard: WebDashboard) -> None:
        dashboard._connections.add(connection)

    monkeypatch.setattr(WebDashboard, "start", start_offline)
    docker = realm.backend._run_docker

    async def refuse_remove(argv, **kwargs):
        if argv[0] == "rm":
            return 1, "", "permission denied"
        return await docker(argv, **kwargs)

    monkeypatch.setattr(realm.backend, "_run_docker", refuse_remove)
    with pytest.raises(ExecutionError, match="cleanup is unconfirmed"):
        if resume:
            checkpoint = _write_checkpoint(tmp_path, workflow, current_agent="second")
            await resume_workflow_async(
                checkpoint_path=checkpoint, environment=environment, web=True, no_interactive=True
            )
        else:
            await run_workflow_async(workflow, {}, environment=environment, web=True)

    connection.close.assert_awaited_once()
    assert json.loads((realm.root / "state.json").read_text())["volumes"]


async def test_pooled_releases_share_root_deadline_and_cannot_authorize_late_disposal(
    docker_workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: every peer gets a release attempt before the same cutoff, not N timeouts.
    workflow, environment, realm = docker_workflow
    allocate = realm.backend.open_mcp_stdio
    started = {name: asyncio.Event() for name in ("a", "b")}
    release = {name: asyncio.Event() for name in started}
    finished = {name: asyncio.Event() for name in started}

    async def hold_target(spec: McpServerSpec, lease: WorkspaceLease) -> McpStdioTarget:
        target = await allocate(spec, lease)

        async def close() -> None:
            started[spec.server].set()
            await release[spec.server].wait()
            await target.aclose()
            finished[spec.server].set()

        return replace(target, aclose=close)

    monkeypatch.setattr(realm.backend, "open_mcp_stdio", hold_target)
    run = asyncio.create_task(run_workflow_async(workflow, {}, environment=environment))
    try:
        await asyncio.wait_for(started["a"].wait(), 30)
        await asyncio.wait_for(started["b"].wait(), 3)
        loop = asyncio.get_running_loop()
        clock = loop.time
        monkeypatch.setattr(loop, "time", lambda: clock() + 31)
        with pytest.raises(ExecutionError, match="cleanup is unconfirmed"):
            await asyncio.wait_for(asyncio.shield(run), 3)
    finally:
        for name in started:
            release[name].set()
            await asyncio.wait_for(finished[name].wait(), 10)
        await asyncio.gather(run, return_exceptions=True)

    state = json.loads((realm.root / "state.json").read_text())
    assert not state["containers"]
    assert state["volumes"], "Late target cleanup must never authorize a workspace sweep"
