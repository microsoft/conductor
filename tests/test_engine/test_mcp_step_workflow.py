"""Integration tests for `type: mcp` steps in WorkflowEngine.

Covers:
- A workflow of ONLY mcp steps completes without the LLM provider ever
  being called
- The result envelope round-trips through ``context.to_dict()`` JSON
  serialization (checkpoint parity)
- set -> mcp -> route on ``output.is_error`` works for both outcomes
- ``mcp_started`` / ``mcp_completed`` / ``mcp_failed`` events carry no
  argument or result values
- An ``output:`` schema mismatch fails the workflow via a single redacted
  ``mcp_failed``
- An unknown server fails at runtime naming the available servers (the
  static validator is never called by ``conductor run``)
- A templated runtime ``working_dir`` rendering to a nonexistent directory
  fails redacted (the rendered path and template stay in the debug log only)
- A value-bearing ``BaseException`` (e.g. ``SystemExit``) from the manager
  wraps into the generic redacted failure; ``CancelledError`` re-raises
  untouched with no ``mcp_failed``
- With ``workflow.context.mode: explicit``, mcp ``arguments`` can reference
  ``workflow.input.*`` (always available to local-render step types) and
  prior-step outputs declared via ``input:``

All MCP interaction is mocked — no real MCP servers are spawned. The mock
pattern mirrors ``tests/test_engine/test_mcp_step_pool.py``: ``MCPManager``
is patched where the engine lazily imports it, and the real
``McpStepExecutor`` runs against the mock manager.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import (
    ContextConfig,
    LimitsConfig,
    MCPServerDef,
    MCPStepDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    SetStepDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.context import WorkflowContext
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ExecutionError, InterruptError
from tests.test_engine.mcp_placement import owned_opening, placement_config, placement_engine


@pytest.fixture
def realm(tmp_path, monkeypatch):
    from tests.test_mcp.fixtures.docker_realm import create_realm

    return create_realm(tmp_path, monkeypatch)


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("auto_removed", [False, True])
async def test_mcp_only_initialize_failure_retains_history_and_refuses_restage(
    realm, monkeypatch, tmp_path, child, auto_removed
):
    # Requirement: initialize mutations survive failed MCP-only startup and marker-loss resume.
    from dataclasses import replace

    from conductor.config.environment import EnvironmentDocument, ResolvedEnvironment
    from conductor.engine.checkpoint import CheckpointManager
    from conductor.engine.workflow import RunContext
    from conductor.execution.docker import DockerRunnerBackend
    from conductor.execution.errors import WorkspaceAttachError
    from tests.test_execution.test_workspace_retention import restore
    from tests.test_mcp.fixtures.docker_stdio import LaunchReceipt, PeerReceipt

    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"original bundle bytes\n")
    config = WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "mcp-only",
                "entry_point": "files",
                "bundle": {"assets": ["payload"]},
                "workspace": {"persistence": "on-failure"},
                "runtime": {
                    "mcp_servers": {
                        "srv": {
                            "command": "peer",
                            "env": {"PEER_FAILURE": "startup", "PEER_INITIALIZE_FILE": "payload"},
                            "secrets": [
                                {"ref": "private", "scope": "mcp", "delivery": {"env": "PRIVATE"}}
                            ],
                        }
                    }
                },
            },
            "agents": [],
            "for_each": [
                {
                    "name": "files",
                    "type": "for_each",
                    "source": "workflow.input.items",
                    "as": "item",
                    "agent": {"name": "call", "type": "mcp", "server": "srv", "tool": "receive"},
                    "routes": [{"to": "$end"}],
                }
            ],
        }
    )
    consumer = "for_each.files.agent"
    if child:
        config.workflow.workspace = None
        (source / "child.yaml").write_text(config.model_dump_json(by_alias=True))
        config = WorkflowConfig.model_validate(
            {
                "workflow": {
                    "name": "parent",
                    "entry_point": "child",
                    "workspace": {"persistence": "on-failure"},
                },
                "agents": [
                    {
                        "name": "child",
                        "type": "workflow",
                        "workflow": "child.yaml",
                        "execution": {"profile": "local"},
                        "routes": [{"to": "$end"}],
                    }
                ],
            }
        )
        consumer = "main/child.yaml::for_each.files.agent"
    path = source / "workflow.yaml"
    path.write_text(config.model_dump_json(by_alias=True))
    environment = ResolvedEnvironment(
        document=EnvironmentDocument.model_validate(
            {
                "default": "docker",
                "profiles": {
                    "docker": {"backend": "docker", "docker": {"image": "peer:image"}},
                    "local": {"backend": "local"},
                },
                "secrets": {"private": {"source": {"env": "TERMINAL_PRIVATE"}, "allow": ["mcp"]}},
            }
        ),
        name="retention",
        source="path",
        path=None,
        digest="sha256:retention",
    )
    workspace_root = str(tmp_path / "workspaces")
    secret = "terminal-private-value-8743"
    monkeypatch.setenv("TERMINAL_PRIVATE", secret)
    monkeypatch.setenv("FAKE_DOCKER_WORKSPACE_ROOT", workspace_root)
    realm.backend._cli_env["FAKE_DOCKER_WORKSPACE_ROOT"] = workspace_root
    engine = WorkflowEngine(
        config,
        workflow_path=path,
        execution_environment=environment,
        run_context=RunContext(run_id="mcp-only"),
    )
    engine._execution_session.backends["docker"] = realm.backend
    cache = engine._execution_session.secret_cache
    redactor = engine._execution_session.redactor
    events = _collect_events(engine)
    if auto_removed:
        # Requirement: --rm startup exit before identity proof seals cleanup without losing failure.
        allocate = realm.backend.open_mcp_stdio

        async def allocate_auto_removed(spec, lease):
            target = await allocate(spec, lease)

            async def close():
                launch = LaunchReceipt.model_validate_json(
                    (tmp_path / f"{target.identity}.launcher.json").read_text()
                )
                assert launch.joined and launch.exit_code == 23
                state_path = tmp_path / "state.json"
                state = json.loads(state_path.read_text())
                state["containers"].pop(target.identity)
                state_path.write_text(json.dumps(state))
                await target.aclose()

            return replace(target, aclose=close)

        monkeypatch.setattr(realm.backend, "open_mcp_stdio", allocate_auto_removed)
    (tmp_path / "checkpoints").mkdir()
    monkeypatch.setattr(CheckpointManager, "get_checkpoints_dir", lambda: tmp_path / "checkpoints")

    with pytest.raises(ExecutionError):
        await engine.run({"items": ["one"], "private": secret})

    assert engine._last_checkpoint_path is not None
    checkpoint = CheckpointManager.load_checkpoint(engine._last_checkpoint_path)
    # Requirement: the original secret cache survives until deferred terminal serialization.
    assert secret not in engine._last_checkpoint_path.read_text()
    root_events = [event.type for event in events if not event.data.get("subworkflow_path")]
    assert root_events.count("workflow_failed") == 1
    assert root_events.count("checkpoint_saved") == 1
    assert "workflow_completed" not in root_events
    assert checkpoint.workspace is not None
    assert checkpoint.workspace["executed_backends"] == ["docker"]
    assert engine._execution_session._file_consumers == {"docker": {consumer}}
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text())
    volume = "conductor-ws-mcp-only"
    assert engine._execution_session.cleanup_uncertain is auto_removed
    assert engine._execution_session.sealed
    assert engine._execution_session.secret_cache is cache
    assert engine._execution_session.redactor is redactor
    assert redactor.active is auto_removed
    assert bool(cache._cache) is auto_removed
    assert bool(state["containers"]) is auto_removed
    assert volume in state["volumes"]
    file = Path(workspace_root) / volume / "payload"
    assert file.read_bytes() == b"initialize-mutated\n"
    assert (source / "payload").read_bytes() == b"original bundle bytes\n"
    launches = list(tmp_path.glob("conductor-mcp-*.launcher.json"))
    assert len(launches) == 1
    launch = LaunchReceipt.model_validate_json(launches[0].read_text())
    peer = PeerReceipt.model_validate_json(
        launches[0].with_name(launches[0].name.replace(".launcher.json", ".json")).read_text()
    )
    assert launch.joined and peer.closed and not peer.called
    assert not Path(launch.env_file).exists()
    state["markers"].pop(volume)
    state_path.write_text(json.dumps(state))
    before = len((tmp_path / "docker.jsonl").read_text().splitlines())
    home = tmp_path / "other-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONDUCTOR_HOME", str(home / ".conductor"))
    resumed = WorkflowEngine(
        config,
        workflow_path=path,
        execution_environment=environment,
        run_context=RunContext(run_id="mcp-only"),
    )
    resumed._execution_session.backends["docker"] = DockerRunnerBackend("docker")
    restore(resumed, checkpoint)

    with pytest.raises(WorkspaceAttachError if auto_removed else ExecutionError):
        await resumed.resume(checkpoint.current_agent, checkpoint=checkpoint)

    later = [
        json.loads(line)["argv"]
        for line in (tmp_path / "docker.jsonl").read_text().splitlines()[before:]
    ]
    assert not any(row[0] == "run" or row[:2] == ["cp", "-a"] for row in later)
    assert file.read_bytes() == b"initialize-mutated\n"
    final = json.loads(state_path.read_text())
    assert final["volumes"] == state["volumes"]
    assert final["containers"] == state["containers"]
    print(
        {
            "case": "mcp-only-initialize",
            "workspace": checkpoint.workspace,
            "consumers": engine._execution_session._file_consumers,
            "bytes": file.read_bytes().decode(),
            "sdk_joined": launch.joined,
            "peer_pid": peer.pid,
            "launcher_pid": launch.pid,
            "resume_commands": later,
        }
    )


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_docker_root_terminal_serializes_before_clearing_original_cache(
    realm, monkeypatch, tmp_path, resumed, failed
):
    # Requirement: run/resume publish once after cleanup with the original secrets, then clear them.
    from conductor.config.environment import EnvironmentDocument, ResolvedEnvironment
    from conductor.engine.checkpoint import CheckpointManager
    from conductor.providers.registry import ProviderRegistry
    from conductor.redaction import REDACTED_MARKER

    secret = "terminal-publication-private-8217"
    monkeypatch.setenv("TERMINAL_PRIVATE", secret)
    config = WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "terminal-owner",
                "entry_point": "call",
                "runtime": {
                    "mcp_servers": {
                        "srv": {
                            "command": "peer",
                            "env": {"PEER_FAILURE": "startup" if failed else "none"},
                            "secrets": [
                                {"ref": "private", "scope": "mcp", "delivery": {"env": "PRIVATE"}}
                            ],
                        }
                    }
                },
            },
            "agents": [
                {
                    "name": "call",
                    "type": "mcp",
                    "server": "srv",
                    "tool": "receive",
                    "routes": [{"to": "$end"}],
                }
            ],
            "output": {"private": "{{ workflow.input.private }}"},
        }
    )
    source = tmp_path / "source"
    source.mkdir()
    path = source / "workflow.yaml"
    path.write_text(config.model_dump_json(by_alias=True))
    environment = ResolvedEnvironment(
        document=EnvironmentDocument.model_validate(
            {
                "default": "docker",
                "profiles": {"docker": {"backend": "docker", "docker": {"image": "peer:image"}}},
                "secrets": {"private": {"source": {"env": "TERMINAL_PRIVATE"}, "allow": ["mcp"]}},
            }
        ),
        name="terminal",
        source="path",
        path=None,
        digest="sha256:terminal",
    )
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()
    monkeypatch.setattr(CheckpointManager, "get_checkpoints_dir", lambda: checkpoints)
    async with ProviderRegistry(config) as registry:
        engine = WorkflowEngine(
            config, registry=registry, workflow_path=path, execution_environment=environment
        )
        session = engine._execution_session
        session.backends["docker"] = realm.backend
        cache, redactor = session.secret_cache, session.redactor
        events = _collect_events(engine)
        publication = []

        def observe(event):
            if event.type in ("workflow_completed", "workflow_failed"):
                publication.append(
                    (
                        event.type,
                        session.sealed,
                        session._cleanup_task is not None and session._cleanup_task.done(),
                        redactor.active,
                        bool(cache._cache),
                    )
                )

        assert engine._event_emitter is not None
        engine._event_emitter.subscribe(observe)
        inputs = {"private": secret}

        async def execute():
            if resumed:
                engine.context.set_workflow_inputs(inputs)
                return await engine.resume("call")
            return await engine.run(inputs)

        if failed:
            with pytest.raises(ExecutionError):
                await execute()
            assert engine._last_checkpoint_path is not None
            assert secret not in engine._last_checkpoint_path.read_text()
        else:
            assert await execute() == {"private": secret}
            completed = [event for event in events if event.type == "workflow_completed"]
            assert len(completed) == 1
            assert completed[0].data["output"] == {"private": REDACTED_MARKER}

        terminal = "workflow_failed" if failed else "workflow_completed"
        assert publication == [(terminal, True, True, True, True)]
        assert [event.type for event in events].count("checkpoint_saved") == int(failed)
        assert session.secret_cache is cache and session.redactor is redactor
        assert not session.managed and not session.cleanup_uncertain
        assert not redactor.active and cache._cache == {}


async def test_standalone_docker_mcp_finalization_clears_its_own_redactor(realm):
    # Requirement: an active MCP lifecycle without an engine root still owns its secret disposal.
    config = placement_config()
    async with placement_engine(config, realm) as engine:
        session = engine._execution_session
        redactor = session.redactor
        redactor.register(["standalone-private-value-9731"])
        await engine._run_mcp_step(config.agents[0], {})
        assert session.root_task is None and session.mcp_lifecycle.active

        await session.finalize_leases("succeeded")

        assert session.redactor is redactor and not redactor.active


@pytest.mark.parametrize("expired", [False, True])
async def test_mixed_cancel_early_invoke_orders_actual_sdk_cleanup(
    realm, monkeypatch, expired, request
):
    # Requirement: invoke cancellation itself orders SDK/target/finalize before engine finally.
    import sys
    from dataclasses import replace
    from pathlib import Path

    from conductor.engine.mcp_entry import McpStepEntry
    from conductor.engine.workspace_claim import acquire_workspace_claim
    from conductor.execution.local import LocalRunnerBackend
    from conductor.execution.local_process_scope import LocalProcessScopeController
    from conductor.execution.types import CommandSpec

    config = placement_config()
    async with placement_engine(config, realm) as engine:
        session = engine._execution_session
        local = session.backends["local"]
        assert isinstance(local, LocalRunnerBackend)
        monkeypatch.setenv("CONDUCTOR_HOME", str(realm.root / "home"))
        claim = acquire_workspace_claim(realm.run.run_id)
        request.addfinalizer(claim.release)
        session._claim_active = True
        session._local_consumer = True
        session._root_run_spec = replace(realm.run, workspace_persistence="durable")
        await session.ensure_backends(["local"])
        local_lease = session.leases["local"]
        binding = session._managed_binding
        assert binding is not None
        local_result = await session.invoke(
            local.run_command(
                CommandSpec(
                    sys.executable,
                    (
                        "-c",
                        "import os; from pathlib import Path; "
                        "Path('local-ready').write_text('ready'); print(os.getpid())",
                    ),
                    working_dir=binding.work_root,
                ),
                local_lease,
                on_dispatch=lambda: session.record_backend_execution("local", step="local-ready"),
            )
        )
        assert local_result.exit_code == 0
        local_pid = int(local_result.stdout)
        assert (Path(binding.work_root) / "local-ready").read_text() == "ready"
        await engine._run_mcp_step(config.agents[0], {})
        entry = next(iter(engine._mcp_step_managers.values()))
        assert isinstance(entry, McpStepEntry)
        manager_close = entry.manager.close
        target = entry.target
        assert target is not None
        original_finalize = realm.backend.finalize_run
        started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []

        async def close_manager(*, strict=False):
            assert strict
            closing.set()
            await release.wait()
            await manager_close(strict=True)
            order.append("manager")

        async def close_target():
            order.append("target")
            await target.aclose()

        async def finalize(lease, outcome, *, retain=False):
            assert order == ["manager", "target"]
            order.append("finalize")
            await original_finalize(lease, outcome, retain=retain)

        async def active_call():
            await entry.manager.call_tool_structured("srv", "receive", {})
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(entry.manager, "close", close_manager)
        entry.target = replace(target, aclose=close_target)
        monkeypatch.setattr(realm.backend, "finalize_run", finalize)
        invocation = asyncio.create_task(session.invoke(active_call(), mcp=True))
        session.root_task = invocation
        try:
            await asyncio.wait_for(started.wait(), 5)
            invocation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await invocation
            assert session.sealed
            deadline = session.cleanup_deadline
            await asyncio.wait_for(closing.wait(), 5)
            if expired:
                loop = asyncio.get_running_loop()
                clock = loop.time
                monkeypatch.setattr(loop, "time", lambda: clock() + 31)
                await session.finalize_leases("cancelled")
                assert session.cleanup_uncertain
                assert "finalize" not in order
            else:
                joining_started = asyncio.Event()

                async def join_root():
                    joining_started.set()
                    await session.finalize_leases("cancelled")

                joining = asyncio.create_task(join_root())
                await joining_started.wait()
                joining.cancel()
                asyncio.get_running_loop().call_soon(joining.cancel)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await joining
            release.set()
            await session.finalize_leases("cancelled")
            assert entry._cleanup is not None
            await asyncio.wait_for(asyncio.shield(entry._cleanup), 5)
            assert session.cleanup_deadline == deadline
            assert order == (
                ["target", "manager"] if expired else ["manager", "target", "finalize"]
            )
            print(
                {
                    "scenario": "mixed-early-invoke",
                    "expired": expired,
                    "deadline": deadline,
                    "order": order,
                    "entry": id(entry),
                }
            )
        finally:
            release.set()
            controller = binding.runtime_context.process_scopes
            assert isinstance(controller, LocalProcessScopeController)
            await controller.close()
            pending = tuple(controller._tasks)
            if pending:
                done, unfinished = await asyncio.wait(pending, timeout=10)
                assert not unfinished
                for owner in done:
                    if not owner.cancelled():
                        owner.result()
            controller._accepting.join(timeout=5)
            assert not controller._accepting.is_alive()
            assert not controller.owners.tasks and not controller._connections
            print(
                {
                    "scenario": "mixed-native-custody",
                    "local_pid": local_pid,
                    "closed": controller._closed,
                    "owners": len(controller.owners.tasks),
                }
            )
            monkeypatch.setattr(realm.backend, "finalize_run", original_finalize)


async def test_docker_only_multiple_entries_share_budget_and_new_root_gets_fresh_budget(
    realm, monkeypatch
):
    # Requirement: two strict owners consume one deadline; only a new root gets another budget.
    from dataclasses import replace

    from conductor.config.environment import builtin_local_environment
    from conductor.engine.execution_resolution import ExecutionResolverSession
    from conductor.execution.docker import DockerRunnerBackend

    config = placement_config(cwd="{{ cwd }}")
    async with placement_engine(config, realm) as engine:
        for cwd in ("one", "two"):
            await engine._run_mcp_step(config.agents[0], {"cwd": cwd})
        session = engine._execution_session
        entries = session.mcp_lifecycle.attempts[0].entries
        assert len(entries) == 2
        release, all_started = asyncio.Event(), asyncio.Event()
        closes = []

        for entry in entries:
            close = entry.manager.close

            async def hold(*, strict=False, original=close, owner=entry):
                closes.append(owner)
                if len(closes) == 2:
                    all_started.set()
                await release.wait()
                await original(strict=strict)

            monkeypatch.setattr(entry.manager, "close", hold)
        session.begin_teardown("cancelled", retain=True)
        deadline = session.cleanup_deadline
        await asyncio.wait_for(all_started.wait(), 5)
        loop = asyncio.get_running_loop()
        clock = loop.time
        monkeypatch.setattr(loop, "time", lambda: clock() + 31)
        await session.finalize_leases("cancelled")
        assert session.cleanup_uncertain
        assert {entry.deadline for entry in entries} == {deadline}
        release.set()
        for entry in entries:
            assert entry._cleanup is not None
            await asyncio.wait_for(asyncio.shield(entry._cleanup), 5)
        await session.finalize_leases("cancelled")
        assert session.cleanup_deadline == deadline and session.quarantined
        fresh = ExecutionResolverSession(builtin_local_environment())
        fresh_backend = DockerRunnerBackend(realm.backend._docker_binary)
        fresh_backend._cli_env["FAKE_DOCKER_STATE"] = str(realm.root / "fresh-state.json")
        fresh.backends["docker"] = fresh_backend
        await fresh.prepare_leases(replace(realm.run, run_id="fresh-root"))
        await fresh.finalize_leases("succeeded")
        assert fresh.cleanup_deadline is not None and deadline is not None
        assert fresh.cleanup_deadline > deadline and not fresh.quarantined
        print(
            {
                "scenario": "docker-only-multiple",
                "deadline": deadline,
                "new_root_deadline": fresh.cleanup_deadline,
                "entries": [id(e) for e in entries],
            }
        )


async def test_delayed_startup_retains_actual_sdk_owner_after_root_deadline(
    realm, monkeypatch, caplog
):
    # Requirement: terminal strict-close observation cannot conceal an SDK owner still starting.
    from contextlib import asynccontextmanager
    from dataclasses import replace

    import mcp.client.stdio as sdk

    import conductor.mcp.manager as module

    entered, release = asyncio.Event(), asyncio.Event()
    original_stdio = module.stdio_client
    original_spawn = sdk._create_platform_compatible_process
    processes, sinks, order = [], [], []

    async def record_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    @asynccontextmanager
    async def held_stdio(params, *, errlog):
        sinks.append(errlog)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Model a platform startup owner that has accepted cancellation
            # but cannot confirm terminal process allocation until released.
            await release.wait()
        async with original_stdio(params, errlog=errlog) as transport:
            yield transport

    monkeypatch.setattr(module, "stdio_client", held_stdio)
    monkeypatch.setattr(sdk, "_create_platform_compatible_process", record_spawn)
    async with placement_engine(placement_config(), realm) as engine:
        session = engine._execution_session
        # Complete prerequisite staging before timing the held SDK startup boundary.
        execution = engine._execution_resolver.manifest.profiles["call"].execution
        assert execution is not None
        await realm.backend._ensure_staged(session.leases["docker"], execution, None)
        async with owned_opening(
            engine._get_mcp_step_manager("srv", None, step_key="call"), release
        ) as opening:
            await asyncio.wait_for(entered.wait(), 5)
            entry = session.mcp_lifecycle.attempts[0].entries[0]
            target = entry.target
            assert target is not None
            target_closed = asyncio.Event()

            async def close_target():
                order.append("target")
                try:
                    await target.aclose()
                finally:
                    target_closed.set()

            entry.target = replace(target, aclose=close_target)
            session.begin_teardown("cancelled", retain=True)
            deadline = session.cleanup_deadline
            loop = asyncio.get_running_loop()
            clock = loop.time
            monkeypatch.setattr(loop, "time", lambda: clock() + 31)
            await session.finalize_leases("cancelled")
            await asyncio.wait_for(target_closed.wait(), 5)
            assert session.cleanup_uncertain and entry.cleanup_unconfirmed
            assert processes == [] and engine._mcp_step_managers == {}
            assert "workspace and fence state requires independent verification" in caplog.text
            assert "workspace and fence retained" not in caplog.text
            release.set()
            try:
                await asyncio.wait_for(opening, 10)
            except (RuntimeError, asyncio.CancelledError):
                # The actual SDK reports EOF after the late fake CLI discovers
                # that its already-released payload file is unavailable.
                order.append("startup-terminal")
            owners = tuple(entry.manager._owners)
            done, pending = await asyncio.wait(owners, timeout=10)
            assert done and not pending
            for owner in done:
                if not owner.cancelled():
                    owner.exception()
            assert entry._cleanup is not None
            await asyncio.wait_for(asyncio.shield(entry._cleanup), 5)
            assert processes and all(process.returncode is not None for process in processes)
            assert all(sink.closed for sink in sinks)
            assert session.cleanup_deadline == deadline and session.quarantined
            assert engine._mcp_step_managers == {}
            state = json.loads((realm.root / "state.json").read_text())
            assert state["volumes"] and state["containers"]
            print(
                {
                    "scenario": "delayed-startup",
                    "deadline": deadline,
                    "sdk_pids": [process.pid for process in processes],
                    "returncodes": [process.returncode for process in processes],
                    "order": order,
                }
            )


@pytest.mark.parametrize("failure", ["transport", "malformed", "protocol", "output", "is_error"])
async def test_docker_transport_only_invalidates_failed_tool_exchange(realm, monkeypatch, failure):
    # Requirement: only a wire transport failure evicts; later explicit calls never reuse it.
    from types import SimpleNamespace

    from mcp import McpError
    from mcp.types import ErrorData

    config = placement_config(failure="transport" if failure == "transport" else "none")
    async with placement_engine(config, realm) as engine:
        manager = await engine._get_mcp_step_manager("srv", None, step_key="call")
        original = manager.sessions["srv"].call_tool
        calls = []

        async def exchange(*args, **kwargs):
            calls.append("wire")
            result = await original(*args, **kwargs)
            if failure == "malformed":
                return SimpleNamespace(
                    content=result.content, structured_content=[], is_error=False
                )
            if failure == "protocol":
                raise McpError(ErrorData(code=-32602, message="protocol-canary"))
            if failure == "is_error":
                return result.model_copy(update={"isError": True})
            return result

        monkeypatch.setattr(manager.sessions["srv"], "call_tool", exchange)
        step = config.agents[0]
        if failure == "output":
            from conductor.config.schema import OutputField

            step = step.model_copy(update={"output": {"missing": OutputField(type="number")}})
        if failure == "is_error":
            result = await engine._run_mcp_step(step, {})
            assert result["is_error"]
        else:
            with pytest.raises(ExecutionError):
                await engine._run_mcp_step(step, {})
        assert calls == ["wire"]
        if failure == "transport":
            assert engine._mcp_step_managers == {}
            config.workflow.runtime.mcp_servers["srv"].env["PEER_FAILURE"] = "none"
        else:
            assert next(iter(engine._mcp_step_managers.values())).manager is manager
            monkeypatch.setattr(manager.sessions["srv"], "call_tool", original)
        result = await engine._run_mcp_step(config.agents[1], {})
        assert result["is_error"] is False
        current = next(iter(engine._mcp_step_managers.values())).manager
        assert (current is manager) is (failure != "transport")
        assert len(list(realm.root.glob("*.launcher.json"))) == (2 if failure == "transport" else 1)
        print(
            {
                "scenario": "transport-classification",
                "failure": failure,
                "reused": current is manager,
                "attempts": len(calls),
            }
        )


async def test_docker_startup_failure_preserves_qualified_dispatch(realm) -> None:
    # Requirement: initialize can mutate files, so failed startup retains qualified Docker history.
    config = placement_config(failure="startup")
    config.for_each[0].max_concurrent = 1
    async with placement_engine(config, realm) as engine:
        with pytest.raises(ExecutionError):
            await engine._execute_for_each_group(config.for_each[0])
        assert engine._execution_session.executed_backends() == {"docker"}
        assert engine._execution_session._file_consumers["docker"] == {"for_each.first.agent"}
        assert engine._mcp_step_managers == {}
        assert engine._mcp_step_pending == 0
        assert len(list(realm.root.glob("*.launcher.json"))) == 1


async def test_docker_validation_before_publication_and_manager_target_cleanup(realm, monkeypatch):
    # Requirement: validation gates publication/tools; failure closes SDK before the target.
    from dataclasses import replace

    from conductor.execution.errors import ExecutionSpecError

    config = placement_config()
    async with placement_engine(config, realm) as engine:
        allocate = realm.backend.open_mcp_stdio
        order = []

        async def watched_allocate(spec, lease):
            target = await allocate(spec, lease)

            def mark():
                assert engine._execution_session._file_consumers["docker"] == {"call"}
                order.append("mark")
                target.mark_launch_attempted()

            async def validate():
                assert engine._mcp_step_managers == {}
                assert order == ["mark"]
                order.append("validate")
                await target.validate_started()
                raise ExecutionSpecError("fixture refuses post-spawn validation")

            async def close():
                launch = json.loads((realm.root / f"{target.identity}.launcher.json").read_text())
                peer = json.loads((realm.root / f"{target.identity}.json").read_text())
                assert launch["joined"] and peer["closed"] and not peer["called"]
                order.append("target-close-after-sdk")
                await target.aclose()

            return replace(
                target, mark_launch_attempted=mark, validate_started=validate, aclose=close
            )

        monkeypatch.setattr(realm.backend, "open_mcp_stdio", watched_allocate)
        with pytest.raises(ExecutionError):
            await engine._run_mcp_step(config.agents[0], {})
        assert order == ["mark", "validate", "target-close-after-sdk"]
        assert engine._mcp_step_managers == {} and engine._mcp_step_pending == 0


async def test_docker_child_namespace_dispatch_and_reuse(realm) -> None:
    # Requirement: child placement resolves locally; dispatch history keeps its bundle namespace.
    from dataclasses import replace

    config = placement_config()
    async with placement_engine(config, realm) as engine:
        assert realm.run.bundle is not None
        engine._execution_session._root_run_spec = replace(
            realm.run,
            bundle=replace(realm.run.bundle, source_roots=((str(realm.root), "main"),)),
        )
        child_config = placement_config()
        for step in child_config.agents:
            step.execution = child_config.for_each[1].agent.execution
        child = WorkflowEngine(
            child_config,
            workflow_path=realm.root / "child.yaml",
            _execution_session=engine._execution_session,
            _subworkflow_depth=1,
        )
        try:
            await child._run_mcp_step(child_config.agents[0], {})
            await child._run_mcp_step(child_config.agents[1], {})
        finally:
            await child._close_mcp_step_managers()
        assert engine._execution_session._file_consumers["docker"] == {
            "main/child.yaml::call",
            "main/child.yaml::again",
        }
        assert len(list(realm.root.glob("*.launcher.json"))) == 1
        launcher = next(realm.root.glob("*.launcher.json"))
        assert "--network" in json.loads(launcher.read_text())["argv"]


async def test_docker_cleanup_diagnostic_is_value_free(realm, monkeypatch, caplog) -> None:
    # Requirement: Docker cleanup warnings never publish exception text or rendered cwd values.
    canary = "cleanup-private-canary"
    config = placement_config(cwd=canary)
    async with placement_engine(config, realm) as engine:
        await engine._run_mcp_step(config.agents[0], {})
        entry = next(iter(engine._mcp_step_managers.values()))
        close = entry.manager.close

        async def failed_close(*, strict=False):
            await close(strict=strict)
            raise RuntimeError(canary)

        monkeypatch.setattr(entry.manager, "close", failed_close)
        await engine._close_mcp_step_managers()
        assert "Docker MCP cleanup is unconfirmed" in caplog.text
        assert canary not in caplog.text


def test_docker_diagnostic_write_failure_keeps_suppression_notice(tmp_path, monkeypatch, caplog):
    # Requirement: a failed private metadata write exposes no OS error path or exception values.
    canary = "diagnostic-private-canary"
    destination = tmp_path / canary
    destination.mkdir()
    engine = _make_engine(_mcp_workflow())
    monkeypatch.setattr(engine, "_mcp_diagnostic_path", lambda: destination)
    result = engine._write_mcp_diagnostic(
        agent_name="call",
        server="srv",
        tool="receive",
        exc=RuntimeError(canary),
        metadata_only=True,
    )
    assert result is None
    assert "stderr suppressed" in engine._diagnostic_suffix(result, metadata_only=True)
    assert canary not in caplog.text


_SECRET_ARG = "s3cr3t-token-value"
_SECRET_RESULT = "classified-result-body"


def _make_engine(config: WorkflowConfig) -> WorkflowEngine:
    return WorkflowEngine(config, MagicMock())


def _collect_events(engine: WorkflowEngine) -> list[WorkflowEvent]:
    emitter = WorkflowEventEmitter()
    received: list[WorkflowEvent] = []
    emitter.subscribe(received.append)
    engine._event_emitter = emitter
    return received


def _envelope(is_error: bool = False) -> dict[str, Any]:
    """A raw manager-shaped envelope (before the executor's structured merge)."""
    return {
        "content": [
            {
                "type": "text",
                "text": _SECRET_RESULT,
                "truncated": False,
            }
        ],
        "structured": {"answer": 42},
        "is_error": is_error,
    }


def _patch_manager(envelope: Any = None) -> Any:
    """Patch MCPManager where the engine lazily imports it.

    The fake manager advertises one tool ``echo``. ``envelope`` is either a
    dict returned from every ``call_tool_structured`` or a callable used as
    the AsyncMock side_effect (for failure paths).
    """
    patcher = patch("conductor.mcp.manager.MCPManager")
    manager_cls = patcher.start()
    manager = manager_cls.return_value
    manager.connect_server = AsyncMock(return_value=[])
    manager.close = AsyncMock(return_value=None)
    manager.get_server_tools = MagicMock(
        return_value=[{"name": "srv__echo", "original_name": "echo"}]
    )
    if callable(envelope):
        manager.call_tool_structured = AsyncMock(side_effect=envelope)
    else:
        manager.call_tool_structured = AsyncMock(return_value=envelope or _envelope())
    return patcher


def _mcp_workflow(*, arguments: dict[str, Any] | None = None) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="mcp-only",
            entry_point="call",
            runtime=RuntimeConfig(
                provider="copilot",
                mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")},
            ),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[
            MCPStepDef(
                name="call",
                server="srv",
                tool="echo",
                arguments=arguments or {"q": "hello"},
                routes=[RouteDef(to="$end")],
            ),
        ],
        output={"answer": "{{ call.output.answer }}"},
    )


