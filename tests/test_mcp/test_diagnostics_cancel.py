"""Partial SDK startup cancellation retains ownership of the stderr sink."""

import asyncio
import json

import anyio
import pytest

from conductor.mcp import diagnostics
from conductor.mcp.manager import MCPManager
from tests.test_engine.mcp_placement import placement_config, placement_engine


@pytest.fixture
def realm(tmp_path, monkeypatch):
    from tests.test_mcp.fixtures.docker_realm import create_realm

    return create_realm(tmp_path, monkeypatch)


async def test_diagnostic_partial_startup_cancel_closes_owned_sink(realm, monkeypatch):
    # Requirement: cancellation after real SDK spawn closes the transport before its owned sink.
    from contextlib import asynccontextmanager

    import conductor.mcp.manager as module
    from conductor.execution.types import McpServerSpec, ResolvedExecutionSpec

    spawned = anyio.Event()
    hold = anyio.Event()
    sinks = []
    descriptors = []
    open_stdio = module.stdio_client

    @asynccontextmanager
    async def held_stdio(params, *, errlog):
        sinks.append(errlog)
        descriptors.append(errlog.fileno())
        async with open_stdio(params, errlog=errlog) as transport:
            spawned.set()
            await hold.wait()
            yield transport

    monkeypatch.setattr(module, "stdio_client", held_stdio)
    async with placement_engine(placement_config(), realm) as engine:
        lease = engine._execution_session.leases["docker"]
        target = await realm.backend.open_mcp_stdio(
            McpServerSpec("srv", ("peer",), {}, execution=ResolvedExecutionSpec("peer:image")),
            lease,
        )
        manager = MCPManager()
        target.mark_launch_attempted()
        task = asyncio.create_task(
            manager.connect_server(
                "srv",
                target.command,
                args=list(target.args),
                env=dict(target.env),
                cwd=None,
                redact_errors=True,
                safe_transport_diagnostics=True,
            )
        )
        try:
            with anyio.fail_after(15):
                await spawned.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            await manager.close(strict=True)
            await target.aclose()
        assert sinks and all(sink.closed for sink in sinks)
        assert all(owner.done() for owner in manager._owners)
        assert not diagnostics.SAFE_TRANSPORT_DIAGNOSTICS.get()
        (realm.root / "cancel-sink-observation.json").write_text(
            json.dumps(
                {
                    "descriptors": descriptors,
                    "all_closed": all(sink.closed for sink in sinks),
                    "sdk_owners_terminal": all(owner.done() for owner in manager._owners),
                }
            )
        )
