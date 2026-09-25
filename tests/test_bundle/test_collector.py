"""Contract tests for the run-bundle dependency collector."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import conductor.bundle.collector as collector_module
from conductor.bundle.collector import MAX_SUBWORKFLOW_DEPTH, CollectedBundle, collect_bundle
from conductor.bundle.errors import (
    BundleCapsError,
    BundleCycleError,
    BundleDynamicTemplateError,
    BundleError,
    BundleRootEscapeError,
    BundleSymlinkEscapeError,
)
from conductor.bundle.model import compute_bundle_digest
from conductor.config.schema import PluginDef
from conductor.config.validator import _MAX_SUBWORKFLOW_VALIDATION_DEPTH
from conductor.exceptions import ConfigurationError
from conductor.executor.agent import _merge_skills
from conductor.plugins.registry import resolve_plugins
from conductor.registry.cache import CACHE_LAYOUT_VERSION, _readiness_marker_payload, _sentinel_path
from conductor.skills.discovery import resolve_effective_skills


def _workflow(prompt: str, *, extra_workflow: str = "") -> str:
    return f"""\
workflow:
  name: test
  entry_point: agent
  bundle:{extra_workflow or " {}"}
agents:
  - name: agent
    prompt: {prompt}
    routes:
      - to: $end
"""


def _collect(path: Path) -> CollectedBundle:
    return collect_bundle(path, environment=None, allow_network=False, on_warning=lambda _m: None)


def _write_plugin(root: Path, *, skill: str = "plugin-skill") -> None:
    (root / ".github" / "plugin").mkdir(parents=True)
    (root / ".github" / "plugin" / "plugin.json").write_text(
        '{"name":"review","mcpServers":".mcp.json"}', encoding="utf-8"
    )
    (root / ".mcp.json").write_text(
        '{"mcpServers":{"review-tools":{"command":"review"}}}', encoding="utf-8"
    )
    skill_dir = root / "skills" / skill
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: Test.\n---\n\nPlugin body.\n", encoding="utf-8"
    )
    (root / "agents").mkdir()
    (root / "agents" / "reviewer.agent.md").write_text(
        "---\nname: reviewer\ndescription: Reviews.\n---\n\nReview.\n", encoding="utf-8"
    )


def _plugin_workflow(plugin_block: str, *, skills: str = "") -> str:
    return f"""\
workflow:
  name: plugin-components
  entry_point: agent
agents:
  - name: agent
    prompt: test
{skills}    plugins:
{plugin_block}
    routes:
      - to: $end
"""


def test_loader_graph_and_payload_are_complete(tmp_path: Path) -> None:
    # Requirement: nested !yamlfile/!file dependencies become materializable payload entries.
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Prompt body", encoding="utf-8")
    metadata = tmp_path / "metadata.yaml"
    metadata.write_text("note: !file prompt.md\n", encoding="utf-8")
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _workflow("!file prompt.md").replace("bundle: {}", "metadata: !yamlfile metadata.yaml"),
        encoding="utf-8",
    )

    bundle = _collect(workflow)

    assert {"tree/main/workflow.yaml", "tree/main/prompt.md", "tree/main/metadata.yaml"} <= {
        entry.logical_path for entry in bundle.entries
    }
    assert set(bundle.files) == {
        entry.logical_path for entry in bundle.entries if entry.kind == "file"
    }
    assert set(bundle.links) == {
        entry.logical_path for entry in bundle.entries if entry.kind == "symlink"
    }
    assert bundle.manifest.bundle_digest == compute_bundle_digest(
        bundle.entries,
        bundle.manifest.skills_topology,
        bundle.manifest.plugins_topology,
    )


def test_jinja_recursive_closure_and_include_semantics(tmp_path: Path) -> None:
    # Requirement: fixed-root recursive includes bundle every existing literal candidate.
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "base.md").write_text("{% import 'macro.md' as m %}", encoding="utf-8")
    (templates / "macro.md").write_text("{% macro x() %}x{% endmacro %}", encoding="utf-8")
    (templates / "present.md").write_text("present", encoding="utf-8")
    prompt = templates / "prompt.md"
    prompt.write_text(
        "{% extends 'base.md' %}{% include ['missing.md', 'present.md'] %}"
        "{% include 'optional.md' ignore missing %}",
        encoding="utf-8",
    )
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_workflow("!file templates/prompt.md"), encoding="utf-8")

    bundle = _collect(workflow)
    paths = {entry.logical_path for entry in bundle.entries}

    assert "tree/main/templates/base.md" in paths
    assert "tree/main/templates/macro.md" in paths
    assert "tree/main/templates/present.md" in paths


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics require POSIX privileges")
def test_jinja_visited_key_includes_normalized_search_root(tmp_path: Path) -> None:
    # Requirement: one resolved prompt is rescanned when reached through a distinct search root.
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "prompt.md").write_text("{% include 'partial.md' %}", encoding="utf-8")
    (shared / "partial.md").write_text("shared", encoding="utf-8")
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    (alternate / "partial.md").write_text("alternate", encoding="utf-8")
    (alternate / "prompt.md").symlink_to(shared / "prompt.md")
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _workflow("!file shared/prompt.md").replace(
            "routes:\n", "system_prompt: !file alternate/prompt.md\n    routes:\n"
        ),
        encoding="utf-8",
    )

    paths = {entry.logical_path for entry in _collect(workflow).entries}

    assert "tree/main/shared/partial.md" in paths
    assert "tree/main/alternate/partial.md" in paths


def test_local_and_for_each_subworkflows_collect_their_includes(tmp_path: Path) -> None:
    # Requirement: top-level and inline for_each workflow steps recurse with their own graphs.
    for name in ("one", "two"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "prompt.md").write_text(name, encoding="utf-8")
        (directory / "workflow.yaml").write_text(_workflow("!file prompt.md"), encoding="utf-8")
    root = tmp_path / "workflow.yaml"
    root.write_text(
        """\
