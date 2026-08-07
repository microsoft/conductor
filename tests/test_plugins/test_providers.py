"""Tests that plugin components reach each native provider's SDK options.

The executor→provider seam is covered in ``test_executor_integration``.
These cover the last hop: that each provider actually puts the components
onto the options object its SDK reads, rather than accepting them and
dropping them — which would look identical from the executor's side.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError
from conductor.providers.copilot import CopilotProvider

AGENT_SPECS: list[dict[str, Any]] = [
    {
        "name": "prs:code-reviewer",
        "description": "Reviews code.",
        "prompt": "You are a reviewer.",
        "infer": True,
        "tools": ["read"],
    }
]
PLUGIN_SERVERS: dict[str, Any] = {"plugin-srv": {"type": "stdio", "command": "npx"}}


class TestCopilotSessionKwargs:
    """Copilot registers each component individually on the session.

    ``plugin_directories`` — the SDK's whole-plugin surface — is
    deliberately unused: ``excluded_tools`` hides an MCP tool from the
    model but does not stop the server subprocess from launching, so a
    registered root cannot honour ``mcp: false``.
    """

    def _session_kwargs(self, **execute_kwargs: Any) -> dict[str, Any]:
        provider = CopilotProvider(mcp_servers={"workflow-srv": {"type": "stdio", "command": "wf"}})
        captured: dict[str, Any] = {}

        async def fake_create_session(**kwargs: Any) -> Any:
            captured.update(kwargs)
            raise RuntimeError("stop after session construction")

        client = MagicMock()
        client.create_session = AsyncMock(side_effect=fake_create_session)
        provider._client = client
        provider._started = True

        agent = AgentDef(
            name="a",
            model="m",
            prompt="hi",
            output={"result": OutputField(type="string")},
            retry={"max_attempts": 1},
        )
        with (
            patch.object(provider, "_ensure_client_started", new=AsyncMock()),
            pytest.raises(ProviderError),
        ):
            asyncio.run(provider.execute(agent, {}, "hi", **execute_kwargs))
        return captured

    def test_custom_agents_are_registered(self) -> None:
        kwargs = self._session_kwargs(custom_agents=AGENT_SPECS)
        assert kwargs["custom_agents"] == AGENT_SPECS

    def test_qualified_agent_name_is_preserved(self) -> None:
        # Verified against a live session: the SDK lists `myplug:quokka`
        # among launchable agent types, so the namespace survives.
        kwargs = self._session_kwargs(custom_agents=AGENT_SPECS)
        assert kwargs["custom_agents"][0]["name"] == "prs:code-reviewer"

    def test_plugin_mcp_servers_merge_with_workflow_servers(self) -> None:
        kwargs = self._session_kwargs(extra_mcp_servers=PLUGIN_SERVERS)
        assert set(kwargs["mcp_servers"]) == {"workflow-srv", "plugin-srv"}

    def test_plugin_servers_are_stamped_with_the_working_directory(self) -> None:
        # A plugin's stdio server must pick up the agent's cwd exactly as a
        # workflow-declared one does.
        kwargs = self._session_kwargs(extra_mcp_servers=PLUGIN_SERVERS)
        assert "working_directory" in kwargs["mcp_servers"]["plugin-srv"]

    def test_name_collision_is_refused_not_silently_resolved(self) -> None:
        # Dropping one of the two would leave a declared component
        # unreachable, which is the failure plugins exist to remove.
        # `conductor validate` reports the same clash, but `conductor run`
        # never invokes the static validator.
        provider = CopilotProvider(mcp_servers={"workflow-srv": {"type": "stdio", "command": "wf"}})
        with pytest.raises(ProviderError, match="declared by both an enabled plugin"):
            provider._merge_mcp_servers(
                "/tmp", {"workflow-srv": {"type": "stdio", "command": "plugin"}}
            )

    def test_nothing_registered_when_no_plugins(self) -> None:
        kwargs = self._session_kwargs()
        assert "custom_agents" not in kwargs
        assert set(kwargs["mcp_servers"]) == {"workflow-srv"}

    def test_plugin_directories_is_never_used(self) -> None:
        kwargs = self._session_kwargs(custom_agents=AGENT_SPECS, extra_mcp_servers=PLUGIN_SERVERS)
        assert "plugin_directories" not in kwargs

    def test_provider_declares_plugin_support(self) -> None:
        assert CopilotProvider().supports_native_plugins is True
        assert CopilotProvider.CAPABILITIES.plugins is True


class TestClaudeAgentSdkAgents:
    """Plugin subagents become inline ``AgentDefinition`` objects."""

    def test_specs_translate_to_agent_definitions(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import _build_sdk_agents

        agents = _build_sdk_agents(AGENT_SPECS)
        assert list(agents) == ["prs:code-reviewer"]
        definition = agents["prs:code-reviewer"]
        assert definition.description == "Reviews code."
        assert definition.prompt == "You are a reviewer."
        assert definition.tools == ["read"]

    def test_absent_tools_stay_none(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import _build_sdk_agents

        agents = _build_sdk_agents([{"name": "p:a", "description": "d", "prompt": "p"}])
        assert agents["p:a"].tools is None

    def test_no_agents_yields_none_not_empty(self) -> None:
        # Unlike `skills`, an empty mapping has no opt-out meaning here, so
        # the option must stay out of the request entirely.
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import _build_sdk_agents

        assert _build_sdk_agents(None) is None
        assert _build_sdk_agents([]) is None

    def test_provider_declares_plugin_support(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        assert ClaudeAgentSdkProvider.CAPABILITIES.plugins is True


class TestNonNativeProvidersDeclareNoPluginSupport:
    """A provider that cannot deliver every component must say so.

    An inaccurate ``True`` silently drops plugin content at run time; the
    validator trusts this flag.
    """

    def test_claude(self) -> None:
        from conductor.providers.claude import ClaudeProvider

        assert ClaudeProvider.CAPABILITIES.plugins is False

    def test_hermes(self) -> None:
        from conductor.providers.hermes import HermesProvider

        assert HermesProvider.CAPABILITIES.plugins is False

    def test_aca(self) -> None:
        from conductor.providers.aca import AcaRuntimeProvider

        assert AcaRuntimeProvider.CAPABILITIES.plugins is False
