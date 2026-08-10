"""Claude Agent SDK provider — delegates agentic loop to the claude-agent-sdk package."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from conductor.exceptions import ProviderError
from conductor.providers._schema import (
    SchemaDepthError,
    build_json_schema_field,
    build_json_schema_properties,
)
from conductor.providers.base import (
    AgentOutput,
    AgentProvider,
    EventCallback,
    refuse_mcp_server_clashes,
)
from conductor.providers.capabilities import ProviderCapabilities

if TYPE_CHECKING:
    from claude_agent_sdk import SdkPluginConfig  # ty: ignore[unresolved-import]

    from conductor.config.schema import AgentDef, OutputField
    from conductor.skills import SkillPlugin

try:
    from claude_agent_sdk import (  # ty: ignore[unresolved-import]
        AgentDefinition,
        ClaudeAgentOptions,
        query,
    )

    CLAUDE_AGENT_SDK_AVAILABLE = True
except ImportError:
    CLAUDE_AGENT_SDK_AVAILABLE = False
    query: Any = None
    ClaudeAgentOptions: Any = None
    AgentDefinition: Any = None

logger = logging.getLogger(__name__)


def _build_sdk_agents(custom_agents: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Translate plugin subagent specs into SDK ``AgentDefinition`` objects.

    The specs arrive in the Copilot SDK's ``CustomAgentConfig`` shape —
    :class:`~conductor.plugins.agents.PluginAgent` renders one canonical
    form and each provider adapts it, rather than the plugin layer
    growing a per-provider renderer.

    ``AgentDefinition`` has no ``name`` field: the SDK keys the mapping
    by name instead. ``infer`` has no counterpart and is dropped — it is
    Copilot's switch for "may the model dispatch to this", which is
    unconditionally true for a plugin agent Conductor registered on
    purpose.

    Args:
        custom_agents: Specs from the executor, or ``None``.

    Returns:
        A name-keyed mapping for ``ClaudeAgentOptions.agents``, or
        ``None`` when there are none. ``None`` rather than ``{}``
        deliberately: unlike ``skills``, an empty mapping here has no
        opt-out meaning, so leaving the field at its default keeps the
        option out of the request entirely.
    """
    if not custom_agents:
        return None
    if AgentDefinition is None:
        raise ProviderError(
            "Plugin subagents were requested but the installed claude-agent-sdk "
            "does not provide AgentDefinition.",
            suggestion="Upgrade claude-agent-sdk, or run this agent on 'copilot'.",
            is_retryable=False,
        )
    agents: dict[str, Any] = {}
    for spec in custom_agents:
        name = spec["name"]
        if spec.get("tools") is not None:
            # A plugin's ``tools:`` frontmatter is written in its authoring
            # CLI's vocabulary. Copilot writes ``read`` / ``execute``; this
            # CLI's identifiers are ``Read`` / ``Bash``. Conductor searches
            # both CLIs' install roots and recognises both manifest
            # conventions, so a Copilot-authored plugin genuinely arrives
            # here — and forwarding its list unchanged hands the subagent a
            # tool set containing no valid identifier. Dropping the list
            # instead would silently widen the agent to the session default.
            # Both are wrong, and the same reasoning already refuses a
            # narrowing per-server MCP filter and the per-agent allowlist.
            raise ProviderError(
                f"Plugin subagent '{name}' declares tools={spec['tools']!r}, which "
                f"claude-agent-sdk cannot honour — a plugin's tool names are written "
                f"in its authoring CLI's vocabulary and do not translate to Claude CLI "
                f"tool IDs.",
                suggestion=(
                    f"Set 'agents: false' on the plugin shipping '{name}', remove the "
                    f"'tools:' line from its agent definition to inherit the session "
                    f"default, or run this agent on 'copilot'."
                ),
                is_retryable=False,
            )
        agents[name] = AgentDefinition(
            description=spec["description"],
            prompt=spec["prompt"],
        )
    return agents


def _build_field_schema(field: OutputField, depth: int = 0) -> dict[str, Any]:
    """Thin delegate to the shared JSON-Schema field builder.

    Keep this entry point intact because tests import it directly. Depth
    errors from the core are translated to the historical ProviderError
    message so downstream assertions stay stable.
    """
    try:
        return build_json_schema_field(field, depth=depth, max_depth=10)
    except SchemaDepthError as exc:
        # Pinned message: downstream tests assert the exact text.
        raise ProviderError("Output schema nesting exceeds 10 levels") from exc


def _build_properties(fields: dict[str, OutputField], depth: int = 0) -> dict[str, Any]:
    """Thin delegate to the shared JSON-Schema properties builder."""
    try:
        return build_json_schema_properties(fields, depth=depth, max_depth=10)
    except SchemaDepthError as exc:
        # Pinned message: downstream tests assert the exact text.
        raise ProviderError("Output schema nesting exceeds 10 levels") from exc


def _build_output_format(output: dict[str, OutputField]) -> dict[str, Any]:
    """Build the ``output_format`` payload passed to ``ClaudeAgentOptions``.

    The SDK expects a wrapping ``{"type": "json_schema", "schema": ...}`` object
    around the actual JSON-Schema document. All declared fields are marked
    required in the schema sent to the SDK. Conductor does not currently
    validate the SDK's returned content against this schema — a missing
    key produces a dict with that key absent rather than a hard failure.
    If schema validation is added later, revisit this default.
    """
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": _build_properties(output),
            "required": list(output.keys()),
        },
    }


# Default tool preset granted when an agent omits the `tools:` list. This
# mirrors the SDK's `claude_code` preset (filesystem, bash, web, etc.) — i.e.
# the same behavior the user gets when running the `claude` CLI directly. It is
# selected from the RAW ``agent.tools is None`` signal, NOT from the executor's
# resolved list: for an agent that declares no `tools:`, the executor returns the
# workflow-tools copy, which is empty only when the workflow declares no `tools:`.
_DEFAULT_TOOL_PRESET: dict[str, str] = {"type": "preset", "preset": "claude_code"}

# Native CLI tool that loads an enabled skill on demand. An explicit
# ``tools: []`` sends ``--tools ""`` (empty base tool set), which would leave a
# declared skill unreachable, so this one tool is granted back when skills are
# enabled.
_SKILL_TOOL: Final[str] = "Skill"

# Keys ``_translate_mcp_servers`` can carry onto the SDK's config shapes.
# ``tools`` and ``timeout`` are handled explicitly above (refused / warned),
# so they count as recognised even though they are not forwarded.
_STDIO_KEYS: Final[frozenset[str]] = frozenset(
    {"type", "command", "args", "env", "tools", "timeout"}
)
_REMOTE_KEYS: Final[frozenset[str]] = frozenset({"type", "url", "headers", "tools", "timeout"})

# Display-only previews for the verbose CLI pretty-printer (NOT surfaced
# in events — see ``_TOOL_RESULT_PREVIEW_LEN`` below for the on-the-wire
# truncation).
_VERBOSE_ARG_PREVIEW_LEN: Final[int] = 200
_VERBOSE_RESULT_PREVIEW_LEN: Final[int] = 200
_REASONING_PREVIEW_LEN: Final[int] = 150

# ``_TOOL_RESULT_PREVIEW_LEN`` is load-bearing: it is the upper limit the
# dashboard and JSONL stream observe for ``agent_tool_complete`` results.
# Changing it changes what every downstream consumer sees.
_TOOL_RESULT_PREVIEW_LEN: Final[int] = 500

