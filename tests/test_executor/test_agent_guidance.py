"""Tests for AgentExecutor guidance injection.

Tests cover:
- Guidance section is appended to rendered prompt
- None guidance does not modify prompt
- Guidance appears after the main prompt content
"""

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.executor.agent import AgentExecutor
from conductor.providers.copilot import CopilotProvider


@pytest.fixture
def simple_agent() -> AgentDef:
    """Create a simple agent definition."""
    return AgentDef(
        name="test_agent",
        model="gpt-4",
        prompt="Answer the question: {{ workflow.input.question }}",
        output={"answer": OutputField(type="string")},
    )


class TestAgentExecutorGuidanceInjection:
    """Tests for guidance injection into agent prompts."""

    @pytest.mark.asyncio
    async def test_guidance_appended_to_prompt(self, simple_agent: AgentDef) -> None:
        """Verify guidance section is appended to the rendered prompt."""
        received_prompts: list[str] = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            return {"answer": "test"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)
        context = {"workflow": {"input": {"question": "What is Python?"}}}

        guidance = (
            "\n\n[User Guidance]\n"
            "The following guidance was provided by the user during workflow execution. "
            "Incorporate this guidance into your response:\n"
            "- Focus on Python 3 only"
        )

        await executor.execute(simple_agent, context, guidance_section=guidance)

        assert len(received_prompts) == 1
        assert "What is Python?" in received_prompts[0]
        assert "[User Guidance]" in received_prompts[0]
        assert "- Focus on Python 3 only" in received_prompts[0]

    @pytest.mark.asyncio
    async def test_none_guidance_does_not_modify_prompt(self, simple_agent: AgentDef) -> None:
        """Verify None guidance produces no modification to the prompt."""
        received_prompts: list[str] = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            return {"answer": "test"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)
        context = {"workflow": {"input": {"question": "What is Python?"}}}

        await executor.execute(simple_agent, context, guidance_section=None)

        assert len(received_prompts) == 1
        assert received_prompts[0] == "Answer the question: What is Python?"

    @pytest.mark.asyncio
    async def test_guidance_default_is_none(self, simple_agent: AgentDef) -> None:
        """Verify that not passing guidance_section behaves like None."""
        received_prompts: list[str] = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            return {"answer": "test"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)
        context = {"workflow": {"input": {"question": "What is Python?"}}}

        await executor.execute(simple_agent, context)

        assert len(received_prompts) == 1
        assert "[User Guidance]" not in received_prompts[0]

    @pytest.mark.asyncio
    async def test_guidance_appears_after_main_prompt(self, simple_agent: AgentDef) -> None:
        """Verify guidance section appears after the main prompt content."""
        received_prompts: list[str] = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            return {"answer": "test"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)
        context = {"workflow": {"input": {"question": "What is Python?"}}}

        guidance = "\n\n[User Guidance]\nIncorporate this guidance:\n- Be brief"
        await executor.execute(simple_agent, context, guidance_section=guidance)

        prompt = received_prompts[0]
        main_end = prompt.index("What is Python?") + len("What is Python?")
        guidance_start = prompt.index("[User Guidance]")
        assert guidance_start > main_end

    @pytest.mark.asyncio
    async def test_guidance_with_multiple_entries(self, simple_agent: AgentDef) -> None:
        """Verify multi-entry guidance is passed through correctly."""
        received_prompts: list[str] = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            return {"answer": "test"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)
        context = {"workflow": {"input": {"question": "What is Python?"}}}

        guidance = (
            "\n\n[User Guidance]\n"
            "The following guidance was provided by the user during workflow execution. "
            "Incorporate this guidance into your response:\n"
            "- Focus on Python 3 only\n"
            "- Use async patterns"
        )
        await executor.execute(simple_agent, context, guidance_section=guidance)

        prompt = received_prompts[0]
        assert "- Focus on Python 3 only" in prompt
        assert "- Use async patterns" in prompt