workflow:
  name: root
  entry_point: first
agents:
  - name: first
    type: workflow
    workflow: one/workflow.yaml
    routes:
      - to: batch
for_each:
  - name: batch
    type: for_each
    source: workflow.input.items
    as: item
    agent:
      name: nested
      type: workflow
      workflow: two/workflow.yaml
    routes:
      - to: $end
""",
        encoding="utf-8",
    )

    paths = {entry.logical_path for entry in _collect(root).entries}

    assert "tree/main/one/prompt.md" in paths
    assert "tree/main/two/prompt.md" in paths


def test_pinned_registry_subworkflow_uses_cache_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: a pinned registry workflow resolves offline and preserves repository layout.
    sha = "a" * 40
    home = tmp_path / "home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    cache = home / "cache" / "registries"
    child = cache / "_adhoc" / "acme" / "flows" / sha[:12] / "nested" / "child.yaml"
    child.parent.mkdir(parents=True)
    child.write_text(_workflow("inline"), encoding="utf-8")
    metadata = cache / "_adhoc" / "acme" / "flows" / "_meta" / sha[:12]
    metadata.mkdir(parents=True)
    (metadata / "source.json").write_text(
        json.dumps(
            {
                "cache_layout_version": CACHE_LAYOUT_VERSION,
                "registry_type": "github",
                "source": "acme/flows",
                "full_sha": sha,
            }
        ),
        encoding="utf-8",
    )
    (metadata / "index.yaml").write_text(
        "workflows:\n  child:\n    description: ''\n    path: nested/child.yaml\n",
        encoding="utf-8",
    )
    sentinel = _sentinel_path("_adhoc/acme/flows", sha, "child")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(_readiness_marker_payload(), encoding="utf-8")
    root = tmp_path / "workflow.yaml"
    root.write_text(
        """\
workflow:
  name: root
  entry_point: nested
agents:
  - name: nested
    type: workflow
    workflow: child@acme/flows#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    routes:
      - to: $end
