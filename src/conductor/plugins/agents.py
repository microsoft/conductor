"""Parse a plugin's ``agents/<name>.agent.md`` subagent definitions.

A plugin's subagents are the component whose absence started issue #378:
a skill loads, reads instructions telling it to dispatch to
``prs:code-reviewer``, and cannot. Conductor registers them itself rather
than handing the SDK the whole plugin root — see :mod:`conductor.plugins`
for why.

The format is the one both agent CLIs write: ``---`` fenced YAML
declaring ``name``, ``description``, and optionally ``tools``, followed
by the agent's system prompt as the body.

.. code-block:: text

    ---
    name: code-reviewer
    description: Reviews code against the project's guidelines.
    tools: [read, edit, execute, search, 'ado/*']
    user-invocable: false
    ---
    You are an expert code reviewer...

``tools`` entries are the **host CLI's** tool vocabulary (``read``,
``execute``, ``ado/*``), not Conductor workflow tool names, so they are
forwarded verbatim — translating them would be inventing a mapping that
does not exist.

``user-invocable`` is deliberately not mapped. It governs whether a
*human* can pick the agent from a CLI menu, whereas the SDK's ``infer``
governs whether the *model* may dispatch to it. Conductor has no menu,
and an agent the model cannot reach is the exact failure this feature
exists to fix, so every parsed agent is registered with ``infer`` set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conductor.frontmatter import (
    BLOCK_SCALAR_HINT,
    FrontmatterError,
    split_frontmatter,
)
from conductor.plugins.errors import PluginManifestError
from conductor.plugins.manifest import PLUGIN_AGENTS_DIR, SAFE_NAME

logger = logging.getLogger(__name__)

# Suffix identifying an agent definition inside a plugin's ``agents/``
# directory. Both CLIs use it, and it keeps a stray README from being
# parsed as an agent.
AGENT_SUFFIX: str = ".agent.md"


@dataclass(frozen=True)
class PluginAgent:
    """One subagent shipped by a plugin."""

    name: str
    """Agent name as declared in frontmatter."""

    plugin_name: str
    """Name of the plugin that ships this agent."""

    description: str
    """What the agent is for. The model reads this when deciding whether
    to dispatch, so an agent without one is unusable rather than merely
    undocumented."""

    prompt: str
    """The agent's system prompt — everything after the frontmatter."""

    tools: list[str] | None
    """Host-CLI tool identifiers the agent may use, or ``None`` to
    inherit the session default."""

    path: Path
    """Absolute path to the ``.agent.md`` file, for use in messages."""

    def __post_init__(self) -> None:
        """Enforce that :attr:`qualified_name` can safely reach an SDK.

        Both halves are checked here rather than only in
        :func:`read_plugin_agent`, because this is a public constructor
        and ``plugin_name`` is taken on trust by every parsing entry
        point. The name is joined with ``:`` and forwarded to
        ``custom_agents`` / ``AgentDefinition``, so a stray ``:`` or ``,``
        would split into something the SDK reads differently.

        Raises:
            PluginManifestError: If either name component is unusable, or
                the description or prompt is empty.
        """
        for label, value in (("name", self.name), ("plugin_name", self.plugin_name)):
            if not SAFE_NAME.match(value):
                raise PluginManifestError(
                    f"PluginAgent.{label} must match {SAFE_NAME.pattern} (it is joined "
                    f"into a delimited identifier), got {value!r}"
                )
        if not self.description.strip() or not self.prompt.strip():
            raise PluginManifestError(
                f"PluginAgent {self.plugin_name}:{self.name} needs a non-empty "
                "description and prompt — the model reads the first to decide whether "
                "to dispatch, and the second is the agent's instructions."
            )

    @property
    def qualified_name(self) -> str:
        """``<plugin>:<agent>`` — how both SDKs namespace a plugin agent.

        Empirically accepted by Copilot's ``custom_agents``: a session
        given ``{"name": "myplug:quokka"}`` listed ``myplug:quokka``
        among its launchable agent types. Preserving the namespace is
        what keeps two plugins that each ship a ``review`` agent from
        colliding.
        """
        return f"{self.plugin_name}:{self.name}"

    def to_custom_agent_config(self) -> dict[str, Any]:
        """Render this agent as a Copilot SDK ``CustomAgentConfig``.

        Returns:
            A dict matching the SDK's ``CustomAgentConfig`` TypedDict.
            ``tools`` is omitted entirely rather than sent as ``None``
            when the agent declares none, so the SDK applies its own
            default instead of being told "no tools".
        """
        config: dict[str, Any] = {
            "name": self.qualified_name,
            "description": self.description,
            "prompt": self.prompt,
            # The model must be able to dispatch to it — see the
            # ``user-invocable`` note in the module docstring.
            "infer": True,
        }
        if self.tools is not None:
            config["tools"] = list(self.tools)
        return config


