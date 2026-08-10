"""Tests that plugin components reach each native provider's SDK options.

The executor→provider seam is covered in ``test_executor_integration``.
These cover the last hop: that each provider actually puts the components
onto the options object its SDK reads, rather than accepting them and
dropping them — which would look identical from the executor's side.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import ProviderError
from conductor.providers.base import AgentProvider
from conductor.providers.copilot import CopilotProvider

from .conftest import PLUGIN_CAPABLE_CAPS

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

        agents = _build_sdk_agents(
            [{"name": "prs:code-reviewer", "description": "Reviews code.", "prompt": "Review."}]
        )
        assert list(agents) == ["prs:code-reviewer"]
        definition = agents["prs:code-reviewer"]
        assert definition.description == "Reviews code."
        assert definition.prompt == "Review."
        assert definition.tools is None

    def test_foreign_tool_vocabulary_is_refused(self) -> None:
        # A plugin's `tools:` is written in its authoring CLI's vocabulary
        # (`read`, `execute`), which does not translate to Claude CLI tool
        # IDs. Forwarding it hands the subagent no valid identifier;
        # dropping it silently widens the agent. Both are wrong.
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import _build_sdk_agents

        with pytest.raises(ProviderError, match="do not translate to Claude CLI"):
            _build_sdk_agents(AGENT_SPECS)

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


class TestClaudeAgentSdkDelivery:
    """The last hop: components must reach ``ClaudeAgentOptions``, not just translate.

    Testing ``_build_sdk_agents`` alone cannot tell a provider that wires
    the result onto the options object from one that builds it and drops
    it — which is exactly the silent partial load plugins exist to remove.
    """

    async def test_plugin_agents_reach_the_options_object(self) -> None:
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        options_mock = MagicMock()

        async def fake_query(**kwargs: Any) -> Any:
            if False:
                yield None

        with (
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider()
            await provider.execute(
                agent=AgentDef(name="a", prompt="hi"),
                context={},
                rendered_prompt="hi",
                custom_agents=[
                    {"name": "prs:code-reviewer", "description": "Reviews.", "prompt": "Review."}
                ],
            )

        agents = options_mock.call_args[1]["agents"]
        assert list(agents) == ["prs:code-reviewer"]
        assert agents["prs:code-reviewer"].prompt == "Review."

    async def test_no_plugin_agents_leaves_the_option_unset(self) -> None:
        # ``None`` rather than ``{}``: an empty mapping has no opt-out
        # meaning here, so the option stays out of the request entirely.
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        options_mock = MagicMock()

        async def fake_query(**kwargs: Any) -> Any:
            if False:
                yield None

        with (
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
            patch("conductor.providers.claude_agent_sdk.ClaudeAgentOptions", options_mock),
        ):
            provider = ClaudeAgentSdkProvider()
            await provider.execute(
                agent=AgentDef(name="a", prompt="hi"), context={}, rendered_prompt="hi"
            )

        assert options_mock.call_args[1]["agents"] is None

    async def test_mcp_name_collision_is_refused(self) -> None:
        # The only layer that runs during `conductor run` on this provider.
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        async def fake_query(**kwargs: Any) -> Any:
            if False:
                yield None

        with (
            patch("conductor.providers.claude_agent_sdk.CLAUDE_AGENT_SDK_AVAILABLE", True),
            patch("conductor.providers.claude_agent_sdk.query", fake_query),
        ):
            provider = ClaudeAgentSdkProvider(
                mcp_servers={"shared": {"type": "stdio", "command": "wf"}}
            )
            with pytest.raises(ProviderError, match="declared by both an enabled plugin"):
                await provider.execute(
                    agent=AgentDef(name="a", prompt="hi"),
                    context={},
                    rendered_prompt="hi",
                    extra_mcp_servers={"shared": {"type": "stdio", "command": "plugin"}},
                )

    def test_unknown_mcp_keys_are_refused(self) -> None:
        # A plugin's .mcp.json is arbitrary third-party JSON. Dropping a key
        # it declares (an `oauth` block, `disabled: true`) would start a
        # server configured differently from what its author wrote.
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import _translate_mcp_servers

        with pytest.raises(ProviderError, match="no equivalent for"):
            _translate_mcp_servers(
                {"enghub": {"type": "http", "url": "https://x", "oauth": {"clientId": "abc"}}}
            )

    def test_empty_tools_with_plugin_agents_is_refused(self) -> None:
        # `--tools ""` leaves no dispatch tool, so the subagents would be
        # registered and unreachable.
        pytest.importorskip("claude_agent_sdk")
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        with pytest.raises(ProviderError, match="no way to dispatch"):
            ClaudeAgentSdkProvider._resolve_tool_config(
                [],
                AgentDef(name="a", prompt="hi", tools=[]),
                skills_enabled=False,
                agents_enabled=True,
            )


class TestCopilotResumeCarriesSessionState:
    """A resumed session must not run with less than the workflow declared.

    Skills and subagents are session-scoped, so a resume that omits them
    degrades silently — and `skill_directories` on resume is new here, so
    this guards the pre-existing skills feature too.
    """

    async def test_resume_receives_skills_and_agents(self) -> None:
        provider = CopilotProvider()
        provider.set_resume_session_ids({"a": "sid-1"})
        provider.set_resume_session_cwds({"a": os.getcwd()})
        captured: dict[str, Any] = {}

        async def fake_resume(session_id: str, **kwargs: Any) -> Any:
            captured.update(kwargs)
            raise RuntimeError("stop after resume kwargs")

        client = MagicMock()
        client.resume_session = AsyncMock(side_effect=fake_resume)
        client.create_session = AsyncMock(side_effect=RuntimeError("stop"))
        provider._client = client
        provider._started = True

        agent = AgentDef(name="a", model="m", prompt="hi", retry={"max_attempts": 1})
        with (
            patch.object(provider, "_ensure_client_started", new=AsyncMock()),
            pytest.raises(ProviderError),
        ):
            await provider.execute(
                agent,
                {},
                "hi",
                skill_directories=["/tmp/skills/demo"],
                custom_agents=AGENT_SPECS,
            )

        assert captured["skill_directories"] == ["/tmp/skills/demo"]
        assert [spec["name"] for spec in captured["custom_agents"]] == ["prs:code-reviewer"]


class TestPluginCapabilityContractIsEnforced:
    """``plugins=True`` is a promise about three delivery channels.

    Declaring it while dropping one is the worst outcome available — it
    reinstates exactly the silent partial load issue #378 removed. So the
    declaration is checked rather than trusted.
    """

    def test_declaring_plugins_without_the_kwargs_fails_at_import(self) -> None:
        caps = PLUGIN_CAPABLE_CAPS

        with pytest.raises(TypeError, match="does not accept"):

            class _Broken(AgentProvider):
                CAPABILITIES = caps

                @property
                def supports_native_plugins(self) -> bool:
                    return True

                async def execute(  # type: ignore[override]
                    self,
                    agent: AgentDef,
                    context: dict[str, Any],
                    rendered_prompt: str,
                    tools: list[str] | None = None,
                    interrupt_signal: Any = None,
                    event_callback: Any = None,
                    skill_directories: list[str] | None = None,
                ) -> Any: ...

                async def validate_connection(self) -> bool: ...
                async def close(self) -> None: ...

    def test_declaring_plugins_without_the_property_fails_at_import(self) -> None:
        caps = PLUGIN_CAPABLE_CAPS

        with pytest.raises(TypeError, match="supports_native_plugins is falsy"):

            class _Mismatched(AgentProvider):
                CAPABILITIES = caps

                async def execute(  # type: ignore[override]
                    self,
                    agent: AgentDef,
                    context: dict[str, Any],
                    rendered_prompt: str,
                    tools: list[str] | None = None,
                    interrupt_signal: Any = None,
                    event_callback: Any = None,
                    skill_directories: list[str] | None = None,
                    custom_agents: list[dict[str, Any]] | None = None,
                    extra_mcp_servers: dict[str, Any] | None = None,
                ) -> Any: ...

                async def validate_connection(self) -> bool: ...
                async def close(self) -> None: ...

    @pytest.mark.parametrize(
        "provider_type", ["copilot", "claude-agent-sdk", "claude", "hermes", "aca"]
    )
    def test_declaration_and_mechanism_agree(self, provider_type: str) -> None:
        # Keeps the descriptor and the property from drifting apart, and is
        # what gives ``uses_native_plugins`` a production-shaped consumer.
        from conductor.providers.capabilities import get_capabilities, uses_native_plugins

        mechanism = uses_native_plugins(provider_type)
        if mechanism is None:
            pytest.skip(f"{provider_type} mechanism not statically resolvable")
        assert get_capabilities(provider_type).plugins is mechanism