""",
        encoding="utf-8",
    )

    bundle = _collect(root)

    assert any(
        entry.logical_path == "tree/registry/_adhoc/acme/flows/aaaaaaaaaaaa/nested/child.yaml"
        for entry in bundle.entries
    )
    assert bundle.descriptor.provenance.registry[0].resolved_sha == sha


def test_per_agent_skill_union_and_claude_plugin_flavor(tmp_path: Path) -> None:
    # Requirement: tri-state skills and Claude-flavored plugin agent files are bundled.
    for name in ("default", "override"):
        skill = tmp_path / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test.\n---\n\nBody.\n", encoding="utf-8"
        )
    discovered = tmp_path / ".claude" / "skills" / "discovered"
    discovered.mkdir(parents=True)
    (discovered / "SKILL.md").write_text(
        "---\nname: discovered\ndescription: Test.\n---\n\nBody.\n", encoding="utf-8"
    )
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name":"review"}', encoding="utf-8")
    (plugin / "agents").mkdir()
    (plugin / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews.\n---\n\nReview.\n", encoding="utf-8"
    )
    (plugin / "hooks").mkdir()
    (plugin / "hooks" / "unsafe.sh").write_text("exit 0", encoding="utf-8")
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
workflow:
  name: union
  entry_point: inherited
  runtime:
    provider: claude-agent-sdk
    skills: [./skills/default]
    skill_discovery:
      sources: [project]
    plugins: [./plugin]
agents:
  - name: inherited
    prompt: inherited
    routes:
      - to: overridden
  - name: overridden
    prompt: overridden
    skills: [./skills/override]
    plugins: []
    routes:
      - to: opted-out
  - name: opted-out
    prompt: opted-out
    skills: []
    plugins: []
    routes:
      - to: $end
""",
        encoding="utf-8",
    )

    bundle = _collect(workflow)
    paths = {entry.logical_path for entry in bundle.entries}

    assert set(bundle.manifest.skills_topology) == {"default", "override", "discovered"}
    assert "tree/plugins/review/agents/reviewer.md" in paths
    assert all("hooks" not in path for path in paths)
    assert bundle.descriptor.provenance.plugins[0].flavor == "claude"


def test_plugin_component_switches_exclude_agents_and_mcp(tmp_path: Path) -> None:
    # Requirement: disabled agent and MCP components stay out while enabled skills remain.
    plugin = tmp_path / "plugin"
    _write_plugin(plugin)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _plugin_workflow("      - name: ./plugin\n        agents: false\n        mcp: false\n"),
        encoding="utf-8",
    )

    bundle = _collect(workflow)
    paths = {entry.logical_path for entry in bundle.entries}

    assert "tree/plugins/review/.github/plugin/plugin.json" in paths
    assert "tree/plugins/review/skills/plugin-skill/SKILL.md" in paths
    assert "tree/plugins/review/agents/reviewer.agent.md" not in paths
    assert "tree/plugins/review/.mcp.json" not in paths
    assert bundle.manifest.skills_topology["plugin-skill"] == (
        "tree/plugins/review/skills/plugin-skill/SKILL.md"
    )
    assert bundle.descriptor.provenance.plugins[0].model_dump() == {
        "name": "review",
        "flavor": "copilot",
        "origin": "path",
        "sha": None,
    }


def test_plugin_manifest_selected_mcp_file_is_collected_only_when_enabled(tmp_path: Path) -> None:
    # Requirement: the manifest-selected MCP source, not an assumed default, follows mcp enablement.
    plugin = tmp_path / "plugin"
    (plugin / ".github" / "plugin").mkdir(parents=True)
    (plugin / "config").mkdir()
    (plugin / ".github" / "plugin" / "plugin.json").write_text(
        '{"name":"review","mcpServers":"config/servers.json"}', encoding="utf-8"
    )
    (plugin / "config" / "servers.json").write_text(
        '{"mcpServers":{"review-tools":{"command":"review"}}}', encoding="utf-8"
    )
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_plugin_workflow("      - ./plugin\n"), encoding="utf-8")

    enabled_paths = {entry.logical_path for entry in _collect(workflow).entries}
    workflow.write_text(
        _plugin_workflow("      - name: ./plugin\n        mcp: false\n"), encoding="utf-8"
    )
    disabled_paths = {entry.logical_path for entry in _collect(workflow).entries}

    source = "tree/plugins/review/config/servers.json"
    assert source in enabled_paths
    assert source not in disabled_paths


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics require POSIX privileges")
def test_plugin_manifest_symlinked_mcp_file_collects_target(tmp_path: Path) -> None:
    # Requirement: a manifest-selected MCP symlink includes its target bytes in the bundle.
    plugin = tmp_path / "plugin"
    (plugin / ".github" / "plugin").mkdir(parents=True)
    (plugin / "config").mkdir()
    (plugin / ".github" / "plugin" / "plugin.json").write_text(
        '{"name":"review","mcpServers":"config/servers.json"}', encoding="utf-8"
    )
    actual = plugin / "config" / "actual-servers.json"
    actual.write_text('{"mcpServers":{"review-tools":{"command":"review"}}}', encoding="utf-8")
    (plugin / "config" / "servers.json").symlink_to("actual-servers.json")
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_plugin_workflow("      - ./plugin\n"), encoding="utf-8")

    bundle = _collect(workflow)
    entries = {entry.logical_path: entry for entry in bundle.entries}

    link = "tree/plugins/review/config/servers.json"
    assert entries[link].link_target == "actual-servers.json"
    assert "tree/plugins/review/config/actual-servers.json" in entries


