"""Shared fixtures for building synthetic plugins on disk.

Every test here works against a plugin tree it built itself. Nothing reads
the developer's real ``~`` — the ``home`` fixture is passed explicitly to
:func:`~conductor.plugins.registry.resolve_plugin`, mirroring the same
choice :mod:`conductor.skills.discovery` made for discovery.
"""

from __future__ import annotations

import functools
import json
import subprocess
from pathlib import Path

import pytest

from conductor.providers.capabilities import ProviderCapabilities

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


def make_marketplace(
    root: Path,
    name: str,
    plugins: dict[str, str],
    *,
    manifest: str = ".claude-plugin",
    plugin_root: str | None = None,
) -> Path:
    """Write a marketplace *catalog* manifest and return its root.

    Only the manifest — callers create the plugin trees themselves, which
    is what lets a test point ``source`` at a directory that does or does
    not hold a plugin.

    Args:
        root: Directory to write the catalog into.
        name: Marketplace name.
        plugins: Plugin name to the ``source`` string recorded for it.
        manifest: ``".claude-plugin"`` or ``".github/plugin"``. The two
            conventions anchor ``source`` differently in the wild, which
            is why both are exercised.
        plugin_root: Optional ``metadata.pluginRoot``.
    """
    root.mkdir(parents=True, exist_ok=True)
    metadata: dict = {}
    if plugin_root is not None:
        metadata["pluginRoot"] = plugin_root
    document = {
        "name": name,
        "metadata": metadata,
        "plugins": [{"name": key, "source": value} for key, value in plugins.items()],
    }
    manifest_dir = root / manifest
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "marketplace.json").write_text(json.dumps(document), encoding="utf-8")
    return root


def make_git_repo(root: Path, *, tag: str | None = None) -> str:
    """Commit whatever is under ``root`` and return the commit SHA.

    Tests fetch over ``file://`` against real repositories built here, so
    the git shell-out is exercised for real without a network. Mocking
    ``subprocess`` instead would test the mock: the behaviours that
    matter (annotated tags dereferencing, shallow fetch of a bare SHA,
    an unreachable remote) are all properties of git itself.
    """
    run = functools.partial(subprocess.run, cwd=root, check=True, capture_output=True)
    # An explicit initial branch, so a test naming a branch is not at the
    # mercy of the developer's `init.defaultBranch`.
    run(["git", "init", "-q", "-b", "main", "."])
    run(["git", "add", "-A"])
    run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "initial",
        ]
    )
    if tag is not None:
        run(["git", "tag", tag])
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test in this package off the developer's real ``~``.

    Autouse and package-wide rather than per-class, because the leak is
    easy to reintroduce and invisible when it happens: resolution reaches
    ``Path.home()`` from two places that take no fixture —
    ``fetch.get_plugin_cache_base`` when ``CONDUCTOR_HOME`` is unset, and
    ``resolve_plugins(home=None)`` for installed-marketplace lookup. A
    developer with a marketplace installed under a name a fixture also
    uses would see a test assert the opposite of what it means; the case
    that caught this asserted a *missing* plugin and passed vacuously
    because an ambient one satisfied it.

    Tests that need to control the installed set still take the explicit
    ``home`` fixture and pass it, which this does not interfere with.
    """
    home = tmp_path / "isolated-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CONDUCTOR_HOME", str(home / ".conductor"))


@pytest.fixture
def plugin_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the plugin cache at a temporary directory.

    ``CONDUCTOR_HOME`` rather than patching the resolver, so the env-var
    contract itself is exercised — and so no test can write into the
    developer's real ``~/.conductor``.
    """
    from conductor.plugins.fetch import clear_resolution_memo

    home = tmp_path / "conductor-home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    clear_resolution_memo()
    yield home
    clear_resolution_memo()


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


PLUGIN_CAPABLE_CAPS = ProviderCapabilities(
    tier="stable",
    mcp_tools=True,
    workflow_tools_passthrough=True,
    streaming_events=True,
    agent_reasoning_events=True,
    reasoning_effort=None,
    structured_output="native",
    interrupt=True,
    max_session_seconds=True,
    checkpoint_resume=True,
    usage_tracking=True,
    concurrent_safe=True,
    skills=True,
    plugins=True,
)
"""Descriptor for a fake provider that honours the whole plugin contract."""
