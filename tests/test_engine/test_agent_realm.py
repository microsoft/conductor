"""Agent placement and same-realm grading contracts."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from conductor.config.schema import (
    AgentDef,
    ContextConfig,
    ForEachDef,
    LimitsConfig,
    OutputField,
    ParallelGroup,
    ReasoningConfig,
    RouteDef,
    RuntimeConfig,
    ValidatorConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.run_manifest import ManifestExecutionSpec
from conductor.engine.workflow import AgentRealm, WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.exceptions import ConfigurationError
from conductor.execution import (
    AgentEventSink,
    AgentResult,
    AgentSpec,
    LocalRunnerBackend,
    ResolvedExecutionSpec,
    RunnerCapabilities,
    WorkspaceLease,
)
from conductor.execution.docker import DockerRunnerBackend
from conductor.executor.agent import AgentExecutor
from conductor.gates.dialog import DialogMessage, DialogResult
from conductor.gates.interrupt import InterruptAction, InterruptResult
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider


class RecordingAgentBackend(LocalRunnerBackend):
    """Return data-shaped remote results without consulting the host SDK."""

    def __init__(self, *, fail_grader: bool = False) -> None:
        self.specs: list[AgentSpec] = []
        self.leases: list[WorkspaceLease | None] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.fail_grader = fail_grader

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            batch=False, sessions=False, shared_workspace=False, snapshots=False, agent=True
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
        self.specs.append(spec)
        self.leases.append(lease)
        if on_event is not None:
            on_event("agent_message", {"content": "from remote"})
        if "passed" in (spec.output_schema or {}):
            assert spec.tools == ()
            assert spec.mcp_servers is None
            assert on_event is None
            if self.fail_grader:
                raise RuntimeError("grader unavailable")
            return AgentResult(
                content={"passed": False, "issues": ["revise"]},
                model="grader",
                input_tokens=2,
                output_tokens=3,
            )
        if spec.name == "finder":
            return AgentResult(content={"items": ["a"]}, model="gpt-4o")
        return AgentResult(
            content={"answer": "remote"},
            model="gpt-4o",
            input_tokens=5,
            output_tokens=7,
            continuation_state=["must not cross the wire"],
        )


def _agent(*, validated: bool = False) -> AgentDef:
    return AgentDef(
        name="writer",
        prompt="Write {{ workflow.input.topic }}",
        output={"answer": OutputField(type="string")},
        validator=ValidatorConfig(criteria="Check correctness") if validated else None,
        routes=[RouteDef(to="$end")],
    )


def _engine(agent: AgentDef) -> tuple[WorkflowEngine, list[WorkflowEvent]]:
    config = WorkflowConfig(
        workflow=WorkflowDef(
            name="realm-dispatch",
            entry_point="writer",
            runtime=RuntimeConfig(provider="copilot"),
            context=ContextConfig(mode="accumulate"),
            limits=LimitsConfig(max_iterations=10),
        ),
        agents=[agent],
        output={"answer": "{{ writer.output.answer }}"},
    )
    emitter = WorkflowEventEmitter()
    events: list[WorkflowEvent] = []
    emitter.subscribe(events.append)
    return WorkflowEngine(
        config, CopilotProvider(mock_handler=lambda *_: {"answer": "host"}), event_emitter=emitter
    ), events


def _place(
    engine: WorkflowEngine, backend: RecordingAgentBackend, monkeypatch: pytest.MonkeyPatch
) -> AgentRealm:
    # Requirement: all agent follow-ups use one backend and its original lease.
    realm = AgentRealm(
        backend=backend,
        lease=WorkspaceLease(lease_id="run", backend="docker", incarnation="original"),
        execution=ResolvedExecutionSpec(image="runner:test"),
        env_overlay={},
        credential_resolver=lambda _: {"github_token": "test-token"},
        image="runner:test",
        name="docker",
    )
    monkeypatch.setattr(engine, "_agent_realm", lambda *_args, **_kwargs: realm)
    return realm


@pytest.mark.asyncio
async def test_remote_main_and_grader_share_realm_and_rerun_statelessly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: the grader and failed-validation retry never use the host SDK.
    backend = RecordingAgentBackend()
    engine, events = _engine(_agent(validated=True))
    realm = _place(engine, backend, monkeypatch)

    result = await engine.run({"topic": "carefully"})

    assert result == {"answer": "remote"}
    assert len(backend.specs) == 3
    primary, grader, rerun = backend.specs
    assert all(lease is realm.lease for lease in backend.leases)
    assert all(spec.execution is realm.execution for spec in backend.specs)
    assert primary.rendered_prompt == "Write carefully"
    assert grader.tools == () and grader.mcp_servers is None
    assert rerun.rendered_prompt.startswith("Write carefully")
    assert rerun.execution_id != primary.execution_id
    assert all(
        spec.provider_credentials == {"github_token": "test-token"} for spec in backend.specs
    )
    for event_type in ("agent_started", "agent_completed"):
        data = next(event.data for event in events if event.type == event_type)
        assert data["execution_backend"] == "docker"
        assert data["realm_image"] == "runner:test"


@pytest.mark.asyncio
async def test_remote_grader_error_remains_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: a failed remote grader keeps the primary output without rerunning it.
    backend = RecordingAgentBackend(fail_grader=True)
    engine, events = _engine(_agent(validated=True))
    _place(engine, backend, monkeypatch)

    result = await engine.run({"topic": "carefully"})

    assert result == {"answer": "remote"}
    assert len(backend.specs) == 2
    complete = next(event for event in events if event.type == "agent_validator_complete")
    assert complete.data["errored"] is True


@pytest.mark.asyncio
async def test_no_profile_uses_cached_provider_without_building_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: workflows without realm profiles make no AgentSpec/backend calls.
    engine, events = _engine(_agent())
    provider = engine.provider
    assert provider is not None
    assert engine.executor is not None
    monkeypatch.setattr(
        engine.executor, "build_realm_spec", AsyncMock(side_effect=AssertionError("spec built"))
    )
    monkeypatch.setattr(
        engine._execution_resolver,
        "backend_for_step",
        Mock(side_effect=AssertionError("backend lookup")),
    )
    # Requirement: the default realm never starts a runner, connects a socket,
    # makes an HTTP request, or serializes an agent onto the wire.
    forbidden = Mock(side_effect=AssertionError("unexpected runner/container/wire I/O"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr("conductor.runner.protocol.request_to_wire_body", forbidden)
    monkeypatch.setattr(DockerRunnerBackend, "run_agent", forbidden)

    result = await engine.run({"topic": "locally"})

    assert result == {"answer": "host"}
    assert engine.provider is provider
    forbidden.assert_not_called()
    assert "execution_backend" not in next(e.data for e in events if e.type == "agent_started")


@pytest.mark.asyncio
async def test_remote_result_discards_opaque_continuation() -> None:
    # Requirement: remote continuation cannot enter the host provider's in-memory path.
    backend = RecordingAgentBackend()
    executor = AgentExecutor(CopilotProvider(mock_handler=lambda *_: {"answer": "host"}))
    output = await executor.execute(
        AgentDef(name="writer", prompt="Task", output={"answer": OutputField(type="string")}),
        {},
        execution_backend=backend,
        realm_credential_resolver=lambda _: {"github_token": "test-token"},
    )
    assert output.continuation_state is None
    assert output.raw_response is None
    assert output.tokens_used == 12


def test_engine_resolves_remote_backend_and_original_lease() -> None:
    # Requirement: per-step manifest identity chooses backend, lease and execution data.
    backend = RecordingAgentBackend()
    engine, _ = _engine(_agent())
    manifest = engine._execution_resolver.manifest
    local = manifest.profiles["writer"]
    remote = local.model_copy(
        update={
            "backend": "docker",
            "execution": ManifestExecutionSpec(image="runner:test"),
        }
    )
    engine._execution_resolver._manifest = manifest.model_copy(
        update={"profiles": {**manifest.profiles, "writer": remote}}
    )
    lease = WorkspaceLease(lease_id="run", backend="docker", incarnation="original")
    engine._execution_session.backends["docker"] = backend
    engine._execution_session.leases["docker"] = lease

    realm = engine._agent_realm("writer")

    assert realm is not None
    assert realm.backend is backend
    assert realm.lease is lease
    assert realm.execution == ResolvedExecutionSpec(image="runner:test")
    assert realm.image == "runner:test"


@pytest.mark.asyncio
async def test_remote_spec_uses_rendered_fields_and_provider_defaults() -> None:
    # Requirement: the runner receives effective fields, not raw Jinja AgentDef data.
    backend = RecordingAgentBackend()
    provider = CopilotProvider(
        mock_handler=lambda *_: {"answer": "host"},
        model="gpt-4o",
        max_agent_iterations=8,
    )
    agent = AgentDef(
        name="writer",
        model="{{ workflow.input.model }}",
        prompt="Write {{ workflow.input.topic }}",
        system_prompt="System {{ workflow.input.topic }}",
        reasoning=ReasoningConfig(effort="{{ workflow.input.effort }}"),
        output={"answer": OutputField(type="string")},
    )
    context = {"workflow": {"input": {"model": "gpt-4.1", "topic": "notes", "effort": "low"}}}

    await AgentExecutor(provider).execute(
        agent,
        context,
        execution_backend=backend,
        agent_env_overlay={"MCP_TOKEN": "secret"},
        realm_credential_resolver=lambda _: {"github_token": "test-token"},
    )

    spec = backend.specs[0]
    assert spec.model_provider == "copilot"
    assert spec.model == "gpt-4.1"
    assert spec.rendered_prompt == "Write notes"
    assert spec.system_prompt == "System notes"
    assert spec.reasoning_effort == "low"
    assert spec.max_agent_iterations == 8
    assert spec.output_schema == {"answer": {"type": "string"}}
    assert spec.env_overlay == {"MCP_TOKEN": "secret"}
    assert "secret" not in repr(spec)


@pytest.mark.asyncio
async def test_local_provider_kwargs_and_callback_order_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: the no-realm path invokes the cached provider with its exact old kwargs.
    provider = CopilotProvider(mock_handler=lambda *_: {"answer": "unused"})
    seen: list[tuple[str, Any]] = []

    async def execute(**kwargs: Any) -> AgentOutput:
        seen.append(("provider", kwargs))
        kwargs["event_callback"]("agent_message", {"content": "local"})
        return AgentOutput(content={"answer": "local"}, raw_response="local")

    monkeypatch.setattr(provider, "execute", execute)
    executor = AgentExecutor(provider)
    agent = AgentDef(name="writer", prompt="Task", output={"answer": OutputField(type="string")})
    context: dict[str, Any] = {}

    def callback(event_type: str, data: dict[str, Any]) -> None:
        seen.append((event_type, data))

    first = await executor.execute(agent, context, event_callback=callback)
    second = await executor.execute(agent, context, event_callback=callback)

    assert first.content == second.content == {"answer": "local"}
    assert [name for name, _ in seen] == [
        "agent_prompt_rendered",
        "provider",
        "agent_message",
        "agent_prompt_rendered",
        "provider",
        "agent_message",
    ]
    expected = {
        "agent": agent,
        "context": context,
        "rendered_prompt": "Task",
        "tools": [],
        "interrupt_signal": None,
        "event_callback": callback,
        "skill_directories": None,
        "custom_agents": None,
        "extra_mcp_servers": None,
        "continuation_state": None,
    }
    for name, kwargs in seen:
        if name != "provider":
            continue
        assert kwargs == expected
        assert kwargs["agent"] is agent
        assert kwargs["context"] is context
        assert kwargs["event_callback"] is callback


@pytest.mark.asyncio
async def test_explicit_local_backend_runs_provider_through_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: explicit local placement delegates without changing provider
    # arguments, event order, or opaque output identity.
    provider = CopilotProvider(mock_handler=lambda *_: {"answer": "unused"})
    backend = LocalRunnerBackend()
    lease = WorkspaceLease(lease_id="local", backend="local", incarnation="first")
    signal = asyncio.Event()
    events: list[str] = []
    original = LocalRunnerBackend.run_agent
    observed: list[tuple[AgentSpec, WorkspaceLease | None, Any, Any, Any]] = []

    async def run_agent(
        self: LocalRunnerBackend,
        spec: AgentSpec,
        passed_lease: WorkspaceLease | None,
        **kwargs: Any,
    ) -> AgentResult:
        observed.append(
            (
                spec,
                passed_lease,
                kwargs["on_event"],
                kwargs["interrupt_signal"],
                kwargs["execute_local"],
            )
        )
        return await original(self, spec, passed_lease, **kwargs)

    async def execute(**kwargs: Any) -> AgentOutput:
        events.append("provider")
        assert kwargs == {
            "agent": agent,
            "context": context,
            "rendered_prompt": "Task",
            "tools": [],
            "interrupt_signal": signal,
            "event_callback": callback,
            "skill_directories": None,
            "custom_agents": None,
            "extra_mcp_servers": None,
            "continuation_state": None,
        }
        kwargs["event_callback"]("agent_message", {"content": "local"})
        return expected

    def callback(event_type: str, data: dict[str, Any]) -> None:
        events.append(event_type)

    agent = AgentDef(name="writer", prompt="Task", output={"answer": OutputField(type="string")})
    context: dict[str, Any] = {}
    expected = AgentOutput(content={"answer": "local"}, raw_response="opaque")
    monkeypatch.setattr(provider, "execute", execute)
    monkeypatch.setattr(LocalRunnerBackend, "run_agent", run_agent)

    output = await AgentExecutor(provider).execute(
        agent,
        context,
        event_callback=callback,
        interrupt_signal=signal,
        execution_backend=backend,
        workspace_lease=lease,
    )

    assert output is expected
    assert events == ["agent_prompt_rendered", "provider", "agent_message"]
    assert len(observed) == 1
    spec, passed_lease, on_event, passed_signal, execute_local = observed[0]
    assert spec.name == "writer" and spec.rendered_prompt == "Task"
    assert passed_lease is lease
    assert on_event is callback and passed_signal is signal
    assert callable(execute_local)


@pytest.mark.asyncio
async def test_unavailable_remote_backend_does_not_fall_back_to_host() -> None:
    # Requirement: an unimplemented remote realm fails rather than using local credentials.
    class UnavailableBackend(LocalRunnerBackend):
        def capabilities(self) -> RunnerCapabilities:
            return RunnerCapabilities(
                batch=False, sessions=False, shared_workspace=False, snapshots=False, agent=False
            )

    executor = AgentExecutor(CopilotProvider(mock_handler=lambda *_: {"answer": "host"}))
    with pytest.raises(ConfigurationError, match="cannot run agents"):
        await executor.execute(
            AgentDef(name="writer", prompt="Task"),
            {},
            execution_backend=UnavailableBackend(),
        )


@pytest.mark.asyncio
async def test_parallel_members_use_remote_backend_with_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: each parallel member routes through its own resolved agent realm.
    backend = RecordingAgentBackend()
    engine, events = _engine(_agent())
    engine.config = engine.config.model_copy(
        update={
            "workflow": engine.config.workflow.model_copy(update={"entry_point": "team"}),
            "agents": [
                _agent().model_copy(update={"routes": []}),
                AgentDef(
                    name="peer", prompt="Write too", output={"answer": OutputField(type="string")}
                ),
            ],
            "parallel": [
                ParallelGroup(name="team", agents=["writer", "peer"], routes=[RouteDef(to="$end")])
            ],
            "output": {"done": "true"},
        }
    )
    _place(engine, backend, monkeypatch)

    await engine.run({"topic": "parallel"})

    assert {spec.name for spec in backend.specs} == {"writer", "peer"}
    starts = [event for event in events if event.type == "parallel_agent_started"]
    completions = [event for event in events if event.type == "parallel_agent_completed"]
    assert len(starts) == len(completions) == 2
    assert all(event.data["execution_backend"] == "docker" for event in starts + completions)


@pytest.mark.asyncio
async def test_for_each_inline_agent_retains_item_event_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: remote on_event remains tagged with the correct for-each item.
    backend = RecordingAgentBackend()
    engine, events = _engine(_agent())
    engine.config = engine.config.model_copy(
        update={
            "workflow": engine.config.workflow.model_copy(update={"entry_point": "finder"}),
            "agents": [
                AgentDef(
                    name="finder",
                    prompt="Find",
                    output={"items": OutputField(type="array")},
                    routes=[RouteDef(to="items")],
                )
            ],
            "for_each": [
                ForEachDef(
                    name="items",
                    type="for_each",
                    source="finder.output.items",
                    **{"as": "item"},
                    agent=AgentDef(
                        name="worker",
                        prompt="Write {{ item }}",
                        output={"answer": OutputField(type="string")},
                    ),
                    max_concurrent=1,
                    routes=[RouteDef(to="$end")],
                )
            ],
            "output": {"done": "true"},
        }
    )
    _place(engine, backend, monkeypatch)

    await engine.run({})

    inline = next(spec for spec in backend.specs if spec.name.startswith("worker["))
    assert inline.rendered_prompt == "Write a"
    start = next(event for event in events if event.type == "for_each_agent_started")
    assert start.data["execution_backend"] == "docker"
    message = next(
        event for event in events if event.type == "agent_message" and "item_key" in event.data
    )
    assert message.data["agent_name"] == "items"
    assert message.data["item_key"] == "0"
    done = next(event for event in events if event.type == "for_each_item_completed")
    assert done.data["execution_backend"] == "docker"


@pytest.mark.asyncio
async def test_dialog_refinement_keeps_primary_realm(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: a dialog-triggered refinement uses the original backend.
    backend = RecordingAgentBackend()
    engine, _ = _engine(_agent())
    _place(engine, backend, monkeypatch)
    monkeypatch.setattr(
        engine._dialog_evaluator,
        "evaluate",
        AsyncMock(return_value=SimpleNamespace(trigger=True, reason="", question="Continue?")),
    )
    monkeypatch.setattr(
        engine._dialog_handler,
        "handle_dialog",
        AsyncMock(
            return_value=DialogResult(
                dialog_id="dialog", messages=[DialogMessage(role="user", content="yes")]
            )
        ),
    )
    executor = engine.executor
    assert executor is not None

    output = await engine._handle_dialog(
        _agent(),
        AgentOutput(content={"answer": "draft"}, raw_response=None),
        {"workflow": {"input": {"topic": "dialog"}}},
        executor,
        engine._agent_realm("writer"),
    )

    assert output.content == {"answer": "remote"}
    assert len(backend.specs) == 1
    assert "yes" in backend.specs[0].rendered_prompt


@pytest.mark.asyncio
async def test_guidance_reexecution_keeps_primary_realm(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: an interrupted remote agent never follows up through the host SDK.
    backend = RecordingAgentBackend()
    engine, _ = _engine(_agent())
    _place(engine, backend, monkeypatch)
    monkeypatch.setattr(
        engine._interrupt_handler,
        "handle_interrupt",
        AsyncMock(return_value=InterruptResult(action=InterruptAction.CONTINUE, guidance="revise")),
    )
    executor = engine.executor
    assert executor is not None

    output = await engine._handle_partial_output(
        _agent(),
        AgentOutput(content={"answer": "draft"}, raw_response=None, partial=True),
        {"workflow": {"input": {"topic": "guidance"}}},
        None,
        executor,
        0.0,
        engine._agent_realm("writer"),
    )

    assert output.content == {"answer": "remote"}
    assert len(backend.specs) == 1
    assert "revise" in backend.specs[0].rendered_prompt