def _require_text(value: Any, field: str, path: Path) -> str:
    """Extract a non-empty string frontmatter field.

    Raises:
        PluginManifestError: If the field is absent, not a string, or blank.
    """
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError(
            f"Agent definition at {path} declares no usable {field!r} in its YAML "
            f"frontmatter (got {value!r}).\n\n{BLOCK_SCALAR_HINT}"
        )
    return value.strip()


def _parse_tools(value: Any, path: Path) -> list[str] | None:
    """Normalise the optional ``tools`` frontmatter field.

    Raises:
        PluginManifestError: If ``tools`` is present but is not a list of
            non-empty strings.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise PluginManifestError(
            f"Agent definition at {path} declares 'tools' as {type(value).__name__}; "
            "expected a list of tool identifiers."
        )
    tools: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise PluginManifestError(
                f"Agent definition at {path} declares a 'tools' entry that is not a "
                f"non-empty string (got {entry!r})."
            )
        tools.append(entry.strip())
    return tools


def read_plugin_agent(path: Path, plugin_name: str) -> PluginAgent:
    """Parse one ``*.agent.md`` file.

    Args:
        path: Absolute path to the agent definition.
        plugin_name: Name of the owning plugin, used to namespace the
            agent.

    Returns:
        The parsed :class:`PluginAgent`.

    Raises:
        PluginManifestError: If the file cannot be read, has no
            frontmatter, has unparseable frontmatter, omits ``name`` or
            ``description``, declares an unusable ``tools`` list, or has
            an empty body. Each of these would otherwise register an
            agent the model cannot use, or drop one the workflow asked
            for — both silent.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PluginManifestError(f"Agent definition at {path} could not be read: {exc}") from exc

    try:
        parsed, body = split_frontmatter(text)
    except FrontmatterError as exc:
        raise PluginManifestError(
            f"Agent definition at {path} has {exc}\n\n{BLOCK_SCALAR_HINT}"
        ) from exc

    if parsed is None:
        raise PluginManifestError(
            f"Agent definition at {path} has no YAML frontmatter. It must begin with "
            "a '---' line declaring 'name' and 'description', followed by a closing "
            "'---' line and the agent's prompt."
        )

    name = _require_text(parsed.get("name"), "name", path)
    if not SAFE_NAME.match(name):
        raise PluginManifestError(
            f"Agent definition at {path} declares name {name!r}, which contains "
            f"characters outside {SAFE_NAME.pattern}. The name is joined with its "
            "plugin's into a delimiter-separated identifier, so it must not contain "
            "':' or ','."
        )

    prompt = body.strip()
    if not prompt:
        raise PluginManifestError(
            f"Agent definition at {path} has an empty body. The text after the "
            "closing '---' is the agent's system prompt; without it the agent has "
            "no instructions."
        )

    return PluginAgent(
        name=name,
        plugin_name=plugin_name,
        description=_require_text(parsed.get("description"), "description", path),
        prompt=prompt,
        tools=_parse_tools(parsed.get("tools"), path),
        path=path,
    )


def read_plugin_agents(root: Path, plugin_name: str) -> list[PluginAgent]:
    """Parse every agent definition a plugin ships.

    Args:
        root: Plugin root.
        plugin_name: Name of the plugin, used to namespace each agent.

    Returns:
        One :class:`PluginAgent` per ``*.agent.md`` directly inside the
        plugin's ``agents/`` directory, sorted by file name. Empty when
        the plugin ships no ``agents/`` directory.

        The scan is deliberately non-recursive: every plugin observed
        keeps a flat ``agents/`` directory, and descending would give a
        nested file a name indistinguishable from a top-level one.

    Raises:
        PluginManifestError: If ``agents/`` cannot be listed, if any
            definition is unusable, or if two definitions claim the same
            agent name. A clash is refused rather than deduped: one of
            the two would be dropped, and nothing would say which.
    """
    agents_dir = root / PLUGIN_AGENTS_DIR
    try:
        if not agents_dir.is_dir():
            return []
        entries = sorted(agents_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise PluginManifestError(
            f"Plugin agents directory {agents_dir} could not be read: {exc}"
        ) from exc

    agents: list[PluginAgent] = []
    claimed: dict[str, Path] = {}
    for entry in entries:
        if not entry.name.endswith(AGENT_SUFFIX):
            continue
        try:
            if not entry.is_file():
                continue
        except OSError as exc:
            raise PluginManifestError(
                f"Agent definition at {entry} could not be read: {exc}"
            ) from exc
        agent = read_plugin_agent(entry, plugin_name)
        prior = claimed.get(agent.name)
        if prior is not None:
            raise PluginManifestError(
                f"Plugin {plugin_name!r} declares two agents named {agent.name!r} "
                f"({prior} and {entry}). Agent names are namespaced per plugin, so "
                "one would silently shadow the other."
            )
        claimed[agent.name] = entry
        agents.append(agent)
    return agents
