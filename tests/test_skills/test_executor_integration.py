"""Tests for skill resolution inside :class:`AgentExecutor`.

Covers both provider variants of the parity contract:

* Native-skill providers (``supports_native_skills = True``) skip eager
  preamble injection — skill content is loaded by the SDK.
* Non-native providers eagerly inject ``SKILL.md`` + ``references/*.md``
  into the rendered prompt.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from conductor.config.schema import AgentDef
from conductor.executor.agent import AgentExecutor
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.copilot import CopilotProvider
from conductor.skills import get_skill_directory


class _StubNonNativeProvider(AgentProvider, abstract=True):
    """Provider stub that does NOT support native skill loading.

    Exercises the eager preamble injection path the same way Claude
    does today. Uses ``abstract=True`` to opt out of the
    :class:`ProviderCapabilities` declaration enforced on production
    providers — this is a test fake, not a real provider.
    """

    captured: list[str] | None = None

    @property
    def supports_native_skills(self) -> bool:
        return False

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
        self.captured = skill_directories
        return AgentOutput(content={"echo": rendered_prompt}, raw_response=rendered_prompt)

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class TestCopilotProviderNativeSkills:
    """Copilot owns native ``skill_directories``; preamble is NOT injected."""

    def test_no_skill_content_in_rendered_prompt(self) -> None:
        provider = CopilotProvider()
        executor = AgentExecutor(provider, workflow_skills=["conductor"])
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello world")
        rendered = executor.render_prompt(agent, {})
        assert "<skills>" not in rendered
        assert '<skill name="conductor">' not in rendered
        assert "Hello world" in rendered

    def test_provider_advertises_native_support(self) -> None:
        assert CopilotProvider().supports_native_skills is True


class _CapturingNativeProvider(AgentProvider, abstract=True):
    """Native-skill provider stub that records what the executor forwards.

    The negative "no <skills> in the prompt" assertions cannot tell a
    working native path from one that dropped the skills entirely, so this
    captures the positive side.
    """

    @property
    def supports_native_skills(self) -> bool:
        return True

    def __init__(self) -> None:
        self.captured: list[str] | None = None

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
        self.captured = skill_directories
        return AgentOutput(content={"ok": True}, raw_response="ok")

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class TestSkillDirectoriesReachTheProvider:
    """The executor -> provider seam that native skill loading rides on.

    Without these, `skill_directories=None` (or a dropped
    `supports_native_skills` check) suppresses every skill while the whole
    suite stays green -- the exact silent-drop failure #352 was about.
    """

    @staticmethod
    def _run(provider: _CapturingNativeProvider, agent: AgentDef) -> None:
        executor = AgentExecutor(provider, workflow_skills=["conductor"])
        asyncio.run(executor.execute(agent, {}))

    def test_workflow_default_reaches_provider(self) -> None:
        provider = _CapturingNativeProvider()
        self._run(provider, AgentDef(name="a", model="m", prompt="p"))
        assert provider.captured == [str(get_skill_directory("conductor"))]

    def test_agent_list_reaches_provider(self) -> None:
        provider = _CapturingNativeProvider()
        executor = AgentExecutor(provider, workflow_skills=[])
        agent = AgentDef(name="a", model="m", prompt="p", skills=["conductor"])
        asyncio.run(executor.execute(agent, {}))
        assert provider.captured == [str(get_skill_directory("conductor"))]

    def test_agent_opt_out_reaches_provider_as_no_dirs(self) -> None:
        provider = _CapturingNativeProvider()
        self._run(provider, AgentDef(name="a", model="m", prompt="p", skills=[]))
        assert not provider.captured

    def test_non_native_provider_gets_no_directories(self) -> None:
        """Forwarding to a non-native provider would double-load the skill on
        top of the eager injection it already received."""
        provider = _StubNonNativeProvider()
        executor = AgentExecutor(provider, workflow_skills=["conductor"])
        asyncio.run(executor.execute(AgentDef(name="a", model="m", prompt="p"), {}))
        assert provider.captured is None


class TestPathSkillsReachTheProvider:
    """Path entries ride the same executor -> provider seam as built-in names
    (issue #350). ``workflow_dir`` is the only thing that makes a relative
    entry resolvable, so a dropped constructor argument would silently turn
    every team-local skill into a resolution error."""

    @staticmethod
    def _make_skill(directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {directory.name}\ndescription: A test skill.\n---\nBody text\n"
        )
        return directory

    def test_absolute_path_reaches_provider(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path / "acme")
        provider = _CapturingNativeProvider()
        executor = AgentExecutor(provider, workflow_skills=[str(skill)])
        asyncio.run(executor.execute(AgentDef(name="a", model="m", prompt="p"), {}))
        assert provider.captured == [str(skill)]

    def test_relative_path_resolves_against_workflow_dir(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path / "team-skills" / "acme")
        provider = _CapturingNativeProvider()
        executor = AgentExecutor(
            provider, workflow_skills=["./team-skills/acme"], workflow_dir=tmp_path
        )
        asyncio.run(executor.execute(AgentDef(name="a", model="m", prompt="p"), {}))
        assert provider.captured == [str(skill)]

    def test_skills_root_expands_before_reaching_provider(self, tmp_path: Path) -> None:
        """Conductor expands a root itself so every provider — including the
        eager-injection ones, which need a name per skill — sees the same set."""
        root = tmp_path / "skills"
        for name in ("beta", "alpha"):
            self._make_skill(root / name)
        provider = _CapturingNativeProvider()
        executor = AgentExecutor(provider, workflow_skills=[str(root)])
        asyncio.run(executor.execute(AgentDef(name="a", model="m", prompt="p"), {}))
        assert provider.captured == [str(root / "alpha"), str(root / "beta")]

    def test_path_skill_content_is_eagerly_injected(self, tmp_path: Path) -> None:
        skill = self._make_skill(tmp_path / "acme")
        provider = _StubNonNativeProvider()
        executor = AgentExecutor(provider, workflow_skills=["./acme"], workflow_dir=tmp_path)
        output = asyncio.run(executor.execute(AgentDef(name="a", model="m", prompt="p"), {}))
        prompt = output.content["echo"]
        assert '<skill name="acme">' in prompt
        assert "Body text" in prompt
        assert skill.name in prompt


class TestClaudeAgentSdkNativeSkills:
    """claude-agent-sdk loads skills through the SDK, not the prompt."""

    def setup_method(self) -> None:
        pytest.importorskip("claude_agent_sdk", reason="claude-agent-sdk extra not installed")

    def test_no_skill_content_in_rendered_prompt(self) -> None:
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        executor = AgentExecutor(ClaudeAgentSdkProvider(), workflow_skills=["conductor"])
        agent = AgentDef(name="a", model="claude-sonnet-4-5", prompt="Hello world")
        rendered = executor.render_prompt(agent, {})
        assert "<skills>" not in rendered
        assert '<skill name="conductor">' not in rendered
        assert "Hello world" in rendered

    def test_provider_advertises_native_support(self) -> None:
        from conductor.providers.claude_agent_sdk import ClaudeAgentSdkProvider

        assert ClaudeAgentSdkProvider().supports_native_skills is True


class TestNonNativeProviderEagerInjection:
    """Non-native providers receive skill content via the rendered prompt."""

    def test_not_injected_when_no_skills(self) -> None:
        executor = AgentExecutor(_StubNonNativeProvider())
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello world")
        rendered = executor.render_prompt(agent, {})
        assert "<skills>" not in rendered
        assert "Hello world" in rendered

    def test_injected_when_agent_lists_skill(self) -> None:
        executor = AgentExecutor(_StubNonNativeProvider())
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello world", skills=["conductor"])
        rendered = executor.render_prompt(agent, {})
        assert "<skills>" in rendered
        assert '<skill name="conductor">' in rendered
        assert "Hello world" in rendered

    def test_injected_when_workflow_default(self) -> None:
        executor = AgentExecutor(_StubNonNativeProvider(), workflow_skills=["conductor"])
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello world")
        rendered = executor.render_prompt(agent, {})
        assert "<skills>" in rendered

    def test_agent_empty_list_opts_out_of_workflow_default(self) -> None:
        executor = AgentExecutor(_StubNonNativeProvider(), workflow_skills=["conductor"])
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello world", skills=[])
        rendered = executor.render_prompt(agent, {})
        assert "<skills>" not in rendered

    def test_agent_overrides_workflow_default(self) -> None:
        executor = AgentExecutor(_StubNonNativeProvider(), workflow_skills=[])
        agent = AgentDef(name="a", model="gpt-4", prompt="Hello world", skills=["conductor"])
        rendered = executor.render_prompt(agent, {})
        assert "<skills>" in rendered

    def test_skills_appear_before_prompt(self) -> None:
        executor = AgentExecutor(_StubNonNativeProvider(), workflow_skills=["conductor"])
        agent = AgentDef(name="a", model="gpt-4", prompt="MY_PROMPT_HERE")
        rendered = executor.render_prompt(agent, {})
        assert rendered.index("<skills>") < rendered.index("MY_PROMPT_HERE")

    def test_skills_appear_after_instructions_preamble(self) -> None:
        preamble = "<workspace_instructions>\nFollow conventions.\n</workspace_instructions>\n\n"
        executor = AgentExecutor(
            _StubNonNativeProvider(),
            instructions_preamble=preamble,
            workflow_skills=["conductor"],
        )
        agent = AgentDef(name="a", model="gpt-4", prompt="MY_PROMPT_HERE")
        rendered = executor.render_prompt(agent, {})
        instr = rendered.index("<workspace_instructions>")
        skills = rendered.index("<skills>")
        prompt = rendered.index("MY_PROMPT_HERE")
        assert instr < skills < prompt


class TestResolveSkillsForAgent:
    """Tri-state resolution: agent overrides workflow default."""

    def test_agent_none_inherits_workflow(self) -> None:
        executor = AgentExecutor(CopilotProvider(), workflow_skills=["conductor"])
        agent = AgentDef(name="a", model="gpt-4", prompt="p")
        assert [s.name for s in executor._resolve_skills_for_agent(agent)] == ["conductor"]

    def test_agent_list_overrides_workflow(self) -> None:
        executor = AgentExecutor(CopilotProvider(), workflow_skills=[])
        agent = AgentDef(name="a", model="gpt-4", prompt="p", skills=["conductor"])
        assert [s.name for s in executor._resolve_skills_for_agent(agent)] == ["conductor"]

    def test_agent_empty_list_opts_out(self) -> None:
        executor = AgentExecutor(CopilotProvider(), workflow_skills=["conductor"])
        agent = AgentDef(name="a", model="gpt-4", prompt="p", skills=[])
        assert executor._resolve_skills_for_agent(agent) == []

    def test_default_when_nothing_set(self) -> None:
        executor = AgentExecutor(CopilotProvider())
        agent = AgentDef(name="a", model="gpt-4", prompt="p")
        assert executor._resolve_skills_for_agent(agent) == []
