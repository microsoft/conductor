"""Unit tests for AgentExecutor.

Tests cover:
- Prompt rendering with context
- Provider execution
- Output validation
- Tool resolution
- Error handling
"""

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.exceptions import TemplateError, ValidationError
from conductor.executor.agent import AgentExecutor, resolve_agent_tools
from conductor.providers.base import AgentOutput
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


@pytest.fixture
def agent_with_system_prompt() -> AgentDef:
    """Create an agent with system prompt."""
    return AgentDef(
        name="test_agent",
        model="gpt-4",
        system_prompt="You are a helpful assistant for {{ workflow.input.topic }}.",
        prompt="Answer: {{ workflow.input.question }}",
        output={"answer": OutputField(type="string")},
    )


@pytest.fixture
def agent_without_output_schema() -> AgentDef:
    """Create an agent without output schema."""
    return AgentDef(
        name="test_agent",
        model="gpt-4",
        prompt="Do something",
        output=None,
    )


class TestAgentExecutorBasic:
    """Basic AgentExecutor tests."""

    @pytest.mark.asyncio
    async def test_execute_renders_prompt(self, simple_agent: AgentDef) -> None:
        """Test that execute renders the prompt template."""
        received_prompts = []

        def mock_handler(agent, prompt, context):
            received_prompts.append(prompt)
            return {"answer": "Python is great"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)

        context = {"workflow": {"input": {"question": "What is Python?"}}}
        await executor.execute(simple_agent, context)

        assert len(received_prompts) == 1
        assert "What is Python?" in received_prompts[0]

    @pytest.mark.asyncio
    async def test_execute_returns_output(self, simple_agent: AgentDef) -> None:
        """Test that execute returns the agent output."""

        def mock_handler(agent, prompt, context):
            return {"answer": "The answer is 42"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)

        context = {"workflow": {"input": {"question": "What is the answer?"}}}
        output = await executor.execute(simple_agent, context)

        assert isinstance(output, AgentOutput)
        assert output.content["answer"] == "The answer is 42"

    @pytest.mark.asyncio
    async def test_execute_validates_output(self, simple_agent: AgentDef) -> None:
        """Test that execute validates output against schema."""

        def mock_handler(agent, prompt, context):
            return {"answer": 42}  # Wrong type - should be string

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)

        context = {"workflow": {"input": {"question": "test"}}}

        with pytest.raises(ValidationError, match="wrong type"):
            await executor.execute(simple_agent, context)

    @pytest.mark.asyncio
    async def test_execute_without_schema_skips_validation(
        self, agent_without_output_schema: AgentDef
    ) -> None:
        """Test that execute skips validation when no schema defined."""

        def mock_handler(agent, prompt, context):
            return {"anything": "goes", "here": 123}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)

        context = {"workflow": {"input": {}}}
        output = await executor.execute(agent_without_output_schema, context)

        assert output.content["anything"] == "goes"
        assert output.content["here"] == 123


