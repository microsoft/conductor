"""Tests for `type: mcp` steps inside parallel groups and for-each groups.

Covers:
- An mcp step as a parallel-group member completes with ``group_name`` in
  every ``mcp_*`` payload and no ``parallel_agent_started`` (LLM-only event)
- for_each inline mcp over an N-element array invokes the tool N times, with
  ``item_key`` present in ALL ``mcp_*`` events
- Two concurrently active items stay isolated: one item's completion never
  closes or mutates the other's events or output
- The per-server slot lock serializes for_each items against ONE server
  (max simultaneous calls = 1) while different servers run concurrently
- A Jinja-templated ``runtime.working_dir`` resolving to different
  directories per item produces one pool manager per (server, cwd)
- ``is_error: true`` on an item is DATA, not an exception: fail_fast /
  continue_on_error are not triggered and the group completes as on success
- A child completing with ``CancelledError`` by itself (a BaseException that
  bypasses ``except Exception``) still triggers the fail-fast cancel+drain —
  the sibling observes cancellation before the pool close in run()'s
  finally — and the CancelledError propagates unchanged; external
  cancellation of the whole group cancels+drains children the same way

Legacy cases use MCP doubles. Docker placement cases run real SDK peers
through an executable fake Docker CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    MCPServerDef,
    MCPStepDef,
    OutputField,
    ParallelGroup,
    RouteDef,
    RuntimeConfig,
    SetStepDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ExecutionError
from tests.test_engine.mcp_placement import placement_config, placement_engine


@pytest.fixture
def realm(tmp_path, monkeypatch):
    from tests.test_mcp.fixtures.docker_realm import create_realm

    return create_realm(tmp_path, monkeypatch)


async def test_same_name_for_each_docker_profiles_and_events(realm) -> None:
    # Requirement: equal inline names resolve by group; same full keys reuse across item metadata.
    config = placement_config()
    async with placement_engine(config, realm) as engine:
        events = []
        engine._event_emitter.subscribe(events.append)
        for group in config.for_each:
            result = await engine._execute_for_each_group(group)
            assert result.count == 2 and result.errors == {}
        assert len(engine._mcp_step_managers) == 2
        assert len(list(realm.root.glob("*.launcher.json"))) == 2
        assert engine._execution_session.executed_backends() == {"docker"}
        assert engine._execution_session._file_consumers["docker"] == {
            "for_each.first.agent",
            "for_each.second.agent",
            "for_each.third.agent",
        }
        mcp_events = [event for event in events if event.type.startswith("mcp_")]
        assert len(mcp_events) == 12
        assert {
            (event.type, event.data["group_name"], event.data["item_key"], event.data["index"])
            for event in mcp_events
        } == {
            (kind, group, str(index), index)
            for kind in ("mcp_started", "mcp_completed")
            for group in ("first", "second", "third")
            for index in (0, 1)
        }


def _make_engine(config: WorkflowConfig) -> WorkflowEngine:
    return WorkflowEngine(config, MagicMock())


def _collect_events(engine: WorkflowEngine) -> list[WorkflowEvent]:
    emitter = WorkflowEventEmitter()
    received: list[WorkflowEvent] = []
    emitter.subscribe(received.append)
    engine._event_emitter = emitter
    return received


def _envelope(is_error: bool = False, answer: int = 42) -> dict[str, Any]:
    """A raw manager-shaped envelope (before the executor's structured merge)."""
    return {
        "content": [{"type": "text", "text": f"result-{answer}", "truncated": False}],
        "structured": {"answer": answer},
        "is_error": is_error,
    }


def _patch_manager(*, call: Any = None) -> Any:
    """Patch MCPManager where the engine lazily imports it.

    The fake manager advertises one tool ``echo`` on every server.
    ``call`` is an optional async side_effect for ``call_tool_structured``;
    without it every call returns a success envelope.
    """
    patcher = patch("conductor.mcp.manager.MCPManager")
    manager_cls = patcher.start()
    manager = manager_cls.return_value
    manager.connect_server = AsyncMock(return_value=[])
    manager.close = AsyncMock()
    manager.get_server_tools = MagicMock(
        side_effect=lambda name: [{"name": f"{name}__echo", "original_name": "echo"}]
    )
    if call is None:
        manager.call_tool_structured = AsyncMock(return_value=_envelope())
    else:
        manager.call_tool_structured = AsyncMock(side_effect=call)
    return patcher


def _runtime(
    *, mcp_servers: dict[str, MCPServerDef], working_dir: str | None = None
) -> RuntimeConfig:
    return RuntimeConfig(provider="copilot", mcp_servers=mcp_servers, working_dir=working_dir)


def _mcp_agent(name: str, server: str, arguments: dict[str, Any] | None = None) -> AgentDef:
    return MCPStepDef(
        name=name,
        server=server,
        tool="echo",
        arguments=arguments or {"q": "hello"},
    )


class _ConcurrencyProbe:
    """Async side effect tracking max simultaneous ``call_tool_structured`` calls."""

    def __init__(self, delay: float = 0.02) -> None:
        self._delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def run(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self._delay)
            return _envelope()
        finally:
            self.active -= 1


class TestMcpInParallelGroup:
    @pytest.mark.asyncio
    async def test_mcp_member_completes_with_group_events(self) -> None:
        # Requirement: an mcp step as a parallel-group member completes; every
        # mcp_* payload carries group_name; the member completion event has
        # agent_type "mcp" with NO output field (no-values policy); and NO
        # parallel_agent_started is emitted for the mcp member (that event is
        # LLM-only, mirroring the set branch).
        provider = MagicMock()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-parallel",
                entry_point="grp",
                runtime=_runtime(mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")}),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                _mcp_agent("call", "srv"),
                SetStepDef(name="flag", value="ready", routes=[]),
            ],
            parallel=[
                ParallelGroup(name="grp", agents=["call", "flag"], routes=[RouteDef(to="$end")])
            ],
            output={
                "answer": "{{ grp.outputs.call.answer }}",
                "flag": "{{ grp.outputs.flag }}",
            },
        )
        engine = _make_engine(config)
        received = _collect_events(engine)

        patcher = _patch_manager()
        try:
            result = await engine.run({})
        finally:
            patcher.stop()

        assert result == {"answer": 42, "flag": "ready"}
        provider.execute.assert_not_called()

        mcp_events = [ev for ev in received if ev.type.startswith("mcp_")]
        assert {ev.type for ev in mcp_events} == {"mcp_started", "mcp_completed"}
        for ev in mcp_events:
            assert ev.data["group_name"] == "grp"

        completed = next(
            ev
            for ev in received
            if ev.type == "parallel_agent_completed" and ev.data["agent_name"] == "call"
        )
        assert completed.data["agent_type"] == "mcp"
        assert "output" not in completed.data

        started = [ev for ev in received if ev.type == "parallel_agent_started"]
        assert all(ev.data["agent_name"] != "call" for ev in started)

    @pytest.mark.asyncio
    async def test_two_servers_in_parallel_group_run_concurrently(self) -> None:
        # Requirement: two mcp steps on DIFFERENT servers in one parallel
        # group are not serialized by the per-server slot lock — max
        # simultaneous calls observed is 2 (proven by a cross-wait: each call
        # waits for the other's start, so under serialization the wait
        # deadline would trip).
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-parallel-two-servers",
                entry_point="grp",
                runtime=_runtime(
                    mcp_servers={
                        "one": MCPServerDef(type="stdio", command="npx"),
                        "two": MCPServerDef(type="stdio", command="npx"),
                    }
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[_mcp_agent("cx", "one"), _mcp_agent("cy", "two")],
            parallel=[ParallelGroup(name="grp", agents=["cx", "cy"], routes=[RouteDef(to="$end")])],
            output={"total": "{{ grp.outputs.cx.answer + grp.outputs.cy.answer }}"},
        )
        engine = _make_engine(config)

        probe = _ConcurrencyProbe()
        started: dict[str, asyncio.Event] = {"one": asyncio.Event(), "two": asyncio.Event()}
        other = {"one": "two", "two": "one"}

        async def cross_wait(server: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            started[server].set()
            await asyncio.wait_for(started[other[server]].wait(), timeout=5)
            return await probe.run()

        patcher = _patch_manager(call=cross_wait)
        try:
            result = await engine.run({})
        finally:
            patcher.stop()

        assert result == {"total": 84}
        assert probe.max_active == 2
        assert probe.calls == 2

    @pytest.mark.asyncio
    async def test_fail_fast_cancels_and_drains_sibling_mcp_call(self) -> None:
        # Requirement: when one mcp member fails under fail_fast, the sibling
        # still blocked inside its call is cancelled and AWAITED before the
        # exception propagates — no mcp task outlives the group to race the
        # pool close in run()'s finally or emit events after the failure.
        # Group failure events must also carry no raw exception text.
        canary = "secret-argument-value"
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-parallel-fail-fast",
                entry_point="grp",
                runtime=_runtime(
                    mcp_servers={
                        "one": MCPServerDef(type="stdio", command="npx"),
                        "two": MCPServerDef(type="stdio", command="npx"),
                    }
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                _mcp_agent("fast", "one"),
                _mcp_agent("slow", "two"),
            ],
            parallel=[ParallelGroup(name="grp", agents=["fast", "slow"])],
            output={},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)

        order: list[str] = []
        original_close = engine._close_mcp_step_managers

        async def recording_close() -> None:
            order.append("pool-close")
            await original_close()

        engine._close_mcp_step_managers = recording_close  # type: ignore[method-assign]

        blocking = asyncio.Event()

        async def fast_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            # Fail only once the sibling is genuinely blocked inside its call,
            # so the drain must cancel an in-flight step, not a not-yet-started one.
            await asyncio.wait_for(blocking.wait(), timeout=5)
            raise RuntimeError(f"fast exploded with {canary}")

        async def slow_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            blocking.set()
            try:
                await asyncio.Event().wait()  # blocks until cancelled
            except asyncio.CancelledError:
                order.append("sibling-cancelled")
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        async def dispatch(server: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            if server == "one":
                return await fast_call()
            return await slow_call()

        patcher = _patch_manager(call=dispatch)
        try:
            with pytest.raises(ExecutionError):
                await engine.run({})
        finally:
            patcher.stop()

        # The sibling observed cancellation, and was drained before the pool
        # was closed at end of run().
        assert order == ["sibling-cancelled", "pool-close"]
        # Group failure events carry only the redacted message.
        failed = [ev for ev in received if ev.type == "parallel_agent_failed"]
        assert len(failed) == 1
        assert failed[0].data["agent_name"] == "fast"
        assert canary not in json.dumps(failed[0].data)
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert canary not in json.dumps(wf_failed[0].data)

    @pytest.mark.asyncio
    async def test_fail_fast_child_cancellederror_drains_sibling(self) -> None:
        # Requirement: a child completing with CancelledError BY ITSELF (a
        # BaseException, not an external cancel) propagates out of gather
        # without entering an except-Exception arm — the fail-fast path must
        # still cancel and drain the sibling blocked inside its mcp call
        # before anything propagates, and the CancelledError must propagate
        # unchanged (no conversion to a group failure, no workflow_failed).
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-parallel-cancelled",
                entry_point="grp",
                runtime=_runtime(
                    mcp_servers={
                        "one": MCPServerDef(type="stdio", command="npx"),
                        "two": MCPServerDef(type="stdio", command="npx"),
                    }
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                _mcp_agent("fast", "one"),
                _mcp_agent("slow", "two"),
            ],
            parallel=[ParallelGroup(name="grp", agents=["fast", "slow"])],
            output={},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)

        order: list[str] = []
        original_close = engine._close_mcp_step_managers

        async def recording_close() -> None:
            order.append("pool-close")
            await original_close()

        engine._close_mcp_step_managers = recording_close  # type: ignore[method-assign]

        blocking = asyncio.Event()

        async def fast_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            # Fail only once the sibling is genuinely blocked inside its call,
            # so the drain must cancel an in-flight step.
            await asyncio.wait_for(blocking.wait(), timeout=5)
            raise asyncio.CancelledError()

        async def slow_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            blocking.set()
            try:
                await asyncio.Event().wait()  # blocks until cancelled
            except asyncio.CancelledError:
                order.append("sibling-cancelled")
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        async def dispatch(server: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            if server == "one":
                return await fast_call()
            return await slow_call()

        patcher = _patch_manager(call=dispatch)
        try:
            with pytest.raises(asyncio.CancelledError):
                await engine.run({})
        finally:
            patcher.stop()

        # The sibling observed cancellation and was drained before the pool
        # close; cancellation stayed cancellation all the way out (no
        # workflow_failed is emitted for it).
        assert order == ["sibling-cancelled", "pool-close"]
        assert not any(ev.type == "workflow_failed" for ev in received)

    @pytest.mark.asyncio
    async def test_external_cancellation_drains_children_before_propagating(self) -> None:
        # Requirement: cancelling the run task from outside (dashboard stop /
        # timeout) lands as CancelledError at the gather and must cancel and
        # drain in-flight group children before propagating — the sibling
        # observes cancellation before the pool close in run()'s finally.
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-parallel-external-cancel",
                entry_point="grp",
                runtime=_runtime(
                    mcp_servers={
                        "one": MCPServerDef(type="stdio", command="npx"),
                        "two": MCPServerDef(type="stdio", command="npx"),
                    }
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                _mcp_agent("idle", "one"),
                _mcp_agent("slow", "two"),
            ],
            parallel=[ParallelGroup(name="grp", agents=["idle", "slow"])],
            output={},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)

        order: list[str] = []
        original_close = engine._close_mcp_step_managers

        async def recording_close() -> None:
            order.append("pool-close")
            await original_close()

        engine._close_mcp_step_managers = recording_close  # type: ignore[method-assign]

        entered = asyncio.Event()

        async def idle_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return _envelope()

        async def slow_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            entered.set()
            try:
                await asyncio.Event().wait()  # blocks until cancelled
            except asyncio.CancelledError:
                order.append("sibling-cancelled")
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        async def dispatch(server: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            if server == "one":
                return await idle_call()
            return await slow_call()

        run_task = asyncio.ensure_future(engine.run({}))
        patcher = _patch_manager(call=dispatch)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
        finally:
            patcher.stop()

        assert order == ["sibling-cancelled", "pool-close"]
        assert not any(ev.type == "workflow_failed" for ev in received)

    @pytest.mark.asyncio
    async def test_repeated_cancellation_still_drains_sibling_cleanup(self) -> None:
        # Requirement: a SECOND cancel() arriving while the fail-fast drain is
        # in flight must not abandon the drain — the sibling's cancellation
        # cleanup (which awaits, e.g. releasing a resource) completes before
        # the pool close in run()'s finally, and the propagated exception is
        # the original CancelledError (the extra cancellation never replaces
        # it).
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-parallel-repeated-cancel",
                entry_point="grp",
                runtime=_runtime(
                    mcp_servers={
                        "one": MCPServerDef(type="stdio", command="npx"),
                        "two": MCPServerDef(type="stdio", command="npx"),
                    }
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                _mcp_agent("idle", "one"),
                _mcp_agent("slow", "two"),
            ],
            parallel=[ParallelGroup(name="grp", agents=["idle", "slow"])],
            output={},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)

        order: list[str] = []
        original_close = engine._close_mcp_step_managers

        async def recording_close() -> None:
            order.append("pool-close")
            await original_close()

        engine._close_mcp_step_managers = recording_close  # type: ignore[method-assign]

        entered = asyncio.Event()

        async def idle_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return _envelope()

        async def slow_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            entered.set()
            try:
                await asyncio.Event().wait()  # blocks until cancelled
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)  # cancellation cleanup that awaits
                order.append("sibling-cleanup-done")
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        async def dispatch(server: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            if server == "one":
                return await idle_call()
            return await slow_call()

        run_task = asyncio.ensure_future(engine.run({}))
        patcher = _patch_manager(call=dispatch)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            run_task.cancel()
            # Let the first cancellation reach the group gather and the drain
            # start (the sibling is now inside its 0.05s cleanup), then cancel
            # again — this second request must land on the shield, not the
            # drain.
            await asyncio.sleep(0.01)
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
        finally:
            patcher.stop()

        # The sibling's cleanup COMPLETED before the pool close, and the
        # propagated exception stayed the original CancelledError (no
        # workflow_failed is emitted for it).
        assert order == ["sibling-cleanup-done", "pool-close"]
        assert not any(ev.type == "workflow_failed" for ev in received)

    @pytest.mark.asyncio
    async def test_output_schema_mismatch_in_group_member_leaks_no_values(self) -> None:
        # Requirement: an output: schema mismatch on an mcp PARALLEL-group
        # member wraps redacted — the ValidationError message echoes the
        # received result value, so the canary must appear in neither
        # parallel_agent_failed nor workflow_failed payloads, nor the raised
        # error (the sibling set member succeeding must not change this).
        canary = "SECRET_CANARY_9f13"
        mcp = _mcp_agent("call", "srv")
        mcp.output = {"answer": OutputField(type="number")}
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-parallel-schema",
                entry_point="grp",
                runtime=_runtime(mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")}),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[mcp, SetStepDef(name="flag", value="ready", routes=[])],
            parallel=[
                ParallelGroup(name="grp", agents=["call", "flag"], routes=[RouteDef(to="$end")])
            ],
            output={},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)

        def _value_envelope(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": "ok", "truncated": False}],
                "structured": {"answer": canary},
                "is_error": False,
            }

        patcher = _patch_manager(call=_value_envelope)
        try:
            with pytest.raises(ExecutionError) as exc_info:
                await engine.run({})
        finally:
            patcher.stop()

        assert canary not in str(exc_info.value)
        failed = [ev for ev in received if ev.type == "parallel_agent_failed"]
        assert len(failed) == 1
        assert failed[0].data["agent_name"] == "call"
        assert canary not in json.dumps(failed[0].data)
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert canary not in json.dumps(wf_failed[0].data)

    @pytest.mark.asyncio
    async def test_templated_working_dir_in_group_member_leaks_no_values(self) -> None:
        # Requirement: the runtime working_dir not-a-directory check on an mcp
        # PARALLEL-group member must not leak the Jinja-rendered path or the
        # raw template — both come from the execution context. The redacted
        # message surfaces through parallel_agent_failed / workflow_failed;
        # the authored name-only runtime checks stay verbatim (covered by the
        # unknown-server test in test_mcp_step_workflow.py).
        canary = "SECRET_CANARY_9f13"
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-parallel-cwd",
                entry_point="grp",
                runtime=_runtime(
                    mcp_servers={
                        "srv": MCPServerDef(type="stdio", command="npx"),
                        "srv2": MCPServerDef(type="stdio", command="npx"),
                    },
                    working_dir="{{ workflow.input.secret_path }}",
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[_mcp_agent("call", "srv"), _mcp_agent("sibling", "srv2")],
            parallel=[
                ParallelGroup(name="grp", agents=["call", "sibling"], routes=[RouteDef(to="$end")])
            ],
            output={},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)

        patcher = _patch_manager()
        try:
            with pytest.raises(ExecutionError) as exc_info:
                await engine.run({"secret_path": f"/nonexistent/{canary}"})
        finally:
            patcher.stop()

        assert "does not exist or is not a directory" in str(exc_info.value)
        assert canary not in str(exc_info.value)
        assert "{{ workflow.input.secret_path }}" not in str(exc_info.value)
        failed = [ev for ev in received if ev.type == "parallel_agent_failed"]
        # Both members share the workflow-level working_dir, so both hit the
        # same redacted cwd check.
        assert len(failed) == 2
        for ev in failed:
            assert canary not in json.dumps(ev.data)
            assert "{{ workflow.input.secret_path }}" not in json.dumps(ev.data)
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert canary not in json.dumps(wf_failed[0].data)
        assert "{{ workflow.input.secret_path }}" not in json.dumps(wf_failed[0].data)


class TestMcpInForEach:
    def _config(self, *, max_concurrent: int = 3) -> WorkflowConfig:
        return WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-for-each",
                entry_point="loop",
                runtime=_runtime(mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")}),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[],
            for_each=[
                ForEachDef(
                    name="loop",
                    type="for_each",
                    source="workflow.input.items",
                    **{"as": "item"},
                    max_concurrent=max_concurrent,
                    failure_mode="fail_fast",
                    agent=_mcp_agent("call", "srv", arguments={"q": "{{ item }}"}),
                    routes=[RouteDef(to="$end")],
                )
            ],
            output={"count": "{{ loop.outputs | length }}"},
        )

    @pytest.mark.asyncio
    async def test_three_items_with_item_key_in_all_events(self) -> None:
        # Requirement: a for_each inline mcp step over a 3-element array
        # completes all items, and item_key is present in ALL mcp_* event
        # payloads (plus group_name) so per-item events are distinguishable;
        # for_each_item_completed carries no output field (no-values policy).
        engine = _make_engine(self._config())
        received = _collect_events(engine)

        patcher = _patch_manager()
        try:
            result = await engine.run({"items": ["a", "b", "c"]})
        finally:
            patcher.stop()

        assert result == {"count": 3}

        mcp_started = [ev for ev in received if ev.type == "mcp_started"]
        mcp_completed = [ev for ev in received if ev.type == "mcp_completed"]
        assert len(mcp_started) == 3
        assert len(mcp_completed) == 3
        for ev in mcp_started + mcp_completed:
            assert ev.data["group_name"] == "loop"
            assert ev.data["item_key"] in {"0", "1", "2"}
            assert ev.data["index"] in {0, 1, 2}
        assert {ev.data["item_key"] for ev in mcp_started} == {"0", "1", "2"}
        assert {ev.data["index"] for ev in mcp_started} == {0, 1, 2}

        item_completed = [ev for ev in received if ev.type == "for_each_item_completed"]
        assert len(item_completed) == 3
        for ev in item_completed:
            assert ev.data["item_key"] in {"0", "1", "2"}
            assert ev.data["index"] in {0, 1, 2}
            assert "output" not in ev.data

    @pytest.mark.asyncio
    async def test_invocation_count_matches_items(self) -> None:
        # Requirement: the tool is invoked exactly once per item, each call
        # receiving its own item's rendered arguments.
        engine = _make_engine(self._config())
        seen: list[str] = []

        async def record_call(
            _server: str, _tool: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            seen.append(arguments["q"])
            return _envelope()

        patcher = _patch_manager(call=record_call)
        try:
            await engine.run({"items": ["a", "b", "c"]})
        finally:
            patcher.stop()

        assert sorted(seen) == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_interleaved_completions_do_not_cross_contaminate_items(self) -> None:
        # Requirement: with two concurrently active items, one item's
        # completion (is_error=true) never closes or mutates the other —
        # no for_each_item_failed is emitted, both items complete, and each
        # item's stored envelope carries only its own outcome. The slot lock
        # is loosened to per-call locks so both items are genuinely in flight
        # at once (real single-server serialization is pinned by the
        # max_concurrent test; real two-server concurrency by the parallel
        # test) — one for_each agent cannot name two servers, and server is
        # literal-only by schema.
        engine = _make_engine(self._config(max_concurrent=2))
        received = _collect_events(engine)

        async def per_call_slot(_server: str) -> asyncio.Lock:
            return asyncio.Lock()

        engine._mcp_step_slot = per_call_slot  # type: ignore[method-assign]

        started: dict[str, asyncio.Event] = {"x": asyncio.Event(), "y": asyncio.Event()}
        finished_x = asyncio.Event()

        async def item_call(_server: str, _tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            item = arguments["q"]
            started[item].set()
            if item == "x":
                # x completes while y is still in flight.
                await asyncio.wait_for(started["y"].wait(), timeout=5)
                finished_x.set()
                return _envelope(is_error=True, answer=1)
            await asyncio.wait_for(finished_x.wait(), timeout=5)
            return _envelope(is_error=False, answer=2)

        patcher = _patch_manager(call=item_call)
        try:
            result = await engine.run({"items": ["x", "y"]})
        finally:
            patcher.stop()

        assert result == {"count": 2}
        assert not any(ev.type == "for_each_item_failed" for ev in received)
        assert not any(ev.type == "mcp_failed" for ev in received)
        assert not any(ev.type == "workflow_failed" for ev in received)

        # item keys are positional indexes ("0" for x, "1" for y)
        x_completed = next(
            ev for ev in received if ev.type == "mcp_completed" and ev.data["item_key"] == "0"
        )
        assert x_completed.data["is_error"] is True
        y_completed = next(
            ev for ev in received if ev.type == "mcp_completed" and ev.data["item_key"] == "1"
        )
        assert y_completed.data["is_error"] is False

        # Stored outputs stay per-item: x kept its error envelope, y its own.
        outputs = engine.context.agent_outputs["loop"]["outputs"]
        assert len(outputs) == 2
        by_is_error = {out["is_error"]: out["answer"] for out in outputs}
        assert by_is_error == {True: 1, False: 2}

    @pytest.mark.asyncio
    async def test_max_concurrent_two_against_one_server_serializes(self) -> None:
        # Requirement: even with max_concurrent: 2, items against ONE server
        # run strictly sequentially — the per-server slot lock caps max
        # simultaneous calls at 1.
        engine = _make_engine(self._config(max_concurrent=2))
        probe = _ConcurrencyProbe()

        patcher = _patch_manager(call=probe.run)
        try:
            await engine.run({"items": ["a", "b", "c"]})
        finally:
            patcher.stop()

        assert probe.calls == 3
        assert probe.max_active == 1

    @pytest.mark.asyncio
    async def test_templated_working_dir_pools_per_item_cwd(self, tmp_path: Any) -> None:
        # Requirement: a Jinja-templated runtime.working_dir resolving to two
        # different directories per for_each item produces one pool entry per
        # (server, cwd), and every call runs with its own item's cwd.
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()
        config = self._config(max_concurrent=2)
        config.workflow.runtime.working_dir = "{{ item }}"
        engine = _make_engine(config)

        original = engine._get_mcp_step_manager
        resolved: list[tuple[str, str]] = []

        async def recording(server_name: str, resolved_cwd: str) -> Any:
            resolved.append((server_name, resolved_cwd))
            return await original(server_name, resolved_cwd)

        engine._get_mcp_step_manager = recording  # type: ignore[method-assign]

        patcher = _patch_manager()
        try:
            await engine.run({"items": [str(dir_a), str(dir_b)]})
        finally:
            patcher.stop()

        assert sorted(resolved) == [
            ("srv", os.path.normpath(str(dir_a))),
            ("srv", os.path.normpath(str(dir_b))),
        ]

    @pytest.mark.asyncio
    async def test_fail_fast_cancels_and_drains_sibling_item(self) -> None:
        # Requirement (for_each variant): a failing item under fail_fast
        # cancels and drains the sibling item before the exception
        # propagates — the sibling observes cancellation before the pool
        # close in run()'s finally, item failure events carry no raw
        # exception text, and mcp_failed reports the real error type.
        #
        # Roles follow CALL order, not item identity: both items share one
        # server, so the per-server slot serializes them and no coordination
        # event could ever let both be inside a call at once (an earlier
        # version of this test waited on exactly such an event — the wait
        # timed out, the canary-bearing RuntimeError never fired, and the
        # redaction assertions were vacuous). The first item to acquire the
        # slot fails immediately; the sibling is cancelled wherever the
        # fail-fast drain finds it (blocked at the slot boundary or blocked
        # inside its call) — both land in `order` before the pool close.
        canary = "secret-argument-value"
        engine = _make_engine(self._config(max_concurrent=2))
        received = _collect_events(engine)

        order: list[str] = []
        original_close = engine._close_mcp_step_managers

        async def recording_close() -> None:
            order.append("pool-close")
            await original_close()

        engine._close_mcp_step_managers = recording_close  # type: ignore[method-assign]

        async def item_call(_server: str, _tool: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            if not any(entry.startswith("call") for entry in order):
                order.append("call-raised")
                raise RuntimeError(f"call exploded with {canary}")
            # The sibling blocks until the fail-fast drain cancels it.
            order.append("sibling-blocked")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("sibling-cancelled")
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        patcher = _patch_manager(call=item_call)
        try:
            with pytest.raises(ExecutionError):
                await engine.run({"items": ["a", "b"]})
        finally:
            patcher.stop()

        assert order == ["call-raised", "sibling-blocked", "sibling-cancelled", "pool-close"]
        # The canary-bearing RuntimeError genuinely happened (an earlier
        # version never raised it) and its type reached mcp_failed.
        mcp_failed = [ev for ev in received if ev.type == "mcp_failed"]
        assert len(mcp_failed) == 1
        assert mcp_failed[0].data["error_type"] == "RuntimeError"
        assert canary not in json.dumps(mcp_failed[0].data)
        failed = [ev for ev in received if ev.type == "for_each_item_failed"]
        assert len(failed) == 1
        assert canary not in json.dumps(failed[0].data)
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert canary not in json.dumps(wf_failed[0].data)

    @pytest.mark.asyncio
    async def test_fail_fast_item_cancellederror_drains_sibling(self) -> None:
        # Requirement: an item completing with CancelledError BY ITSELF (a
        # BaseException that bypasses except Exception) must still trigger the
        # fail-fast cancel+drain — the sibling item observes cancellation
        # (whether parked on the per-server slot or inside its call) before
        # the pool close in run()'s finally — and the CancelledError
        # propagates unchanged out of the run.
        engine = _make_engine(self._config(max_concurrent=2))
        received = _collect_events(engine)

        order: list[str] = []
        original_close = engine._close_mcp_step_managers

        async def recording_close() -> None:
            order.append("pool-close")
            await original_close()

        engine._close_mcp_step_managers = recording_close  # type: ignore[method-assign]

        # One server serializes the items on the slot lock, so the sibling
        # may observe cancellation at the slot or inside its call — record
        # both by wrapping the step (event_fields carries the item_key).
        original_step = engine._run_mcp_step

        async def recording_step(
            agent: AgentDef,
            agent_context: Any,
            *,
            event_fields: Any = None,
            for_each_group: str | None = None,
        ) -> Any:
            try:
                return await original_step(
                    agent,
                    agent_context,
                    event_fields=event_fields,
                    for_each_group=for_each_group,
                )
            except asyncio.CancelledError:
                if event_fields and event_fields.get("item_key") == "1":
                    order.append("sibling-cancelled")
                raise

        engine._run_mcp_step = recording_step  # type: ignore[method-assign]

        async def item_call(_server: str, _tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if arguments["q"] == "a":
                raise asyncio.CancelledError()
            await asyncio.Event().wait()  # blocks until cancelled
            raise AssertionError("unreachable")  # pragma: no cover

        patcher = _patch_manager(call=item_call)
        try:
            with pytest.raises(asyncio.CancelledError):
                await engine.run({"items": ["a", "b"]})
        finally:
            patcher.stop()

        assert order == ["sibling-cancelled", "pool-close"]
        assert not any(ev.type == "workflow_failed" for ev in received)

    @pytest.mark.asyncio
    async def test_repeated_cancellation_still_drains_item_cleanup(self) -> None:
        # Requirement (for_each): same invariant as the parallel variant — a
        # second cancel() arriving while the fail-fast drain is in flight
        # must not abandon the drain; item "a"'s cancellation cleanup (which
        # awaits) completes before the pool close in run()'s finally, and
        # the propagated exception is the original CancelledError.
        engine = _make_engine(self._config(max_concurrent=2))
        received = _collect_events(engine)

        order: list[str] = []
        original_close = engine._close_mcp_step_managers

        async def recording_close() -> None:
            order.append("pool-close")
            await original_close()

        engine._close_mcp_step_managers = recording_close  # type: ignore[method-assign]

        entered = asyncio.Event()

        async def item_call(_server: str, _tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if arguments["q"] == "a":
                entered.set()
                try:
                    await asyncio.Event().wait()  # blocks until cancelled
                except asyncio.CancelledError:
                    await asyncio.sleep(0.05)  # cancellation cleanup that awaits
                    order.append("sibling-cleanup-done")
                    raise
            await asyncio.Event().wait()  # item "b" parked on the slot: cancelled there
            raise AssertionError("unreachable")  # pragma: no cover

        run_task = asyncio.ensure_future(engine.run({"items": ["a", "b"]}))
        patcher = _patch_manager(call=item_call)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            run_task.cancel()
            # Let the first cancellation reach the batch gather and the drain
            # start (item "a" is now inside its 0.05s cleanup), then cancel
            # again — this second request must land on the shield, not the
            # drain.
            await asyncio.sleep(0.01)
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
        finally:
            patcher.stop()

        assert order == ["sibling-cleanup-done", "pool-close"]
        assert not any(ev.type == "workflow_failed" for ev in received)

    @pytest.mark.asyncio
    async def test_output_schema_mismatch_in_item_leaks_no_values(self) -> None:
        # Requirement: an output: schema mismatch on a for_each mcp item wraps
        # redacted — the canary result value must appear in neither
        # for_each_item_failed nor workflow_failed payloads, nor the raised
        # error, and the mcp_failed payload stays redacted too.
        canary = "SECRET_CANARY_9f13"
        config = self._config()
        config.for_each[0].agent.output = {"answer": OutputField(type="number")}
        engine = _make_engine(config)
        received = _collect_events(engine)

        def _value_envelope(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": "ok", "truncated": False}],
                "structured": {"answer": canary},
                "is_error": False,
            }

        patcher = _patch_manager(call=_value_envelope)
        try:
            with pytest.raises(ExecutionError) as exc_info:
                await engine.run({"items": ["only"]})
        finally:
            patcher.stop()

        assert canary not in str(exc_info.value)
        mcp_failed = [ev for ev in received if ev.type == "mcp_failed"]
        assert len(mcp_failed) == 1
        assert canary not in json.dumps(mcp_failed[0].data)
        item_failed = [ev for ev in received if ev.type == "for_each_item_failed"]
        assert len(item_failed) == 1
        assert canary not in json.dumps(item_failed[0].data)
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert canary not in json.dumps(wf_failed[0].data)

    @pytest.mark.asyncio
    async def test_templated_working_dir_from_item_leaks_no_values(self) -> None:
        # Requirement: when runtime.working_dir renders from the LOOP
        # VARIABLE ("{{ item }}"), the not-a-directory check must not leak
        # the item's value or the raw template — the for_each_item_failed
        # and workflow_failed payloads stay value-free. The item value is a
        # relative path segment, so the rendered cwd cannot exist.
        canary = "SECRET_CANARY_9f13"
        config = self._config()
        config.workflow.runtime.working_dir = "{{ item }}"
        engine = _make_engine(config)
        received = _collect_events(engine)

        patcher = _patch_manager()
        try:
            with pytest.raises(ExecutionError) as exc_info:
                await engine.run({"items": [canary]})
        finally:
            patcher.stop()

        assert "does not exist or is not a directory" in str(exc_info.value)
        assert canary not in str(exc_info.value)
        assert "{{ item }}" not in str(exc_info.value)
        mcp_failed = [ev for ev in received if ev.type == "mcp_failed"]
        assert len(mcp_failed) == 1
        assert canary not in json.dumps(mcp_failed[0].data)
        assert "{{ item }}" not in json.dumps(mcp_failed[0].data)
        item_failed = [ev for ev in received if ev.type == "for_each_item_failed"]
        assert len(item_failed) == 1
        assert canary not in json.dumps(item_failed[0].data)
        assert "{{ item }}" not in json.dumps(item_failed[0].data)
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert canary not in json.dumps(wf_failed[0].data)
        assert "{{ item }}" not in json.dumps(wf_failed[0].data)

    @pytest.mark.asyncio
    async def test_is_error_item_is_a_successful_item_under_fail_fast(self) -> None:
        # Requirement (explicit): is_error=true on an item is DATA for
        # routing, not an exception — fail_fast is NOT triggered, no
        # for_each_item_failed / workflow_failed is emitted, and the group
        # completes exactly as on a successful call.
        engine = _make_engine(self._config())
        received = _collect_events(engine)

        patcher = _patch_manager(call=lambda *_a, **_k: _envelope(is_error=True, answer=7))
        try:
            result = await engine.run({"items": ["only"]})
        finally:
            patcher.stop()

        assert result == {"count": 1}
        assert not any(ev.type == "for_each_item_failed" for ev in received)
        assert not any(ev.type == "workflow_failed" for ev in received)
        completed = [ev for ev in received if ev.type == "for_each_item_completed"]
        assert len(completed) == 1
        assert completed[0].data["item_key"] == "0"
        stored = engine.context.agent_outputs["loop"]["outputs"][0]
        assert stored["is_error"] is True
        assert stored["answer"] == 7