def test_plugin_manifest_mcp_file_outside_plugin_root_keeps_relative_layout(tmp_path: Path) -> None:
    # Requirement: an authorized manifest MCP path outside the plugin root stays relocatable.
    plugin = tmp_path / "plugins" / "review"
    shared = tmp_path / "plugins" / "shared"
    (plugin / ".github" / "plugin").mkdir(parents=True)
    shared.mkdir(parents=True)
    (plugin / ".github" / "plugin" / "plugin.json").write_text(
        '{"name":"review","mcpServers":"../shared/servers.json"}', encoding="utf-8"
    )
    (shared / "servers.json").write_text(
        '{"mcpServers":{"review-tools":{"command":"review"}}}', encoding="utf-8"
    )
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_plugin_workflow("      - ./plugins/review\n"), encoding="utf-8")

    paths = {entry.logical_path for entry in _collect(workflow).entries}

    assert "tree/plugins/shared/servers.json" in paths


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics require POSIX privileges")
def test_plugin_manifest_external_mcp_symlink_keeps_relative_layout(tmp_path: Path) -> None:
    # Requirement: an external MCP symlink and its target share the portable sibling namespace.
    plugin = tmp_path / "plugins" / "review"
    shared = tmp_path / "plugins" / "shared"
    (plugin / ".github" / "plugin").mkdir(parents=True)
    shared.mkdir(parents=True)
    (plugin / ".github" / "plugin" / "plugin.json").write_text(
        '{"name":"review","mcpServers":"../shared/servers.json"}', encoding="utf-8"
    )
    (shared / "actual-servers.json").write_text(
        '{"mcpServers":{"review-tools":{"command":"review"}}}', encoding="utf-8"
    )
    (shared / "servers.json").symlink_to("actual-servers.json")
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_plugin_workflow("      - ./plugins/review\n"), encoding="utf-8")

    bundle = _collect(workflow)
    entries = {entry.logical_path: entry for entry in bundle.entries}

    link = "tree/plugins/shared/servers.json"
    assert entries[link].link_target == "actual-servers.json"
    assert "tree/plugins/shared/actual-servers.json" in entries


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics require POSIX privileges")
def test_plugin_skill_symlink_collects_directory_target(tmp_path: Path) -> None:
    # Requirement: plugin skill directory links collect their targets into the plugin namespace.
    plugin = tmp_path / "plugin"
    _write_plugin(plugin)
    shared = plugin / "shared-references"
    shared.mkdir()
    (shared / "guide.md").write_text("guide", encoding="utf-8")
    references = plugin / "skills" / "plugin-skill" / "references"
    references.symlink_to("../../shared-references", target_is_directory=True)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_plugin_workflow("      - ./plugin\n"), encoding="utf-8")

    bundle = _collect(workflow)
    entries = {entry.logical_path: entry for entry in bundle.entries}

    link = "tree/plugins/review/skills/plugin-skill/references"
    assert entries[link].link_target == "../../shared-references"
    assert "tree/plugins/review/shared-references/guide.md" in entries


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics require POSIX privileges")
def test_plugin_skill_symlink_cycle_is_rejected(tmp_path: Path) -> None:
    # Requirement: plugin skill directory link cycles fail explicitly instead of hanging.
    plugin = tmp_path / "plugin"
    _write_plugin(plugin)
    skill = plugin / "skills" / "plugin-skill"
    (skill / "loop").symlink_to(skill, target_is_directory=True)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_plugin_workflow("      - ./plugin\n"), encoding="utf-8")

    with pytest.raises(BundleCycleError, match="Circular bundle directory traversal"):
        _collect(workflow)


