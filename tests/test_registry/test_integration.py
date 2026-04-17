"""Integration tests for the registry system.

Tests the full flow: configure registry → resolve ref → fetch workflow → get cached path.
Uses local path registries to avoid network dependencies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.registry.cache import fetch_workflow, get_cached_workflow_path
from conductor.registry.config import (
    RegistryType,
    add_registry,
    load_config,
    remove_registry,
)
from conductor.registry.resolver import resolve_ref

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CONDUCTOR_HOME at a temp directory and return its path."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


def _create_local_registry(
    root: Path,
    workflows: dict[str, dict],
    *,
    sibling_files: dict[str, dict[str, str]] | None = None,
) -> Path:
    """Build a minimal local registry directory.

    Args:
        root: Parent directory (e.g. tmp_path).
        workflows: Mapping of workflow-name → dict with keys
            ``description``, ``path``, ``versions``, and ``content``
            (the YAML text of the workflow file).
        sibling_files: Optional mapping of workflow-name → dict of
            filename → content for extra files alongside the workflow.

    Returns:
        Path to the registry root directory.
    """
    from ruamel.yaml import YAML

    registry_dir = root / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)

    # Build index.yaml
    index_data: dict = {"workflows": {}}
    for name, info in workflows.items():
        index_data["workflows"][name] = {
            "description": info.get("description", ""),
            "path": info["path"],
            "versions": info.get("versions", []),
        }

        # Write the workflow file
        wf_path = registry_dir / info["path"]
        wf_path.parent.mkdir(parents=True, exist_ok=True)
        wf_path.write_text(info["content"])

        # Write sibling files
        if sibling_files and name in sibling_files:
            for fname, fcontent in sibling_files[name].items():
                (wf_path.parent / fname).write_text(fcontent)

    yaml = YAML()
    with open(registry_dir / "index.yaml", "w") as f:
        yaml.dump(index_data, f)

    return registry_dir


_SIMPLE_WORKFLOW = """\
name: test-workflow
agents:
  helper:
    model: copilot
    instructions: Say hello
steps:
  - agent: helper
