"""Agent execution orchestration for Conductor.

This module provides the AgentExecutor class for executing a single agent
with prompt rendering and output validation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from conductor.exceptions import ValidationError
from conductor.executor.output import parse_json_output, validate_output
from conductor.executor.template import TemplateRenderer
from conductor.providers.base import AgentOutput


def _verbose_log(message: str, style: str = "dim") -> None:
    """Lazy import wrapper for verbose_log to avoid circular imports."""
    from conductor.cli.run import verbose_log

    verbose_log(message, style)


def _verbose_log_section(title: str, content: str) -> None:
    """Lazy import wrapper for verbose_log_section to avoid circular imports."""
    from conductor.cli.run import verbose_log_section

    verbose_log_section(title, content)


if TYPE_CHECKING:
    from conductor.config.schema import AgentDef
    from conductor.providers.base import AgentProvider


def resolve_agent_tools(
    agent_tools: list[str] | None,
    workflow_tools: list[str],
) -> list[str]:
    """Resolve which tools an agent should have access to.

    The resolution follows these rules:
    - agent_tools=None (omitted): Agent gets ALL workflow tools
    - agent_tools=[] (empty list): Agent gets NO tools
    - agent_tools=[list]: Agent gets only specified tools (must be subset of workflow)

    Args:
        agent_tools: Agent's tool specification (None=all, []=none, [list]=subset)
        workflow_tools: Tools defined at workflow level

    Returns:
        List of tool names for this agent

    Raises:
        ValidationError: If agent specifies tools not in workflow tools
    """
    if agent_tools is None:
        # None means all workflow tools
        return workflow_tools.copy()

    if not agent_tools:
        # Empty list means no tools
        return []

    # Validate subset
    invalid = set(agent_tools) - set(workflow_tools)
    if invalid:
        sorted_invalid = sorted(invalid)
        sorted_available = sorted(workflow_tools)
        raise ValidationError(
            f"Agent specifies unknown tools: {sorted_invalid}",
            suggestion=f"Available workflow tools: {sorted_available}",
        )

    return agent_tools.copy()


class AgentExecutor:
    """Executes a single agent with prompt rendering and output validation.

    The AgentExecutor handles the complete lifecycle of executing an agent:
    1. Render the prompt template with the provided context
    2. Resolve which tools the agent has access to
    3. Execute the agent via the provider
    4. Validate the output against the agent's schema (if defined)

    Example:
        >>> from conductor.providers.copilot import CopilotProvider
        >>> provider = CopilotProvider()
        >>> executor = AgentExecutor(provider, workflow_tools=["web_search"])
        >>> output = await executor.execute(agent, context)
    """

    def __init__(
        self,
        provider: AgentProvider,
        workflow_tools: list[str] | None = None,
    ) -> None:
        """Initialize the AgentExecutor.

        Args:
            provider: The agent provider to use for execution.
            workflow_tools: Tools defined at workflow level. Defaults to empty list.
        """
        self.provider = provider
        self.workflow_tools = workflow_tools or []
        self.renderer = TemplateRenderer()

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
    ) -> AgentOutput:
        """Execute an agent with the given context.

        This method:
        1. Renders the agent's prompt template with context
        2. Resolves which tools the agent has access to
        3. Calls the provider to execute the agent
        4. Validates output against the agent's schema (if defined)

        Args:
            agent: Agent definition from workflow config.
            context: Context for prompt rendering, built by WorkflowContext.

        Returns:
            Validated agent output.

        Raises:
            TemplateError: If prompt rendering fails.
            ProviderError: If agent execution fails.
            ValidationError: If output doesn't match schema or tools are invalid.
        """
        # Render prompt with context
        rendered_prompt = self.renderer.render(agent.prompt, context)

        # Verbose: Log rendered prompt
        _verbose_log_section(
            f"Prompt for '{agent.name}'",
            rendered_prompt,
        )

        # Render system prompt if present (used by some providers)
        # Note: System prompt support will be fully utilized in later EPICs
        if agent.system_prompt:
            _ = self.renderer.render(agent.system_prompt, context)

        # Resolve tools for this agent
        resolved_tools = resolve_agent_tools(agent.tools, self.workflow_tools)

        # Verbose: Log resolved tools
        if resolved_tools:
            _verbose_log(f"  Tools: {resolved_tools}")

        # Execute via provider
        output = await self.provider.execute(
            agent=agent,
            context=context,
            rendered_prompt=rendered_prompt,
            tools=resolved_tools,
        )

        # Ensure output.content is a dict
        if not isinstance(output.content, dict):
            # Try to parse raw response as JSON if content is not a dict
            if output.raw_response and isinstance(output.raw_response, str):
                output = AgentOutput(
                    content=parse_json_output(output.raw_response),
                    raw_response=output.raw_response,
                    tokens_used=output.tokens_used,
                    model=output.model,
                )
            else:
                # Wrap the content in a dict
                output = AgentOutput(
                    content={"result": output.content},
                    raw_response=output.raw_response,
                    tokens_used=output.tokens_used,
                    model=output.model,
                )

        # Validate output against schema
        if agent.output:
            validate_output(output.content, agent.output)

        return output

    def render_prompt(self, agent: AgentDef, context: dict[str, Any]) -> str:
        """Render an agent's prompt template.

        This is useful for debugging or dry-run mode.

        Args:
            agent: Agent definition from workflow config.
            context: Context for prompt rendering.

        Returns:
            Rendered prompt string.

        Raises:
            TemplateError: If prompt rendering fails.
        """
        return self.renderer.render(agent.prompt, context)