class TestMcpOnlyWorkflow:
    @pytest.mark.asyncio
    async def test_mcp_only_workflow_completes_without_llm(self) -> None:
        # Requirement: a workflow of ONLY mcp steps completes end-to-end and
        # never touches the LLM provider — mcp steps are provider-free.
        provider = MagicMock()
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-chain",
                entry_point="first",
                runtime=RuntimeConfig(
                    provider="copilot",
                    mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")},
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                MCPStepDef(
                    name="first",
                    server="srv",
                    tool="echo",
                    arguments={"q": "hello"},
                    routes=[RouteDef(to="second")],
                ),
                MCPStepDef(
                    name="second",
                    server="srv",
                    tool="echo",
                    arguments={"q": "follow-up-{{ first.output.answer }}"},
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"answer": "{{ second.output.answer }}"},
        )
        patcher = _patch_manager()
        try:
            engine = WorkflowEngine(config, provider)
            result = await engine.run({})
        finally:
            patcher.stop()

        assert result == {"answer": 42}
        provider.execute.assert_not_called()
        # Both steps executed and the second saw the first's merged key.
        assert engine.context.agent_outputs["first"]["answer"] == 42
        assert engine.context.agent_outputs["second"]["answer"] == 42

    @pytest.mark.asyncio
    async def test_context_round_trips_through_json(self) -> None:
        # Requirement: the stored mcp envelope is JSON-safe — a checkpoint
        # save/load (context.to_dict -> json.dumps -> from_dict) preserves it.
        config = _mcp_workflow()
        patcher = _patch_manager()
        try:
            engine = _make_engine(config)
            await engine.run({})
        finally:
            patcher.stop()

        snapshot = engine.context.to_dict()
        rendered = json.dumps(snapshot)
        restored = WorkflowContext.from_dict(json.loads(rendered))
        stored = restored.agent_outputs["call"]
        assert stored["is_error"] is False
        assert stored["answer"] == 42  # merged structured key survives
        assert stored["content"][0]["text"] == _SECRET_RESULT