"""


# ---------------------------------------------------------------------------
# Full local registry flow
# ---------------------------------------------------------------------------


class TestFullLocalFlow:
    """Configure → resolve → fetch → verify cached content."""

    def test_local_registry_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "hello": {
                    "description": "A greeting workflow",
                    "path": "hello/workflow.yaml",
                    "versions": ["1.0.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        # Add registry
        add_registry("my-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)

        # Resolve with explicit registry and version
        ref = resolve_ref("hello@my-reg@1.0.0")
        assert ref.kind == "registry"
        assert ref.workflow == "hello"
        assert ref.registry_name == "my-reg"
        assert ref.version == "1.0.0"
        assert ref.registry_entry is not None

        # Fetch
        cached_path = fetch_workflow("my-reg", ref.registry_entry, "hello", version="1.0.0")
        assert cached_path.exists()
        assert cached_path.name == "workflow.yaml"
        assert "test-workflow" in cached_path.read_text()


# ---------------------------------------------------------------------------
# Default registry flow
# ---------------------------------------------------------------------------


class TestDefaultRegistryFlow:
    """Resolve using the default registry (no @registry in ref)."""

    def test_resolve_via_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "greeter": {
                    "description": "Greet someone",
                    "path": "greeter.yaml",
                    "versions": ["0.1.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("default-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)

        # Resolve without @registry — should use default
        ref = resolve_ref("greeter")
        assert ref.kind == "registry"
        assert ref.registry_name == "default-reg"
        assert ref.workflow == "greeter"

        # Fetch and verify
        cached = fetch_workflow("default-reg", ref.registry_entry, "greeter", version="0.1.0")
        assert cached.exists()
        assert "test-workflow" in cached.read_text()


# ---------------------------------------------------------------------------
# Latest version resolution
# ---------------------------------------------------------------------------


class TestLatestVersionResolution:
    """When no version is specified, the last version in the list is used."""

    def test_resolves_latest_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "multi-ver": {
                    "description": "Has multiple versions",
                    "path": "multi-ver.yaml",
                    "versions": ["1.0.0", "1.1.0", "2.0.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("ver-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)

        # Fetch without version — should resolve to 2.0.0 (last in list)
        ref = resolve_ref("multi-ver")
        assert ref.registry_entry is not None

        cached = fetch_workflow("ver-reg", ref.registry_entry, "multi-ver")
        assert cached.exists()

        # The cache directory should be under the "2.0.0" version folder
        assert "2.0.0" in str(cached)


# ---------------------------------------------------------------------------
# Cache reuse
# ---------------------------------------------------------------------------


class TestCacheReuse:
    """Second fetch of the same ref returns the cached path without re-copying."""

    def test_second_fetch_returns_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "cached-wf": {
                    "description": "Cached workflow",
                    "path": "cached-wf.yaml",
                    "versions": ["1.0.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("cache-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        ref = resolve_ref("cached-wf")
        assert ref.registry_entry is not None

        # First fetch
        path1 = fetch_workflow("cache-reg", ref.registry_entry, "cached-wf", version="1.0.0")

        # Second fetch — should return the same cached path
        path2 = fetch_workflow("cache-reg", ref.registry_entry, "cached-wf", version="1.0.0")

        assert path1 == path2
        assert path1.exists()

        # Also verify via the direct cache lookup API
        cached = get_cached_workflow_path("cache-reg", "cached-wf", "1.0.0")
        assert cached == path1


# ---------------------------------------------------------------------------
# Sibling files
# ---------------------------------------------------------------------------


class TestSiblingFiles:
    """Sibling files in the workflow directory are also cached."""

    def test_siblings_copied_to_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "with-siblings": {
                    "description": "Has extra files",
                    "path": "with-siblings/workflow.yaml",
                    "versions": ["1.0.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
            sibling_files={
                "with-siblings": {
                    "prompt.txt": "You are a helpful assistant.",
                    "schema.json": '{"type": "object"}',
                },
            },
        )

        add_registry("sib-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        ref = resolve_ref("with-siblings")
        assert ref.registry_entry is not None

        cached = fetch_workflow("sib-reg", ref.registry_entry, "with-siblings", version="1.0.0")
        cache_dir = cached.parent

        # Siblings should be in the same directory as the workflow
        assert (cache_dir / "prompt.txt").exists()
        assert (cache_dir / "prompt.txt").read_text() == "You are a helpful assistant."
        assert (cache_dir / "schema.json").exists()
        assert (cache_dir / "schema.json").read_text() == '{"type": "object"}'


# ---------------------------------------------------------------------------
# CLI round-trip
# ---------------------------------------------------------------------------


class TestCLIRoundTrip:
    """Exercise the CLI commands for add → list → list <name> → remove."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_home(tmp_path, monkeypatch)
        self._tmp_path = tmp_path

    def test_full_cli_lifecycle(self) -> None:
        # Create a local registry on disk
        reg_dir = _create_local_registry(
            self._tmp_path,
            {
                "demo": {
                    "description": "Demo workflow",
                    "path": "demo.yaml",
                    "versions": ["1.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        # Add registry with --default
        result = runner.invoke(
            app, ["registry", "add", "test-reg", str(reg_dir), "--type", "path", "--default"]
        )
        assert result.exit_code == 0, result.output
        assert "added" in result.output

        # List registries — should show test-reg
        result = runner.invoke(app, ["registry", "list"])
        assert result.exit_code == 0, result.output
        assert "test-reg" in result.output
        assert "✓" in result.output  # default marker

        # List workflows in test-reg
        result = runner.invoke(app, ["registry", "list", "test-reg"])
        assert result.exit_code == 0, result.output
        assert "demo" in result.output
        assert "Demo workflow" in result.output

        # Remove registry
        result = runner.invoke(app, ["registry", "remove", "test-reg"])
        assert result.exit_code == 0, result.output
        assert "removed" in result.output

        # List again — should be empty
        result = runner.invoke(app, ["registry", "list"])
        assert result.exit_code == 0, result.output
        assert "No registries configured" in result.output

    def test_add_list_remove_multiple(self) -> None:
        """Add two registries, list both, remove one, verify the other remains."""
        reg1 = _create_local_registry(
            self._tmp_path / "r1_parent",
            {
                "wf-a": {
                    "description": "A",
                    "path": "a.yaml",
                    "versions": ["1.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )
        reg2 = _create_local_registry(
            self._tmp_path / "r2_parent",
            {
                "wf-b": {
                    "description": "B",
                    "path": "b.yaml",
                    "versions": ["2.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        runner.invoke(app, ["registry", "add", "reg-a", str(reg1), "--type", "path"])
        runner.invoke(app, ["registry", "add", "reg-b", str(reg2), "--type", "path"])

        result = runner.invoke(app, ["registry", "list"])
        assert "reg-a" in result.output
        assert "reg-b" in result.output

        runner.invoke(app, ["registry", "remove", "reg-a"])

        result = runner.invoke(app, ["registry", "list"])
        assert "reg-a" not in result.output
        assert "reg-b" in result.output

    def test_set_default_and_resolve(self) -> None:
        """Set a default registry and resolve a bare workflow name."""
        reg_dir = _create_local_registry(
            self._tmp_path,
            {
                "auto": {
                    "description": "Auto-resolved",
                    "path": "auto.yaml",
                    "versions": ["1.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        runner.invoke(app, ["registry", "add", "def-reg", str(reg_dir), "--type", "path"])
        runner.invoke(app, ["registry", "set-default", "def-reg"])

        # Verify config
        config = load_config()
        assert config.default == "def-reg"

        # Resolve bare name (no @)
        ref = resolve_ref("auto")
        assert ref.kind == "registry"
        assert ref.registry_name == "def-reg"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Additional edge-case coverage for the integration flow."""

    def test_remove_default_clears_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removing the default registry clears the default setting."""
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "wf": {
                    "description": "",
                    "path": "wf.yaml",
                    "versions": ["1.0"],
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("gone", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        assert load_config().default == "gone"

        remove_registry("gone")
        assert load_config().default is None

    def test_file_path_takes_precedence_over_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing file on disk resolves as 'file', not 'registry'."""
        _setup_home(tmp_path, monkeypatch)

        local_file = tmp_path / "my-workflow.yaml"
        local_file.write_text(_SIMPLE_WORKFLOW)

        ref = resolve_ref(str(local_file))
        assert ref.kind == "file"
        assert ref.path == local_file
