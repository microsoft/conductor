"""ACA realm lease, wire transport, and legacy compatibility tests."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from conductor.config.environment import AcaProfileOptions
from conductor.engine.aca_execution import AcaIdentifierState, AcaRunnerBackend
from conductor.exceptions import ConfigurationError, ProviderError
from conductor.execution.types import RunSpec
from conductor.runner.protocol import RunnerAgentPayload
from tests.test_engine.aca_equivalence_fakes import agent as _agent
from tests.test_engine.aca_equivalence_fakes import provider as _provider
from tests.test_engine.aca_equivalence_fakes import spec as _spec


class _V1Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: RunnerAgentPayload
    rendered_prompt: str
    tools: list[str] | None = None
    mcp_servers: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    inner_provider: str = "copilot"
    inner_provider_settings: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None


@pytest.mark.asyncio
async def test_aca_backend_streams_events_and_finalizes_lease() -> None:
    # Requirement: the backend reuses the ACA gateway transport and releases its lease.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(
                200, json={"ready": True, "protocol_version": 2, "features": ["interrupt"]}
            )
        frames = [
            {"type": "agent_message", "data": {"content": "hello"}},
            {"type": "result", "data": {"content": {"answer": "done"}, "input_tokens": 3}},
        ]
        return httpx.Response(200, content="\n".join(map(json.dumps, frames)) + "\n")

    transport = _provider(httpx.MockTransport(handler))
    backend = AcaRunnerBackend(
        AcaProfileOptions(pool_endpoint="https://pool.example.com"), transport=transport
    )
    lease = await backend.prepare_run(RunSpec("run-1"))
    events: list[tuple[str, Mapping[str, Any]]] = []
    result = await backend.run_agent(_spec(), lease, on_event=lambda t, d: events.append((t, d)))

    assert result.content == {"answer": "done"}
    assert result.input_tokens == 3
    assert events == [("agent_message", {"content": "hello"})]
    assert backend.capabilities().interrupt is True
    body = json.loads(requests[-1].content)
    assert body["execution_id"] == "call-1"
    assert requests[-1].url.params["identifier"]
    await backend.finalize_run(lease, "succeeded")
    assert backend._leases == {}
    assert transport._http_client is None


@pytest.mark.asyncio
async def test_aca_backend_rejects_unstaged_skill_directories() -> None:
    # Requirement: host skill paths fail before optional SDK construction or network I/O.
    backend = AcaRunnerBackend(AcaProfileOptions(pool_endpoint="https://pool.example.com"))
    lease = await backend.prepare_run(RunSpec("run-1"))
    with pytest.raises(ConfigurationError):
        await backend.run_agent(replace(_spec(), skill_directories=("/host/skill",)), lease)
    await backend.finalize_run(lease, "failed")


@pytest.mark.asyncio
async def test_legacy_and_profile_use_identical_v1_requests() -> None:
    # Requirement: both routes preserve v1 wire identity, event order, and terminal result.
    captured: list[tuple[str, dict[str, Any]]] = []
    expected_events: list[tuple[str, dict[str, Any]]] = [
        ("agent_turn_start", {"turn": "awaiting_model"}),
        ("agent_reasoning", {"content": "checking inputs"}),
        ("agent_tool_start", {"tool_name": "read", "arguments": {"path": "README.md"}}),
        ("agent_tool_complete", {"tool_name": "read", "result": "ok", "status": "success"}),
        ("agent_message", {"content": "reviewed"}),
    ]
    terminal = {
        "content": {"x": 1},
        "model": "gpt-4.1",
        "input_tokens": 11,
        "output_tokens": 3,
        "partial": False,
    }
    frames: list[dict[str, Any]] = [{"type": kind, "data": data} for kind, data in expected_events]
    frames.append({"type": "result", "data": terminal})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ready": True, "protocol_version": 1})
        captured.append((request.url.params["identifier"], json.loads(request.content)))
        return httpx.Response(200, content=chr(10).join(map(json.dumps, frames)) + chr(10))

    gateway = httpx.MockTransport(handler)
    legacy_events: list[tuple[str, dict[str, Any]]] = []
    profile_events: list[tuple[str, dict[str, Any]]] = []
    with (
        patch("conductor.engine.aca_execution.secrets.token_hex", return_value="deadbeef"),
        patch("conductor.providers.aca.secrets.token_hex", return_value="deadbeef"),
        patch.dict("os.environ", {"COPILOT_GITHUB_TOKEN": "host-token"}, clear=True),
    ):
        legacy = _provider(gateway)
        profile_transport = _provider(gateway)
        backend = AcaRunnerBackend(
            AcaProfileOptions(pool_endpoint="https://pool.example.com"), transport=profile_transport
        )
        lease = await backend.prepare_run(RunSpec("run-1"))
        with pytest.warns(DeprecationWarning):
            legacy_result = await legacy.execute(
                _agent(),
                {},
                "review",
                event_callback=lambda t, d: legacy_events.append((t, dict(d))),
            )
        profile_result = await backend.run_agent(
            _spec(), lease, on_event=lambda t, d: profile_events.append((t, dict(d)))
        )

    assert len(captured) == 2
    assert captured[0] == captured[1]
    assert "execution_id" not in captured[0][1]
    assert legacy_events == profile_events == expected_events
    assert legacy_result.content == profile_result.content == terminal["content"]
    assert legacy_result.raw_response == profile_result.raw_response == terminal
    assert legacy_result.model == profile_result.model == terminal["model"]
    assert legacy_result.input_tokens == profile_result.input_tokens == terminal["input_tokens"]
    assert legacy_result.output_tokens == profile_result.output_tokens == terminal["output_tokens"]
    assert legacy_result.partial is profile_result.partial is False
    await backend.finalize_run(lease, "succeeded")
    await legacy.close()


@pytest.mark.asyncio
async def test_legacy_executes_against_strict_v1_runner(caplog: pytest.LogCaptureFixture) -> None:
    # Requirement: a deployed v1 runner rejects extra v2 keys yet accepts the legacy flow.
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ready": True, "protocol_version": 1}

    @app.post("/execute")
    def execute(request: _V1Request) -> StreamingResponse:
        assert request.agent.name == "reviewer"
        return StreamingResponse(
            iter([json.dumps({"type": "result", "data": {"content": {"answer": "v1"}}}) + "\n"]),
            media_type="application/x-ndjson",
        )

    with patch.dict("os.environ", {"COPILOT_GITHUB_TOKEN": "host-token"}, clear=True):
        provider = _provider(httpx.ASGITransport(app=app))
        assert await provider.validate_connection()
        with pytest.warns(DeprecationWarning):
            output = await provider.execute(_agent(), {}, "review")
    assert output.content == {"answer": "v1"}
    assert any(
        record.name == "conductor.providers.aca" and record.levelno == logging.WARNING
        for record in caplog.records
    )
    await provider.close()


@pytest.mark.parametrize(("interrupt_requested", "addressed"), [(False, False), (True, True)])
@pytest.mark.asyncio
async def test_legacy_v2_wire_changes_only_for_addressed_interrupt(
    interrupt_requested: bool, addressed: bool
) -> None:
    # Requirement: a newer runner changes legacy wire shape only to enable /interrupt.
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200, json={"ready": True, "protocol_version": 2, "features": ["interrupt"]}
            )
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, content=json.dumps({"type": "result", "data": {"content": {}}}) + chr(10)
        )

    with patch.dict("os.environ", {"COPILOT_GITHUB_TOKEN": "host-token"}, clear=True):
        provider = _provider(httpx.MockTransport(handler))
        assert await provider.validate_connection()
        with pytest.warns(DeprecationWarning):
            await provider.execute(
                _agent(),
                {},
                "review",
                interrupt_signal=asyncio.Event() if interrupt_requested else None,
            )
    assert ("execution_id" in requests[0]) is addressed
    await provider.close()


@pytest.mark.parametrize("stream_error", [False, True])
@pytest.mark.asyncio
async def test_legacy_and_profile_classify_gateway_error_equally(stream_error: bool) -> None:
    # Requirement: HTTP and terminal NDJSON errors classify the same across both routes.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ready": True, "protocol_version": 1})
        error = {"code": "Busy", "traceId": "t-1", "message": "pool busy"}
        if stream_error:
            return httpx.Response(
                200, content=json.dumps({"type": "error", "data": error}) + chr(10)
            )
        return httpx.Response(429, json={"error": error})

    gateway = httpx.MockTransport(handler)
    with patch.dict("os.environ", {"COPILOT_GITHUB_TOKEN": "host-token"}, clear=True):
        legacy = _provider(gateway)
        backend = AcaRunnerBackend(
            AcaProfileOptions(pool_endpoint="https://pool.example.com"),
            transport=_provider(gateway),
        )
        lease = await backend.prepare_run(RunSpec("run-1"))
        with pytest.warns(DeprecationWarning), pytest.raises(ProviderError) as old:
            await legacy.execute(_agent(), {}, "review")
        with pytest.raises(ProviderError) as new:
            await backend.run_agent(_spec(), lease)
    assert old.value.status_code == new.value.status_code == (None if stream_error else 429)
    assert old.value.suggestion == new.value.suggestion == "ACA error code=Busy traceId=t-1"
    assert str(old.value) == str(new.value)
    await backend.finalize_run(lease, "failed")
    await legacy.close()


def test_aca_identifier_slots_reuse_only_released_reservations() -> None:
    # Requirement: out-of-order completion cannot collide with an in-flight sibling.
    state = AcaIdentifierState(salt="deadbeef")
    logical = state.identifier_for("reviewer", {}, "workflow")
    first, slot_a = state.acquire(logical)
    second, slot_b = state.acquire(logical)
    third, slot_c = state.acquire(logical)
    state.release(logical, slot_a)
    fourth, slot_d = state.acquire(logical)
    assert len({first, second, third}) == 3
    assert fourth == first
    assert fourth not in (second, third)
    for slot in (slot_b, slot_c, slot_d):
        state.release(logical, slot)
    assert state.active == {}


def test_item_scope_keeps_legacy_null_key_priority() -> None:
    # Requirement: a present null _key does not fall back to _index for item reuse.
    state = AcaIdentifierState(salt="deadbeef")
    assert state.identifier_for(
        "reviewer", {"_key": None, "_index": 4}, "item"
    ) == state.identifier_for("reviewer", {}, "item")


@pytest.mark.asyncio
async def test_negotiated_interrupt_targets_execution_id() -> None:
    # Requirement: a new ACA runner receives the execution identity on /interrupt.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    provider = _provider(httpx.MockTransport(handler))
    provider._active_execution_ids["session"] = "call-1"
    await provider._send_interrupt("session")
    assert requests[0].url.path == "/interrupt"
    assert json.loads(requests[0].content) == {"execution_id": "call-1"}
    await provider.close()
