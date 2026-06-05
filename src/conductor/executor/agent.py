"""Agent execution orchestration for Conductor.

This module provides the AgentExecutor class for executing a single agent
with prompt rendering and output validation.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, get_args

from conductor.exceptions import ValidationError
from conductor.executor.output import parse_json_output, validate_output
from conductor.executor.template import TemplateRenderer
from conductor.providers.base import AgentOutput, EventCallback
from conductor.providers.context_tier import ContextTier
from conductor.providers.reasoning import ReasoningEffort
from conductor.templating import is_jinja_template


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
        instructions_preamble: str | None = None,
        workflow_skills: list[str] | None = None,
    ) -> None:
        """Initialize the AgentExecutor.

        Args:
            provider: The agent provider to use for execution.
            workflow_tools: Tools defined at workflow level. Defaults to empty list.
            instructions_preamble: Optional workspace instructions text to prepend
                to every agent's rendered prompt.
            workflow_skills: Workflow-level default skills (from
                ``runtime.skills``). Agents inherit this list unless they
                set their own ``skills:`` field — ``[]`` opts out
                explicitly, ``[name, ...]`` overrides the default.
        """
        self.provider = provider
        self.workflow_tools = workflow_tools or []
        self.instructions_preamble = instructions_preamble
        self._workflow_skills: list[str] = list(workflow_skills or [])
        self.renderer = TemplateRenderer()

    def _render_enum_field(
        self,
        *,
        value: str,
        context: dict[str, Any],
        allowed: tuple[str, ...],
        field_name: str,
        agent_name: str,
    ) -> str:
        """Render a templated enum field and validate the resolved literal.

        Mirrors the ``model`` rendering above: a ``{{ ... }}`` value is
        rendered with the full agent context, stripped (the renderer keeps
        trailing newlines), and checked against ``allowed``. Raises a
        :class:`~conductor.exceptions.ValidationError` when the resolved
        value is not one of the permitted literals so the failure is actionable
        at execute time rather than silently forwarded to the provider/SDK.
        """
        resolved = self.renderer.render(value, context).strip()
        if resolved not in allowed:
            if not resolved:
                # An empty resolution is almost always a conditional template
                # (``{% if ... %}``) with no matching branch. Fail closed — the
                # same way a non-empty invalid value (below) and the
                # provider-side resolver guards do — rather than silently
                # treating empty as "unset": to fall back to the runtime
                # default, omit the field or add an else-branch emitting the
                # desired literal.
                raise ValidationError(
                    f"Agent '{agent_name}': {field_name} template resolved to an empty value.",
                    suggestion=(
                        f"A conditional template with no matching branch "
                        f"produced nothing. Emit one of {list(allowed)}, add an "
                        f"else-branch, or omit {field_name} to use the runtime "
                        f"default."
                    ),
                )
            raise ValidationError(
                f"Agent '{agent_name}': {field_name} template resolved to "
                f"{resolved!r}, which is not a valid value.",
                suggestion=f"Resolved value must be one of {list(allowed)}.",
            )
        return resolved

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
        guidance_section: str | None = None,
        interrupt_signal: asyncio.Event | None = None,
        event_callback: EventCallback | None = None,
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
            guidance_section: Optional user guidance section to append to the
                rendered prompt. When provided, this is appended after the
                rendered prompt text.
            interrupt_signal: Optional event for mid-agent interrupt signaling.
                Forwarded to the provider's execute method.
            event_callback: Optional callback for streaming SDK events upstream.
                When provided, the executor emits an ``agent_prompt_rendered``
                event with the rendered prompt, then forwards the callback
                to the provider for SDK-level streaming events.

        Returns:
            Validated agent output.

        Raises:
            TemplateError: If prompt rendering fails.
            ProviderError: If agent execution fails.
            ValidationError: If output doesn't match schema or tools are invalid.
        """
        # Render model field if it contains template expressions
        if is_jinja_template(agent.model):
            rendered_model = self.renderer.render(agent.model, context)
            agent = agent.model_copy(update={"model": rendered_model})

        # #262: resolve templated reasoning.effort / context_tier the same
        # way model is handled above. These fields are strict ``Literal``
        # aliases that the schema deliberately accepts as templates (deferring
        # literal validation to here); render the value with full context, then
        # validate the resolved literal so the provider sees a concrete value.
        # ``is_jinja_template`` both detects templates and narrows the widened
        # ``ReasoningEffort | str`` / ``ContextTier | str | None`` field types
        # to ``str`` for the type checker before the value reaches
        # ``_render_enum_field``. (``ReasoningEffort`` and ``ContextTier`` are
        # ``Literal`` aliases, not ``Enum`` types — hence the ``get_args``
        # calls below.)
        effort = agent.reasoning.effort if agent.reasoning is not None else None
        if is_jinja_template(effort):
            resolved_effort = self._render_enum_field(
                value=effort,
                context=context,
                allowed=get_args(ReasoningEffort),
                field_name="reasoning.effort",
                agent_name=agent.name,
            )
            # ``agent.reasoning`` is not None here (effort came from it).
            assert agent.reasoning is not None
            agent = agent.model_copy(
                update={"reasoning": agent.reasoning.model_copy(update={"effort": resolved_effort})}
            )

        tier = agent.context_tier
        if is_jinja_template(tier):
            resolved_tier = self._render_enum_field(
                value=tier,
                context=context,
                allowed=get_args(ContextTier),
                field_name="context_tier",
                agent_name=agent.name,
            )
            agent = agent.model_copy(update={"context_tier": resolved_tier})

        # Render prompt with context
        rendered_prompt = self.renderer.render(agent.prompt, context)

        # Prepend prompt prefix (workspace instructions + optional skills)
        prefix = self._build_prompt_prefix(agent)
        if prefix:
            rendered_prompt = prefix + rendered_prompt

        # Append user guidance section if provided
        if guidance_section:
            rendered_prompt = rendered_prompt + guidance_section

        # Emit prompt rendered event via callback
        if event_callback is not None:
            with contextlib.suppress(Exception):
                event_callback(
                    "agent_prompt_rendered",
                    {
                        "rendered_prompt": rendered_prompt,
                        "context_keys": list(context.keys()) if isinstance(context, dict) else [],
                    },
                )

        # Verbose: Log rendered prompt
        _verbose_log_section(
            f"Prompt for '{agent.name}'",
            rendered_prompt,
        )

        # Render system prompt if present and update the agent so that providers
        # which forward `agent.system_prompt` (e.g., the Copilot provider) see
        # the rendered text instead of the raw template with unfilled `{{ }}`
        # placeholders. Without this, agents whose instructions live in
        # `system_prompt` send unrendered Jinja to the model, which then
        # correctly reports "the prompt template contains unfilled variables"
        # and refuses to do useful work.
        if agent.system_prompt:
            rendered_system_prompt = self.renderer.render(agent.system_prompt, context)
            agent = agent.model_copy(update={"system_prompt": rendered_system_prompt})

        # Resolve tools for this agent
        resolved_tools = resolve_agent_tools(agent.tools, self.workflow_tools)

        # Verbose: Log resolved tools
        if resolved_tools:
            _verbose_log(f"  Tools: {resolved_tools}")

        # Resolve skill directories for providers with native skill support
        # (Copilot passes these on session_kwargs; Claude has already had
        # the skill content eager-injected into rendered_prompt above and
        # ignores this).
        skill_dirs: list[str] | None = None
        if getattr(self.provider, "supports_native_skills", False):
            skill_names = self._resolve_skills_for_agent(agent)
            if skill_names:
                from conductor.skills import resolve_skill_directories

                skill_dirs = [str(p) for p in resolve_skill_directories(skill_names)]

        # Execute via provider
        output = await self.provider.execute(
            agent=agent,
            context=context,
            rendered_prompt=rendered_prompt,
            tools=resolved_tools,
            interrupt_signal=interrupt_signal,
            event_callback=event_callback,
            skill_directories=skill_dirs,
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

        # Validate output against schema (skip for partial output from interrupts)
        if agent.output and not output.partial:
            validate_output(output.content, agent.output)

        return output

    def render_prompt(self, agent: AgentDef, context: dict[str, Any]) -> str:
        """Render an agent's prompt template including workspace instructions.

        This is useful for debugging or dry-run mode.

        Args:
            agent: Agent definition from workflow config.
            context: Context for prompt rendering.

        Returns:
            Rendered prompt string with workspace instructions and optional
            skill content prepended if configured.

        Raises:
            TemplateError: If prompt rendering fails.
        """
        rendered = self.renderer.render(agent.prompt, context)
        prefix = self._build_prompt_prefix(agent)
        if prefix:
            rendered = prefix + rendered
        return rendered

    def _resolve_skills_for_agent(self, agent: AgentDef) -> list[str]:
        """Resolve the effective skill list for an agent.

        Resolution order:
        - If the agent explicitly sets ``skills`` (including ``[]``),
          that value wins.
        - Otherwise, inherit the workflow-level default
          (``runtime.skills``).

        Returns an empty list when no skills are enabled, or when the
        agent is not a provider-backed type (script / wait / set /
        terminate / human_gate / workflow — schema rejects ``skills``
        on these so this is defensive only).
        """
        if agent.type not in (None, "agent"):
            return []
        if agent.skills is not None:
            return list(agent.skills)
        return list(self._workflow_skills)

    def _build_prompt_prefix(self, agent: AgentDef) -> str:
        """Build the prefix to prepend before an agent's rendered prompt.

        Combines workspace instructions and (on providers that lack
        native skill support) eager skill-content injection into a
        single prefix string. Shared by :meth:`execute` and
        :meth:`render_prompt` so the rendered prompts match the prompts
        sent to the provider.

        On providers that support native skill loading
        (:attr:`AgentProvider.supports_native_skills`), the skill
        directories are passed to the SDK on the provider side and we
        skip preamble injection to avoid double-loading.
        """
        parts: list[str] = []
        if self.instructions_preamble:
            parts.append(self.instructions_preamble)
        if not getattr(self.provider, "supports_native_skills", False):
            skill_names = self._resolve_skills_for_agent(agent)
            if skill_names:
                from conductor.skills import load_skill_content, resolve_skill_directories

                dirs = resolve_skill_directories(skill_names)
                content = load_skill_content(list(zip(skill_names, dirs, strict=True)))
                if content:
                    parts.append(content)
        return "".join(parts)
