"""ACA realm keeps a primary agent and its synthetic grader in one sandbox directory."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import conductor.engine.execution_resolution as resolution_module
from conductor.config.environment import (
    AcaProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
)
from conductor.config.schema import ProviderSettings, WorkflowConfig
from conductor.engine.aca_execution import AcaRunnerBackend
from conductor.engine.workflow import WorkflowEngine
from conductor.providers.aca import AcaRuntimeProvider
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
