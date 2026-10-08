"""Canonical script-to-agent event contract across local and Docker realms."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from conductor.config.schema import (
    AgentDef,
    OutputField,
    RouteDef,
    RuntimeConfig,
    ScriptStepDef,
    StepDef,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import AgentRealm, WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.execution.docker import DockerRunnerBackend
from conductor.execution.types import BundleRef, ResolvedExecutionSpec, RunSpec
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider


@pytest.fixture
def canonical_events() -> tuple[str, ...]:
    """The full emitted vocabulary for one script followed by one agent."""
    return (
        "workflow_started",
        "agent_started",
        "script_started",
        "script_completed",
        "route_taken",
        "agent_started",
        "agent_prompt_rendered",
        "agent_message",
        "agent_completed",
        "route_taken",
        "workflow_completed",
    )


def _workflow() -> WorkflowConfig:
    steps: list[StepDef] = [
        ScriptStepDef(
            name="script",
            command=sys.executable,
            args=["-c", "print('ready')"],
            timeout=None,
            routes=[RouteDef(to="agent")],
        ),
        AgentDef(
            name="agent",
            prompt="Use {{ script.output.stdout }}",
            output={"answer": OutputField(type="string")},
            timeout_seconds=None,
            max_session_seconds=None,
            max_agent_iterations=None,
            routes=[RouteDef(to="$end")],
        ),
    ]
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="realm-parity",
            entry_point="script",
            runtime=RuntimeConfig.model_validate({"provider": "copilot"}),
        ),
        agents=steps,
        output={"script": "{{ script.output.stdout }}", "answer": "{{ agent.output.answer }}"},
    )


@pytest.mark.asyncio
async def test_script_agent_event_and_output_parity(
    canonical_events: tuple[str, ...],
    fake_realm: tuple[DockerRunnerBackend, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Compared fields are fixed: script exit_code, agent names, rendered_prompt,
    # message text, completed output and output_keys. Realm identity, elapsed, tokens and costs are
    # intentionally excluded because execution placement changes their values.
    backend, _log, _state = fake_realm
    store = tmp_path / "bundle"
    (store / "tree/main").mkdir(parents=True)
    (store / "bundle.json").write_text(
        json.dumps({"entries": [], "skills_topology": {}, "plugins_topology": {}}),
        encoding="utf-8",
    )
    bundle = BundleRef("sha256:parity", str(store), source_roots=((str(tmp_path), "main"),))
    lease = await backend.prepare_run(RunSpec(run_id="parity", bundle=bundle))
    observed: list[tuple[dict[str, Any], list[WorkflowEvent]]] = []
    try:
        for remote in (False, True):
            emitter = WorkflowEventEmitter()
            events: list[WorkflowEvent] = []
            emitter.subscribe(events.append)
            provider = CopilotProvider(mock_handler=lambda *_: {"answer": "unused"})
            provider.execute = AsyncMock(side_effect=lambda **kwargs: _local_answer(kwargs))
            engine = WorkflowEngine(_workflow(), provider, event_emitter=emitter)
            if remote:
                realm = AgentRealm(
                    backend=backend,
                    lease=lease,
                    execution=ResolvedExecutionSpec(image="runner:2"),
                    env_overlay={},
                    credential_resolver=lambda _: {"github_token": "test-token"},
                    image="runner:2",
                    name="docker",
                )
                monkeypatch.setattr(
                    engine, "_agent_realm", lambda *_args, _realm=realm, **_kwargs: _realm
                )
            observed.append((await engine.run({}), events))
        (local_output, local_events), (docker_output, docker_events) = observed
        assert local_output == docker_output == {"script": "ready\n", "answer": "agent"}
        assert tuple(event.type for event in local_events) == canonical_events
        assert tuple(event.type for event in docker_events) == canonical_events
        for events in (local_events, docker_events):
            assert next(e.data["exit_code"] for e in events if e.type == "script_completed") == 0
            assert [e.data["agent_name"] for e in events if e.type == "agent_started"] == [
                "script",
                "agent",
            ]
            assert (
                next(e.data["rendered_prompt"] for e in events if e.type == "agent_prompt_rendered")
                == "Use ready\n"
            )
            assert next(e.data["text"] for e in events if e.type == "agent_message") == "streamed"
            completed = next(e.data for e in events if e.type == "agent_completed")
            assert completed["output"] == {"answer": "agent"}
            assert completed["output_keys"] == ["answer"]
    finally:
        await backend.finalize_run(lease, "succeeded")


def _local_answer(kwargs: dict[str, Any]) -> AgentOutput:
    kwargs["event_callback"]("agent_message", {"text": "streamed"})
    return AgentOutput(content={"answer": "agent"}, raw_response=None)