class TestMcpRouting:
    @pytest.mark.asyncio
    async def test_route_on_is_error_branches_both_ways(self) -> None:
        # Requirement: set -> mcp -> route on output.is_error completes and
        # picks the right branch for both is_error=true and is_error=false.
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-route",
                entry_point="flag",
                runtime=RuntimeConfig(
                    provider="copilot",
                    mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")},
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                SetStepDef(
                    name="flag",
                    values={"q": "{{ workflow.input.q }}"},
                    routes=[RouteDef(to="call")],
                ),
                MCPStepDef(
                    name="call",
                    server="srv",
                    tool="echo",
                    arguments={"q": "{{ flag.output.q }}"},
                    routes=[
                        RouteDef(to="on_error", when="{{ output.is_error }}"),
                        RouteDef(to="on_ok"),
                    ],
                ),
                SetStepDef(
                    name="on_error",
                    value="error-path",
                    routes=[RouteDef(to="$end")],
                ),
                SetStepDef(
                    name="on_ok",
                    value="ok-path",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={
                "path": (
                    "{% if on_error is defined %}{{ on_error.output }}"
                    "{% else %}{{ on_ok.output }}{% endif %}"
                )
            },
        )

        patcher = _patch_manager(_envelope(is_error=False))
        try:
            result_ok = await _make_engine(config).run({"q": "hi"})
        finally:
            patcher.stop()

        patcher = _patch_manager(_envelope(is_error=True))
        try:
            result_err = await _make_engine(config).run({"q": "hi"})
        finally:
            patcher.stop()

        assert result_ok == {"path": "ok-path"}
        assert result_err == {"path": "error-path"}


class TestMcpEventPayloads:
    @pytest.mark.asyncio
    async def test_events_carry_no_argument_or_result_values(self) -> None:
        # Requirement: no mcp_* event payload may contain argument values or
        # result values — only server/tool/argument_keys/elapsed/is_error/
        # result_bytes/truncated/spill_path (failed: error_type/message).
        config = _mcp_workflow(arguments={"token": _SECRET_ARG, "q": "hello"})
        engine = _make_engine(config)
        received = _collect_events(engine)

        patcher = _patch_manager()
        try:
            await engine.run({})
        finally:
            patcher.stop()

        mcp_events = [ev for ev in received if ev.type.startswith("mcp_")]
        assert {ev.type for ev in mcp_events} == {"mcp_started", "mcp_completed"}

        started = next(ev for ev in mcp_events if ev.type == "mcp_started")
        # Argument VALUES stay out; only key names are listed.
        assert started.data["argument_keys"] == ["q", "token"]
        assert _SECRET_ARG not in json.dumps(started.data)

        completed = next(ev for ev in mcp_events if ev.type == "mcp_completed")
        assert completed.data["server"] == "srv"
        assert completed.data["tool"] == "echo"
        assert completed.data["is_error"] is False
        assert completed.data["truncated"] is False
        assert completed.data["result_bytes"] > 0
        # The result body never appears in ANY event payload.
        assert _SECRET_RESULT not in json.dumps([ev.data for ev in received])

    @pytest.mark.asyncio
    async def test_failure_event_is_redacted(self) -> None:
        # Requirement: a failing mcp step emits exactly one mcp_failed whose
        # message is generic (no raw exception text), raises a redacted
        # ExecutionError (not the raw manager error — its text can carry
        # argument/result values), and workflow_failed carries no canary
        # value either.
        def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(f"call exploded with {_SECRET_ARG}")

        config = _mcp_workflow()
        engine = _make_engine(config)
        received = _collect_events(engine)

        patcher = _patch_manager(_boom)
        try:
            with pytest.raises(ExecutionError) as exc_info:
                await engine.run({})
        finally:
            patcher.stop()

        # The raised error is the generic redacted form, not the manager's
        # raw text (which embedded the secret argument value), and it points
        # at the private diagnostic file — the one place raw details land
        # (`--log-file` is a console mirror, not a Python logging sink).
        assert "call exploded" not in str(exc_info.value)
        assert _SECRET_ARG not in str(exc_info.value)
        assert "full diagnostic: " in str(exc_info.value)

        # The diagnostic sink holds the full traceback, canary included.
        diag_match = re.search(r"full diagnostic: (.+)$", str(exc_info.value))
        assert diag_match is not None
        diag_text = Path(diag_match.group(1)).read_text(encoding="utf-8")
        assert "call exploded" in diag_text
        assert _SECRET_ARG in diag_text

        failed = [ev for ev in received if ev.type == "mcp_failed"]
        assert len(failed) == 1
        failed_data = failed[0].data
        assert failed_data["error_type"] == "RuntimeError"
        assert _SECRET_ARG not in json.dumps(failed_data)
        assert _SECRET_RESULT not in json.dumps(failed_data)
        assert "full diagnostic: " in failed_data["message"]
        # The manager saw the call; the failure came from inside the step.
        assert failed_data["server"] == "srv"
        assert failed_data["tool"] == "echo"
        # workflow_failed must not smuggle the raw exception text either.
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert _SECRET_ARG not in json.dumps(wf_failed[0].data)
        assert "call exploded" not in json.dumps(wf_failed[0].data)


class TestMcpOutputSchemaValidation:
    @pytest.mark.asyncio
    async def test_schema_mismatch_fails_workflow_with_single_redacted_event(self) -> None:
        # Requirement: an output: schema mismatch on an mcp step raises a
        # REDACTED ExecutionError (not the ValidationError, whose message
        # echoes the received result value) and emits exactly one redacted
        # mcp_failed — the canary result scalar must appear in neither the
        # raised error nor any event payload (incl. workflow_failed).
        canary = "SECRET_CANARY_9f13"
        config = _mcp_workflow()
        config.agents[0].output = {"answer": OutputField(type="number")}

        def _value_envelope(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": "ok", "truncated": False}],
                "structured": {"answer": canary},
                "is_error": False,
            }

        engine = _make_engine(config)
        received = _collect_events(engine)

        patcher = _patch_manager(_value_envelope)
        try:
            with pytest.raises(ExecutionError) as exc_info:
                await engine.run({})
        finally:
            patcher.stop()

        # The raised error is the generic redacted form — the schema
        # ValidationError's "received: '<value>'" text must not surface — and
        # it points at the private diagnostic file holding the full details.
        assert canary not in str(exc_info.value)
        assert "full diagnostic: " in str(exc_info.value)

        diag_match = re.search(r"full diagnostic: (.+)$", str(exc_info.value))
        assert diag_match is not None
        diag_text = Path(diag_match.group(1)).read_text(encoding="utf-8")
        assert canary in diag_text

        failed = [ev for ev in received if ev.type == "mcp_failed"]
        assert len(failed) == 1
        assert failed[0].data["error_type"] == "ValidationError"
        assert canary not in json.dumps(failed[0].data)
        # No mcp_completed on the failure path; the run itself failed too.
        assert not any(ev.type == "mcp_completed" for ev in received)
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert canary not in json.dumps(wf_failed[0].data)


class TestMcpRuntimeChecks:
    @pytest.mark.asyncio
    async def test_unknown_server_names_available_servers(self) -> None:
        # Requirement: an mcp step referencing a server absent from
        # runtime.mcp_servers raises ExecutionError naming the available
        # servers at runtime (conductor run never calls the validator).
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-ghost",
                entry_point="call",
                runtime=RuntimeConfig(
                    provider="copilot",
                    mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")},
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                MCPStepDef(
                    name="call",
                    server="ghost",
                    tool="echo",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)

        with pytest.raises(ExecutionError, match="Available servers: srv"):
            await engine.run({})

        assert any(ev.type == "mcp_failed" for ev in received)

    @pytest.mark.asyncio
    async def test_templated_working_dir_value_leaks_no_values(self) -> None:
        # Requirement: the runtime working_dir not-a-directory check must not
        # leak the Jinja-rendered path or the raw template — both are rendered
        # from the execution context and can carry values. The propagated
        # message is redacted; the authored name-only runtime-check messages
        # (like the unknown-server one above) stay verbatim.
        canary = "SECRET_CANARY_9f13"
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-cwd-leak",
                entry_point="call",
                runtime=RuntimeConfig(
                    provider="copilot",
                    working_dir="{{ workflow.input.secret_path }}",
                    mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")},
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                MCPStepDef(
                    name="call",
                    server="srv",
                    tool="echo",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        engine = _make_engine(config)
        received = _collect_events(engine)

        with pytest.raises(ExecutionError) as excinfo:
            await engine.run({"secret_path": f"/nonexistent/{canary}"})

        assert "does not exist or is not a directory" in str(excinfo.value)
        assert canary not in str(excinfo.value)
        assert "{{ workflow.input.secret_path }}" not in str(excinfo.value)

        failed = [ev for ev in received if ev.type == "mcp_failed"]
        assert len(failed) == 1
        assert canary not in json.dumps(failed[0].data)
        assert "{{ workflow.input.secret_path }}" not in json.dumps(failed[0].data)
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert canary not in json.dumps(wf_failed[0].data)
        assert "{{ workflow.input.secret_path }}" not in json.dumps(wf_failed[0].data)

    @pytest.mark.asyncio
    async def test_mixed_wildcard_tools_list_allows_any_tool(self) -> None:
        # Requirement: the runtime allowlist check uses wildcard MEMBERSHIP,
        # exactly like config/validator.py — ["*", "health"] must allow any
        # tool here precisely when `conductor validate` accepts it (a
        # size/exact-list rule would fail the step before it even connects).
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-wildcard",
                entry_point="call",
                runtime=RuntimeConfig(
                    provider="copilot",
                    mcp_servers={
                        "srv": MCPServerDef(type="stdio", command="npx", tools=["*", "health"])
                    },
                ),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                MCPStepDef(
                    name="call",
                    server="srv",
                    tool="echo",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        engine = _make_engine(config)

        patcher = _patch_manager()
        try:
            await engine.run({})
        finally:
            patcher.stop()

        assert engine.context.agent_outputs["call"]["is_error"] is False


class TestMcpStepInterrupt:
    """Dashboard/keyboard Stop wiring for `type: mcp` steps (main loop)."""

    def _engine_with_interrupt(
        self, config: WorkflowConfig, *, web_dashboard: Any = None
    ) -> tuple[WorkflowEngine, asyncio.Event]:
        interrupt_event = asyncio.Event()
        engine = WorkflowEngine(
            config,
            MagicMock(),
            interrupt_event=interrupt_event,
            web_dashboard=web_dashboard,
        )
        return engine, interrupt_event

    @pytest.mark.asyncio
    async def test_stop_during_call_pauses_and_resume_reexecutes(self) -> None:
        # Requirement: a Stop landing mid-call cancels the in-flight
        # invocation (never auto-replayed inside the cancelled execution),
        # enters the dashboard pause flow (agent_paused), and Resume
        # re-executes the step from the top — the documented at-least-once
        # semantics — rather than the workflow silently continuing past the
        # pause as if provider-level interruption had handled it.
        from types import SimpleNamespace

        web_dashboard = SimpleNamespace(
            has_connections=lambda: True,
            resume_event=asyncio.Event(),
            kill_event=asyncio.Event(),
            disconnect_event=asyncio.Event(),
        )
        config = _mcp_workflow()
        engine, interrupt_event = self._engine_with_interrupt(config, web_dashboard=web_dashboard)
        received = _collect_events(engine)

        calls: list[str] = []

        async def call(_server: str, _tool: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            calls.append("call")
            if len(calls) == 1:
                # First execution: signal the Stop from inside the call, then
                # block until the interrupt race cancels us.
                interrupt_event.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    calls.append("cancelled")
                    raise
                raise AssertionError("unreachable")  # pragma: no cover
            return _envelope()

        patcher = _patch_manager(call)
        try:

            async def resume_once_paused() -> None:
                for _ in range(200):
                    if any(ev.type == "agent_paused" for ev in received):
                        web_dashboard.resume_event.set()
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("agent_paused never emitted")

            result, _ = await asyncio.gather(engine.run({}), resume_once_paused())
        finally:
            patcher.stop()

        # The interrupted call was cancelled, then Resume re-executed the
        # step: exactly one successful completion, no mcp_failed.
        assert calls == ["call", "cancelled", "call"]
        assert result == {"answer": 42}
        assert [ev.type for ev in received].count("mcp_completed") == 1
        assert not any(ev.type == "mcp_failed" for ev in received)
        assert any(ev.type == "agent_paused" for ev in received)
        assert any(ev.type == "agent_resumed" for ev in received)

    @pytest.mark.asyncio
    async def test_stop_during_call_kill_unwinds(self) -> None:
        # Requirement: choosing Kill at the pause unwinds the workflow via
        # InterruptError (stopped_by_user), never completing the step.
        from types import SimpleNamespace

        web_dashboard = SimpleNamespace(
            has_connections=lambda: True,
            resume_event=asyncio.Event(),
            kill_event=asyncio.Event(),
            disconnect_event=asyncio.Event(),
        )
        config = _mcp_workflow()
        engine, interrupt_event = self._engine_with_interrupt(config, web_dashboard=web_dashboard)
        received = _collect_events(engine)

        calls: list[str] = []

        async def call(_server: str, _tool: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            calls.append("call")
            interrupt_event.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                calls.append("cancelled")
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        patcher = _patch_manager(call)
        try:

            async def kill_once_paused() -> None:
                for _ in range(200):
                    if any(ev.type == "agent_paused" for ev in received):
                        web_dashboard.kill_event.set()
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("agent_paused never emitted")

            with pytest.raises(InterruptError):
                await asyncio.gather(engine.run({}), kill_once_paused())
        finally:
            patcher.stop()

        assert calls == ["call", "cancelled"]
        assert not any(ev.type == "mcp_completed" for ev in received)
        assert not any(ev.type == "mcp_failed" for ev in received)

    @pytest.mark.asyncio
    async def test_stop_during_call_disconnect_parks_resumable_stop(self) -> None:
        # Requirement: when every dashboard client disconnects while the
        # pause is pending, the interrupted call is NOT re-executed — there
        # is no one to make the resume decision, and transparently repeating
        # a side-effecting call could duplicate unknown external effects.
        # The run parks as a resumable stop instead: an InterruptError
        # subclass (stopped_by_user on workflow_failed), no mcp_failed, no
        # agent_resumed, exactly one attempted call, so `conductor resume`
        # becomes the explicit at-least-once re-execution boundary.
        from types import SimpleNamespace

        web_dashboard = SimpleNamespace(
            has_connections=lambda: True,
            resume_event=asyncio.Event(),
            kill_event=asyncio.Event(),
            disconnect_event=asyncio.Event(),
        )
        config = _mcp_workflow()
        engine, interrupt_event = self._engine_with_interrupt(config, web_dashboard=web_dashboard)
        received = _collect_events(engine)

        calls: list[str] = []

        async def call(_server: str, _tool: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            calls.append("call")
            interrupt_event.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                calls.append("cancelled")
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        patcher = _patch_manager(call)
        try:

            async def disconnect_once_paused() -> None:
                for _ in range(200):
                    if any(ev.type == "agent_paused" for ev in received):
                        web_dashboard.disconnect_event.set()
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("agent_paused never emitted")

            with pytest.raises(InterruptError) as exc_info:
                await asyncio.gather(engine.run({}), disconnect_once_paused())
        finally:
            patcher.stop()

        assert "not resumed" in str(exc_info.value)
        assert calls == ["call", "cancelled"]
        assert not any(ev.type == "mcp_completed" for ev in received)
        assert not any(ev.type == "mcp_failed" for ev in received)
        assert not any(ev.type == "agent_resumed" for ev in received)
        failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(failed) == 1
        assert failed[0].data["stopped_by_user"] is True

    @pytest.mark.asyncio
    async def test_stop_during_call_no_clients_parks_resumable_stop(self) -> None:
        # Requirement: a dashboard attached with ZERO connected clients is
        # also "no one to decide", so the interrupted call is not
        # auto-replayed — diverging from the LLM auto-resume, whose re-run
        # only costs tokens. The run parks as a resumable stop without ever
        # presenting a pause (no agent_paused / agent_resumed).
        from types import SimpleNamespace

        web_dashboard = SimpleNamespace(
            has_connections=lambda: False,
            resume_event=asyncio.Event(),
            kill_event=asyncio.Event(),
            disconnect_event=asyncio.Event(),
        )
        config = _mcp_workflow()
        engine, interrupt_event = self._engine_with_interrupt(config, web_dashboard=web_dashboard)
        received = _collect_events(engine)

        calls: list[str] = []

        async def call(_server: str, _tool: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            calls.append("call")
            interrupt_event.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                calls.append("cancelled")
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        patcher = _patch_manager(call)
        try:
            with pytest.raises(InterruptError) as exc_info:
                await engine.run({})
        finally:
            patcher.stop()

        assert "not resumed" in str(exc_info.value)
        assert calls == ["call", "cancelled"]
        assert not any(ev.type == "agent_paused" for ev in received)
        assert not any(ev.type == "agent_resumed" for ev in received)
        assert not any(ev.type == "mcp_failed" for ev in received)
        failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(failed) == 1
        assert failed[0].data["stopped_by_user"] is True

    @pytest.mark.asyncio
    async def test_disconnect_park_saves_failure_checkpoint(self, tmp_path: Path) -> None:
        # Requirement: the parked run is genuinely resumable — the failure
        # checkpoint is written with current_agent pointing at the parked
        # mcp step, so `conductor resume` re-enters exactly that step (the
        # explicit at-least-once boundary), not an earlier one.
        from types import SimpleNamespace

        from conductor.engine.checkpoint import CheckpointManager

        workflow_file = tmp_path / "workflow.yaml"
        workflow_file.write_text("name: mcp-only\n")

        web_dashboard = SimpleNamespace(
            has_connections=lambda: True,
            resume_event=asyncio.Event(),
            kill_event=asyncio.Event(),
            disconnect_event=asyncio.Event(),
        )
        interrupt_event = asyncio.Event()
        provider = MagicMock()
        # The failure checkpoint serializes collected provider session ids —
        # a bare MagicMock return value is not JSON-serializable.
        provider.get_session_ids.return_value = {}
        provider.get_session_cwds.return_value = {}
        engine = WorkflowEngine(
            _mcp_workflow(),
            provider,
            interrupt_event=interrupt_event,
            web_dashboard=web_dashboard,
            workflow_path=workflow_file,
        )
        received = _collect_events(engine)

        calls: list[str] = []

        async def call(_server: str, _tool: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            calls.append("call")
            interrupt_event.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                calls.append("cancelled")
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        patcher = _patch_manager(call)
        try:
            with patch.object(CheckpointManager, "get_checkpoints_dir", return_value=tmp_path):

                async def disconnect_once_paused() -> None:
                    for _ in range(200):
                        if any(ev.type == "agent_paused" for ev in received):
                            web_dashboard.disconnect_event.set()
                            return
                        await asyncio.sleep(0.01)
                    raise AssertionError("agent_paused never emitted")

                with pytest.raises(InterruptError):
                    await asyncio.gather(engine.run({}), disconnect_once_paused())
        finally:
            patcher.stop()

        saved = [ev for ev in received if ev.type == "checkpoint_saved"]
        assert len(saved) == 1
        checkpoint_path = engine._last_checkpoint_path
        assert checkpoint_path is not None and checkpoint_path.exists()
        data = json.loads(checkpoint_path.read_text())
        assert data["current_agent"] == "call"

    @pytest.mark.asyncio
    async def test_call_completed_before_stop_returns_normally(self) -> None:
        # Requirement: when the call has already completed by the time the
        # interrupt fires, the result is returned (no spurious
        # interruption); the pending Stop is consumed by the between-step
        # interrupt check as on every other step type.
        config = _mcp_workflow()
        engine, interrupt_event = self._engine_with_interrupt(config)
        _collect_events(engine)

        async def call(_server: str, _tool: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            return _envelope()

        patcher = _patch_manager(call)
        try:
            interrupt_event.set()  # already pending before the step runs
            # The pending flag fires the race immediately; simulate the
            # dashboard-less CLI path by clearing it as the menu would.
            engine._interrupt_handler = MagicMock()
            engine._interrupt_handler.handle_interrupt = AsyncMock(
                return_value=MagicMock(action="continue")
            )
            await engine.run({})
        finally:
            patcher.stop()

        assert engine.context.agent_outputs["call"]["answer"] == 42

    @pytest.mark.asyncio
    async def test_system_exit_from_manager_wraps_redacted(self) -> None:
        # Requirement: a value-bearing BaseException raised from MCP
        # SDK / connect / renderer code (e.g. SystemExit("secret")) must not
        # bypass the redaction and reach workflow_failed via the outer
        # except BaseException — it becomes a redacted step failure with
        # exactly one mcp_failed, and the canary appears in no surface.
        canary = "SECRET_CANARY_9f13"
        engine = _make_engine(_mcp_workflow())
        received = _collect_events(engine)

        async def _raise_system_exit(*_args: Any, **_kwargs: Any) -> Any:
            raise SystemExit(canary)

        patcher = _patch_manager(envelope=_raise_system_exit)
        try:
            with pytest.raises(ExecutionError) as exc_info:
                await engine.run({})
        finally:
            patcher.stop()

        assert "SystemExit" not in str(exc_info.value)
        assert canary not in str(exc_info.value)
        failed = [ev for ev in received if ev.type == "mcp_failed"]
        assert len(failed) == 1
        assert canary not in json.dumps(failed[0].data)
        wf_failed = [ev for ev in received if ev.type == "workflow_failed"]
        assert len(wf_failed) == 1
        assert canary not in json.dumps(wf_failed[0].data)

    @pytest.mark.asyncio
    async def test_cancelled_step_reraises_without_mcp_failed(self) -> None:
        # Requirement: cancellation is not a step failure — the step re-raises
        # CancelledError untouched and emits NO mcp_failed, preserving the
        # engine's cancellation semantics.
        engine = _make_engine(_mcp_workflow())
        received = _collect_events(engine)

        async def _raise_cancelled(*_args: Any, **_kwargs: Any) -> Any:
            raise asyncio.CancelledError()

        patcher = _patch_manager(envelope=_raise_cancelled)
        try:
            with pytest.raises(asyncio.CancelledError):
                await engine._run_mcp_step(
                    engine.config.agents[0], engine.context.build_for_agent("call", [])
                )
        finally:
            patcher.stop()

        assert not any(ev.type == "mcp_failed" for ev in received)


class TestMcpExplicitContextMode:
    @pytest.mark.asyncio
    async def test_arguments_render_workflow_inputs_and_declared_outputs(self) -> None:
        # Requirement: with workflow.context.mode: explicit, the validator
        # allows workflow.input.* references in mcp arguments (mirroring
        # set/script/wait), so the runtime context must make them renderable
        # too — a reference that passes static validation must render at
        # run time. Prior-step outputs still require an explicit input:
        # declaration, as for every step type.
        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="mcp-explicit",
                entry_point="prep",
                runtime=RuntimeConfig(
                    provider="copilot",
                    mcp_servers={"srv": MCPServerDef(type="stdio", command="npx")},
                ),
                context=ContextConfig(mode="explicit"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                SetStepDef(
                    name="prep",
                    value="from-prep",
                    routes=[RouteDef(to="call")],
                ),
                MCPStepDef(
                    name="call",
                    server="srv",
                    tool="echo",
                    input=["prep.output"],
                    arguments={
                        "q": "{{ workflow.input.question }}",
                        "prior": "{{ prep.output }}",
                    },
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={},
        )
        engine = _make_engine(config)

        captured: list[dict[str, Any]] = []

        async def capture_call(_server: str, _tool: str, arguments: dict[str, Any]) -> Any:
            captured.append(arguments)
            return _envelope()

        patcher = _patch_manager(envelope=capture_call)
        try:
            await engine.run({"question": "what is python?"})
        finally:
            patcher.stop()

        assert captured == [{"q": "what is python?", "prior": "from-prep"}]