# Default SDK-recognized model when neither the agent nor the workflow sets
# one. The string must match a model alias accepted by the upstream
# ``claude-agent-sdk`` package; revalidate when bumping the upstream pin
# in pyproject.toml.
_DEFAULT_MODEL: Final[str] = "claude-sonnet-4-5"

# Sentinel meaning "expose every tool this server offers" in
# ``MCPServerDef.tools``. Any other value is a narrowing filter the SDK has no
# way to express — see :func:`_translate_mcp_servers`.
_ALL_TOOLS: Final[str] = "*"


def _translate_mcp_servers(mcp_servers: dict[str, Any]) -> dict[str, Any]:
    """Translate Conductor MCP server configs into the SDK's config shapes.

    The input is the already-resolved mapping built by
    :func:`conductor.cli.run._build_mcp_servers` — ``env`` values have been
    expanded from the process environment and any OAuth ``Authorization``
    header has been fetched by the time it reaches us. Output matches the
    SDK's ``McpStdioServerConfig`` / ``McpHttpServerConfig`` /
    ``McpSSEServerConfig`` TypedDicts.

    Two Conductor fields have no SDK counterpart and are handled differently
    on purpose:

    * ``tools`` — a per-server allowlist. ``["*"]`` (the default) means "no
      filter" and is simply dropped. Any narrowing value is **refused**:
      ignoring it would hand the model more tools than the workflow declared,
      the same security regression that justifies refusing the per-agent
      ``tools:`` allowlist elsewhere in this provider.
    * ``timeout`` — dropped with a warning. Unlike a tool filter, losing a
      timeout cannot widen tool access, so it does not warrant a hard failure.

    Args:
        mcp_servers: Mapping of server name to resolved Conductor config.

    Returns:
        Mapping of server name to SDK-shaped config dict.

    Raises:
        ProviderError: If a server declares a narrowing ``tools`` filter,
            omits a field its type requires, or carries a type this provider
            cannot translate.
    """
    translated: dict[str, Any] = {}

    for name, config in mcp_servers.items():
        server_type = config.get("type") or "stdio"

        tools = config.get("tools")
        if tools is not None and list(tools) != [_ALL_TOOLS]:
            raise ProviderError(
                f"MCP server '{name}' declares a tool filter tools={list(tools)!r}, "
                "but claude-agent-sdk cannot enforce per-server tool filters "
                "(the SDK's MCP config has no equivalent field). Forwarding the "
                "server unfiltered would grant more tools than declared.",
                suggestion=(
                    f"Set 'tools: [\"*\"]' on MCP server '{name}' to accept every "
                    "tool it offers, or use the 'copilot' provider for agents that "
                    "need per-server tool filtering."
                ),
                # Config errors never become valid on a retry. Set explicitly:
                # the default heuristic sniffs the message for "timeout" /
                # "connection", which user-controlled server and tool names
                # could otherwise trip.
                is_retryable=False,
            )

        if config.get("timeout") is not None:
            logger.warning(
                "MCP server '%s' sets timeout=%s, which claude-agent-sdk does not "
                "support; the CLI's own default will apply instead.",
                name,
                config["timeout"],
            )

        # Fail closed on anything this translation cannot carry. The function
        # was written for ``MCPServerDef``'s closed field set; a plugin's
        # ``.mcp.json`` is arbitrary third-party JSON, and dropping a key it
        # declares starts a server configured differently from what its author
        # wrote — an ``oauth`` block silently becoming an unauthenticated
        # request, or ``disabled: true`` becoming a launched subprocess. Same
        # standard the narrowing ``tools:`` filter is held to just below.
        recognised = _STDIO_KEYS if server_type == "stdio" else _REMOTE_KEYS
        unknown = sorted(set(config) - recognised)
        if unknown:
            raise ProviderError(
                f"MCP server '{name}' declares key(s) {unknown!r} that claude-agent-sdk's "
                f"config has no equivalent for. Forwarding it without them would start a "
                f"server configured differently from what was declared.",
                suggestion=(
                    f"Set 'mcp: false' on the plugin shipping '{name}', declare the "
                    f"server in 'runtime.mcp_servers' where Conductor resolves it in "
                    f"full, or run this agent on 'copilot'."
                ),
                is_retryable=False,
            )

        if server_type == "stdio":
            command = config.get("command")
            if not command:
                raise ProviderError(
                    f"MCP server '{name}' is type 'stdio' but declares no 'command'.",
                    suggestion=f"Add a 'command:' to MCP server '{name}'.",
                    is_retryable=False,
                )
            entry: dict[str, Any] = {"type": "stdio", "command": command}
            if config.get("args"):
                entry["args"] = list(config["args"])
            if config.get("env"):
                entry["env"] = dict(config["env"])
        elif server_type in ("http", "sse"):
            url = config.get("url")
            if not url:
                raise ProviderError(
                    f"MCP server '{name}' is type '{server_type}' but declares no 'url'.",
                    suggestion=f"Add a 'url:' to MCP server '{name}'.",
                    is_retryable=False,
                )
            entry = {"type": server_type, "url": url}
            if config.get("headers"):
                entry["headers"] = dict(config["headers"])
        else:
            raise ProviderError(
                f"MCP server '{name}' has unsupported type '{server_type}' for "
                "claude-agent-sdk (expected 'stdio', 'http', or 'sse').",
                is_retryable=False,
            )

        translated[name] = entry

    return translated


def _write_mcp_config(servers: dict[str, Any]) -> str:
    """Write ``servers`` to a private temp file and return its path.

    The SDK serializes a ``mcp_servers`` *dict* straight into a
    ``--mcp-config <json>`` command-line argument, which would publish resolved
    stdio ``env`` values and http/sse ``Authorization`` headers to anyone who
    can read ``/proc/<pid>/cmdline``. Passing a path instead keeps those
    secrets in a file only the current user can read.

    ``tempfile.mkstemp`` creates the file with mode ``0600`` and ``O_EXCL``, so
    the secrets are never briefly world-readable.

    The payload uses the CLI's ``{"mcpServers": {...}}`` envelope — a bare
    mapping is rejected with "mcpServers: Invalid input: expected record,
    received undefined".

    Args:
        servers: Already-translated, SDK-shaped server configs.

    Returns:
        Absolute path to the config file. The caller owns its removal.
    """
    fd, path = tempfile.mkstemp(prefix="conductor-mcp-", suffix=".json")
    try:
        # os.fdopen takes ownership of fd only once it returns; close fd
        # ourselves if it raises, or the descriptor leaks.
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            json.dump({"mcpServers": servers}, handle)
    except BaseException:
        # Includes KeyboardInterrupt mid-write: a partial secrets file is
        # worse than none.
        _remove_mcp_config(path)
        raise
    return path


def _remove_mcp_config(path: str) -> None:
    """Delete an MCP config file written by :func:`_write_mcp_config`.

    Best-effort: a cleanup failure must never mask the error that is already
    propagating, so removal problems are reported and swallowed.

    Args:
        path: Path returned by :func:`_write_mcp_config`.
    """
    try:
        os.unlink(path)
    except OSError:
        # WARNING, not DEBUG: this file holds resolved MCP credentials, and
        # the user is the only one who can clean it up. Conductor installs no
        # logging handlers, so DEBUG here would reach nobody.
        logger.warning(
            "Failed to remove MCP config file %s; it contains resolved MCP "
            "credentials and should be deleted manually.",
            path,
            exc_info=True,
        )


