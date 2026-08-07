"""Agent execution orchestration for Conductor.

This module provides the AgentExecutor class for executing a single agent
with prompt rendering and output validation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, get_args

from conductor.exceptions import ExecutionError, ValidationError
from conductor.executor.output import parse_json_output, validate_output
from conductor.executor.template import TemplateRenderer
from conductor.providers.base import AgentOutput, EventCallback
from conductor.providers.context_tier import ContextTier
from conductor.providers.reasoning import ReasoningEffort
from conductor.skills import BYTES_PER_TOKEN_ESTIMATE
from conductor.templating import is_jinja_template

logger = logging.getLogger(__name__)


def _verbose_log(message: str, style: str = "dim") -> None:
    """Lazy import wrapper for verbose_log to avoid circular imports."""
    from conductor.cli.run import verbose_log

    verbose_log(message, style)


def _verbose_log_section(title: str, content: str) -> None:
    """Lazy import wrapper for verbose_log_section to avoid circular imports."""
    from conductor.cli.run import verbose_log_section

    verbose_log_section(title, content)


if TYPE_CHECKING:
    from pathlib import Path

    from conductor.config.schema import (
        AgentDef,
        PluginDef,
        SkillDiscoveryConfig,
        SkillInjectionConfig,
    )
    from conductor.plugins.registry import ResolvedPlugin
    from conductor.providers.base import AgentProvider
    from conductor.skills import ResolvedSkill


def _merge_skills(
    declared: list[ResolvedSkill],
    from_plugins: list[ResolvedSkill],
    on_warning: Callable[[str], None] | None = None,
) -> list[ResolvedSkill]:
    """Combine declared and plugin-supplied skills, declared winning.

    Both reach the provider through one ``skill_directories`` list, so a
    name claimed twice has to be resolved here. Declared entries win for
    the same reason they beat discovered ones: the author wrote them.

    Deduplication is by skill **name**, not directory, because every
    downstream consumer is name-keyed — the eager preamble emits
    ``<skill name="...">`` per skill and both CLIs resolve by name, so
    two directories claiming one name would silently shadow each other.
    A plugin's own copy losing to a declared one is the intended
    outcome, and is the common case in practice: installing Conductor's
    plugin puts a second ``conductor`` skill on the machine.

    Only declared-vs-plugin collisions reach here — two *plugins*
    claiming one skill name is refused outright by
    :func:`~conductor.plugins.registry.resolve_plugins`, since neither
    entry is more authoritative than the other.

    Args:
        declared: Skills from ``skills:`` / ``runtime.skills``.
        from_plugins: Skills contributed by enabled plugins.
        on_warning: Sink for the shadowing notice. Reported rather than
            logged, because Conductor installs no logging handlers, so a
            dropped skill would otherwise be invisible.
    """
    merged = list(declared)
    claimed = {skill.name: skill for skill in declared}
    for skill in from_plugins:
        shadowing = claimed.get(skill.name)
        if shadowing is not None:
            if on_warning is not None and shadowing.directory != skill.directory:
                on_warning(
                    f"skill {skill.name!r} from plugin {skill.source!r} is shadowed by "
                    f"the declared skill {shadowing.source!r} and was not enabled."
                )
            continue
        claimed[skill.name] = skill
        merged.append(skill)
    return merged


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
        workflow_dir: Path | None = None,
        skill_injection: SkillInjectionConfig | None = None,
        skill_discovery: SkillDiscoveryConfig | None = None,
        workflow_plugins: list[PluginDef] | None = None,
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
            workflow_dir: Directory of the workflow file, used as the base
                for relative skill paths (consistent with ``working_dir``).
                Falls back to the process working directory.
            skill_injection: Size limits for eager skill-content injection
                (from ``runtime.skill_injection``). Defaults apply when
                omitted.
            skill_discovery: Which categories of installed skill to pick up
                (from ``runtime.skill_discovery``). Applies only to agents
                that inherit the workflow-level list, so it is part of the
                default set rather than a separate axis. Disabled when
                omitted.
            workflow_plugins: Workflow-level default plugins (from
                ``runtime.plugins``). Inherited on the same tri-state as
                ``workflow_skills``.
        """
        self.provider = provider
        self.workflow_tools = workflow_tools or []
        self.instructions_preamble = instructions_preamble
        self._workflow_skills: list[str] = list(workflow_skills or [])
        self._workflow_dir = workflow_dir
        if skill_injection is None:
            from conductor.config.schema import SkillInjectionConfig

            skill_injection = SkillInjectionConfig()
        self._skill_injection = skill_injection
        if skill_discovery is None:
            from conductor.config.schema import SkillDiscoveryConfig

            skill_discovery = SkillDiscoveryConfig()
        self._skill_discovery = skill_discovery
        # Keyed by (entries, sources, exclude): discovery globs the
        # filesystem and resolution runs once per agent per call.
        self._skill_cache: dict[
            tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], list[ResolvedSkill]
        ] = {}
        self._workflow_plugins: list[PluginDef] = list(workflow_plugins or [])
        # Keyed by the entries themselves: resolving a plugin walks the
        # filesystem and parses every agent definition it ships, and this
        # runs once per agent per call.
        self._plugin_cache: dict[
            tuple[tuple[str, bool, bool, bool], ...], list[ResolvedPlugin]
        ] = {}
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
        prefix = self._build_prompt_prefix(agent, event_callback)
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

        # Resolve the agent's effective plugins. Each is deconstructed:
        # its skills join the same ``skill_directories`` channel that
        # ``skills:`` entries use, its subagents and MCP servers travel
        # their own kwargs. Resolved before skills so a plugin skill and a
        # declared skill can be checked against each other.
        plugins = self._resolve_plugins_for_agent(agent)
        plugin_skills = [skill for plugin in plugins for skill in plugin.skills]
        custom_agents: list[dict[str, Any]] | None = None
        extra_mcp_servers: dict[str, Any] | None = None
        if plugins:
            agent_specs = [
                spec.to_custom_agent_config() for plugin in plugins for spec in plugin.agents
            ]
            custom_agents = agent_specs or None
            servers = {
                name: config for plugin in plugins for name, config in plugin.mcp_servers.items()
            }
            extra_mcp_servers = servers or None
            _verbose_log(
                "  Plugins: "
                + ", ".join(
                    f"{plugin.name} (skills={len(plugin.skills)}, "
                    f"agents={len(plugin.agents)}, mcp={len(plugin.mcp_servers)})"
                    for plugin in plugins
                )
            )

        # Resolve skill directories for providers with native skill support
        # (Copilot passes these on session_kwargs, claude-agent-sdk maps them
        # to plugin + skill-name options; providers without native support
        # have already had the skill content eager-injected into
        # rendered_prompt above and ignore this).
        skill_dirs: list[str] | None = None
        if getattr(self.provider, "supports_native_skills", False):
            resolved = self._merge_skills_and_plugin_skills(agent, plugin_skills)
            if resolved:
                skill_dirs = [str(item.directory) for item in resolved]
                _verbose_log(f"  Skills: {[item.name for item in resolved]}")

        # Execute via provider
        output = await self.provider.execute(
            agent=agent,
            context=context,
            rendered_prompt=rendered_prompt,
            tools=resolved_tools,
            interrupt_signal=interrupt_signal,
            event_callback=event_callback,
            skill_directories=skill_dirs,
            custom_agents=custom_agents,
            extra_mcp_servers=extra_mcp_servers,
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

        No ``event_callback``: the only caller is the ``validator:`` block's
        re-render of the primary prompt, and the agent's own ``execute`` has
        already emitted any ``skill_injection_warning`` for the same content.
        The warning still reaches the log from here; only the duplicate event
        is suppressed.

        Args:
            agent: Agent definition from workflow config.
            context: Context for prompt rendering.

        Returns:
            Rendered prompt string with workspace instructions and optional
            skill content prepended if configured.

        Raises:
            TemplateError: If prompt rendering fails.
            SkillNotFoundError: If an enabled skill entry cannot be resolved.
            SkillManifestError: If a resolved skill's ``SKILL.md`` is missing,
                unparseable, or incomplete.
            ExecutionError: If the provider does not support skills, or the
                eagerly injected content exceeds ``runtime.skill_injection``.
        """
        rendered = self.renderer.render(agent.prompt, context)
        prefix = self._build_prompt_prefix(agent)
        if prefix:
            rendered = prefix + rendered
        return rendered

    def _resolve_skills_for_agent(self, agent: AgentDef) -> list[ResolvedSkill]:
        """Resolve the effective, on-disk skills for an agent.

        Resolution order:
        - If the agent explicitly sets ``skills`` (including ``[]``),
          that value wins and discovery does not apply. An agent that
          names its skills has said which ones it wants.
        - Otherwise it inherits the workflow-level default
          (``runtime.skills``) *plus* anything ``runtime.skill_discovery``
          turns up. Discovery joins the default set rather than forming a
          second axis, so ``skills: []`` remains the one opt-out.

        Note the inherited case can produce skills from an *empty*
        ``runtime.skills``: discovery alone is enough to enable them.

        Returns an empty list when no skills are enabled, or when the
        agent is not a provider-backed type (script / wait / set /
        terminate / human_gate / workflow — schema rejects ``skills``
        on these so this is defensive only).

        The ``capabilities.skills`` check lives here rather than in
        :meth:`_build_prompt_prefix` so it covers **both** delivery paths.
        Native providers never reach the eager-injection branch, so a
        provider that declared ``skills=False`` while supporting native
        loading would otherwise skip the check entirely.

        Raises:
            ExecutionError: If skills are enabled for an agent whose
                provider declares ``capabilities.skills=False``, or if
                discovery is enabled for a provider with no native skill
                surface.
            SkillNotFoundError: If an explicit entry cannot be resolved.
            SkillManifestError: If an explicit entry's ``SKILL.md`` is
                missing, unparseable, or incomplete.
        """
        if agent.type not in (None, "agent"):
            return []
        overridden = agent.skills is not None
        # Repeat the ``is not None`` rather than reusing ``overridden``: the
        # type checker does not narrow through an intermediate boolean.
        entries = list(agent.skills) if agent.skills is not None else list(self._workflow_skills)
        discovery = (
            self._skill_discovery if not overridden and self._skill_discovery.is_enabled else None
        )
        if not entries and discovery is None:
            return []
        self._reject_unsupported_skills(agent, entries, discovery is not None)
        if discovery is not None:
            self._reject_discovery_without_native_skills(agent, discovery)
        return self._resolve_skills(entries, discovery)

    def _merge_skills_and_plugin_skills(
        self, agent: AgentDef, plugin_skills: list[ResolvedSkill]
    ) -> list[ResolvedSkill]:
        """The agent's declared skills plus the ones its plugins ship.

        Both reach the provider through one ``skill_directories`` list,
        so the two sets are combined here — see :func:`_merge_skills` for
        how a name claimed by both is resolved.
        """
        return _merge_skills(
            self._resolve_skills_for_agent(agent),
            plugin_skills,
            on_warning=lambda message: _verbose_log(f"  Plugins: {message}", style="yellow"),
        )

    def _resolve_plugins_for_agent(self, agent: AgentDef) -> list[ResolvedPlugin]:
        """Resolve the effective, on-disk plugins for an agent.

        Tri-state resolution mirrors :meth:`_resolve_skills_for_agent`:
        an agent that sets ``plugins`` (including ``[]``) overrides the
        workflow default, otherwise it inherits ``runtime.plugins``.
        Discovery has no plugin equivalent by design — a plugin can
        launch MCP subprocesses with the user's credentials, so it is
        loaded only because a workflow named it.

        Returns an empty list when no plugins are enabled, or when the
        agent is not a provider-backed type (the schema rejects
        ``plugins`` on those, so that arm is defensive only).

        Raises:
            ExecutionError: If plugins are enabled for an agent whose
                provider cannot load them. Checked here as well as in
                :func:`conductor.config.validator.validate_workflow_config`
                because ``conductor run`` never invokes the static
                validator — the same reason
                :meth:`_reject_unsupported_skills` exists.
            PluginError: If an entry cannot be resolved, or a resolved
                plugin is unusable.
        """
        if agent.type not in (None, "agent"):
            return []
        entries = list(agent.plugins) if agent.plugins is not None else list(self._workflow_plugins)
        if not entries:
            return []
        self._reject_unsupported_plugins(agent, entries)
        self._reject_unfilterable_agents(agent, entries)
        return self._resolve_plugins(entries)

    def _reject_unfilterable_agents(self, agent: AgentDef, entries: list[PluginDef]) -> None:
        """Refuse ``agents: false`` where the provider cannot honour it.

        On a provider whose only skill surface is registering the plugin
        root, that registration also contributes every subagent the
        plugin ships. Running anyway would grant *more* than the workflow
        declared, which is worse than the silent drop this feature
        removes — so it is refused, matching the same check in
        :func:`conductor.config.validator.validate_workflow_config`.

        Duplicated here for the reason
        :meth:`_reject_unsupported_plugins` is: ``conductor run`` never
        invokes the static validator.

        Raises:
            ExecutionError: If a plugin disables agents while enabling
                skills on such a provider.
        """
        if not getattr(self.provider, "skills_require_plugin_root", False):
            return
        for entry in entries:
            if entry.skills and not entry.agents:
                raise ExecutionError(
                    f"Agent '{agent.name}': plugin {entry.name!r} sets 'agents: false' "
                    f"with skills enabled, which provider "
                    f"'{type(self.provider).__name__}' cannot honour — its only skill "
                    f"surface is registering the plugin root, which also contributes "
                    f"every subagent the plugin ships.",
                    agent_name=agent.name,
                    suggestion=(
                        "Set 'skills: false' as well, drop 'agents: false', or run "
                        "this agent on 'copilot', which registers each component "
                        "individually."
                    ),
                )

    def _reject_unsupported_plugins(self, agent: AgentDef, entries: list[PluginDef]) -> None:
        """Refuse plugins on a provider that cannot load them.

        Unlike skills, there is no provider-agnostic fallback: the
        executor can inject a skill's text into a prompt, but it cannot
        produce a subagent the model can dispatch to or an MCP server it
        can call. Loading only the skills would be the partial load this
        feature exists to remove, so the combination is refused.

        Raises:
            ExecutionError: If the provider declares
                ``capabilities.plugins=False``.
        """
        capabilities = getattr(type(self.provider), "CAPABILITIES", None)
        if capabilities is None or getattr(capabilities, "plugins", False):
            return
        provider = type(self.provider).__name__
        named = ", ".join(repr(entry.name) for entry in entries)
        raise ExecutionError(
            f"Agent '{agent.name}': provider '{provider}' cannot load plugins, but "
            f"plugins are enabled ({named}). A plugin's subagents and MCP servers "
            f"have no equivalent on this provider, so it would run with only part "
            f"of what the plugin ships.",
            agent_name=agent.name,
            suggestion=(
                "Reference the plugin's skills directly with 'skills:' (by path), "
                "or run this agent on a provider that loads plugins (copilot, "
                "claude-agent-sdk)."
            ),
        )

    def _resolve_plugins(self, entries: list[PluginDef]) -> list[ResolvedPlugin]:
        """Resolve plugin entries against the workflow file's directory.

        Memoized per entry list, because resolving a plugin walks its
        ``skills/`` tree and parses every ``*.agent.md`` it ships, and
        this runs once per agent per call.
        """
        from conductor.plugins.registry import resolve_plugins

        key = tuple((e.name, e.skills, e.agents, e.mcp) for e in entries)
        cached = self._plugin_cache.get(key)
        if cached is not None:
            return list(cached)

        resolved = resolve_plugins(
            entries,
            base_dir=self._workflow_dir,
            on_warning=lambda message: _verbose_log(f"  Plugins: {message}", style="yellow"),
        )
        for plugin in resolved:
            if not plugin.dropped:
                continue
            listed = "/".join(plugin.dropped)
            if (
                "hooks" in plugin.dropped
                and plugin.skills
                and getattr(self.provider, "skills_require_plugin_root", False)
            ):
                # Registering the root is the only way to reach this
                # provider's skills, and the root carries hooks with it —
                # so "does not load" would be false, about the one
                # component that runs arbitrary shell.
                _verbose_log(
                    f"  Plugins: {plugin.name} ships {listed}, and its hooks are "
                    f"exposed to the CLI because reaching its skills requires "
                    f"registering the plugin root.",
                    style="yellow",
                )
            else:
                _verbose_log(
                    f"  Plugins: {plugin.name} ships {listed} which Conductor does not load.",
                    style="yellow",
                )
        self._plugin_cache[key] = list(resolved)
        return resolved

    def _reject_discovery_without_native_skills(
        self, agent: AgentDef, discovery: SkillDiscoveryConfig
    ) -> None:
        """Refuse discovery on a provider that eagerly injects skill bodies.

        Mirrors the same check in
        :func:`conductor.config.validator.validate_workflow_config`.
        ``conductor validate`` rejects the combination, but ``conductor
        run`` never invokes the static validator — so without this the
        refusal holds in one command while the other quietly injects the
        whole ambient set into every prompt. ``runtime.skill_injection``
        is not a backstop: it only fires above ``max_bytes``, so a set
        under the limit passes silently and a set over it reports a budget
        problem rather than the real one.

        Raises:
            ExecutionError: If discovery is enabled for this agent and the
                provider has no native skill surface.
        """
        if getattr(self.provider, "supports_native_skills", False):
            return
        raise ExecutionError(
            f"Agent '{agent.name}': provider '{type(self.provider).__name__}' has no "
            f"native skill surface, but 'runtime.skill_discovery' is enabled "
            f"(sources={list(discovery.sources)!r}). Discovered skills would be injected "
            f"in full into every prompt, and the discovered set varies by machine, so "
            f"its size cannot be bounded.",
            agent_name=agent.name,
            suggestion=(
                "Name the skills you want in 'runtime.skills' (or this agent's "
                "'skills:'), or run this agent on a provider with progressive "
                "disclosure (copilot, claude-agent-sdk)."
            ),
        )

    def _resolve_skills(
        self, entries: list[str], discovery: SkillDiscoveryConfig | None
    ) -> list[ResolvedSkill]:
        """Resolve entries and discovery against the workflow file's directory.

        Both delivery paths — native ``skill_directories`` and eager
        preamble injection — go through here so names, paths,
        ``skills/`` roots and discovered skills behave identically
        regardless of provider.

        The result is memoized per ``(entries, discovery)`` because
        discovery globs the filesystem and this runs once per agent.
        """
        from conductor.skills import resolve_effective_skills

        sources = tuple(discovery.sources) if discovery else ()
        exclude = tuple(discovery.exclude) if discovery else ()
        key = (tuple(entries), sources, exclude)
        cached = self._skill_cache.get(key)
        if cached is not None:
            return list(cached)

        resolved = resolve_effective_skills(
            entries,
            sources=sources,
            exclude=exclude,
            base_dir=self._workflow_dir,
            on_warning=lambda message: _verbose_log(f"  Skills: {message}", style="yellow"),
        )
        if sources:
            _verbose_log(
                f"  Skills: discovery enabled ({', '.join(sources)}) — "
                f"{len(resolved)} skill(s) in effect: "
                f"{', '.join(item.name for item in resolved) or '(none)'}",
                style="dim",
            )
        self._skill_cache[key] = list(resolved)
        return resolved

    def _enforce_injection_budget(
        self,
        agent: AgentDef,
        resolved: list[ResolvedSkill],
        content: str,
        event_callback: EventCallback | None = None,
    ) -> None:
        """Apply ``runtime.skill_injection`` limits to eager skill content.

        Measured against the exact string being prepended, so the number
        reported is the number actually paid — on every call to this
        agent and on every retry. (A ``validator:`` block's own grading
        call bypasses prompt rendering and embeds only a truncated
        excerpt, so it does not re-pay this.)

        Args:
            agent: The agent the content is being injected for.
            resolved: The skills that produced the content, used to give
                a per-skill breakdown when a limit is hit.
            content: The rendered skill preamble.
            event_callback: Optional sink for ``skill_injection_warning``
                when the content exceeds ``warn_bytes``. The warning is
                also logged, but Conductor installs no logging handlers,
                so this is the channel that actually reaches the user.

        Raises:
            ExecutionError: If the content exceeds ``max_bytes``.
        """
        limits = self._skill_injection
        size = len(content.encode("utf-8"))
        approx_tokens = size // BYTES_PER_TOKEN_ESTIMATE
        provider = type(self.provider).__name__
        if limits.max_bytes is not None and size > limits.max_bytes:
            raise ExecutionError(
                f"Agent '{agent.name}': eagerly injected skill content is "
                f"{size:,} bytes (~{approx_tokens:,} tokens), over the "
                f"runtime.skill_injection.max_bytes limit of {limits.max_bytes:,}. "
                f"Provider '{provider}' has no native skill surface, so "
                f"this is prepended to every call and every retry.\n"
                f"{self._skill_size_breakdown(resolved)}",
                agent_name=agent.name,
                suggestion=(
                    "Enable fewer skills on this agent, trim the skills' "
                    "references/ trees, run it on a provider with progressive "
                    "disclosure (copilot, claude-agent-sdk), or raise "
                    "runtime.skill_injection.max_bytes."
                ),
            )
        if limits.warn_bytes is not None and size > limits.warn_bytes:
            breakdown = self._skill_size_breakdown(resolved)
            logger.warning(
                "Agent %r: eagerly injecting %s bytes (~%s tokens) of skill content "
                "on every call — provider %r has no progressive disclosure. %s",
                agent.name,
                f"{size:,}",
                f"{approx_tokens:,}",
                provider,
                breakdown,
            )
            # The log alone reaches nobody: Conductor installs no logging
            # handlers, so this surfaces via logging.lastResort as an
            # unattributed line on stderr, interleaved with console output and
            # absent from the JSONL log and the dashboard. Since the defaults
            # trip this for the bundled skill on every eager-provider call, it
            # has to travel the event channel too — same both-halves pattern as
            # ``checkpoint_save_failed`` in engine/workflow.py.
            if event_callback is not None:
                event_callback(
                    "skill_injection_warning",
                    {
                        "agent_name": agent.name,
                        "bytes": size,
                        "approx_tokens": approx_tokens,
                        "warn_bytes": limits.warn_bytes,
                        "provider": provider,
                        "breakdown": breakdown,
                    },
                )

    @staticmethod
    def _skill_size_breakdown(resolved: list[ResolvedSkill]) -> str:
        """Summarise each skill's on-disk injected size, largest first.

        Sizes are raw file bytes, so they total slightly below the measured
        rendered size, which also carries the ``<skills>`` envelope and one
        ``<skill>`` tag per entry.
        """
        sizes: list[tuple[str, int]] = []
        for item in resolved:
            total = 0
            for path in (
                item.directory / "SKILL.md",
                *sorted((item.directory / "references").glob("*.md")),
            ):
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
            sizes.append((item.name, total))
        sizes.sort(key=lambda pair: pair[1], reverse=True)
        return "Per skill: " + ", ".join(f"{name} {size:,}B" for name, size in sizes)

    def _build_prompt_prefix(
        self, agent: AgentDef, event_callback: EventCallback | None = None
    ) -> str:
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

        Raises:
            ExecutionError: If the injected content exceeds
                ``runtime.skill_injection``, or (via
                :meth:`_resolve_skills_for_agent`) if the provider
                declares it does not support skills.
        """
        parts: list[str] = []
        if self.instructions_preamble:
            parts.append(self.instructions_preamble)
        if not getattr(self.provider, "supports_native_skills", False):
            resolved = self._resolve_skills_for_agent(agent)
            if resolved:
                from conductor.skills import load_skill_content

                content = load_skill_content([(item.name, item.directory) for item in resolved])
                if content:
                    self._enforce_injection_budget(agent, resolved, content, event_callback)
                    parts.append(content)
        return "".join(parts)

    def _reject_unsupported_skills(
        self, agent: AgentDef, skill_entries: list[str], discovery_on: bool = False
    ) -> None:
        """Refuse skills on a provider that declares it does not support them.

        Mirrors the ``capabilities.skills`` check in
        :func:`conductor.config.validator.validate_workflow_config`.
        ``conductor validate`` already rejects the combination, but
        ``conductor run`` never invokes the static validator — so without
        this the declaration holds in one command and is silently
        contradicted in the other.

        A provider with no ``CAPABILITIES`` is left alone. That set is
        exactly the abstract ones: ``AgentProvider.__init_subclass__``
        raises at import time unless a subclass either declares
        ``CAPABILITIES`` or opts out with ``abstract=True``, so a real
        provider cannot reach this branch by forgetting to declare one.

        Args:
            agent: The agent whose skills are enabled.
            skill_entries: The declared entries, which may be empty when
                discovery alone enabled skills.
            discovery_on: Whether discovery contributed. Changes the
                message, because reporting ``skills=[]`` would name the
                documented *opt-out* syntax as the cause and suggest a
                remedy that is already in effect.

        Raises:
            ExecutionError: If the provider declares
                ``capabilities.skills=False``.
        """
        capabilities = getattr(type(self.provider), "CAPABILITIES", None)
        if capabilities is None or capabilities.skills:
            return
        if skill_entries:
            cause = f"declares skills={skill_entries!r}"
            remedy = "Remove the skills, opt out with 'skills: []', or override the "
        else:
            cause = "has skills enabled via 'runtime.skill_discovery'"
            remedy = "Disable runtime.skill_discovery, or override the "
        raise ExecutionError(
            f"Agent '{agent.name}' {cause} but provider "
            f"'{type(self.provider).__name__}' does not support skills "
            f"(capabilities.skills=False).",
            agent_name=agent.name,
            suggestion=(
                f"{remedy}agent to a skill-aware provider. 'conductor validate' "
                "reports this before a run starts."
            ),
        )
