"""Tests for skill resolution inside :class:`AgentExecutor`.

Covers both provider variants of the parity contract:

* Native-skill providers (``supports_native_skills = True``) skip eager
  preamble injection — skill content is loaded by the SDK.
* Non-native providers eagerly inject ``SKILL.md`` + ``references/*.md``
  into the rendered prompt.
"""

from __future__ import annotations

import asyncio
from typing import Any

from conductor.config.schema import AgentDef
from conductor.executor.agent import AgentExecutor
from conductor.providers.base import AgentOutput, AgentProvider, EventCallback
from conductor.providers.copilot import CopilotProvider
from conductor.skills.loader import _cached_skill_payload


class _StubNonNativeProvider(AgentProvider, abstract=True):
    """Provider stub that does NOT support native skill loading.

    Exercises the eager preamble injection path the same way Claude
    does today. Uses ``abstract=True`` to opt out of the
    :class:`ProviderCapabilities` declaration enforced on production
    providers — this is a test fake, not a real provider.
    """

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
    ) -> AgentOutput:
        return AgentOutput(content={"echo": rendered_prompt})

    async def validate_connection(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class TestCopilotProviderNativeSkills:
    """Copilot owns native ``skill_directories``; preamble is NOT injected."""

    def setup_method(self) -> None:
        _cached_skill_payload.cache_clear()

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


class TestNonNativeProviderEagerInjection:
    """Non-native providers receive skill content via the rendered prompt."""

    def setup_method(self) -> None:
        _cached_skill_payload.cache_clear()

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
        assert executor._resolve_skills_for_agent(agent) == ["conductor"]

    def test_agent_list_overrides_workflow(self) -> None:
        executor = AgentExecutor(CopilotProvider(), workflow_skills=[])
        agent = AgentDef(name="a", model="gpt-4", prompt="p", skills=["conductor"])
        assert executor._resolve_skills_for_agent(agent) == ["conductor"]

    def test_agent_empty_list_opts_out(self) -> None:
        executor = AgentExecutor(CopilotProvider(), workflow_skills=["conductor"])
        agent = AgentDef(name="a", model="gpt-4", prompt="p", skills=[])
        assert executor._resolve_skills_for_agent(agent) == []

    def test_default_when_nothing_set(self) -> None:
        executor = AgentExecutor(CopilotProvider())
        agent = AgentDef(name="a", model="gpt-4", prompt="p")
        assert executor._resolve_skills_for_agent(agent) == []
