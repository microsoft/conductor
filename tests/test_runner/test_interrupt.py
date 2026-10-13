"""Runner interrupt routing at the HTTP and NDJSON boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from conductor.aca_runner import server
from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    OutputField,
    ProviderSettings,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import AgentRealm, WorkflowEngine
from conductor.execution import (
    AgentEventSink,
    AgentResult,
    AgentSpec,
    LocalRunnerBackend,
    RunnerCapabilities,
    WorkspaceLease,
)
from conductor.gates.interrupt import InterruptAction, InterruptResult
from conductor.providers.aca import AcaRuntimeProvider
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider
from conductor.runner.protocol import RunnerHealthResponse
from tests.test_runner.conftest import InterruptibleProvider, RunnerClientFactory

pytestmark = pytest.mark.usefixtures("interruptible_provider")


def _body(execution_id: str, *, legacy: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent": {"name": "agent"},
        "rendered_prompt": "task",
        "context": {"execution_id": execution_id},
    }
    if not legacy:
        body["execution_id"] = execution_id
    InterruptibleProvider.started[execution_id] = asyncio.Event()
    InterruptibleProvider.finish[execution_id] = asyncio.Event()
    return body


def _terminal(response: httpx.Response) -> dict[str, Any]:
    return json.loads(response.text.splitlines()[-1])


async def test_main_loop_interrupt_returns_partial_through_remote_realm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: the real main loop passes its interrupt signal into the
    # realm seam and receives a partial AgentOutput from the remote result.
    started = asyncio.Event()
    interrupt = asyncio.Event()
    partial_seen: list[bool] = []

    class RemoteBackend(LocalRunnerBackend):
        def capabilities(self) -> RunnerCapabilities:
            return RunnerCapabilities(
                batch=False,
                sessions=False,
                shared_workspace=False,
                snapshots=False,
                agent=True,
                interrupt=True,
            )

        async def run_agent(
            self,
            spec: AgentSpec,
            lease: WorkspaceLease | None,
            *,
            on_event: AgentEventSink | None = None,
            interrupt_signal: asyncio.Event | None = None,
            execute_local: Callable[[], Awaitable[AgentResult]] | None = None,
        ) -> AgentResult:
            assert execute_local is None
            assert lease is realm.lease
            assert interrupt_signal is interrupt
            started.set()
            assert interrupt_signal is not None
            await interrupt_signal.wait()
            return AgentResult(content={"answer": "partial from realm"}, partial=True)

    agent = AgentDef(
        name="writer",
        prompt="Write",
        output={"answer": OutputField(type="string")},
        timeout_seconds=None,
        max_session_seconds=None,
        max_agent_iterations=None,
        routes=[RouteDef(to="$end")],
    )
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="interrupt-realm",
            entry_point="writer",
            runtime=RuntimeConfig.model_validate({"provider": "copilot"}),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[agent],
        output={"answer": "{{ writer.output.answer }}"},
    )
    backend = RemoteBackend()
    realm = AgentRealm(
        backend=backend,
        lease=WorkspaceLease(lease_id="run", backend="remote", incarnation="first"),
        execution=None,
        env_overlay={},
        credential_resolver=lambda _: {"github_token": "test"},
        image=None,
        name="remote",
    )
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=lambda *_: {"answer": "host"}),
        interrupt_event=interrupt,
    )
    monkeypatch.setattr(engine, "_agent_realm", lambda *_args, **_kwargs: realm)
    original = engine._handle_partial_output

    async def capture_partial(*args: Any, **kwargs: Any) -> AgentOutput:
        partial_seen.append(args[1].partial)
        interrupt.clear()
        return await original(*args, **kwargs)

    monkeypatch.setattr(engine, "_handle_partial_output", capture_partial)
    handle_interrupt = AsyncMock(return_value=InterruptResult(action=InterruptAction.CANCEL))
    monkeypatch.setattr(engine._interrupt_handler, "handle_interrupt", handle_interrupt)
    running = asyncio.create_task(engine.run({}))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        interrupt.set()
        result = await asyncio.wait_for(running, timeout=5)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    assert partial_seen == [True]
    assert result == {"answer": "partial from realm"}
    handle_interrupt.assert_awaited_once()


async def test_interrupt_targets_only_one_concurrent_agent(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: main-loop agent calls are interruptible without pausing a sibling;
    # parallel/for-each interrupt propagation is not promised by the engine today.
    async with runner_client_factory() as client:
        first = asyncio.create_task(client.post("/execute", json=_body("first")))
        second = asyncio.create_task(client.post("/execute", json=_body("second")))
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in InterruptibleProvider.started.values())),
            timeout=5,
        )
        response = await client.post("/interrupt", json={"execution_id": "first"})
        assert response.status_code == 200
        assert not InterruptibleProvider.finish["second"].is_set()
        InterruptibleProvider.finish["second"].set()
        first_response, second_response = await asyncio.wait_for(
            asyncio.gather(first, second), timeout=5
        )
        assert _terminal(first_response)["data"]["partial"] is True
        assert _terminal(first_response)["data"]["content"]["execution_id"] == "first"
        assert _terminal(second_response)["data"]["partial"] is False


async def test_interrupt_unknown_returns_404(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: an unknown execution cannot interrupt another running call.
    async with runner_client_factory() as client:
        response = await client.post("/interrupt", json={"execution_id": "missing"})
    assert response.status_code == 404


async def test_interrupt_duplicate_active_identity_returns_409(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: two calls cannot claim the same interrupt target.
    async with runner_client_factory() as client:
        first = asyncio.create_task(client.post("/execute", json=_body("shared")))
        await asyncio.wait_for(InterruptibleProvider.started["shared"].wait(), timeout=5)
        duplicate = await client.post(
            "/execute",
            json={"agent": {"name": "agent"}, "rendered_prompt": "task", "execution_id": "shared"},
        )
        InterruptibleProvider.finish["shared"].set()
        completed = await asyncio.wait_for(first, timeout=5)
    assert duplicate.status_code == 409
    assert _terminal(completed)["data"]["partial"] is False


async def test_interrupt_late_and_repeated_return_409(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: already signaled and terminal identities cannot be signaled again.
    async with runner_client_factory() as client:
        request = asyncio.create_task(client.post("/execute", json=_body("done")))
        await asyncio.wait_for(InterruptibleProvider.started["done"].wait(), timeout=5)
        first = await client.post("/interrupt", json={"execution_id": "done"})
        repeated = await client.post("/interrupt", json={"execution_id": "done"})
        result = await asyncio.wait_for(request, timeout=5)
        late = await client.post("/interrupt", json={"execution_id": "done"})
    assert first.status_code == 200
    assert repeated.status_code == 409
    assert _terminal(result)["data"]["partial"] is True
    assert late.status_code == 409


async def test_interrupt_after_terminal_frame_before_stream_drain_returns_409(
    monkeypatch: pytest.MonkeyPatch,
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: a terminal execution refuses interruption even if its HTTP
    # consumer has not drained the final frame and released the registry entry.
    terminal_ready = asyncio.Event()
    drain = asyncio.Event()
    original = server._stream_execute

    async def delayed_stream(*args: Any, **kwargs: Any) -> AsyncIterator[bytes]:
        async for frame in original(*args, **kwargs):
            if json.loads(frame)["type"] == "result":
                terminal_ready.set()
                await drain.wait()
            yield frame

    monkeypatch.setattr(server, "_stream_execute", delayed_stream)
    async with runner_client_factory() as client:
        request = asyncio.create_task(client.post("/execute", json=_body("terminal")))
        await asyncio.wait_for(InterruptibleProvider.started["terminal"].wait(), timeout=5)
        InterruptibleProvider.finish["terminal"].set()
        try:
            await asyncio.wait_for(terminal_ready.wait(), timeout=5)
            response = await client.post("/interrupt", json={"execution_id": "terminal"})
            assert response.status_code == 409
        finally:
            drain.set()
            await asyncio.wait_for(request, timeout=5)


async def test_interrupt_degraded_handshake(
    caplog: pytest.LogCaptureFixture,
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: only v2 images advertising interrupt can enable realm interrupt;
    # v1 and missing-feature images keep interrupt=False and use legacy abort-read.
    async with runner_client_factory() as client:
        response = await client.get("/health")
    advertised = RunnerHealthResponse.model_validate(response.json())
    assert advertised.protocol_version == 2
    assert "interrupt" in (advertised.features or [])
    for old in ({"ready": True, "protocol_version": 1}, {"ready": True, "protocol_version": 2}):
        health = RunnerHealthResponse.model_validate(old)
        assert not (health.protocol_version == 2 and "interrupt" in (health.features or []))
        if health.protocol_version == 1:
            AcaRuntimeProvider._warn_on_version_skew(object.__new__(AcaRuntimeProvider), health)
    assert any(record.name == "conductor.providers.aca" for record in caplog.records)


async def test_interrupt_auth_and_bad_input(
    monkeypatch: pytest.MonkeyPatch,
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: interruption uses the execute token gate and rejects malformed IDs.
    monkeypatch.setenv("ACA_RUNNER_AUTH_TOKEN", "runner-token")
    async with runner_client_factory() as client:
        unauthorized = await client.post("/interrupt", json={"execution_id": "one"})
        malformed = await client.post(
            "/interrupt", content=b"{broken", headers={"X-Conductor-Runner-Token": "runner-token"}
        )
        missing = await client.post(
            "/interrupt", json={}, headers={"X-Conductor-Runner-Token": "runner-token"}
        )
    assert unauthorized.status_code == 401
    assert malformed.status_code == 422
    assert missing.status_code == 422


async def test_legacy_aca_interrupt_reaches_runner_with_identifier(
    monkeypatch: pytest.MonkeyPatch,
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: the existing legacy ACA POST, which has no JSON body, works
    # against a new runner through the gateway's identifier routing.
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "host-token")
    with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
        legacy = AcaRuntimeProvider(
            provider_settings=ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        )
    async with runner_client_factory() as client:
        legacy._get_access_token = AsyncMock(return_value="transport-token")
        legacy._http_client = client
        request = asyncio.create_task(
            client.post(
                "/execute", params={"identifier": "legacy"}, json=_body("legacy", legacy=True)
            )
        )
        await asyncio.wait_for(InterruptibleProvider.started["legacy"].wait(), timeout=5)
        await legacy._send_interrupt("legacy")
        result = await asyncio.wait_for(request, timeout=5)
    assert _terminal(result)["data"]["partial"] is True


async def test_legacy_aca_identifier_can_be_reused_after_completion(
    runner_client_factory: RunnerClientFactory,
) -> None:
    # Requirement: the legacy gateway's sequential session reuse remains valid.
    async with runner_client_factory() as client:
        first_body = _body("legacy-one", legacy=True)
        first = asyncio.create_task(
            client.post("/execute", params={"identifier": "session"}, json=first_body)
        )
        await asyncio.wait_for(InterruptibleProvider.started["legacy-one"].wait(), timeout=5)
        InterruptibleProvider.finish["legacy-one"].set()
        assert (await asyncio.wait_for(first, timeout=5)).status_code == 200
        second_body = _body("legacy-two", legacy=True)
        second = asyncio.create_task(
            client.post("/execute", params={"identifier": "session"}, json=second_body)
        )
        await asyncio.wait_for(InterruptibleProvider.started["legacy-two"].wait(), timeout=5)
        InterruptibleProvider.finish["legacy-two"].set()
        assert (await asyncio.wait_for(second, timeout=5)).status_code == 200
