"""Integration tests for the registry system.

Tests the full flow: configure registry → resolve ref → fetch workflow → get cached path.
Uses local path registries to avoid network dependencies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.registry.cache import fetch_workflow
from conductor.registry.config import (
    RegistryType,
    add_registry,
    load_config,
    remove_registry,
)
from conductor.registry.errors import RegistryError
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
            ``description``, ``path``, and ``content`` (the YAML text of the
            workflow file).
        sibling_files: Optional mapping of workflow-name → dict of
            filename → content for extra files alongside the workflow.

    Returns:
        Path to the registry root directory.
    """
    from ruamel.yaml import YAML

    registry_dir = root / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)

    index_data: dict = {"workflows": {}}
    for name, info in workflows.items():
        index_data["workflows"][name] = {
            "description": info.get("description", ""),
            "path": info["path"],
        }

        wf_path = registry_dir / info["path"]
        wf_path.parent.mkdir(parents=True, exist_ok=True)
        wf_path.write_text(info["content"])

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
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("my-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)

        # Path registries don't accept refs — use the bare name with explicit registry.
        ref = resolve_ref("hello@my-reg")
        assert ref.kind == "registry"
        assert ref.workflow == "hello"
        assert ref.registry_name == "my-reg"
        assert ref.ref is None
        assert ref.registry_entry is not None

        cached_path = fetch_workflow("my-reg", ref.registry_entry, "hello")
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
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("default-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)

        ref = resolve_ref("greeter")
        assert ref.kind == "registry"
        assert ref.registry_name == "default-reg"
        assert ref.workflow == "greeter"

        cached = fetch_workflow("default-reg", ref.registry_entry, "greeter")
        assert cached.exists()
        assert "test-workflow" in cached.read_text()


# ---------------------------------------------------------------------------
# Path registries reject refs
# ---------------------------------------------------------------------------


class TestPathRegistryRefs:
    """Path registries do not support refs and raise on non-empty refs."""

    def test_fetch_with_ref_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "wf": {
                    "description": "",
                    "path": "wf.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("p-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        ref = resolve_ref("wf")
        assert ref.registry_entry is not None

        with pytest.raises(RegistryError, match="Path registries do not support refs"):
            fetch_workflow("p-reg", ref.registry_entry, "wf", ref="v1.0.0")


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
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("cache-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        ref = resolve_ref("cached-wf")
        assert ref.registry_entry is not None

        path1 = fetch_workflow("cache-reg", ref.registry_entry, "cached-wf")
        path2 = fetch_workflow("cache-reg", ref.registry_entry, "cached-wf")

        assert path1 == path2
        assert path1.exists()
        # Path registries don't use cache — returns source directly.
        assert str(path1).startswith(str(reg_dir))


# ---------------------------------------------------------------------------
# Sibling files
# ---------------------------------------------------------------------------


class TestSiblingFiles:
    """Sibling files in the workflow directory are present alongside the workflow."""

    def test_siblings_alongside_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "with-siblings": {
                    "description": "Has extra files",
                    "path": "with-siblings/workflow.yaml",
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

        cached = fetch_workflow("sib-reg", ref.registry_entry, "with-siblings")
        cache_dir = cached.parent

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
        reg_dir = _create_local_registry(
            self._tmp_path,
            {
                "demo": {
                    "description": "Demo workflow",
                    "path": "demo.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        result = runner.invoke(
            app, ["registry", "add", "test-reg", str(reg_dir), "--type", "path", "--default"]
        )
        assert result.exit_code == 0, result.output
        assert "added" in result.output

        result = runner.invoke(app, ["registry", "list"])
        assert result.exit_code == 0, result.output
        assert "test-reg" in result.output
        assert "✓" in result.output

        result = runner.invoke(app, ["registry", "list", "test-reg"])
        assert result.exit_code == 0, result.output
        assert "demo" in result.output
        assert "Demo workflow" in result.output

        result = runner.invoke(app, ["registry", "remove", "test-reg"])
        assert result.exit_code == 0, result.output
        assert "removed" in result.output

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
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        runner.invoke(app, ["registry", "add", "def-reg", str(reg_dir), "--type", "path"])
        runner.invoke(app, ["registry", "set-default", "def-reg"])

        config = load_config()
        assert config.default == "def-reg"

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
