"""Locate and parse a plugin's manifest.

The single definition of "what a plugin root looks like", shared by
:mod:`conductor.plugins.registry` (which resolves ``plugins:`` entries)
and :mod:`conductor.skills.registry` (which walks up from a skill
directory to find the plugin that owns it, for claude-agent-sdk).

Two conventions are recognised. Claude Code writes
``.claude-plugin/plugin.json``; the Copilot CLI writes
``.github/plugin/plugin.json``. Both resolve at runtime — verified
against a live Copilot session with a synthetic plugin under each — so
recognising only the former was Conductor's own gap, not an upstream
one. On a fairly ordinary machine it stranded 12 of 13 installed
plugins.

Kept free of any :mod:`conductor.skills` import so the edge from
``skills.registry`` into this module cannot become a cycle — see the
layering note in :mod:`conductor.plugins`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conductor.plugins.errors import PluginManifestError

# Manifest locations, in probe order. Both are recognised because both
# exist in the wild; no plugin observed ships both, and if one ever does
# the first match wins rather than the two being merged — a merge would
# have to invent a precedence rule for every field.
PLUGIN_MANIFESTS: tuple[Path, ...] = (
    Path(".claude-plugin") / "plugin.json",
    Path(".github") / "plugin" / "plugin.json",
)

# Conventional MCP declaration file at a plugin root, used when the
# manifest does not name one explicitly.
DEFAULT_MCP_FILE: str = ".mcp.json"

# Directory a plugin keeps its skills in, relative to the plugin root.
PLUGIN_SKILLS_DIR: str = "skills"

# Directory a plugin keeps its subagent definitions in.
PLUGIN_AGENTS_DIR: str = "agents"

# Components Conductor recognises but deliberately does not load. Named
# here so ``conductor validate`` can report them by name rather than
# leaving the difference between the CLI and a workflow invisible.
PLUGIN_DROPPED_DIRS: tuple[str, ...] = ("hooks", "commands")

# Characters allowed in a plugin, skill, or agent name. The parts are
# joined with ``:`` into a qualified name, which claude-agent-sdk expands
# to ``Skill(<name>)`` and joins with ``,`` into a single
# ``--allowedTools`` value — so a name containing either delimiter would
# split into extra permission rules.
SAFE_NAME: re.Pattern[str] = re.compile(r"\A[A-Za-z0-9_.-]+\Z")


@dataclass(frozen=True)
class PluginManifest:
    """The parts of a plugin manifest Conductor uses."""

    name: str
    """Plugin name as declared in the manifest.

    Used as the namespace in the ``<plugin>:<skill>`` and
    ``<plugin>:<agent>`` qualified names both SDKs resolve by.
    """

    root: Path
    """Absolute path to the plugin root (the directory holding the
    manifest's parent directory)."""

    path: Path
    """Absolute path to the manifest file itself, for use in messages."""

    mcp_servers: dict[str, Any]
    """Name-keyed MCP server configurations the plugin declares.

    Empty when the plugin declares none. The keys are the plugin's own
    server names and are **not** rewritten: Copilot prefixes tool names
    with the server name (``ado`` yields ``ado-search_workitem``), and a
    plugin's ``SKILL.md`` refers to those tools by name, so renaming a
    server to avoid a collision would break the instructions that make
    the plugin worth enabling.
    """


def find_manifest(root: Path) -> Path | None:
    """Return the plugin manifest inside ``root``, if there is one.

    Args:
        root: Candidate plugin root.

    Returns:
        Path to the first manifest found, probing
        :data:`PLUGIN_MANIFESTS` in order, or ``None`` when ``root`` is
        not a plugin root.
    """
    for relative in PLUGIN_MANIFESTS:
        candidate = root / relative
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # An unreadable ancestor makes this candidate unusable, but a
            # sibling convention may still resolve. Treat as "not here".
            continue
    return None


def is_plugin_root(root: Path) -> bool:
    """Whether ``root`` holds a recognised plugin manifest."""
    return find_manifest(root) is not None


def _parse_manifest_json(manifest: Path) -> dict[str, Any]:
    """Read a manifest file and return its top-level mapping.

    Raises:
        PluginManifestError: If the file cannot be read or does not parse
            to a JSON object.
    """
    try:
        parsed = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PluginManifestError(
            f"Plugin manifest at {manifest} could not be read: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        # A bare array, string, or null is as unusable as a parse failure.
        raise PluginManifestError(
            f"Plugin manifest at {manifest} is not a JSON object "
            f"(parsed as {type(parsed).__name__})."
        )
    return parsed


def _read_manifest_name(manifest: Path, parsed: dict[str, Any]) -> str:
    """Extract and validate the ``name`` a manifest declares.

    Raises:
        PluginManifestError: If ``name`` is absent, empty, or contains
            characters outside :data:`SAFE_NAME`.
    """
    name = parsed.get("name")
    if not isinstance(name, str) or not name:
        raise PluginManifestError(f"Plugin manifest at {manifest} declares no usable 'name'.")
    if not SAFE_NAME.match(name):
        raise PluginManifestError(
            f"Plugin manifest at {manifest} declares name {name!r}, which contains "
            f"characters outside {SAFE_NAME.pattern}. The name is joined into the "
            "CLI's delimiter-separated tool list, so it must not contain ':' or ','."
        )
    return name


def _unwrap_mcp_document(payload: Any, source: Path) -> dict[str, Any]:
    """Return the server mapping from a loaded ``.mcp.json`` document.

    A standalone MCP file uses the ``{"mcpServers": {...}}`` envelope —
    that is what every plugin observed in the wild writes, and what the
    CLIs expect. A bare mapping of server names is accepted too, since
    an inline manifest declaration takes that shape and the two are
    otherwise indistinguishable to a reader.

    Raises:
        PluginManifestError: If the document is not a mapping, or the
            envelope's value is not one.
    """
    if not isinstance(payload, dict):
        raise PluginManifestError(
            f"MCP declaration at {source} is not a JSON object "
            f"(parsed as {type(payload).__name__})."
        )
    if "mcpServers" in payload:
        servers = payload["mcpServers"]
        if not isinstance(servers, dict):
            raise PluginManifestError(
                f"MCP declaration at {source} has an 'mcpServers' key that is not an "
                f"object (got {type(servers).__name__})."
            )
        return servers
    return payload


def _validate_servers(servers: dict[str, Any], source: Path) -> dict[str, Any]:
    """Check each declared server is a usably-shaped mapping.

    Raises:
        PluginManifestError: If a server name is unusable or its
            configuration is not an object.
    """
    for name, config in servers.items():
        if not isinstance(name, str) or not name.strip():
            raise PluginManifestError(
                f"MCP declaration at {source} contains a server with an unusable name ({name!r})."
            )
        if not isinstance(config, dict):
            raise PluginManifestError(
                f"MCP declaration at {source} defines server {name!r} as "
                f"{type(config).__name__} rather than an object."
            )
    return dict(servers)


def _load_mcp_servers(root: Path, parsed: dict[str, Any], manifest: Path) -> dict[str, Any]:
    """Resolve a plugin's MCP server declarations.

    Three forms, in precedence order:

    1. ``"mcpServers": ".mcp.json"`` — a **string path**, relative to the
       plugin root. This is the form every MCP-shipping plugin observed
       actually uses, so handling only the inline object below would find
       zero servers on all of them.
    2. ``"mcpServers": {...}`` — an inline mapping.
    3. No manifest key, but a conventional ``.mcp.json`` at the root.

    A relative path is joined to the root without a containment check:
    a plugin that wanted to run arbitrary commands could simply declare
    them inline, so rejecting ``../`` would add no protection and would
    reject a legitimate shared file.

    Returns:
        The name-keyed server mapping, empty when none is declared.

    Raises:
        PluginManifestError: If a declared source cannot be read or does
            not hold a usable server mapping.
    """
    declared = parsed.get("mcpServers")

    if isinstance(declared, str):
        if not declared.strip():
            raise PluginManifestError(
                f"Plugin manifest at {manifest} declares an empty 'mcpServers' path."
            )
        source = root / declared
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PluginManifestError(
                f"Plugin manifest at {manifest} points 'mcpServers' at {source}, "
                f"which could not be read: {exc}"
            ) from exc
        return _validate_servers(_unwrap_mcp_document(payload, source), source)

    if isinstance(declared, dict):
        return _validate_servers(_unwrap_mcp_document(declared, manifest), manifest)

    if declared is not None:
        raise PluginManifestError(
            f"Plugin manifest at {manifest} declares 'mcpServers' as "
            f"{type(declared).__name__}; expected a path string or an object."
        )

    fallback = root / DEFAULT_MCP_FILE
    try:
        if not fallback.is_file():
            return {}
        payload = json.loads(fallback.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PluginManifestError(
            f"MCP declaration at {fallback} could not be read: {exc}"
        ) from exc
    return _validate_servers(_unwrap_mcp_document(payload, fallback), fallback)


def read_manifest_name(manifest: Path) -> str:
    """Read just the ``name`` from a manifest file.

    Split out of :func:`read_plugin_manifest` for callers that only need
    the plugin's identity — notably
    :func:`conductor.skills.registry.resolve_skill_plugin`, which must not
    fail over an unrelated broken ``mcpServers`` declaration when all it
    is doing is naming the plugin that owns a skill.

    Args:
        manifest: Path to an existing plugin manifest.

    Returns:
        The declared plugin name.

    Raises:
        PluginManifestError: If the file cannot be read, is not a JSON
            object, or declares no usable ``name``.
    """
    return _read_manifest_name(manifest, _parse_manifest_json(manifest))


def read_plugin_manifest(root: Path) -> PluginManifest:
    """Read the manifest of the plugin rooted at ``root``.

    Args:
        root: Plugin root — the directory holding ``.claude-plugin/`` or
            ``.github/plugin/``. Must be absolute.

    Returns:
        The parsed :class:`PluginManifest`.

    Raises:
        PluginManifestError: If ``root`` holds no recognised manifest, or
            the manifest is unreadable, nameless, or declares MCP servers
            that cannot be loaded.
    """
    manifest = find_manifest(root)
    if manifest is None:
        conventions = ", ".join(str(candidate) for candidate in PLUGIN_MANIFESTS)
        raise PluginManifestError(f"{root} is not a plugin: it contains none of {conventions}.")
    parsed = _parse_manifest_json(manifest)
    return PluginManifest(
        name=_read_manifest_name(manifest, parsed),
        root=root,
        path=manifest,
        mcp_servers=_load_mcp_servers(root, parsed, manifest),
    )
