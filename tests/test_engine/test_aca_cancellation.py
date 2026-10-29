"""ACA backend cancellation and multi-pool lease cleanup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import conductor.engine.execution_resolution as resolution_module
from conductor.config.environment import (
    AcaProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
)
from conductor.engine.aca_execution import AcaRunnerBackend
from conductor.engine.execution_resolution import ExecutionResolverSession
from conductor.execution import LocalRunnerBackend
from conductor.execution.types import (
    AgentEventSink,
    AgentResult,
    AgentSpec,
    RunnerCapabilities,
    RunSpec,
)


class _GatedGateway:
    _runner_protocol_version: int | None = 2
    _runner_features: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def execute_request(
        self,
        body: dict[str, Any],
        identifier: str,
        *,
        interrupt_signal: asyncio.Event | None,
        on_event: AgentEventSink | None,
    ) -> AgentResult:
        del body, identifier, interrupt_signal, on_event
        self.started.set()
        await self.release.wait()
        return AgentResult(content={})


@pytest.mark.asyncio
async def test_cancelled_aca_request_releases_its_identifier_slot() -> None:
    # Requirement: cancellation releases the exact slot even before a terminal frame.
    gateway = _GatedGateway()
    backend = AcaRunnerBackend(transport=gateway)
    lease = await backend.prepare_run(RunSpec("run-1"))
    spec = AgentSpec(
        name="reviewer",
        execution_id="call-1",
        model_provider="copilot",
        model=None,
        rendered_prompt="review",
    )
    task = asyncio.create_task(backend.run_agent(spec, lease))
    await asyncio.wait_for(gateway.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend._leases[lease.incarnation].active == {}
    await backend.finalize_run(lease, "cancelled")


@pytest.mark.asyncio
async def test_finalizing_shared_aca_lease_closes_every_pool_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: finalizing one run closes every ACA pool selected in its environment.
    environment = ResolvedEnvironment(
        document=EnvironmentDocument(
            default="east",
            profiles={
                name: ProfileDefinition(
                    backend="aca", aca=AcaProfileOptions(pool_endpoint=f"https://{name}.test")
                )
                for name in ("east", "west")
            },
        ),
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )
    session = ExecutionResolverSession(environment)
    await session.prepare_leases(RunSpec("run-1"))
    await session.ensure_backends(["aca"])
    east = session._aca_profiles["east"]
    west = session._aca_profiles["west"]
    close_east = AsyncMock()
    close_west = AsyncMock()
    monkeypatch.setattr(east, "_transport", SimpleNamespace(close=close_east))
    monkeypatch.setattr(west, "_transport", SimpleNamespace(close=close_west))
    await session.finalize_leases("succeeded")
    close_east.assert_awaited_once()
    close_west.assert_awaited_once()
    assert session.lease_for_backend("aca") is None


@pytest.mark.asyncio
async def test_aca_factory_accepts_a_test_backend_without_transport_internals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: engine backend factories remain replaceable by protocol implementations.
    class _AcaLikeBackend(LocalRunnerBackend):
        def capabilities(self) -> RunnerCapabilities:
            return RunnerCapabilities(
                batch=False, sessions=True, shared_workspace=False, snapshots=False, agent=True
            )

    environment = ResolvedEnvironment(
        document=EnvironmentDocument(
            default="remote",
            profiles={
                "remote": ProfileDefinition(
                    backend="aca", aca=AcaProfileOptions(pool_endpoint="https://pool.test")
                )
            },
        ),
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )
    fake_backend = _AcaLikeBackend()
    monkeypatch.setitem(resolution_module.BACKEND_FACTORIES, "aca", lambda: fake_backend)
    session = ExecutionResolverSession(environment)
    await session.prepare_leases(RunSpec("run-1"))
    await session.ensure_backends(["aca"])
    assert session._aca_profiles["remote"] is fake_backend
    await session.finalize_leases("succeeded")
