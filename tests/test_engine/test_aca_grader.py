"""ACA realm keeps a primary agent and its synthetic grader in one sandbox directory."""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import conductor.engine.execution_resolution as resolution_module
from conductor.aca_runner import server
from conductor.config.environment import (
    AcaProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
    SecretBinding,
    SecretBindingSource,
)
from conductor.config.schema import AgentDef, ProviderSettings, WorkflowConfig
from conductor.engine.aca_execution import AcaRunnerBackend
from conductor.engine.workflow import WorkflowEngine
from conductor.events import WorkflowEvent, WorkflowEventEmitter
from conductor.providers.aca import AcaRuntimeProvider
from conductor.providers.base import AgentOutput
from conductor.providers.copilot import CopilotProvider


@pytest.mark.asyncio
async def test_aca_primary_and_grader_use_explicit_sandbox_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: grading stays in the primary ACA realm and inherits its sandbox directory.
    working_dirs: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ready": True, "protocol_version": 2})
        body = json.loads(request.content)
        working_dirs.append(body["agent"].get("working_dir"))
        content = (
            {"passed": True, "issues": []}
            if body["agent"]["name"].endswith(" (validator)")
            else {"answer": "remote"}
        )
        return httpx.Response(
            200, content=json.dumps({"type": "result", "data": {"content": content}}) + chr(10)
        )

    with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
        transport = AcaRuntimeProvider(
            provider_settings=ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        )
    transport._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(transport, "_get_access_token", AsyncMock(return_value="aad-token"))
    monkeypatch.setitem(
        resolution_module.BACKEND_FACTORIES,
        "aca",
        lambda: AcaRunnerBackend(transport_factory=lambda _options: transport),
    )
    config = WorkflowConfig.model_validate(
        {
            "workflow": {"name": "aca-grader", "entry_point": "reviewer"},
            "agents": [
                {
                    "name": "reviewer",
                    "prompt": "review",
                    "output": {"answer": {"type": "string"}},
                    "validator": {"criteria": "Accurate"},
                    "sandbox": {"working_dir": "/workspace"},
                    "execution": {"profile": "remote"},
                }
            ],
            "output": {"answer": "{{ reviewer.output.answer }}"},
        }
    )
    environment = ResolvedEnvironment(
        document=EnvironmentDocument(
            default="remote",
            profiles={
                "remote": ProfileDefinition(
                    backend="aca", aca=AcaProfileOptions(pool_endpoint="https://pool.example.com")
                )
            },
        ),
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=lambda *_: {"answer": "host"}),
        execution_environment=environment,
    )
    with patch.dict("os.environ", {"COPILOT_GITHUB_TOKEN": "host-token"}, clear=True):
        result = await engine.run({})
    assert result == {"answer": "remote"}
    assert working_dirs == ["/workspace", "/workspace"]


@pytest.mark.asyncio
async def test_agent_secret_grader_and_retry_reach_actual_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a remote grader runs without the primary secret overlay, then retries in-realm.
    calls: list[tuple[str, dict[str, Any] | None, str]] = []

    class ProviderSpy:
        def __init__(
            self,
            *,
            mcp_servers: dict[str, Any] | None,
            provider_settings: ProviderSettings | None,
            tool_output: Any,
            github_token: str | None,
            temperature: float | None,
            resolve_environment: bool = True,
        ) -> None:
            assert github_token == "model-token"
            assert resolve_environment is False
            self.mcp_servers = mcp_servers

        async def execute(
            self, agent: AgentDef, context: dict[str, Any], prompt: str, **kwargs: Any
        ) -> AgentOutput:
            calls.append((agent.name, self.mcp_servers, prompt))
            if agent.name.endswith(" (validator)"):
                return AgentOutput(
                    content={"passed": False, "issues": ["revise"]}, raw_response=None
                )
            answer = "revised" if "Validation feedback" in prompt else "draft"
            return AgentOutput(content={"answer": answer}, raw_response=None)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(server, "CopilotProvider", ProviderSpy)
    monkeypatch.delenv("ACA_RUNNER_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CONDUCTOR_REALM_GRADER_TOKEN", "tool-secret")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "model-token")
    app = server.create_app()
    config = WorkflowConfig.model_validate(
        {
            "workflow": {
                "name": "grader-with-secret",
                "entry_point": "reviewer",
                "runtime": {
                    "provider": "copilot",
                    "mcp_servers": {"echo": {"command": sys.executable}},
                },
            },
            "agents": [
                {
                    "name": "reviewer",
                    "prompt": "Review",
                    "output": {"answer": {"type": "string"}},
                    "validator": {"criteria": "Improve answer", "max_retries": 1},
                    "execution": {
                        "profile": "remote",
                        "secrets": [
                            {
                                "ref": "tool_token",
                                "scope": "agent",
                                "delivery": {"env": "MCP_TOKEN"},
                            }
                        ],
                    },
                }
            ],
            "output": {"answer": "{{ reviewer.output.answer }}"},
        }
    )
    environment = ResolvedEnvironment(
        document=EnvironmentDocument(
            default="remote",
            profiles={
                "remote": ProfileDefinition(
                    backend="aca", aca=AcaProfileOptions(pool_endpoint="https://pool.example.com")
                )
            },
            secrets={
                "tool_token": SecretBinding(
                    source=SecretBindingSource(env="CONDUCTOR_REALM_GRADER_TOKEN"),
                    allow=["agent"],
                )
            },
        ),
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )
    events: list[WorkflowEvent] = []
    emitter = WorkflowEventEmitter()
    emitter.subscribe(events.append)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://pool.example.com"
        ) as client,
    ):
        with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
            transport = AcaRuntimeProvider(
                provider_settings=ProviderSettings(
                    name="aca", pool_endpoint="https://pool.example.com"
                )
            )
        transport._http_client = client
        monkeypatch.setattr(transport, "_get_access_token", AsyncMock(return_value="aad-token"))
        monkeypatch.setitem(
            resolution_module.BACKEND_FACTORIES,
            "aca",
            lambda: AcaRunnerBackend(transport_factory=lambda _options: transport),
        )
        engine = WorkflowEngine(config, execution_environment=environment, event_emitter=emitter)
        result = await engine.run({})

    assert result == {"answer": "revised"}
    assert [name for name, _servers, _prompt in calls] == [
        "reviewer",
        "reviewer (validator)",
        "reviewer",
    ]
    assert calls[0][1] is not None
    assert calls[0][1]["echo"]["env"]["MCP_TOKEN"] == "tool-secret"
    assert calls[1][1] is None
    assert calls[2][1] is not None
    assert calls[2][1]["echo"]["env"]["MCP_TOKEN"] == "tool-secret"
    completed = next(event for event in events if event.type == "agent_validator_complete")
    assert completed.data["passed"] is False
    assert completed.data["errored"] is False
