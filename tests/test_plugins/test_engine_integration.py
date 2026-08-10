"""Plugin resolution through a real :class:`WorkflowEngine` (issue #378).

An :class:`~conductor.executor.agent.AgentExecutor` built directly in a
test is handed ``workflow_plugins`` and ``workflow_dir`` by the test
itself, so it cannot detect the engine failing to supply either. The
whole feature would silently degrade to "``runtime.plugins`` does
nothing" with every other plugin test still green.

Same reasoning as ``tests/test_skills/test_engine_integration.py``,
applied to the field that carries subagents and MCP servers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from conductor.config.schema import (
    AgentDef,
    OutputField,
    PluginDef,
    RuntimeConfig,
    WorkflowConfig,
    WorkflowDef,
)
from conductor.engine.workflow import WorkflowEngine
from conductor.plugins.errors import PluginNotFoundError
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.capabilities import ProviderCapabilities

from .conftest import make_plugin

_CAPS = ProviderCapabilities(
    tier="stable",
    mcp_tools=True,
    workflow_tools_passthrough=True,
    streaming_events=True,
    agent_reasoning_events=True,
    reasoning_effort=None,
    structured_output="native",
    interrupt=True,
    max_session_seconds=True,
    checkpoint_resume=True,
    usage_tracking=True,
    concurrent_safe=True,
    skills=True,
    plugins=True,
)


class _CapturingProvider(AgentProvider, abstract=True):
    """Records what the executor forwarded on the last ``execute`` call."""

    CAPABILITIES = _CAPS

    def __init__(self) -> None:
        self.skill_directories: list[str] | None = None
        self.custom_agents: list[dict[str, Any]] | None = None
        self.extra_mcp_servers: dict[str, Any] | None = None

    @property
    def supports_native_skills(self) -> bool:
        return True

    @property
    def supports_native_plugins(self) -> bool:
        return True

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        rendered_prompt: str,
        tools: list[str] | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
        skill_directories: list[str] | None = None,
        custom_agents: list[dict[str, Any]] | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
    ) -> AgentOutput:
        self.skill_directories = skill_directories
        self.custom_agents = custom_agents
        self.extra_mcp_servers = extra_mcp_servers
        return AgentOutput(content={"result": "done"}, raw_response="{}")

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def _config(
    plugins: list[PluginDef],
    agent_plugins: list[PluginDef] | None = None,
) -> WorkflowConfig:
    return WorkflowConfig(
        workflow=WorkflowDef(
            name="wf",
            entry_point="worker",
            runtime=RuntimeConfig(provider="copilot", plugins=plugins),
        ),
        agents=[
            AgentDef(
                name="worker",
                prompt="Do the thing.",
                output={"result": OutputField(type="string")},
                plugins=agent_plugins,
            )
        ],
        output={"result": "{{ worker.output.result }}"},
    )


class _StubRegistry:
    """Minimal stand-in for :class:`~conductor.providers.registry.ProviderRegistry`.

    ``conductor run`` always constructs the engine with ``registry=``
    (``cli/run.py``), never with a bare ``provider=`` — so the registry
    branch of ``_get_executor_for_agent`` is the one production takes.
    Only exercising the single-provider branch let the registry branch's
    ``workflow_plugins=`` be deleted with the whole suite still green.
    """

    def __init__(self, provider: AgentProvider) -> None:
        self._provider = provider

    async def get_provider(self, agent: object) -> AgentProvider:
        return self._provider

    async def close(self) -> None:
        return None


def _run(
    config: WorkflowConfig,
    provider: AgentProvider,
    workflow_path: Path | None,
    *,
    via_registry: bool = False,
) -> None:
    if via_registry:
        engine = WorkflowEngine(
            config, registry=_StubRegistry(provider), workflow_path=workflow_path
        )
    else:
        engine = WorkflowEngine(config, provider, workflow_path=workflow_path)
    asyncio.run(engine.run({}))


def _workflow_file(tmp_path: Path) -> Path:
    path = tmp_path / "wf.yaml"
    path.write_text("# placeholder\n")
    return path


class TestRuntimePluginsReachTheProvider:
    def test_engine_supplies_workflow_plugins(self, tmp_path: Path) -> None:
        """Fails if the engine stops threading ``runtime.plugins`` through."""
        make_plugin(
            tmp_path / "prs",
            "prs",
            skills=["review"],
            agents=["code-reviewer"],
            mcp={"srv": {"type": "stdio", "command": "npx"}},
        )
        provider = _CapturingProvider()
        _run(_config([PluginDef(name="./prs")]), provider, _workflow_file(tmp_path))
        assert provider.skill_directories == [str(tmp_path / "prs" / "skills" / "review")]
        assert [spec["name"] for spec in provider.custom_agents or []] == ["prs:code-reviewer"]
        assert list(provider.extra_mcp_servers or {}) == ["srv"]

    def test_relative_path_resolves_against_the_workflow_file(self, tmp_path: Path) -> None:
        """Fails if the engine stops passing ``workflow_dir`` to the executor."""
        make_plugin(tmp_path / "tools" / "mine", "mine", agents=["helper"])
        provider = _CapturingProvider()
        _run(_config([PluginDef(name="./tools/mine")]), provider, _workflow_file(tmp_path))
        assert [spec["name"] for spec in provider.custom_agents or []] == ["mine:helper"]

    def test_resolution_does_not_depend_on_the_process_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_plugin(tmp_path / "flows" / "p", "p", agents=["helper"])
        path = tmp_path / "flows" / "wf.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# placeholder\n")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        provider = _CapturingProvider()
        _run(_config([PluginDef(name="./p")]), provider, path)
        assert [spec["name"] for spec in provider.custom_agents or []] == ["p:helper"]

    def test_agent_level_override_wins(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "wide", "wide", agents=["from-workflow"])
        make_plugin(tmp_path / "narrow", "narrow", agents=["from-agent"])
        provider = _CapturingProvider()
        _run(
            _config([PluginDef(name="./wide")], agent_plugins=[PluginDef(name="./narrow")]),
            provider,
            _workflow_file(tmp_path),
        )
        assert [spec["name"] for spec in provider.custom_agents or []] == ["narrow:from-agent"]

    def test_agent_opt_out_wins(self, tmp_path: Path) -> None:
        make_plugin(tmp_path / "p", "p", agents=["helper"])
        provider = _CapturingProvider()
        _run(
            _config([PluginDef(name="./p")], agent_plugins=[]),
            provider,
            _workflow_file(tmp_path),
        )
        assert provider.custom_agents is None

    def test_no_plugins_forwards_nothing(self, tmp_path: Path) -> None:
        provider = _CapturingProvider()
        _run(_config([]), provider, _workflow_file(tmp_path))
        assert provider.custom_agents is None
        assert provider.extra_mcp_servers is None


class TestFailuresSurfaceThroughTheEngine:
    def test_missing_plugin_fails_the_run(self, tmp_path: Path) -> None:
        provider = _CapturingProvider()
        with pytest.raises(PluginNotFoundError, match="does not exist"):
            _run(_config([PluginDef(name="./gone")]), provider, _workflow_file(tmp_path))


@pytest.mark.parametrize("via_registry", [False, True], ids=["single-provider", "registry"])
class TestBothEngineProviderModes:
    """Both engine branches must thread ``runtime.plugins`` through.

    Parametrized rather than duplicated because the *registry* branch is
    the one ``conductor run`` uses, and it was previously uncovered — its
    ``workflow_plugins=`` argument could be deleted with 5107 tests still
    passing.
    """

    def test_plugins_reach_the_provider(self, tmp_path: Path, via_registry: bool) -> None:
        make_plugin(
            tmp_path / "prs",
            "prs",
            skills=["review"],
            agents=["code-reviewer"],
            mcp={"srv": {"type": "stdio", "command": "npx"}},
        )
        provider = _CapturingProvider()
        _run(
            _config([PluginDef(name="./prs")]),
            provider,
            _workflow_file(tmp_path),
            via_registry=via_registry,
        )
        assert provider.skill_directories == [str(tmp_path / "prs" / "skills" / "review")]
        assert [spec["name"] for spec in provider.custom_agents or []] == ["prs:code-reviewer"]
        assert list(provider.extra_mcp_servers or {}) == ["srv"]

    def test_agent_override_reaches_the_provider(self, tmp_path: Path, via_registry: bool) -> None:
        make_plugin(tmp_path / "wide", "wide", agents=["from-workflow"])
        make_plugin(tmp_path / "narrow", "narrow", agents=["from-agent"])
        provider = _CapturingProvider()
        _run(
            _config([PluginDef(name="./wide")], agent_plugins=[PluginDef(name="./narrow")]),
            provider,
            _workflow_file(tmp_path),
            via_registry=via_registry,
        )
        assert [spec["name"] for spec in provider.custom_agents or []] == ["narrow:from-agent"]