class TestAgentExecutorPromptRendering:
    """Tests for prompt rendering."""

    @pytest.mark.asyncio
    async def test_render_prompt_with_nested_context(self) -> None:
        """Test rendering prompt with nested context values."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="Plan: {{ planner.output.plan }}\nQuestion: {{ workflow.input.question }}",
            output=None,
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)

        context = {
            "workflow": {"input": {"question": "How?"}},
            "planner": {"output": {"plan": "Step 1, Step 2"}},
        }
        await executor.execute(agent, context)

        # Verify prompt was rendered (via call history)
        call_history = provider.get_call_history()
        assert "Step 1, Step 2" in call_history[0]["prompt"]
        assert "How?" in call_history[0]["prompt"]

    @pytest.mark.asyncio
    async def test_render_prompt_with_json_filter(self) -> None:
        """Test rendering prompt with json filter."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="Data: {{ data | json }}",
            output=None,
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)

        context = {"data": {"key": "value", "items": [1, 2, 3]}}
        await executor.execute(agent, context)

        call_history = provider.get_call_history()
        # JSON should be in the prompt
        assert '"key"' in call_history[0]["prompt"]
        assert '"value"' in call_history[0]["prompt"]

    @pytest.mark.asyncio
    async def test_render_prompt_missing_variable_raises(self) -> None:
        """Test that missing template variable raises TemplateError."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="Value: {{ missing.variable }}",
            output=None,
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)

        context = {}

        with pytest.raises(TemplateError, match="Undefined variable"):
            await executor.execute(agent, context)

    def test_render_prompt_helper(self, simple_agent: AgentDef) -> None:
        """Test the render_prompt helper method."""
        provider = CopilotProvider()
        executor = AgentExecutor(provider)

        context = {"workflow": {"input": {"question": "Test question?"}}}
        rendered = executor.render_prompt(simple_agent, context)

        assert "Test question?" in rendered


class TestAgentExecutorWithTools:
    """Tests for agent execution with tools."""

    @pytest.mark.asyncio
    async def test_execute_passes_resolved_tools_to_provider(self) -> None:
        """Test that resolved tools are passed to the provider."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="Use tools",
            tools=["web_search", "calculator"],  # Subset of workflow tools
            output=None,
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        # Workflow has these tools defined
        executor = AgentExecutor(provider, workflow_tools=["web_search", "calculator", "file_read"])

        await executor.execute(agent, {})

        call_history = provider.get_call_history()
        # Agent should get only the tools it requested (subset of workflow tools)
        assert call_history[0]["tools"] == ["web_search", "calculator"]

    @pytest.mark.asyncio
    async def test_execute_with_no_agent_tools_gets_all_workflow_tools(self) -> None:
        """Test execution with no agent tools specified gets all workflow tools."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="All tools",
            tools=None,  # None = all workflow tools
            output=None,
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider, workflow_tools=["web_search", "file_read"])

        await executor.execute(agent, {})

        call_history = provider.get_call_history()
        # Agent should get all workflow tools
        assert call_history[0]["tools"] == ["web_search", "file_read"]

    @pytest.mark.asyncio
    async def test_execute_with_empty_tools_gets_no_tools(self) -> None:
        """Test execution with empty tools list gets no tools."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="No tools allowed",
            tools=[],  # Empty = no tools
            output=None,
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider, workflow_tools=["web_search", "file_read"])

        await executor.execute(agent, {})

        call_history = provider.get_call_history()
        # Agent should get no tools
        assert call_history[0]["tools"] == []

    @pytest.mark.asyncio
    async def test_execute_with_no_workflow_tools(self) -> None:
        """Test execution when workflow has no tools defined."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="No workflow tools",
            tools=None,  # None = all workflow tools (which is empty)
            output=None,
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)  # No workflow_tools specified

        await executor.execute(agent, {})

        call_history = provider.get_call_history()
        # Agent should get empty list when workflow has no tools
        assert call_history[0]["tools"] == []

    @pytest.mark.asyncio
    async def test_execute_with_unknown_tools_raises_error(self) -> None:
        """Test that agent specifying unknown tools raises ValidationError."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="Unknown tools",
            tools=["unknown_tool", "web_search"],  # unknown_tool not in workflow
            output=None,
        )

        def mock_handler(agent, prompt, context):
            return {"result": "ok"}

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider, workflow_tools=["web_search", "file_read"])

        with pytest.raises(ValidationError, match="unknown tools"):
            await executor.execute(agent, {})


