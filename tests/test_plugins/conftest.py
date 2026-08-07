"""Shared fixtures for building synthetic plugins on disk.

Every test here works against a plugin tree it built itself. Nothing reads
the developer's real ``~`` — the ``home`` fixture is passed explicitly to
:func:`~conductor.plugins.registry.resolve_plugin`, mirroring the same
choice :mod:`conductor.skills.discovery` made for discovery.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SKILL_FRONTMATTER = "---\nname: {name}\ndescription: A test skill.\n---\n\nBody.\n"

AGENT_DEFINITION = "---\nname: {name}\ndescription: {description}\n{extra}---\n\n{prompt}\n"


def write_skill(directory: Path, name: str | None = None) -> Path:
    """Create a skill directory holding a valid ``SKILL.md``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        SKILL_FRONTMATTER.format(name=name or directory.name), encoding="utf-8"
    )
    return directory


def write_agent(
    directory: Path,
    name: str,
    *,
    description: str = "Does a thing.",
    tools: list[str] | None = None,
    prompt: str = "You are a test agent.",
) -> Path:
    """Create an ``<name>.agent.md`` inside ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    extra = f"tools: {json.dumps(tools)}\n" if tools is not None else ""
    path = directory / f"{name}.agent.md"
    path.write_text(
        AGENT_DEFINITION.format(name=name, description=description, extra=extra, prompt=prompt),
        encoding="utf-8",
    )
    return path


def make_plugin(
    root: Path,
    name: str,
    *,
    manifest: str = ".claude-plugin",
    skills: list[str] | None = None,
    agents: list[str] | None = None,
    mcp: dict | None = None,
    mcp_inline: bool = False,
    hooks: bool = False,
    commands: bool = False,
) -> Path:
    """Build a plugin tree and return its root.

    Args:
        root: Directory to create the plugin in.
        name: Plugin name written into the manifest.
        manifest: ``".claude-plugin"`` (Claude Code convention) or
            ``".github/plugin"`` (Copilot convention). Both resolve at
            runtime, which is why both are recognised.
        skills: Skill directory names to create under ``skills/``.
        agents: Agent names to create under ``agents/``.
        mcp: Server mapping to declare, or ``None`` for no MCP.
        mcp_inline: Declare ``mcp`` inline in the manifest rather than
            pointing at a ``.mcp.json`` file. Every MCP-shipping plugin
            observed in the wild uses the file reference, so that is the
            default.
        hooks: Create a ``hooks/`` directory Conductor will not load.
        commands: Create a ``commands/`` directory Conductor will not load.
    """
    root.mkdir(parents=True, exist_ok=True)
    document: dict = {"name": name}

    if mcp is not None:
        if mcp_inline:
            document["mcpServers"] = mcp
        else:
            (root / ".mcp.json").write_text(json.dumps({"mcpServers": mcp}), encoding="utf-8")
            document["mcpServers"] = ".mcp.json"

    manifest_dir = root / manifest
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(json.dumps(document), encoding="utf-8")

    for skill in skills or []:
        write_skill(root / "skills" / skill)
    for agent in agents or []:
        write_agent(root / "agents", agent)
    if hooks:
        (root / "hooks").mkdir(exist_ok=True)
    if commands:
        (root / "commands").mkdir(exist_ok=True)
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated home directory for installed-plugin resolution."""
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def installed(home: Path):
    """Install a plugin under a marketplace in the fake home."""

    def _install(name: str, *, marketplace: str = "market", **kwargs) -> Path:
        return make_plugin(
            home / ".copilot" / "installed-plugins" / marketplace / name,
            name,
            **kwargs,
        )

    return _install