def test_per_agent_provider_override_selects_plugin_flavor(tmp_path: Path) -> None:
    # Requirement: each agent resolves plugins with its effective provider's flavor.
    checkout = tmp_path / "checkout"
    copilot = checkout / "copilot-plugin"
    claude = checkout / "claude-plugin"
    (copilot / ".github" / "plugin").mkdir(parents=True)
    (copilot / ".github" / "plugin" / "plugin.json").write_text(
        '{"name":"review"}', encoding="utf-8"
    )
    (claude / ".claude-plugin").mkdir(parents=True)
    (claude / ".claude-plugin" / "plugin.json").write_text('{"name":"review"}', encoding="utf-8")
    (checkout / ".github" / "plugin").mkdir(parents=True)
    (checkout / ".github" / "plugin" / "marketplace.json").write_text(
        json.dumps({"name": "acme", "plugins": [{"name": "review", "source": "copilot-plugin"}]}),
        encoding="utf-8",
    )
    (checkout / ".claude-plugin").mkdir(exist_ok=True)
    (checkout / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "acme", "plugins": [{"name": "review", "source": "claude-plugin"}]}),
        encoding="utf-8",
    )
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
workflow:
  name: provider-flavor
  entry_point: agent
  runtime:
    provider: copilot
    plugin_sources:
      acme:
        source: ./checkout
agents:
  - name: agent
    provider: claude-agent-sdk
    prompt: test
    plugins: [review@acme]
    routes:
      - to: $end
