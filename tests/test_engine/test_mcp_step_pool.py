"""Tests for the engine-owned MCPManager pool backing `type: mcp` steps.

Covers:
- Lazy connect happens once and the manager is reused for repeated calls
- connect_server raising propagates, leaves no half-open manager in the pool,
  and the next call re-attempts the connect
- Two servers connect concurrently (the pool guard never spans I/O)
- run()/resume() finally blocks close all pooled managers and clear the pool
- The per-server slot lock is a stable object per server name and distinct
  across server names

Legacy cases use MCP doubles. Docker placement cases run real SDK peers
through an executable fake Docker CLI.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    LimitsConfig,
    MCPServerDef,
    RouteDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import _MCP_STEP_POOL_MAX, WorkflowEngine
from conductor.exceptions import ExecutionError
from tests.test_engine.mcp_placement import owned_opening, placement_config, placement_engine


@pytest.fixture
def realm(tmp_path, monkeypatch):
    from tests.test_mcp.fixtures.docker_realm import create_realm

    return create_realm(tmp_path, monkeypatch)


def _make_engine(mcp_servers: dict[str, MCPServerDef] | None = None) -> WorkflowEngine:
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="mcp-pool",
            entry_point="start",
            runtime=RuntimeConfig(
                provider="copilot",
                mcp_servers=mcp_servers or {},
            ),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            AgentDef(
                name="start",
                prompt="start",
                output={"done": {"type": "boolean"}},
                routes=[RouteDef(to="$end")],
            )
        ],
    )
    return WorkflowEngine(config, MagicMock())


def _patch_manager_class() -> Any:
    """Patch MCPManager where the engine lazily imports it."""
    return patch("conductor.mcp.manager.MCPManager")


def _manager_factory(**_kwargs: Any) -> MagicMock:
    """Build a distinct fake manager per MCPManager construction.

    ``manager_cls.return_value`` would hand every pool key the same instance,
    proving nothing about distinctness — each construction gets its own.
    """
    manager = MagicMock()
    manager.connect_server = AsyncMock(return_value=[])
    manager.close = AsyncMock()
    return manager


@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_owned_opening_releases_and_joins_after_readiness_failure(
    cleanup_error: bool,
) -> None:
    # Requirement: failed readiness releases and joins its owner without hiding either failure.
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    primary = TimeoutError("readiness failed")
    secondary = RuntimeError("opening cleanup failed")
    owners: list[asyncio.Task[None]] = []

    async def open_until_released() -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            closed.set()
            if cleanup_error:
                raise secondary

    with pytest.raises(TimeoutError) as caught:
        async with owned_opening(open_until_released(), release) as opening:
            owners.append(opening)
            await asyncio.wait_for(entered.wait(), 5)
            raise primary

    assert caught.value is primary
    assert release.is_set() and closed.is_set()
    assert [owner.done() for owner in owners] == [True]
    assert primary.__cause__ is (secondary if cleanup_error else None)


async def test_docker_profile_full_execution_identity_and_reuse(realm) -> None:
    # Requirement: full profile/execution identity splits targets; identical keys reuse a peer.
    config = placement_config()
    async with placement_engine(config, realm) as engine:
        first = await engine._run_mcp_step(config.agents[0], {})
        entry = next(iter(engine._mcp_step_managers.values()))
        await engine._run_mcp_step(config.agents[1], {})
        assert next(iter(engine._mcp_step_managers.values())) is entry
        assert len(engine._mcp_step_managers) == 1
        profile = engine._execution_resolver.manifest.profiles["again"]
        assert profile.execution is not None
        manifest = engine._execution_resolver.manifest
        engine._execution_resolver._manifest = manifest.model_copy(
            update={
                "profiles": {
                    **manifest.profiles,
                    "again": profile.model_copy(
                        update={
                            "execution": profile.execution.model_copy(update={"network": "none"})
                        }
                    ),
                }
            }
        )
        await engine._run_mcp_step(config.agents[1], {})
        assert len(engine._mcp_step_managers) == 2
        assert first["is_error"] is False
        assert len(list(realm.root.glob("*.launcher.json"))) == 2


async def test_docker_cwd_mapped_key_and_single_render(realm, monkeypatch) -> None:
    # Requirement: mapped cwd determines reuse without a host probe or a second template render.
    config = placement_config(cwd="{{ directory }}")
    async with placement_engine(config, realm) as engine:
        render = engine.renderer.render
        rendered = []

        def witness(template, context):
            if template == "{{ directory }}":
                rendered.append(context["directory"])
            return render(template, context)

        monkeypatch.setattr(engine.renderer, "render", witness)
        from pathlib import Path

        probe = Path.is_dir

        def host_probe(path):
            assert not str(path).startswith("/workspace")
            return probe(path)

        monkeypatch.setattr(Path, "is_dir", host_probe)
        for directory in ("new/{{ literal }}", "./new/{{ literal }}", "other"):
            await engine._run_mcp_step(config.agents[0], {"directory": directory})
        assert rendered == ["new/{{ literal }}", "./new/{{ literal }}", "other"]
        assert len(engine._mcp_step_managers) == 2
        import json

        workdirs = []
        for path in realm.root.glob("*.launcher.json"):
            argv = json.loads(path.read_text())["argv"]
            workdirs.append(argv[argv.index("-w") + 1])
        assert sorted(workdirs) == ["/workspace/main/new/{{ literal }}", "/workspace/main/other"]


async def test_docker_incarnation_replaces_stale_pool_identity(realm) -> None:
    # Requirement: a fresh backend-issued incarnation never reuses the previous SDK session.
    config = placement_config()
    async with placement_engine(config, realm) as engine:
        await engine._run_mcp_step(config.agents[0], {})
        old_entry = next(iter(engine._mcp_step_managers.values()))
        old_lease = engine._execution_session.leases["docker"]
        await old_entry.aclose()
        await realm.backend.finalize_run(old_lease, "succeeded")
        new_lease = await realm.backend.prepare_run(realm.run)
        assert old_lease.lease_id == new_lease.lease_id
        assert old_lease.incarnation != new_lease.incarnation
        engine._execution_session.leases["docker"] = new_lease
        try:
            await engine._run_mcp_step(config.agents[0], {})
            assert len(engine._mcp_step_managers) == 2
            assert len(list(realm.root.glob("*.launcher.json"))) == 2
        finally:
            await engine._close_mcp_step_managers()
            await realm.backend.finalize_run(new_lease, "succeeded")


@pytest.mark.parametrize("expired", [False, True])
async def test_allocation_docker_only_root_owns_late_target(realm, monkeypatch, expired) -> None:
    # Requirement: pending allocation is not empty cleanup; late targets never publish or launch.
    from dataclasses import replace

    from conductor.execution.errors import ExecutionSpecError
    from conductor.mcp.manager import MCPManager

    config = placement_config()
    allocated, release = asyncio.Event(), asyncio.Event()
    order = []
    original_open = realm.backend.open_mcp_stdio
    original_finalize = realm.backend.finalize_run
    original_close = MCPManager.close

    async def held_open(spec, lease):
        target = await original_open(spec, lease)

        async def close_target():
            order.append("target")
            await target.aclose()

        allocated.set()
        await release.wait()
        return replace(target, aclose=close_target)

    async def close_manager(manager, *, strict=False):
        assert strict
        await original_close(manager, strict=strict)
        order.append("manager")

    async def finalize(lease, outcome, *, retain=False):
        order.append("finalize")
        await original_finalize(lease, outcome, retain=retain)

    monkeypatch.setattr(realm.backend, "open_mcp_stdio", held_open)
    monkeypatch.setattr(realm.backend, "finalize_run", finalize)
    monkeypatch.setattr(MCPManager, "close", close_manager)
    async with placement_engine(config, realm) as engine:
        session = engine._execution_session
        # Staging is a prerequisite, outside the held allocation readiness budget.
        execution = engine._execution_resolver.manifest.profiles["call"].execution
        assert execution is not None
        await realm.backend._ensure_staged(session.leases["docker"], execution, None)
        async with owned_opening(
            engine._get_mcp_step_manager("srv", None, step_key="call"), release
        ) as opening:
            await asyncio.wait_for(allocated.wait(), 5)
            session.begin_teardown("cancelled", retain=expired)
            deadline = session.cleanup_deadline
            assert session.sealed and deadline is not None
            assert order == []
            if expired:
                opening.cancel()
                loop = asyncio.get_running_loop()
                clock = loop.time
                monkeypatch.setattr(loop, "time", lambda: clock() + 31)
                await session.finalize_leases("cancelled", retain=True)
                assert session.cleanup_uncertain
                assert "finalize" not in order
            release.set()
            if expired:
                with pytest.raises(asyncio.CancelledError):
                    await opening
            else:
                with pytest.raises(ExecutionSpecError, match="sealed"):
                    await opening
            await session.finalize_leases("cancelled", retain=expired)
            entry = session.mcp_lifecycle.attempts[0].entries[0]
            assert entry._cleanup is not None
            await asyncio.wait_for(asyncio.shield(entry._cleanup), 5)
            assert engine._mcp_step_managers == {}
            assert order == (
                ["manager", "target"] if expired else ["manager", "target", "finalize"]
            )
            assert session.cleanup_deadline == deadline
            assert not list(realm.root.glob("*.launcher.json"))


async def test_docker_eviction_retains_cleanup_proof_until_root_finalize(realm, monkeypatch):
    # Requirement: evicted SDK entries remain owned and root finalization joins every target.
    import json

    import conductor.engine.workflow as workflow

    monkeypatch.setattr(workflow, "_MCP_STEP_POOL_MAX", 2)
    config = placement_config(cwd="{{ cwd }}")
    async with placement_engine(config, realm) as engine:
        for cwd in ("one", "two", "three"):
            await engine._run_mcp_step(config.agents[0], {"cwd": cwd})
        entries = engine._execution_session.mcp_lifecycle.attempts[0].entries
        assert len(entries) == 3 and len(engine._mcp_step_managers) == 2
        first = entries[0]
        assert first.target is not None and first._cleanup is not None
        assert first._cleanup.done() and not first.cleanup_unconfirmed
        launch = json.loads((realm.root / f"{first.target.identity}.launcher.json").read_text())
        peer = json.loads((realm.root / f"{first.target.identity}.json").read_text())
        assert launch["joined"] and peer["closed"]
        state = json.loads((realm.root / "state.json").read_text())
        assert first.target.identity not in state["containers"]
        await engine._execution_session.finalize_leases("succeeded")
        assert not engine._execution_session.quarantined
        state = json.loads((realm.root / "state.json").read_text())
        assert state["containers"] == {} and state["volumes"] == {}


class TestLazyConnectAndReuse:
    @pytest.mark.asyncio
    async def test_connect_once_manager_reused(self) -> None:
        # Requirement: the first call connects lazily; repeated calls for the
        # same (server, cwd) key return the pooled manager without reconnecting.
        engine = _make_engine({"srv": MCPServerDef(type="stdio", command="npx")})
        with _patch_manager_class() as manager_cls:
            manager = manager_cls.return_value
            manager.connect_server = AsyncMock(return_value=[])
            async with await engine._mcp_step_slot("srv"):
                first = await engine._get_mcp_step_manager("srv", "/tmp/wd")
            async with await engine._mcp_step_slot("srv"):
                second = await engine._get_mcp_step_manager("srv", "/tmp/wd")
        assert first is second
        manager.connect_server.assert_awaited_once()
        assert engine._mcp_step_managers == {("srv", "/tmp/wd"): manager}

    @pytest.mark.asyncio
    async def test_connect_failure_propagates_and_pool_stays_empty(self) -> None:
        # Requirement: a connect_server failure propagates, leaves no half-open
        # manager in the pool, and the next call re-attempts the connect.
        engine = _make_engine({"srv": MCPServerDef(type="stdio", command="npx")})
        with _patch_manager_class() as manager_cls:
            manager = manager_cls.return_value
            manager.connect_server = AsyncMock(side_effect=RuntimeError("spawn failed"))
            with pytest.raises(RuntimeError, match="spawn failed"):
                async with await engine._mcp_step_slot("srv"):
                    await engine._get_mcp_step_manager("srv", "/tmp/wd")
            assert engine._mcp_step_managers == {}
            with pytest.raises(RuntimeError, match="spawn failed"):
                async with await engine._mcp_step_slot("srv"):
                    await engine._get_mcp_step_manager("srv", "/tmp/wd")
        assert manager.connect_server.await_count == 2

    @pytest.mark.asyncio
    async def test_different_cwds_get_distinct_managers(self) -> None:
        # Requirement: the pool key is (server_name, resolved_cwd) — a for_each
        # whose runtime.working_dir renders differently per item must not reuse
        # the first item's server process for the rest. Each connect must
        # return a DISTINCT manager instance, and a repeated key must reuse
        # its own instance without reconnecting.
        engine = _make_engine({"srv": MCPServerDef(type="stdio", command="npx")})
        with _patch_manager_class() as manager_cls:
            manager_cls.side_effect = _manager_factory
            async with await engine._mcp_step_slot("srv"):
                a1 = await engine._get_mcp_step_manager("srv", "/tmp/a")
            async with await engine._mcp_step_slot("srv"):
                b = await engine._get_mcp_step_manager("srv", "/tmp/b")
            async with await engine._mcp_step_slot("srv"):
                a2 = await engine._get_mcp_step_manager("srv", "/tmp/a")
        assert a1 is not b
        assert a1 is a2
        assert manager_cls.call_count == 2
        assert a1.connect_server.await_count == 1
        assert b.connect_server.await_count == 1
        assert set(engine._mcp_step_managers) == {("srv", "/tmp/a"), ("srv", "/tmp/b")}
        assert engine._mcp_step_managers[("srv", "/tmp/a")] is a1
        assert engine._mcp_step_managers[("srv", "/tmp/b")] is b


class TestConcurrentServers:
    @pytest.mark.asyncio
    async def test_two_servers_connect_concurrently(self) -> None:
        # Requirement: the pool guard only covers lock-dict mutation, never
        # I/O — two servers' lazy connects must overlap, not serialize. Each
        # connect waits for the other's start event; under serialization the
        # first connect would block until the wait_for deadline.
        engine = _make_engine(
            {
                "one": MCPServerDef(type="stdio", command="npx"),
                "two": MCPServerDef(type="stdio", command="npx"),
            }
        )
        started: dict[str, asyncio.Event] = {"one": asyncio.Event(), "two": asyncio.Event()}

        async def fake_connect(name: str, **_kwargs: Any) -> list[dict[str, Any]]:
            started[name].set()
            await asyncio.wait_for(started[{"one": "two", "two": "one"}[name]].wait(), timeout=5)
            return []

        with _patch_manager_class() as manager_cls:
            manager = manager_cls.return_value
            manager.connect_server = AsyncMock(side_effect=fake_connect)

            async def acquire(server: str) -> None:
                async with await engine._mcp_step_slot(server):
                    await engine._get_mcp_step_manager(server, "/tmp/wd")

            await asyncio.wait_for(asyncio.gather(acquire("one"), acquire("two")), timeout=10)
        assert set(engine._mcp_step_managers) == {("one", "/tmp/wd"), ("two", "/tmp/wd")}


class TestSlotLocks:
    @pytest.mark.asyncio
    async def test_same_lock_per_server_distinct_across_servers(self) -> None:
        # Requirement: _mcp_step_slot returns one stable lock object per server
        # name; different servers get different locks (per-server serialization,
        # cross-server concurrency).
        engine = _make_engine()
        lock_a1 = await engine._mcp_step_slot("a")
        lock_a2 = await engine._mcp_step_slot("a")
        lock_b = await engine._mcp_step_slot("b")
        assert lock_a1 is lock_a2
        assert lock_a1 is not lock_b


class TestPoolBound:
    async def _fill_pool(self, engine: WorkflowEngine, *, locked: bool) -> list[MagicMock]:
        managers = []
        for i in range(_MCP_STEP_POOL_MAX):
            manager = MagicMock()
            manager.close = AsyncMock()
            engine._mcp_step_managers[(f"srv{i}", f"/tmp/{i}")] = manager
            engine._mcp_step_locks[f"srv{i}"] = asyncio.Lock()
            managers.append(manager)
        if locked:
            for lock in engine._mcp_step_locks.values():
                await lock.acquire()
        return managers

    @pytest.mark.asyncio
    async def test_pool_cap_evicts_oldest_idle_entry(self) -> None:
        # Requirement: when the pool is at the cap and a new key needs
        # connecting, the oldest entry whose per-server slot lock is free is
        # evicted (closed best-effort) — the pool never grows past the cap.
        engine = _make_engine({"srv": MCPServerDef(type="stdio", command="npx")})
        managers = await self._fill_pool(engine, locked=False)

        with _patch_manager_class() as manager_cls:
            manager_cls.side_effect = _manager_factory
            async with await engine._mcp_step_slot("srv"):
                fresh = await engine._get_mcp_step_manager("srv", "/tmp/new")

        # Oldest (first-inserted) entry was evicted and closed; the new key
        # took its place; every other entry is untouched.
        assert managers[0].close.await_count == 1
        for manager in managers[1:]:
            manager.close.assert_not_called()
        assert ("srv0", "/tmp/0") not in engine._mcp_step_managers
        assert engine._mcp_step_managers[("srv", "/tmp/new")] is fresh
        assert len(engine._mcp_step_managers) == _MCP_STEP_POOL_MAX

    @pytest.mark.asyncio
    async def test_pool_cap_overflow_allowed_when_every_entry_locked(self) -> None:
        # Requirement: eviction must never close a manager mid-call — when
        # every entry's per-server slot lock is held, the new connect is
        # allowed to overflow the cap instead of blocking or killing a
        # live call.
        engine = _make_engine({"srv": MCPServerDef(type="stdio", command="npx")})
        managers = await self._fill_pool(engine, locked=True)

        with _patch_manager_class() as manager_cls:
            manager_cls.side_effect = _manager_factory
            fresh = await engine._get_mcp_step_manager("srv", "/tmp/overflow")

        assert len(engine._mcp_step_managers) == _MCP_STEP_POOL_MAX + 1
        assert engine._mcp_step_managers[("srv", "/tmp/overflow")] is fresh
        for manager in managers:
            manager.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_server_many_cwds_stays_at_cap_through_slot_path(self) -> None:
        # Requirement (regression): the per-server slot lock is held across
        # _get_mcp_step_manager, so every entry of the connecting server
        # reads as "locked" to the old eviction check — one server with many
        # cwds (a for_each over templated working_dirs) bypassed the cap and
        # grew unbounded. Entries of the CURRENT server must be evictable;
        # 20 sequential slot-locked connects for one server keep the pool at
        # the cap, closing the oldest manager each time.
        engine = _make_engine({"srv": MCPServerDef(type="stdio", command="npx")})
        managers: list[MagicMock] = []

        def tracking_factory(**_kwargs: Any) -> MagicMock:
            manager = _manager_factory(**_kwargs)
            managers.append(manager)
            return manager

        total = _MCP_STEP_POOL_MAX + 4
        with _patch_manager_class() as manager_cls:
            manager_cls.side_effect = tracking_factory
            for i in range(total):
                async with await engine._mcp_step_slot("srv"):
                    await engine._get_mcp_step_manager("srv", f"/tmp/wd{i}")

        assert len(engine._mcp_step_managers) == _MCP_STEP_POOL_MAX
        # The oldest (total - cap) managers were evicted and closed, in
        # insertion order; the surviving entries are the newest ones.
        evicted = managers[: total - _MCP_STEP_POOL_MAX]
        survivors = managers[total - _MCP_STEP_POOL_MAX :]
        for manager in evicted:
            manager.close.assert_awaited_once()
        for manager in survivors:
            manager.close.assert_not_called()
        assert set(engine._mcp_step_managers) == {
            ("srv", f"/tmp/wd{i}") for i in range(total - _MCP_STEP_POOL_MAX, total)
        }

    @pytest.mark.asyncio
    async def test_concurrent_first_connects_reserve_capacity(self) -> None:
        # Requirement: admission reserves a slot for an in-flight connect —
        # with the pool one below the cap, two concurrent first-time
        # connects to DISTINCT servers must not both pass a size-only check
        # (which would leave cap+1 managers cached and nothing evicted); the
        # second evicts an idle entry and the pool finishes AT the cap.
        engine = _make_engine(
            {
                "srvA": MCPServerDef(type="stdio", command="npx"),
                "srvB": MCPServerDef(type="stdio", command="npx"),
            }
        )
        prefilled: list[MagicMock] = []
        for i in range(_MCP_STEP_POOL_MAX - 1):
            manager = MagicMock()
            manager.close = AsyncMock()
            engine._mcp_step_managers[(f"old{i}", f"/tmp/{i}")] = manager
            engine._mcp_step_locks[f"old{i}"] = asyncio.Lock()
            prefilled.append(manager)

        started = 0
        both_started = asyncio.Event()

        async def gated_connect(**_kwargs: Any) -> list[dict[str, Any]]:
            # Both connects must be genuinely in flight before either
            # returns, or the test proves nothing about the admission race.
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
            return []

        def factory(**_kwargs: Any) -> MagicMock:
            manager = _manager_factory()
            manager.connect_server = AsyncMock(side_effect=gated_connect)
            return manager

        async def get(server: str) -> Any:
            async with await engine._mcp_step_slot(server):
                return await engine._get_mcp_step_manager(server, "/tmp/new")

        with _patch_manager_class() as manager_cls:
            manager_cls.side_effect = factory
            first, second = await asyncio.gather(get("srvA"), get("srvB"))

        assert first is not second
        # One prefilled entry was evicted to make room; the pool ends exactly
        # at the cap (cap-1 prefilled, one evicted, two new = cap).
        assert sum(m.close.await_count for m in prefilled) == 1
        assert len(engine._mcp_step_managers) == _MCP_STEP_POOL_MAX
        assert engine._mcp_step_pending == 0

    @pytest.mark.asyncio
    async def test_eviction_close_reraises_cancellation(self) -> None:
        # Requirement: MCPManager.close() deliberately absorbs CancelledError
        # while draining connection teardown — awaited directly during
        # eviction, that would swallow the workflow's cancellation and let a
        # NEW tool call start after it. The eviction close runs shielded, the
        # close still completes, and CancelledError is re-raised BEFORE any
        # new connect happens.
        engine = _make_engine({"srv": MCPServerDef(type="stdio", command="npx")})
        managers = await self._fill_pool(engine, locked=False)

        close_gate = asyncio.Event()

        async def blocking_close() -> None:
            await asyncio.wait_for(close_gate.wait(), timeout=5)

        managers[0].close = AsyncMock(side_effect=blocking_close)

        with _patch_manager_class() as manager_cls:
            manager_cls.side_effect = _manager_factory

            async def connect_and_get() -> Any:
                async with await engine._mcp_step_slot("srv"):
                    return await engine._get_mcp_step_manager("srv", "/tmp/new")

            task = asyncio.create_task(connect_and_get())
            await asyncio.sleep(0.05)  # let it reach the eviction close
            task.cancel()
            await asyncio.sleep(0.05)
            # The close is still draining: cancellation was absorbed by the
            # manager's close, not lost — the eviction has not returned yet.
            assert not task.done()
            close_gate.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        # The evicted manager finished closing; the new connect never ran.
        managers[0].close.assert_awaited_once()
        assert ("srv", "/tmp/new") not in engine._mcp_step_managers
        assert engine._mcp_step_pending == 0

    @pytest.mark.asyncio
    async def test_close_clears_locks_even_when_pool_is_empty(self) -> None:
        # Requirement: cleanup clears BOTH dicts unconditionally — a failed
        # connect leaves slot locks behind without pooling any manager, and
        # the old early return on an empty pool would have leaked them.
        engine = _make_engine()
        engine._mcp_step_locks = {"srv": asyncio.Lock()}

        await engine._close_mcp_step_managers()

        assert engine._mcp_step_managers == {}
        assert engine._mcp_step_locks == {}


class TestCleanup:
    @pytest.mark.asyncio
    async def test_close_clears_pool_and_swallows_failures(self) -> None:
        # Requirement: cleanup closes every pooled manager best-effort (one
        # failing close does not prevent the others) and clears the pool.
        engine = _make_engine()
        good = MagicMock()
        good.close = AsyncMock()
        bad = MagicMock()
        bad.close = AsyncMock(side_effect=RuntimeError("close failed"))
        engine._mcp_step_managers = {("good", "/tmp"): good, ("bad", "/tmp"): bad}
        engine._mcp_step_locks = {"good": asyncio.Lock()}

        await engine._close_mcp_step_managers()

        good.close.assert_awaited_once()
        bad.close.assert_awaited_once()
        assert engine._mcp_step_managers == {}
        assert engine._mcp_step_locks == {}

    @pytest.mark.asyncio
    async def test_run_finally_closes_pooled_managers(self) -> None:
        # Requirement: run()'s finally block shuts the pool down even when the
        # loop body raised — a run that fails mid-step must not leak the
        # server process.
        engine = _make_engine()
        manager = MagicMock()
        manager.close = AsyncMock()
        engine._mcp_step_managers = {("srv", "/tmp"): manager}

        async def failing_loop(_entry: str) -> dict[str, Any]:
            raise ExecutionError("boom")

        engine._execute_loop = failing_loop  # type: ignore[method-assign]
        with pytest.raises(ExecutionError, match="boom"):
            await engine.run({})

        manager.close.assert_awaited_once()
        assert engine._mcp_step_managers == {}

    @pytest.mark.asyncio
    async def test_resume_finally_closes_pooled_managers(self) -> None:
        # Requirement: resume()'s finally block performs the same cleanup —
        # run/resume parity for the pool lifecycle.
        engine = _make_engine()
        manager = MagicMock()
        manager.close = AsyncMock()
        engine._mcp_step_managers = {("srv", "/tmp"): manager}

        async def ok_loop(_entry: str) -> dict[str, Any]:
            return {}

        engine._execute_loop = ok_loop  # type: ignore[method-assign]
        await engine.resume("start")

        manager.close.assert_awaited_once()
        assert engine._mcp_step_managers == {}
