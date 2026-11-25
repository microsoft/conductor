"""Ordered release of failed and unpublished lease resources."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from pathlib import Path

import pytest

from conductor.config.environment import builtin_local_environment
from conductor.engine.execution_resolution import ExecutionResolverSession
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.types import McpServerSpec, RunSpec


async def test_failed_release_refuses_workspace_disposal(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Requirement: a failed release is not a clean shutdown and cannot authorize a sweep.
    backend, log, store = fake_realm
    session = ExecutionResolverSession(builtin_local_environment())
    session.backends["docker"] = backend
    await session.prepare_leases(mcp_run)
    lease = session.leases["docker"]
    target = await backend.open_mcp_stdio(mcp_spec, lease)
    session.register_resource(target, target.aclose, lease=lease)
    original = backend._run_docker
    attempted = []

    async def refuse(argv, **kwargs):
        attempted.append(tuple(argv))
        if argv[0] == "rm":
            return 1, "", "error: no such container: PRIVATE_OTHER_REFERENCE"
        return await original(argv, **kwargs)

    monkeypatch.setattr(backend, "_run_docker", refuse)
    before = len(log.read_text().splitlines())

    await session.finalize_leases("failed")

    assert session.quarantined
    assert attempted == [("rm", "-f", target.args[-1])]
    assert len(log.read_text().splitlines()) == before
    assert "PRIVATE_OTHER_REFERENCE" not in caplog.text
    state = json.loads(store.read_text())
    assert state["containers"][target.identity]["Id"] == target.args[-1]
    assert f"conductor-ws-{lease.lease_id}" in state["volumes"]


async def test_late_lease_owners_cannot_dispose_after_original_root_deadline(
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    mcp_run: RunSpec,
    mcp_spec: McpServerSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: independent leases start together; even a successful late release cannot sweep.
    backend, _, store = fake_realm
    session = ExecutionResolverSession(builtin_local_environment())
    session.backends["docker"] = backend
    await session.prepare_leases(mcp_run)
    docker_lease = session.leases["docker"]
    target = await backend.open_mcp_stdio(mcp_spec, docker_lease)
    volume = f"conductor-ws-{docker_lease.lease_id}"
    release = asyncio.Event()
    started = [asyncio.Event() for _ in session.leases]

    async def close(ready: asyncio.Event, docker: bool) -> None:
        ready.set()
        await release.wait()
        if docker:
            await target.aclose()

    for lease, ready in zip(session.leases.values(), started, strict=True):
        session.register_resource(ready, partial(close, ready, lease == docker_lease), lease=lease)

    root = asyncio.create_task(session.finalize_leases("succeeded"))
    try:
        await asyncio.wait_for(asyncio.gather(*(ready.wait() for ready in started)), 3)
        deadline = session.cleanup_deadline
        assert deadline is not None
        loop = asyncio.get_running_loop()
        clock = loop.time
        monkeypatch.setattr(loop, "time", lambda: clock() + 31)
        await asyncio.wait_for(session.finalize_leases("cancelled", retain=True), 3)
        assert session.cleanup_deadline == deadline
        assert session.quarantined
    finally:
        release.set()
        await root
        await asyncio.gather(*session._resource_tasks)

    state = json.loads(store.read_text())
    assert not state["containers"]
    assert volume in state["volumes"]