class TestResolveAgentTools:
    """Tests for the resolve_agent_tools function."""

    def test_none_agent_tools_returns_all_workflow_tools(self) -> None:
        """Test that None agent tools returns all workflow tools."""
        workflow_tools = ["tool_a", "tool_b", "tool_c"]
        result = resolve_agent_tools(None, workflow_tools)
        assert result == ["tool_a", "tool_b", "tool_c"]

    def test_none_agent_tools_returns_copy(self) -> None:
        """Test that returned list is a copy, not the original."""
        workflow_tools = ["tool_a", "tool_b"]
        result = resolve_agent_tools(None, workflow_tools)
        result.append("tool_c")
        assert workflow_tools == ["tool_a", "tool_b"]

    def test_empty_agent_tools_returns_empty_list(self) -> None:
        """Test that empty agent tools returns empty list."""
        workflow_tools = ["tool_a", "tool_b", "tool_c"]
        result = resolve_agent_tools([], workflow_tools)
        assert result == []

    def test_subset_agent_tools_returns_subset(self) -> None:
        """Test that subset of tools is returned correctly."""
        workflow_tools = ["tool_a", "tool_b", "tool_c"]
        agent_tools = ["tool_a", "tool_c"]
        result = resolve_agent_tools(agent_tools, workflow_tools)
        assert result == ["tool_a", "tool_c"]

    def test_subset_agent_tools_returns_copy(self) -> None:
        """Test that returned subset is a copy, not the original."""
        workflow_tools = ["tool_a", "tool_b", "tool_c"]
        agent_tools = ["tool_a", "tool_b"]
        result = resolve_agent_tools(agent_tools, workflow_tools)
        result.append("tool_c")
        assert agent_tools == ["tool_a", "tool_b"]

    def test_unknown_tools_raises_validation_error(self) -> None:
        """Test that unknown tools raise ValidationError."""
        workflow_tools = ["tool_a", "tool_b"]
        agent_tools = ["tool_a", "unknown_tool"]

        with pytest.raises(ValidationError, match="unknown tools"):
            resolve_agent_tools(agent_tools, workflow_tools)

    def test_multiple_unknown_tools_lists_all(self) -> None:
        """Test that multiple unknown tools are all listed in error."""
        workflow_tools = ["tool_a"]
        agent_tools = ["tool_b", "tool_c"]

        with pytest.raises(ValidationError) as exc_info:
            resolve_agent_tools(agent_tools, workflow_tools)

        error_msg = str(exc_info.value)
        assert "tool_b" in error_msg
        assert "tool_c" in error_msg

    def test_unknown_tools_shows_available_tools_in_suggestion(self) -> None:
        """Test that error suggestion includes available tools."""
        workflow_tools = ["web_search", "file_read"]
        agent_tools = ["unknown"]

        with pytest.raises(ValidationError) as exc_info:
            resolve_agent_tools(agent_tools, workflow_tools)

        # Check suggestion includes available tools
        assert exc_info.value.suggestion is not None
        assert "file_read" in exc_info.value.suggestion
        assert "web_search" in exc_info.value.suggestion

    def test_empty_workflow_tools_with_none_agent_tools(self) -> None:
        """Test that empty workflow tools with None agent tools returns empty."""
        workflow_tools: list[str] = []
        result = resolve_agent_tools(None, workflow_tools)
        assert result == []

    def test_empty_workflow_tools_with_agent_tools_raises(self) -> None:
        """Test that agent tools with empty workflow tools raises error."""
        workflow_tools: list[str] = []
        agent_tools = ["tool_a"]

        with pytest.raises(ValidationError, match="unknown tools"):
            resolve_agent_tools(agent_tools, workflow_tools)

    def test_all_workflow_tools_as_agent_subset(self) -> None:
        """Test requesting all workflow tools as explicit subset works."""
        workflow_tools = ["tool_a", "tool_b"]
        agent_tools = ["tool_a", "tool_b"]
        result = resolve_agent_tools(agent_tools, workflow_tools)
        assert result == ["tool_a", "tool_b"]


class TestAgentExecutorOutputHandling:
    """Tests for output handling edge cases."""

    @pytest.mark.asyncio
    async def test_missing_output_field_raises(self) -> None:
        """Test that missing required output field raises ValidationError."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="Test",
            output={
                "required_field": OutputField(type="string"),
                "another_field": OutputField(type="number"),
            },
        )

        def mock_handler(agent, prompt, context):
            return {"required_field": "value"}  # Missing another_field

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)

        with pytest.raises(ValidationError, match="Missing required output field"):
            await executor.execute(agent, {})

    @pytest.mark.asyncio
    async def test_output_with_multiple_types(self) -> None:
        """Test validation of output with multiple field types."""
        agent = AgentDef(
            name="test",
            model="gpt-4",
            prompt="Test",
            output={
                "text": OutputField(type="string"),
                "count": OutputField(type="number"),
                "active": OutputField(type="boolean"),
                "items": OutputField(type="array"),
                "meta": OutputField(type="object"),
            },
        )

        def mock_handler(agent, prompt, context):
            return {
                "text": "hello",
                "count": 42,
                "active": True,
                "items": [1, 2, 3],
                "meta": {"key": "value"},
            }

        provider = CopilotProvider(mock_handler=mock_handler)
        executor = AgentExecutor(provider)

        output = await executor.execute(agent, {})

        assert output.content["text"] == "hello"
        assert output.content["count"] == 42
        assert output.content["active"] is True
        assert output.content["items"] == [1, 2, 3]
        assert output.content["meta"] == {"key": "value"}