def _resolve_skill_plugins(
    skill_directories: list[str] | None,
) -> tuple[list[str], list[SdkPluginConfig]]:
    """Map resolved skill directories to SDK ``skills`` / ``plugins`` options.

    The SDK has no "skill directory" surface: a skill is enabled by name and
    discovered through the plugin that owns it. Each directory is therefore
    resolved back to its Claude Code plugin root, which is registered via
    ``plugins`` (``--plugin-dir``) and referenced by the plugin-qualified
    ``<plugin>:<skill>`` name.

    Args:
        skill_directories: Absolute skill directory paths from
            :class:`~conductor.executor.agent.AgentExecutor`, or ``None``
            when no skills are enabled.

    Returns:
        A ``(skill_names, plugin_configs)`` tuple. The lists are not
        index-parallel: two skills shipped by one plugin produce two names
        and a single plugin registration. Both are empty when no skills are
        enabled — and an empty ``skills`` list is meaningful to the SDK: it
        suppresses every skill rather than falling back to CLI discovery
        defaults.

    Raises:
        ProviderError: If a skill cannot be turned into a name the CLI will
            resolve — it lives under no plugin root, its plugin manifest is
            unusable, or two plugins claim the same qualified name. Each of
            those would otherwise hand the agent less than the workflow
            declared, silently.
    """
    if not skill_directories:
        return [], []

    from conductor.skills import SkillPluginError, resolve_skill_plugin

    plugins: list[SkillPlugin] = []
    for directory in skill_directories:
        try:
            plugin = resolve_skill_plugin(Path(directory))
        except SkillPluginError as exc:
            raise ProviderError(
                f"Skill directory {directory!r} belongs to a Claude Code plugin that "
                f"cannot be loaded: {exc}",
                suggestion=(
                    "Repair the plugin, or run this agent on a provider that loads "
                    "skill directories directly (copilot). A reinstall usually fixes "
                    "this for a built-in skill."
                ),
                is_retryable=False,
            ) from exc
        if plugin is None:
            raise ProviderError(
                f"Skill directory {directory!r} is not part of a Claude Code plugin "
                "(no .claude-plugin/plugin.json shipping it in the nearest parent "
                "directories), and claude-agent-sdk can only load skills that a "
                "plugin provides.",
                suggestion=(
                    "Package the skill as a plugin, or run this agent on a "
                    "provider that loads skill directories directly (copilot)."
                ),
                is_retryable=False,
            )
        plugins.append(plugin)

    # Two skills can ship from one plugin: register the root once but keep every
    # name. Dropping a name would under-serve the workflow, so a genuine clash --
    # two different roots claiming one qualified name -- is refused rather than
    # deduped away.
    claimed: dict[str, Path] = {}
    for plugin in plugins:
        prior = claimed.setdefault(plugin.qualified_name, plugin.plugin_root)
        if prior != plugin.plugin_root:
            raise ProviderError(
                f"Two different plugins both provide the skill "
                f"{plugin.qualified_name!r}: {prior} and {plugin.plugin_root}. The CLI "
                "cannot tell them apart, so one of the skills this workflow declared "
                "would be dropped.",
                suggestion=(
                    "Rename one of them in its .claude-plugin/plugin.json, or enable "
                    "only one of the two."
                ),
                is_retryable=False,
            )

    skill_names = list(claimed)
    plugin_paths = list(dict.fromkeys(str(p.plugin_root) for p in plugins))
    logger.debug("Enabling skills %s from plugin roots %s", skill_names, plugin_paths)
    return skill_names, [{"type": "local", "path": path} for path in plugin_paths]