""",
        encoding="utf-8",
    )

    bundle = _collect(workflow)

    assert bundle.manifest.plugins_topology["review"].endswith("/.claude-plugin/plugin.json")
    assert bundle.descriptor.provenance.plugins[0].flavor == "claude"


def test_plugin_skills_switch_excludes_skill_content_and_topology(tmp_path: Path) -> None:
    # Requirement: disabling plugin skills removes their files and topology dependency.
    plugin = tmp_path / "plugin"
    _write_plugin(plugin)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _plugin_workflow("      - name: ./plugin\n        skills: false\n"),
        encoding="utf-8",
    )

    bundle = _collect(workflow)
    paths = {entry.logical_path for entry in bundle.entries}

    assert all(not path.startswith("tree/plugins/review/skills/") for path in paths)
    assert "plugin-skill" not in bundle.manifest.skills_topology
    assert "tree/plugins/review/agents/reviewer.agent.md" in paths
    assert "tree/plugins/review/.mcp.json" in paths


def test_declared_skill_shadows_same_named_plugin_skill(tmp_path: Path) -> None:
    # Requirement: declared skills win plugin collisions in bundle topology as at runtime.
    name = "shared"
    declared = tmp_path / "declared" / name
    declared.mkdir(parents=True)
    (declared / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test.\n---\n\nDeclared body.\n", encoding="utf-8"
    )
    plugin = tmp_path / "plugin"
    _write_plugin(plugin, skill=name)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _plugin_workflow(
            "      - ./plugin\n",
            skills="    skills: [./declared/shared]\n",
        ),
        encoding="utf-8",
    )
    collector_warnings: list[str] = []

    bundle = collect_bundle(
        workflow,
        environment=None,
        allow_network=False,
        on_warning=collector_warnings.append,
    )
    runtime_warnings: list[str] = []
    declared_skills = resolve_effective_skills(
        ["./declared/shared"],
        sources=(),
        exclude=(),
        base_dir=tmp_path,
        on_warning=runtime_warnings.append,
    )
    plugin_skills = list(
        resolve_plugins(
            [PluginDef(name="./plugin")],
            base_dir=tmp_path,
            flavor="copilot",
        )[0].skills
    )
    runtime_winner = _merge_skills(
        declared_skills, plugin_skills, on_warning=runtime_warnings.append
    )[0]
    paths = {entry.logical_path for entry in bundle.entries}

    assert runtime_winner.directory == declared
    assert bundle.manifest.skills_topology[name] == "tree/skills/shared/SKILL.md"
    assert "tree/skills/shared/SKILL.md" in paths
    assert "tree/plugins/review/skills/shared/SKILL.md" in paths
    assert collector_warnings == runtime_warnings
    assert "shadowed by the declared skill" in collector_warnings[0]


def test_assets_hidden_rules_and_additional_root(tmp_path: Path) -> None:
    # Requirement: assets honor hidden glob rules and use stable additional-root metadata.
    (tmp_path / "run.sh").write_text("run", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.sh").write_text("ci", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret.sh").write_text("secret", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-assets"
    outside.mkdir()
    (outside / "data.txt").write_text("data", encoding="utf-8")
    authored = f"../{outside.name}"
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _workflow(
            "inline",
            extra_workflow=(
                "\n    assets: ['*.sh', '.github/**', '**', "
                f"'{authored}/data.txt']\n    additional_roots: ['{authored}']"
            ),
        ),
        encoding="utf-8",
    )

    bundle = _collect(workflow)
    paths = {entry.logical_path for entry in bundle.entries}

    assert "tree/main/run.sh" in paths
    assert "tree/main/.github/ci.sh" in paths
    assert all("/.git/" not in f"/{path}/" for path in paths)
    root_entry = next(entry for entry in bundle.entries if entry.logical_path.endswith("data.txt"))
    assert root_entry.origin_detail == "asset:3;additional_root=00"
    assert str(outside.parent) not in root_entry.origin_detail


def test_additional_root_origin_is_independent_of_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: different host paths and basenames produce identical manifests and digests.
    def collect_under(host: Path, root_name: str) -> CollectedBundle:
        project = host / "project"
        outside = host / root_name
        project.mkdir(parents=True)
        outside.mkdir()
        (outside / "data.txt").write_text("data", encoding="utf-8")
        monkeypatch.setenv("BUNDLE_SHARED_ROOT", str(outside))
        workflow = project / "workflow.yaml"
        workflow.write_text(
            _workflow(
                "inline",
                extra_workflow=(
                    "\n    assets: ['../${ROOT_NAME}/data.txt']"
                    "\n    additional_roots: ['${BUNDLE_SHARED_ROOT}']"
                ),
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("ROOT_NAME", root_name)
        return _collect(workflow)

    first = collect_under(tmp_path / "first-host-location", "shared-one")
    second = collect_under(tmp_path / "different-host-location", "other-assets")

    first_entry = next(entry for entry in first.entries if entry.logical_path.endswith("data.txt"))
    second_entry = next(
        entry for entry in second.entries if entry.logical_path.endswith("data.txt")
    )
    assert first_entry == second_entry
    assert first_entry.logical_path == "tree/roots/00/data.txt"
    assert first_entry.origin_detail == "asset:0;additional_root=00"
    assert first.manifest.bundle_digest == second.manifest.bundle_digest


def test_dynamic_template_and_unset_environment_fail_precisely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: dynamic targets are typed errors and env expansion keeps ConfigurationError.
    prompt = tmp_path / "prompt.md"
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_workflow("!file prompt.md"), encoding="utf-8")
    prompt.write_text("line\n{% include target %}", encoding="utf-8")
    with pytest.raises(BundleDynamicTemplateError, match="include.*line 2"):
        _collect(workflow)
    monkeypatch.delenv("BUNDLE_MISSING", raising=False)
    prompt.write_text("{% include '${BUNDLE_MISSING}.md' %}", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="BUNDLE_MISSING"):
        _collect(workflow)


def test_root_escape_and_missing_declared_root_fail(tmp_path: Path) -> None:
    # Requirement: local escapes need authorization and declared roots must exist.
    outside = tmp_path.parent / f"{tmp_path.name}-prompt.md"
    outside.write_text("outside", encoding="utf-8")
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_workflow(f"!file ../{outside.name}"), encoding="utf-8")
    with pytest.raises(BundleRootEscapeError, match="additional_roots"):
        _collect(workflow)
    workflow.write_text(
        _workflow("inline", extra_workflow="\n    additional_roots: [missing-root]"),
        encoding="utf-8",
    )
    with pytest.raises(BundleError, match="does not exist"):
        _collect(workflow)


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics require POSIX privileges")
def test_symlink_payload_and_escape(tmp_path: Path) -> None:
    # Requirement: in-root links hash their linkname while escaping targets are rejected.
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to("target.txt")
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _workflow("inline", extra_workflow="\n    assets: [link.txt]"), encoding="utf-8"
    )
    bundle = _collect(workflow)
    entry = next(item for item in bundle.entries if item.logical_path.endswith("link.txt"))
    assert entry.digest == f"sha256:{hashlib.sha256(b'target.txt').hexdigest()}"
    assert bundle.links[entry.logical_path] == "target.txt"

    link.unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link.symlink_to(outside)
    with pytest.raises(BundleSymlinkEscapeError):
        _collect(workflow)


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics require POSIX privileges")
def test_include_and_asset_symlinks_collect_components_and_directory_targets(
    tmp_path: Path,
) -> None:
    # Requirement: include and asset links retain links and recursively collect authorized targets.
    real_prompts = tmp_path / "real-prompts"
    real_prompts.mkdir()
    (real_prompts / "prompt.md").write_text("prompt", encoding="utf-8")
    (tmp_path / "prompts").symlink_to(real_prompts, target_is_directory=True)
    real_assets = tmp_path / "real-assets"
    nested = real_assets / "nested"
    nested.mkdir(parents=True)
    (nested / "data.txt").write_text("data", encoding="utf-8")
    (tmp_path / "assets-link").symlink_to(real_assets, target_is_directory=True)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _workflow(
            "!file prompts/prompt.md",
            extra_workflow="\n    assets: [assets-link]",
        ),
        encoding="utf-8",
    )

    bundle = _collect(workflow)
    entries = {entry.logical_path: entry for entry in bundle.entries}

    assert entries["tree/main/prompts"].link_target == "real-prompts"
    assert "tree/main/real-prompts/prompt.md" in entries
    assert entries["tree/main/assets-link"].link_target == "real-assets"
    assert "tree/main/real-assets/nested/data.txt" in entries


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics require POSIX privileges")
def test_recursive_asset_symlink_cycle_is_rejected(tmp_path: Path) -> None:
    # Requirement: recursive directory symlink cycles fail explicitly instead of hanging.
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "loop").symlink_to(assets, target_is_directory=True)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _workflow("inline", extra_workflow="\n    assets: [assets]"), encoding="utf-8"
    )

    with pytest.raises(BundleCycleError, match="Circular bundle symlink"):
        _collect(workflow)


def test_cycle_depth_constant_and_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement: inode cycles fail explicitly, depth constants match, and caps name actual values.
    assert MAX_SUBWORKFLOW_DEPTH == _MAX_SUBWORKFLOW_VALIDATION_DEPTH
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    template = """\
workflow:
  name: cycle
  entry_point: nested
