"""Run-scoped ACA resolution, static flip, and optional-SDK import safety."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from conductor.config.environment import (
    AcaProfileOptions,
    EnvironmentDocument,
    ProfileDefinition,
    ResolvedEnvironment,
)
from conductor.config.schema import AgentDef, ProviderSettings, WorkflowConfig
from conductor.config.validator import _EnvironmentValidationContext, validate_workflow_config
from conductor.engine.aca_execution import AcaRunnerBackend
from conductor.engine.execution_resolution import ExecutionResolver, ExecutionResolverSession
from conductor.engine.run_manifest import compile_run_manifest
from conductor.engine.workflow import WorkflowEngine
from conductor.exceptions import ConfigurationError, ProviderError
from conductor.execution.types import AgentSpec, RunSpec
from conductor.executor.agent import AgentExecutor
from conductor.providers.aca import AcaRuntimeProvider
from conductor.providers.copilot import CopilotProvider


def _environment(pools: dict[str, str]) -> ResolvedEnvironment:
    return ResolvedEnvironment(
        document=EnvironmentDocument(
            default=next(iter(pools)),
            profiles={
                name: ProfileDefinition(
                    backend="aca", aca=AcaProfileOptions(pool_endpoint=endpoint)
                )
                for name, endpoint in pools.items()
            },
        ),
        name="test",
        source="path",
        path=None,
        digest="sha256:test",
    )


def _agent_config() -> WorkflowConfig:
    return WorkflowConfig.model_validate(
        {
            "workflow": {"name": "aca-flip", "entry_point": "reviewer"},
            "agents": [
                {"name": "reviewer", "prompt": "review", "execution": {"profile": "remote"}}
            ],
        }
    )


@pytest.mark.asyncio
async def test_programmatic_aca_only_run_never_materializes_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: ACA lease preparation works without a workflow file or bundle I/O.
    import conductor.engine.execution_resolution as resolution_module

    async def forbidden_bundle(*args: object) -> None:
        pytest.fail("ACA must not materialize a run bundle")

    monkeypatch.setattr(resolution_module, "materialize_run_bundle", forbidden_bundle)
    session = ExecutionResolverSession(_environment({"remote": "https://pool.example.com"}))
    await session.prepare_leases(RunSpec("run-1"))
    await session.ensure_backends(["aca"])
    lease = session.lease_for_backend("aca")
    assert lease is not None and lease.backend == "aca"
    assert session._root_run_spec is not None and session._root_run_spec.bundle is None
    await session.finalize_leases("succeeded")


@pytest.mark.asyncio
async def test_programmatic_aca_workflow_executes_without_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: the actual engine prepares ACA and runs without a workflow file or bundle.
    import conductor.engine.bundle_prep as bundle_prep
    import conductor.engine.execution_resolution as resolution_module

    async def forbidden_bundle(*args: object) -> None:
        pytest.fail("ACA must not materialize a bundle")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ready": True, "protocol_version": 2})
        frame = {"type": "result", "data": {"content": {"answer": "remote"}}}
        return httpx.Response(200, content=json.dumps(frame) + chr(10))

    with patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", True):
        transport = AcaRuntimeProvider(
            provider_settings=ProviderSettings(name="aca", pool_endpoint="https://pool.example.com")
        )
    transport._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(transport, "_get_access_token", AsyncMock(return_value="aad-token"))
    backend = AcaRunnerBackend(transport_factory=lambda _options: transport)
    prepare = AsyncMock(wraps=backend.prepare_run)
    monkeypatch.setattr(backend, "prepare_run", prepare)
    monkeypatch.setitem(resolution_module.BACKEND_FACTORIES, "aca", lambda: backend)
    monkeypatch.setattr(resolution_module, "materialize_run_bundle", forbidden_bundle)
    monkeypatch.setattr(bundle_prep, "materialize_run_bundle", forbidden_bundle)
    config = _agent_config().model_copy(
        update={"output": {"answer": "{{ reviewer.output.answer }}"}}
    )
    engine = WorkflowEngine(
        config,
        CopilotProvider(mock_handler=lambda *_: {"answer": "host"}),
        execution_environment=_environment({"remote": "https://pool.example.com"}),
    )
    with patch.dict("os.environ", {"COPILOT_GITHUB_TOKEN": "host-token"}, clear=True):
        result = await engine.run({})
    assert result == {"answer": "remote"}
    assert prepare.await_count == 1


@pytest.mark.asyncio
async def test_distinct_aca_profiles_keep_separate_pool_options() -> None:
    # Requirement: two ACA profiles retain their own pool while sharing one run lease.
    session = ExecutionResolverSession(
        _environment({"east": "https://east.test", "west": "https://west.test"})
    )
    await session.prepare_leases(RunSpec("run-1"))
    await session.ensure_backends(["aca"])
    east = session._aca_profiles["east"]
    west = session._aca_profiles["west"]
    assert isinstance(east, AcaRunnerBackend)
    assert isinstance(west, AcaRunnerBackend)
    assert east.options is not None and east.options.pool_endpoint == "https://east.test"
    assert west.options is not None and west.options.pool_endpoint == "https://west.test"
    assert east._leases is west._leases
    await session.finalize_leases("succeeded")


@pytest.mark.asyncio
async def test_aca_step_uses_its_named_pool_profile() -> None:
    # Requirement: resolution dispatches each agent to its selected ACA pool.
    config = WorkflowConfig.model_validate(
        {
            "workflow": {"name": "pools", "entry_point": "east_agent"},
            "agents": [
                {"name": "east_agent", "prompt": "east", "execution": {"profile": "east"}},
                {"name": "west_agent", "prompt": "west", "execution": {"profile": "west"}},
            ],
        }
    )
    session = ExecutionResolverSession(
        _environment({"east": "https://east.test", "west": "https://west.test"})
    )
    await session.prepare_leases(RunSpec("run-1"))
    await session.ensure_backends(["aca"])
    resolver = ExecutionResolver(config, session, workflow_path=None, publish_manifest=False)
    assert resolver.backend_for_step("east_agent") is session._aca_profiles["east"]
    assert resolver.backend_for_step("west_agent") is session._aca_profiles["west"]
    await session.finalize_leases("succeeded")


def test_aca_factory_and_descriptor_import_without_azure_identity() -> None:
    # Requirement: capability lookup and factory construction never import the optional SDK.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['azure'] = None; "
            "from conductor.engine.run_manifest import BACKEND_CAPABILITY_PROVIDERS; "
            "from conductor.engine.execution_resolution import BACKEND_FACTORIES; "
            "assert BACKEND_CAPABILITY_PROVIDERS['aca'].capabilities().agent; "
            "assert BACKEND_FACTORIES['aca']().capabilities().agent",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_aca_missing_extra_fails_only_on_first_agent_use() -> None:
    # Requirement: profile resolution stays offline; missing Azure SDK fails on dispatch.
    session = ExecutionResolverSession(_environment({"remote": "https://pool.example.com"}))
    await session.prepare_leases(RunSpec("run-1"))
    await session.ensure_backends(["aca"])
    backend = session._aca_profiles["remote"]
    lease = session.lease_for_backend("aca")
    spec = AgentSpec(
        name="reviewer",
        execution_id="call-1",
        model_provider="copilot",
        model=None,
        rendered_prompt="review",
    )
    with (
        patch("conductor.providers.aca.AZURE_IDENTITY_AVAILABLE", False),
        pytest.raises(ProviderError) as exc_info,
    ):
        await backend.run_agent(spec, lease)
    assert exc_info.value.suggestion is not None
    await session.finalize_leases("failed")


def test_aca_flip_agent_profile_passes_static_validation_and_compile() -> None:
    # Requirement: the capability flip accepts agent execution on an ACA profile.
    config = _agent_config()
    environment = _environment({"remote": "https://pool.example.com"})
    context: _EnvironmentValidationContext = {
        "refs_found": True,
        "environments": {"test": environment},
        "explicit": True,
        "root_workflow_dir": None,
        "warned_no_environments": False,
        "warned_no_secret_environments": False,
    }
    assert validate_workflow_config(config, _environment_context=context) == []
    manifest = compile_run_manifest(config, workflow_path=None, environment=environment)
    assert manifest.profiles["reviewer"].backend == "aca"


def test_aca_flip_bare_validate_accepts_discovered_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: bare validate accepts an agent profile discovered without --environment.
    environment = _environment({"remote": "https://pool.example.com"})
    monkeypatch.setattr(
        "conductor.config.environment.discover_all_environments",
        lambda *_args, **_kwargs: {"test": environment},
    )
    validate_workflow_config(_agent_config(), workflow_path=tmp_path / "workflow.yaml")


def test_aca_sandbox_directory_is_explicit_and_host_dir_not_forwarded() -> None:
    # Requirement: only ACA uses the container-relative sandbox directory.
    agent = AgentDef.model_validate(
        {
            "name": "reviewer",
            "prompt": "review",
            "working_dir": "/host/project",
            "sandbox": {"working_dir": "/workspace"},
            "validator": {"criteria": "Accurate"},
        }
    )
    executor = AgentExecutor(CopilotProvider(mock_handler=lambda *_: {"answer": "ok"}))
    aca_spec = executor.build_realm_spec(agent, "review", {}, backend_name="aca")
    docker_spec = executor.build_realm_spec(agent, "review", {}, backend_name="docker")
    assert aca_spec.working_dir == "/workspace"
    assert docker_spec.working_dir == "/host/project"
    without_sandbox = agent.model_copy(update={"sandbox": None})
    assert (
        executor.build_realm_spec(without_sandbox, "review", {}, backend_name="aca").working_dir
        is None
    )


def test_aca_profile_rejects_agent_level_identifier_scope() -> None:
    # Requirement: ACA profile scope cannot be overridden per agent in the new form.
    agent = AgentDef.model_validate(
        {
            "name": "reviewer",
            "prompt": "review",
            "sandbox": {"identifier_scope": "item"},
        }
    )
    executor = AgentExecutor(CopilotProvider(mock_handler=lambda *_: {"answer": "ok"}))
    with pytest.raises(ConfigurationError):
        executor.build_realm_spec(agent, "review", {}, backend_name="aca")