class ClaudeAgentSdkProvider(AgentProvider):
    """Claude Agent SDK provider.

    Uses the claude-agent-sdk package (async iterator API) to execute agents.
    The SDK manages the agentic loop, tool execution, and structured output
    extraction internally.
    """

    CAPABILITIES = ProviderCapabilities(
        tier="experimental",
        # Workflow-level ``runtime.mcp_servers`` are translated to the SDK's
        # own MCP config shapes and passed via ``ClaudeAgentOptions``. Only
        # declared servers attach: ``strict_mcp_config`` is always set, so
        # ambient project/user MCP config is ignored. A narrowing per-server
        # ``tools:`` filter has no SDK equivalent and is refused.
        mcp_tools=True,
        # Per-agent ``tools: []`` disables all *built-in* tools except the
        # ``Skill`` loader when skills are enabled; declared MCP servers
        # still attach (the SDK has no per-request MCP toggle), which
        # is why the validator rejects ``tools: []`` alongside ``mcp_servers:``
        # for this provider. Per-agent ``tools: [<names>]`` is refused loudly
        # at execute time because workflow tool names do not translate to
        # Claude CLI tool IDs.
        # The capability records the strict end of that contract — when the
        # user declares a non-empty allowlist, the validator surfaces it as
        # an error before runtime hits the refusal.
        workflow_tools_passthrough=False,
        # The SDK yields messages incrementally via the async iterator —
        # ``agent_message`` / ``agent_tool_*`` events fire as they arrive.
        streaming_events=True,
        # ``ThinkingBlock`` content is forwarded as ``agent_reasoning``.
        agent_reasoning_events=True,
        # The SDK does expose an ``effort`` field on ClaudeAgentOptions,
        # but the provider does not currently wire ``agent.reasoning.effort``
        # through to it. Declare ``None`` until that plumbing exists.
        reasoning_effort=None,
        # The SDK's ``output_format={"type": "json_schema", ...}`` plus
        # follow-on JSON parsing approximates native schema enforcement,
        # but the model still occasionally returns prose. Mark as
        # prompt-injection to keep the validator honest.
        structured_output="prompt_injection",
        # ``interrupt_signal`` is checked between SDK messages and triggers
        # a partial-output return.
        interrupt=True,
        # ``max_session_seconds`` is enforced between messages via
        # ``time.monotonic()``.
        max_session_seconds=True,
        # The SDK manages its own session state inside the ``claude`` CLI;
        # Conductor does not persist or replay it through resume.
        checkpoint_resume=False,
        # Token counts come from ``ResultMessage.usage`` (cumulative
        # session total — see A4 fix).
        usage_tracking=True,
        # No global mutable state shared across calls — the SDK spawns
        # an independent subprocess per query() invocation.
        concurrent_safe=True,
        # The engine-resolved working directory is forwarded to
        # ``ClaudeAgentOptions.cwd``, which the SDK applies as the ``claude``
        # subprocess's cwd. Stdio MCP servers inherit it from that subprocess
        # rather than being stamped individually as they are for Copilot:
        # the SDK's ``McpStdioServerConfig`` has no cwd field.
        working_dir=True,
        # Skills are loaded natively: the owning plugin is registered via
        # ``ClaudeAgentOptions.plugins`` and enabled by its qualified name
        # through ``skills``, so the model reads the frontmatter up front
        # and the body on demand. ``skills`` is also set (to ``[]``) when
        # the workflow declares none — see the option block in ``execute``
        # for why that empty list is what makes ``skills: []`` an opt-out.
        skills=True,
        # Whole plugins are supported by deconstruction: skills through the
        # plugin/qualified-name path below, subagents through
        # ``ClaudeAgentOptions.agents``, MCP through the same temp-file
        # config the workflow's own servers use.
        plugins=True,
        upstream_pin="claude-agent-sdk>=0.2.82",
        maintainer="@lesandiz (best-effort)",
    )

    def __init__(
        self,
        model: str | None = None,
        max_turns: int | None = None,
        max_session_seconds: float | None = None,
        mcp_servers: dict[str, Any] | None = None,
    ) -> None:
        if not CLAUDE_AGENT_SDK_AVAILABLE:
            raise ProviderError(
                "Claude Agent SDK not installed",
                suggestion="Install with: uv add 'claude-agent-sdk>=0.2.82'",
            )

        self._default_model = model or _DEFAULT_MODEL
        self._default_max_turns = max_turns if max_turns is not None else 50
        self._max_session_seconds = max_session_seconds
        # Translate once, here, rather than per execute() call. Providers are
        # constructed lazily, so an untranslatable server config surfaces when
        # the first agent on this provider runs — not at `conductor validate`.
        self._mcp_servers = _translate_mcp_servers(mcp_servers) if mcp_servers else {}

    @property
    def supports_native_skills(self) -> bool:
        """Skills load through the SDK, not through prompt injection.

        :class:`~conductor.executor.agent.AgentExecutor` forwards the
        resolved skill directories on the :meth:`execute`
        ``skill_directories`` kwarg and skips eager preamble injection.
        Each directory is resolved to its owning Claude Code plugin, which
        is registered once and whose skills are enabled by name, so the CLI
        loads only the ``SKILL.md`` frontmatter up front and reads the body
        on demand.
        """
        return True

    @property
    def supports_native_plugins(self) -> bool:
        """Plugin subagents register as ``ClaudeAgentOptions.agents``.

        The SDK takes inline agent definitions keyed by name, so a
        plugin's subagents are registered individually rather than being
        inherited from the plugin root.

        That distinction matters because registering a root is *not*
        filterable: the SDK documents ``plugins`` as providing "custom
        commands, agents, skills, and hooks", with a ``skills`` filter and
        no equivalent for the rest. Conductor still has to register the
        root when a plugin's skills are enabled — the SDK has no bare
        skill-directory surface — so on this provider ``agents: false``
        cannot be honored alongside ``skills: true`` for the same plugin.
        :func:`conductor.config.validator.validate_workflow_config`
        refuses that combination rather than quietly granting more than
        the workflow declared.
        """
        return True

    @property
    def skills_require_plugin_root(self) -> bool:
        """This SDK has no bare skill-directory surface — see above."""
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
        if query is None or ClaudeAgentOptions is None:
            raise ProviderError("Claude Agent SDK not available")

        # Resolved up front so an unloadable skill fails the run rather than
        # quietly handing the agent less than the workflow declared. Providers
        # are constructed lazily, so this surfaces when the first agent on this
        # provider runs, not at `conductor validate`.
        skill_names, skill_plugins = _resolve_skill_plugins(skill_directories)

        # Verbose / full-mode flags drive optional diagnostic output. They
        # live in the CLI layer, so importing them couples this provider
        # to the CLI. Wrap defensively so library users (no CLI installed)
        # still get a working provider — just without the verbose pretty-printer.
        try:
            from conductor.cli.app import is_full, is_verbose

            verbose_enabled = is_verbose()
            full_enabled = is_full()
        except ImportError:
            verbose_enabled = False
            full_enabled = False

        model = agent.model or self._default_model
        max_turns = (
            agent.max_agent_iterations
            if agent.max_agent_iterations is not None
            else self._default_max_turns
        )

        # Per-agent ``max_session_seconds`` overrides the provider default,
        # matching Copilot / Claude semantics. ``None`` means "no timeout".
        max_session_seconds = (
            agent.max_session_seconds
            if agent.max_session_seconds is not None
            else self._max_session_seconds
        )

        sdk_tools, permission_mode = self._resolve_tool_config(
            tools,
            agent,
            skills_enabled=bool(skill_names),
            agents_enabled=bool(custom_agents),
        )

        # ``os.getcwd()`` raises ``OSError`` when the process cwd has been
        # deleted or an ancestor lost traversal permission. Resolve it here
        # with a dedicated handler rather than leaning on the generic arm
        # below, which would report a vanished cwd as a CLI installation
        # problem and hand back a bare pathless errno.
        try:
            resolved_cwd = agent.working_dir or os.getcwd()
        except OSError as exc:
            raise ProviderError(
                f"Agent '{agent.name}' declares no working_dir and the process working "
                f"directory could not be resolved: {exc}",
                suggestion=(
                    "The directory conductor was launched from has been deleted or is "
                    "no longer readable. Re-run from an existing directory, or set an "
                    "explicit working_dir on the agent or runtime."
                ),
                is_retryable=False,
            ) from exc

        options = ClaudeAgentOptions(
            model=model,
            system_prompt=agent.system_prompt,
            # Already resolved by ``WorkflowEngine._resolve_agent_working_dir``
            # (agent over runtime, rendered, absolutized, existence-checked),
            # so pass it through verbatim rather than re-resolving — that would
            # collapse the symlink aliases the engine preserves.
            cwd=resolved_cwd,
            output_format=_build_output_format(agent.output) if agent.output else None,
            max_turns=max_turns,
            permission_mode=permission_mode,
            tools=sdk_tools,
            # Unconditional, including when this workflow declares no servers:
            # the CLI would otherwise load project .mcp.json, user-global, and
            # plugin-provided servers, and permission_mode bypasses approval
            # for whatever they expose. Only declared servers may attach.
            strict_mcp_config=True,
            # The skills counterpart of strict_mcp_config, and unconditional
            # for the same reason: left unset, the CLI loads user settings
            # (~/.claude/settings.json), project settings (.claude/settings.json)
            # and local settings — which between them bring in ambient skills,
            # CLAUDE.md, and hooks the workflow never declared. Setting `skills`
            # makes this doubly load-bearing: the SDK re-defaults setting_sources
            # to ["user", "project"] whenever `skills` is set and this is None.
            # Conductor surfaces instruction files through its own opt-in
            # `--workspace-instructions`; settings and hooks have no equivalent.
            setting_sources=[],
            # Load-bearing but invisible in argv: the SDK forwards an explicit
            # list in the `initialize` control request (_internal/query.py), and
            # only there does [] differ from None. None means "CLI defaults
            # apply", [] means "enable no skills" — which is what makes
            # `skills: []` an honest opt-out. Note this is a context filter, not
            # a sandbox: unlisted skills are hidden from the model's listing and
            # rejected by the Skill tool, but their files stay readable on disk.
            skills=skill_names,
            # Unlike `skills`, [] is already this field's default and means
            # nothing special.
            plugins=skill_plugins,
            # Plugin subagents are registered inline rather than inherited
            # from a plugin root, so a plugin whose skills are disabled can
            # still contribute agents. The reverse is refused — reaching a
            # plugin's skills here means registering its root, which carries
            # the subagents with it, so `agents: false` alongside enabled
            # skills cannot be honoured. Keyed by the qualified
            # ``<plugin>:<agent>`` name so two plugins shipping a same-named
            # agent do not collide.
            agents=_build_sdk_agents(custom_agents),
        )

        content_parts: list[str] = []
        structured_output: Any = None
        total_input_tokens = 0
        total_output_tokens = 0
        result_model: str | None = model
        turn_count = 0
        # Track pending tool_use IDs so we can pair them with ToolResultBlocks
        pending_tools: dict[str, str] = {}
        session_start = time.monotonic()

        # Written inside the try below, never before it: the file holds
        # resolved MCP credentials, so every path out of this method must
        # reach the finally that reclaims it.
        mcp_config_path: str | None = None
        agen: Any = None

        try:
            # Plugin-contributed servers merge on top for this call only:
            # ``plugins:`` is a per-agent field while providers are cached per
            # type. They are translated here rather than in ``__init__`` for
            # the same reason. Note ``strict_mcp_config=True`` above suppresses
            # servers a registered plugin root would otherwise contribute, so
            # a plugin's servers reach the CLI only through this path — which
            # is what makes ``mcp: false`` mean something on this provider.
            session_servers = dict(self._mcp_servers)
            if extra_mcp_servers:
                translated = _translate_mcp_servers(extra_mcp_servers)
                refuse_mcp_server_clashes(translated, session_servers)
                session_servers.update(translated)
            if session_servers:
                mcp_config_path = _write_mcp_config(session_servers)
                options.mcp_servers = mcp_config_path

            # Signal "awaiting model" before entering the SDK iterator: the
            # SDK is about to make the first model call. Dashboards use this
            # to show a "waiting for model" spinner.
            if event_callback:
                _safe_callback(
                    event_callback,
                    "agent_turn_start",
                    {"turn": "awaiting_model"},
                )

            agen = query(prompt=rendered_prompt, options=options)
            async for message in agen:
                if interrupt_signal is not None and interrupt_signal.is_set():
                    return self._build_output(
                        content_parts,
                        structured_output,
                        agent,
                        result_model,
                        total_input_tokens,
                        total_output_tokens,
                        partial=True,
                    )

                # Wall-clock session timeout. The SDK does not expose a per-call
                # timeout, so enforce at each message boundary — the cheapest
                # cancellation point we have. The check is between messages
                # rather than around the full ``async for`` so we can return
                # a clean ProviderError rather than letting asyncio raise.
                if max_session_seconds is not None:
                    elapsed = time.monotonic() - session_start
                    if elapsed > max_session_seconds:
                        raise ProviderError(
                            f"Agent '{agent.name}' exceeded maximum session "
                            f"duration of {max_session_seconds:.0f}s "
                            f"after {turn_count} turn(s)",
                            is_retryable=False,
                        )

                msg_type = type(message).__name__

                if msg_type == "AssistantMessage":
                    msg = cast(Any, message)
                    # Iteration N begins when its assistant response arrives.
                    # Emit BEFORE processing blocks so per-parity rules the
                    # turn marker bounds the iteration's content events.
                    turn_count += 1
                    if event_callback:
                        _safe_callback(
                            event_callback,
                            "agent_turn_start",
                            {"turn": turn_count},
                        )

                    blocks = getattr(msg, "content", None)
                    has_tool_use = False
                    if blocks:
                        has_tool_use = any(
                            (getattr(b, "type", None) or type(b).__name__)
                            in ("tool_use", "ToolUseBlock")
                            for b in blocks
                        )
                        self._process_assistant_blocks(
                            blocks,
                            content_parts,
                            pending_tools,
                            event_callback,
                            verbose_enabled,
                            full_enabled,
                        )

                    if hasattr(msg, "model") and msg.model:
                        result_model = msg.model
                    if hasattr(msg, "usage") and msg.usage:
                        total_input_tokens += msg.usage.get("input_tokens", 0)
                        total_output_tokens += msg.usage.get("output_tokens", 0)

                    # If this turn requested tool calls, the SDK will run
                    # them and then make another model call. Signal
                    # "awaiting model" again so the spinner stays on
                    # through the tool roundtrip.
                    if has_tool_use and event_callback:
                        _safe_callback(
                            event_callback,
                            "agent_turn_start",
                            {"turn": "awaiting_model"},
                        )

                elif msg_type == "UserMessage":
                    msg_content = getattr(message, "content", None)
                    if msg_content:
                        self._process_tool_results(
                            msg_content,
                            pending_tools,
                            event_callback,
                            verbose_enabled,
                            full_enabled,
                        )

                elif msg_type == "ResultMessage":
                    msg = cast(Any, message)
                    if getattr(msg, "structured_output", None) is not None:
                        structured_output = msg.structured_output
                    elif getattr(msg, "result", None) and not content_parts:
                        content_parts.append(msg.result)
                    # ``ResultMessage.usage`` is the CUMULATIVE session total
                    # (per the SDK docstring on ApiUsage.apiUsage). Replace
                    # rather than add — the per-AssistantMessage running sum
                    # exists only as a fallback when no ResultMessage arrives
                    # (e.g. mid-stream interrupt).
                    if hasattr(msg, "usage") and msg.usage:
                        total_input_tokens = msg.usage.get("input_tokens", total_input_tokens)
                        total_output_tokens = msg.usage.get("output_tokens", total_output_tokens)
                    if getattr(msg, "is_error", False):
                        raise ProviderError(
                            self._build_error_message(msg),
                            is_retryable=_is_retryable_result(msg),
                        )

        except ProviderError:
            raise
        except asyncio.CancelledError:
            # Do NOT translate into ProviderError — upstream interrupt
            # handlers rely on CancelledError to unwind cleanly.
            raise
        except Exception as e:
            raise ProviderError(
                f"Claude Agent SDK execution error: {e}",
                suggestion=_classify_error_suggestion(e),
                is_retryable=_is_retryable_exception(e),
            ) from e
        finally:
            # Order matters: close the SDK iterator first so the `claude`
            # subprocess is gone before its config file disappears. Abandoning
            # the generator (the interrupt path returns mid-loop) otherwise
            # defers teardown to the GC, and on Windows unlinking a file the
            # live subprocess still holds open raises PermissionError.
            if agen is not None:
                aclose = getattr(agen, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
            if mcp_config_path is not None:
                _remove_mcp_config(mcp_config_path)

        return self._build_output(
            content_parts,
            structured_output,
            agent,
            result_model,
            total_input_tokens,
            total_output_tokens,
        )

    async def validate_connection(self) -> bool:
        """Check that the SDK is importable and the ``claude`` CLI is locatable.

        Mirrors the SDK's own CLI lookup logic (bundled binary first, then
        ``shutil.which``, then the SDK's hardcoded fallback locations). We
        avoid an actual API round-trip because that would require valid
        credentials and consume tokens — caller code can still surface auth
        failures at first ``execute()``.

        Returns:
            True when both the SDK import and CLI lookup succeed.
        """
        if not CLAUDE_AGENT_SDK_AVAILABLE:
            return False

        import shutil
        from pathlib import Path

        # Bundled CLI takes precedence (matches the SDK's own resolution).
        try:
            import claude_agent_sdk  # ty: ignore[unresolved-import]

            sdk_dir = Path(claude_agent_sdk.__file__).parent
            for candidate in (sdk_dir / "_bundled" / "claude",):
                if candidate.exists() and candidate.is_file():
                    return True
        except Exception:
            logger.debug("Bundled CLI probe failed", exc_info=True)

        if shutil.which("claude"):
            return True

        # SDK's hardcoded fallback locations — keep in sync with
        # claude_agent_sdk._internal.transport.subprocess_cli._find_cli.
        for path in (
            Path.home() / ".npm-global/bin/claude",
            Path("/usr/local/bin/claude"),
            Path.home() / ".local/bin/claude",
            Path.home() / "node_modules/.bin/claude",
            Path.home() / ".yarn/bin/claude",
            Path.home() / ".claude/local/claude",
        ):
            if path.exists() and path.is_file():
                return True

        logger.warning(
            "Claude CLI not found on PATH, in bundled package, or in any "
            "known fallback location. Install with `npm install -g "
            "@anthropic-ai/claude-code`."
        )
        return False

    async def close(self) -> None:
        pass

    @staticmethod
    def _resolve_tool_config(
        tools: list[str] | None,
        agent: AgentDef,
        *,
        skills_enabled: bool,
        agents_enabled: bool = False,
    ) -> tuple[Any, str | None]:
        """Resolve the SDK ``tools`` and ``permission_mode`` for an agent.

        Conductor's ``tools:`` allowlist contains workflow-tool names that
        resolve through ``runtime.tools`` — they are NOT Claude CLI tool
        identifiers. We therefore refuse to forward a non-empty allowlist
        to the SDK rather than silently grant the wrong native tools.

        The ``tools`` argument is the executor's *resolved* list from
        :func:`conductor.executor.agent.resolve_agent_tools`. That function
        erases the distinction between an omitted ``tools:`` and an explicit
        ``tools: []``: both arrive here as an empty list whenever the
        workflow declares no workflow-level ``tools:`` (``config.tools`` is
        empty; a non-empty list makes an omitted agent resolve non-empty). We
        therefore consult the RAW ``agent.tools`` field — the only place the
        omitted-vs-explicit signal survives — to pick the default.

        Semantics:

        * ``tools`` empty (``[]`` or ``None``) and ``agent.tools is None`` —
          the agent omitted ``tools:``. Fall back to the ``claude_code``
          preset (filesystem, bash, web) and bypass permissions, matching
          what the user gets from the bare ``claude`` CLI.
        * ``tools`` empty and ``agent.tools == []`` — explicit "no tools"
          request. Pass an empty list to the SDK so all tools are disabled.
          Drop the permission bypass because there are no tools to permit.
          When skills are enabled, grant the ``Skill`` tool back: an empty
          base tool set would otherwise leave the declared skill unreachable,
          silently ignoring the ``skills:`` the workflow asked for.
        * ``tools`` non-empty — raise ``ProviderError``. Workflow tool
          name → CLI tool ID translation is not implemented (tracked as
          a follow-up). Silently dropping the allowlist would be a
          security regression; silently passing it through could grant
          the wrong native tool. Refuse loudly.

        Args:
            tools: The executor-resolved ``tools:`` allowlist for this agent.
            agent: The agent definition. ``agent.tools`` carries the raw
                omitted-vs-explicit-empty signal; ``agent.name`` is used in
                the error message.
            skills_enabled: Whether this agent has skills to load. Only
                affects the explicit ``tools: []`` case.

        Returns:
            A ``(sdk_tools, permission_mode)`` tuple suitable for
            ``ClaudeAgentOptions``.

        Raises:
            ProviderError: If ``tools`` is a non-empty list.
        """
        if not tools:
            # The executor passes [] for BOTH "omitted (no workflow tools to
            # inherit)" and explicit "tools: []". Disambiguate via the raw
            # per-agent field, which the executor's resolution erased.
            if agent.tools is None:
                # Omitted -> default claude_code preset (filesystem/bash/web).
                return _DEFAULT_TOOL_PRESET, "bypassPermissions"
            # Explicit `tools: []` -> no tools, no permission bypass. The
            # Skill tool is the one exception, and only when skills are on:
            # it loads declared skill content and grants nothing else. The
            # SDK auto-allows it via `Skill(<name>)` in allowed_tools, so it
            # does not need the permission bypass either.
            if agents_enabled:
                # `--tools ""` leaves the model no tool to dispatch with, so
                # the registered subagents would be unreachable — the same
                # failure the Skill carve-out above exists to prevent. Unlike
                # `Skill`, this SDK exposes no verifiable identifier for the
                # dispatch tool, so there is nothing to grant back; guessing
                # a name is what this provider refuses to do elsewhere.
                raise ProviderError(
                    f"Agent '{agent.name}' sets 'tools: []' while its plugins ship "
                    f"subagents. An empty tool set leaves the model no way to dispatch "
                    f"to them, so they would be registered and unreachable.",
                    suggestion=(
                        "Omit 'tools:' to grant the full claude_code preset, set "
                        "'agents: false' on the plugins, or run this agent on "
                        "'copilot'."
                    ),
                    is_retryable=False,
                )
            if skills_enabled:
                return [_SKILL_TOOL], None
            return [], None
        raise ProviderError(
            f"Agent '{agent.name}' resolves to tools={tools!r} (declared on "
            "the agent or inherited from the workflow-level 'tools:' list), "
            "but claude-agent-sdk does not support workflow tool allowlists "
            "(workflow tool names do not translate to Claude CLI tool IDs).",
            suggestion=(
                "Omit both the per-agent and workflow-level 'tools:' to grant "
                "the full claude_code preset, or set 'tools: []' to disable "
                "every built-in tool (bar the Skill loader when the agent "
                "declares skills)."
            ),
        )

    @staticmethod
    def _process_assistant_blocks(
        blocks: list[Any],
        content_parts: list[str],
        pending_tools: dict[str, str],
        event_callback: EventCallback | None,
        verbose: bool = False,
        full_mode: bool = False,
    ) -> None:
        """Dispatch the content blocks of an ``AssistantMessage``.

        Appends text blocks to ``content_parts`` (the final-output buffer),
        forwards thinking blocks via ``agent_reasoning``, and registers
        tool_use blocks in ``pending_tools`` for later pairing with their
        results in :meth:`_process_tool_results`.

        Args:
            blocks: The ``AssistantMessage.content`` list.
            content_parts: Mutable list of text fragments accumulated so far.
            pending_tools: Mutable mapping of tool_use_id → tool_name.
            event_callback: Optional event forwarder.
            verbose: When True, also write to the verbose console.
            full_mode: When True, include argument / result previews.
        """
        for block in blocks:
            # Some SDK versions report block kind via a ``type`` string field
            # (snake_case), others rely on the dataclass class name (CamelCase).
            # Match both so we are robust to either packaging.
            block_type = getattr(block, "type", None) or type(block).__name__

            if block_type in ("text", "TextBlock"):
                text = getattr(block, "text", "")
                if text:
                    content_parts.append(text)
                    if event_callback:
                        _safe_callback(event_callback, "agent_message", {"content": text})

            elif block_type in ("thinking", "ThinkingBlock"):
                thinking = getattr(block, "thinking", "")
                if thinking:
                    if event_callback:
                        _safe_callback(
                            event_callback,
                            "agent_reasoning",
                            {"content": thinking},
                        )
                    if verbose:
                        _log_event_verbose("agent_reasoning", {"content": thinking}, full_mode)

            elif block_type in ("tool_use", "ToolUseBlock"):
                tool_name = getattr(block, "name", "unknown")
                tool_id = getattr(block, "id", "")
                tool_input = getattr(block, "input", {})
                pending_tools[tool_id] = tool_name
                data = {"tool_name": tool_name, "arguments": tool_input}
                if event_callback:
                    _safe_callback(event_callback, "agent_tool_start", data)
                if verbose:
                    _log_event_verbose("agent_tool_start", data, full_mode)

    @staticmethod
    def _process_tool_results(
        blocks: list[Any],
        pending_tools: dict[str, str],
        event_callback: EventCallback | None,
        verbose: bool = False,
        full_mode: bool = False,
    ) -> None:
        """Pair ``ToolResultBlock`` entries with their pending tool_use IDs.

        Emits ``agent_tool_complete`` for every result, looking up the
        original tool_name from ``pending_tools`` by ``tool_use_id``. If
        the SDK ever delivers a result without a matching pending entry
        (recovered session, races, etc.), the tool_name falls back to
        ``"unknown"`` rather than dropping the event.

        Args:
            blocks: The ``UserMessage.content`` list (a mix of tool results
                and prose).
            pending_tools: Mapping of tool_use_id → tool_name; entries are
                consumed (popped) as their results arrive.
            event_callback: Optional event forwarder.
            verbose: When True, also write to the verbose console.
            full_mode: When True, include result preview.
        """
        for block in blocks:
            block_type = getattr(block, "type", None) or type(block).__name__
            if block_type not in ("tool_result", "ToolResultBlock"):
                continue

            tool_use_id = getattr(block, "tool_use_id", "")
            tool_name = pending_tools.pop(tool_use_id, "unknown")
            content = getattr(block, "content", "")
            result_str = str(content)[:_TOOL_RESULT_PREVIEW_LEN] if content else None
            data = {"tool_name": tool_name, "result": result_str}

            if event_callback:
                _safe_callback(event_callback, "agent_tool_complete", data)
            if verbose:
                _log_event_verbose("agent_tool_complete", data, full_mode)

    @staticmethod
    def _build_error_message(message: Any) -> str:
        parts: list[str] = []

        errors = getattr(message, "errors", None)
        if errors:
            parts.append("; ".join(str(e) for e in errors))

        result = getattr(message, "result", None)
        if result:
            parts.append(str(result))

        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason:
            parts.append(f"stop_reason={stop_reason}")

        num_turns = getattr(message, "num_turns", None)
        if num_turns is not None:
            parts.append(f"after {num_turns} turns")

        if parts:
            return f"Claude Agent SDK execution failed: {', '.join(parts)}"
        return "Claude Agent SDK execution failed (no details available)"

    @staticmethod
    def _build_output(
        content_parts: list[str],
        structured_output: Any,
        agent: AgentDef,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        partial: bool = False,
    ) -> AgentOutput:
        """Assemble the final ``AgentOutput`` from accumulated execution state.

        Resolution order for ``content``:

        1. SDK-provided ``structured_output`` (preferred — already parsed by
           the SDK from a JSON-Schema response).
        2. JSON-parsed concatenation of text blocks (when ``agent.output`` is
           declared — fails loudly with ``ValidationError`` on parse error
           unless this is partial output, in which case the raw text is
           wrapped under ``{"response": ...}``).
        3. Bare ``{"response": ...}`` wrapper (when no schema declared).

        Args:
            content_parts: Text fragments captured from AssistantMessages.
            structured_output: SDK ``ResultMessage.structured_output`` value.
            agent: Agent definition (used for schema awareness and error msg).
            model: SDK-reported model identifier.
            input_tokens: Cumulative input tokens.
            output_tokens: Cumulative output tokens.
            partial: True when the output is from a mid-stream interrupt.
                Disables strict schema enforcement so partial best-effort
                output is preferred over hard failure.

        Returns:
            Populated ``AgentOutput`` ready to return from :meth:`execute`.

        Raises:
            ValidationError: If ``agent.output`` is declared, this is not
                a partial output, and the response cannot be parsed as JSON.
        """
        from conductor.exceptions import ValidationError

        if structured_output is not None:
            if isinstance(structured_output, dict):
                content = structured_output
            elif isinstance(structured_output, str):
                try:
                    content = json.loads(structured_output)
                except json.JSONDecodeError as e:
                    # If the agent declared a schema, a non-JSON
                    # structured_output value is a contract violation —
                    # downstream routes/templates assume the schema holds.
                    # Tolerate only on partial output (interrupt) where
                    # we'd rather surface what we have than nothing.
                    if agent.output and not partial:
                        raise ValidationError(
                            f"Agent '{agent.name}' declared an output schema "
                            f"but returned non-JSON structured_output: "
                            f"{structured_output[:200]!r}",
                            suggestion=(
                                "Ensure the prompt instructs the model to "
                                "emit JSON matching the declared `output:` "
                                "fields, or remove the `output:` schema."
                            ),
                        ) from e
                    content = {"response": structured_output}
            else:
                # The SDK returned ``structured_output`` of a shape the
                # provider does not understand (not a dict, not a str —
                # likely an SDK version drift). If the agent declared an
                # output schema, silently coercing to ``{"response": ...}``
                # would violate the schema contract; downstream routes /
                # templates that key off declared fields would then fail
                # with confusing KeyError / UndefinedError in unrelated
                # parts of the workflow.
                if agent.output and not partial:
                    raise ValidationError(
                        f"Agent '{agent.name}' declared an output schema but "
                        f"the SDK returned structured_output of unexpected "
                        f"type {type(structured_output).__name__}: "
                        f"{str(structured_output)[:200]!r}",
                        suggestion=(
                            "Pin or upgrade claude-agent-sdk to a compatible "
                            "version, or remove the `output:` schema."
                        ),
                    )
                content = {"response": str(structured_output)}
        elif agent.output:
            combined = "\n".join(content_parts)
            try:
                content = json.loads(combined)
            except json.JSONDecodeError as e:
                if not partial:
                    raise ValidationError(
                        f"Agent '{agent.name}' declared an output schema but "
                        f"returned non-JSON text: {combined[:200]!r}",
                        suggestion=(
                            "Ensure the prompt instructs the model to emit "
                            "JSON matching the declared `output:` fields, "
                            "or remove the `output:` schema."
                        ),
                    ) from e
                content = {"response": combined}
        else:
            content = {"response": "\n".join(content_parts)}

        total = input_tokens + output_tokens
        return AgentOutput(
            content=content,
            raw_response=structured_output or "\n".join(content_parts),
            tokens_used=total if total else None,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
            model=model,
            partial=partial,
        )


def _log_event_verbose(event_type: str, data: dict[str, Any], full_mode: bool) -> None:
    """Pretty-print an SDK event to the verbose console (stderr) and log file.

    ``execute()`` only calls this helper when its own CLI import succeeded,
    so the ``try/except ImportError`` around ``_file_console`` is belt-and-
    braces — kept in case a caller invokes the helper directly without
    going through ``execute()``.
    """
    from rich.console import Console
    from rich.text import Text

    try:
        from conductor.cli.run import _file_console
    except ImportError:
        _file_console = None

    console = Console(stderr=True, highlight=False)

    def _print(renderable: Any) -> None:
        console.print(renderable)
        if _file_console is not None:
            _file_console.print(renderable)

    if event_type == "agent_tool_start":
        tool_name = data.get("tool_name", "unknown")
        text = Text()
        text.append("    ├─ ", style="dim")
        text.append("🔧 ", style="")
        text.append(str(tool_name), style="cyan bold")
        _print(text)

        if full_mode:
            args = data.get("arguments")
            if args:
                args_str = str(args)
                args_preview = (
                    args_str[:_VERBOSE_ARG_PREVIEW_LEN] + "..."
                    if len(args_str) > _VERBOSE_ARG_PREVIEW_LEN
                    else args_str
                )
                arg_text = Text()
                arg_text.append("    │     ", style="dim")
                arg_text.append("args: ", style="dim italic")
                arg_text.append(args_preview, style="dim")
                _print(arg_text)

    elif event_type == "agent_tool_complete":
        tool_name = data.get("tool_name")
        if tool_name:
            text = Text()
            text.append("    │  ", style="dim")
            text.append("✓ ", style="green")
            text.append(str(tool_name), style="dim")
            _print(text)

        if full_mode:
            result = data.get("result")
            if result:
                result_str = str(result)
                result_preview = (
                    result_str[:_VERBOSE_RESULT_PREVIEW_LEN] + "..."
                    if len(result_str) > _VERBOSE_RESULT_PREVIEW_LEN
                    else result_str
                )
                result_text = Text()
                result_text.append("    │     ", style="dim")
                result_text.append("result: ", style="dim italic")
                result_text.append(result_preview, style="dim")
                _print(result_text)

    elif event_type == "agent_reasoning":
        if full_mode:
            reasoning = data.get("content", "")
            if reasoning:
                display = (
                    reasoning[:_REASONING_PREVIEW_LEN] + "..."
                    if len(reasoning) > _REASONING_PREVIEW_LEN
                    else reasoning
                )
                text = Text()
                text.append("    │  ", style="dim")
                text.append("💭 ", style="")
                text.append(display.replace("\n", " "), style="italic dim")
                _print(text)


def _safe_callback(callback: EventCallback, event_type: str, data: dict[str, Any]) -> None:
    try:
        callback(event_type, data)
    except Exception:
        logger.debug("Error in event_callback for %s", event_type, exc_info=True)


def _classify_startup_failure(msg: str) -> str | None:
    """Return a launch-failure hint for a ``CLIConnectionError`` message.

    The SDK reuses ``CLIConnectionError`` for failures to *spawn* the CLI, not
    just to talk to a running one. A missing working directory gets a dedicated
    message; ``ENOTDIR`` (the path is a file) and ``EACCES`` arrive through the
    generic "Failed to start Claude Code: <errno>" arm instead. The generic
    connection advice sends users to check firewalls for what is a bad path.

    Matching on upstream free text was audited against ``claude-agent-sdk``
    0.2.87: CLI stderr never reaches a ``CLIConnectionError`` message (a
    non-zero exit becomes ``ProcessError``, which this function never sees), so
    a tool emitting "permission denied" cannot be misfiled as a launch failure.

    Args:
        msg: Lower-cased exception message.

    Returns:
        A tailored hint, or ``None`` when the message is not a launch failure
        and the generic connection advice applies.
    """
    if "working directory does not exist" in msg:
        return (
            "The working directory disappeared between the engine's existence "
            "check and the CLI launch — the agent's working_dir, or the process "
            "cwd when none is set. Check whether an earlier step (e.g. a script "
            "agent) deletes or moves it mid-run."
        )
    if "not a directory" in msg or "permission denied" in msg:
        # The offending path may be the working directory or the CLI binary --
        # the errno text does not say which -- so name both.
        return (
            "The `claude` CLI could not be started. Check that the agent's "
            "working_dir points at an existing, readable directory and that "
            "the `claude` binary is executable."
        )
    return None


def _classify_error_suggestion(exc: BaseException) -> str:
    """Build a remediation hint tailored to the kind of failure observed.

    Inspects the exception class hierarchy and message text to provide an
    actionable hint per failure mode (CLI missing, auth, rate limit,
    network, parse, generic). A single generic suggestion would be
    actively misleading for most failures.
    """
    cls = type(exc).__name__
    msg = str(exc).lower()

    if cls == "CLINotFoundError":
        return (
            "The `claude` CLI is not installed or not on PATH. Install it from "
            "https://docs.anthropic.com/claude/docs/claude-code and verify with `claude --version`."
        )
    if cls == "CLIConnectionError":
        startup_hint = _classify_startup_failure(msg)
        if startup_hint is not None:
            return startup_hint
        return (
            "Could not connect to the `claude` CLI. Check that the binary is "
            "executable and that no firewall is blocking its spawned subprocess."
        )
    if cls in ("CLIJSONDecodeError", "MessageParseError"):
        return (
            "The Claude Agent SDK returned a malformed response. This usually "
            "indicates an SDK version mismatch — try upgrading "
            "`claude-agent-sdk` and the `claude` CLI to compatible versions."
        )
    if cls == "ProcessError":
        # Authentication and rate-limit failures surface as ProcessError with
        # a non-zero exit code; differentiate by stderr content where possible.
        if "auth" in msg or "api key" in msg or "unauthorized" in msg or "401" in msg:
            return (
                "Authentication failed. Verify `ANTHROPIC_API_KEY` is set and "
                "valid, or run `claude login` to refresh credentials."
            )
        if "rate" in msg or "429" in msg or "quota" in msg:
            return (
                "Rate-limited or quota exceeded. Retry after the cooldown, or "
                "lower the workflow's concurrency / iteration count."
            )
        if "network" in msg or "connection" in msg or "timeout" in msg:
            return (
                "Network connectivity issue reaching the Anthropic API. Check "
                "your internet connection and any proxy / firewall settings."
            )
        return (
            "The `claude` CLI subprocess failed. Inspect the error output "
            "above for the underlying cause."
        )

    # Generic fallback — only reached for non-SDK exception classes that
    # somehow propagated up. Keep the original advice as a last resort.
    return "Check that the `claude` CLI is installed and accessible."


def _is_retryable_exception(exc: BaseException) -> bool:
    """Classify an SDK exception as retryable based on type and message.

    Retryable conditions (transient, may succeed on a second attempt):
    network failures, rate limits, server-side 5xx, connection drops.

    Non-retryable: auth (401/403), bad request (400), malformed responses,
    missing CLI, unrecognized errors.
    """
    cls = type(exc).__name__
    msg = str(exc).lower()

    if cls in ("CLIJSONDecodeError", "MessageParseError", "CLINotFoundError"):
        return False

    if cls == "CLIConnectionError":
        # A failure to *launch* the CLI (bad working_dir, non-executable
        # binary) is deterministic — a retry lands on the same path. Only
        # genuine connection drops to a running subprocess are transient.
        return _classify_startup_failure(msg) is None

    if cls == "ProcessError":
        if "auth" in msg or "401" in msg or "403" in msg or "unauthorized" in msg:
            return False
        if "rate" in msg or "429" in msg or "quota" in msg or "overload" in msg:
            return True
        if "500" in msg or "502" in msg or "503" in msg or "504" in msg:
            return True
        return bool("network" in msg or "connection" in msg or "timeout" in msg)

    return False


def _is_retryable_result(message: Any) -> bool:
    """Classify a ResultMessage(is_error=True) as retryable.

    Inspects ``stop_reason``, ``api_error_status``, and the accumulated
    error text. Mirrors :func:`_is_retryable_exception` semantics:
    rate limits and 5xx are retryable; auth and bad requests are not.
    """
    status = getattr(message, "api_error_status", None)
    if isinstance(status, int):
        if status in (401, 403, 400):
            return False
        if status == 429 or 500 <= status < 600:
            return True

    stop_reason = getattr(message, "stop_reason", None)
    if isinstance(stop_reason, str):
        sr = stop_reason.lower()
        if sr in ("rate_limit", "overloaded", "overload", "server_error"):
            return True
        if sr in ("max_tokens", "max_turns", "stop_sequence", "tool_use", "end_turn"):
            # These are normal completion signals, not transient errors.
            # If is_error=True with one of these stop reasons, it's a logic
            # error in the agent — retry won't help.
            return False

    # Fall back to string inspection of the accumulated error text.
    text = " ".join(
        str(p)
        for p in (
            getattr(message, "errors", None) or [],
            getattr(message, "result", None) or "",
            stop_reason or "",
        )
        if p
    ).lower()
    if "rate" in text or "429" in text or "quota" in text or "overload" in text:
        return True
    if "500" in text or "502" in text or "503" in text or "504" in text:
        return True
    return bool("network" in text or "connection" in text or "timeout" in text)