agents:
  - name: nested
    type: workflow
    workflow: {target}
    routes:
      - to: $end
"""
    a.write_text(template.format(target="b.yaml"), encoding="utf-8")
    b.write_text(template.format(target="a.yaml"), encoding="utf-8")
    with pytest.raises(BundleCycleError, match="a.yaml.*b.yaml.*a.yaml"):
        _collect(a)

    workflow = tmp_path / "simple.yaml"
    workflow.write_text(_workflow("inline"), encoding="utf-8")
    monkeypatch.setattr(collector_module, "MAX_BUNDLE_ENTRIES", 0)
    with pytest.raises(BundleCapsError, match="maximum 0, actual 1"):
        _collect(workflow)


def test_git_provenance_intersects_dirty_bundled_files(tmp_path: Path) -> None:
    # Requirement: git provenance reports only modified files that are part of the bundle.
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    workflow = tmp_path / "workflow.yaml"
    prompt = tmp_path / "prompt.md"
    untouched = tmp_path / "untouched.txt"
    workflow.write_text(_workflow("!file prompt.md"), encoding="utf-8")
    prompt.write_text("original", encoding="utf-8")
    untouched.write_text("original", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
    )
    prompt.write_text("dirty", encoding="utf-8")
    untouched.write_text("dirty", encoding="utf-8")

    provenance = _collect(workflow).descriptor.provenance.git

    assert provenance is not None
    assert "tree/main/prompt.md" in provenance.dirty
    assert "tree/main/workflow.yaml" not in provenance.dirty
    assert all("untouched.txt" not in path for path in provenance.dirty)
