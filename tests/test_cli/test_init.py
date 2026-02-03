"""Tests for the init and templates commands.

This module tests:
- Templates listing
- Workflow initialization from templates
- Template rendering
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


class TestTemplatesCommand:
    """Tests for the templates command."""

    def test_templates_command_help(self) -> None:
        """Test that templates --help works."""
        result = runner.invoke(app, ["templates", "--help"])
        assert result.exit_code == 0
        assert "List available workflow templates" in result.output

    def test_templates_lists_available(self) -> None:
        """Test that templates command lists available templates."""
        result = runner.invoke(app, ["templates"])
        assert result.exit_code == 0
        # Should list the templates we created
        assert "simple" in result.output
        assert "loop" in result.output
        assert "human-gate" in result.output

    def test_templates_shows_descriptions(self) -> None:
        """Test that templates command shows descriptions."""
        result = runner.invoke(app, ["templates"])
        assert result.exit_code == 0
        # Should include some descriptive text
        assert "linear" in result.output.lower() or "workflow" in result.output.lower()


class TestInitCommand:
    """Tests for the init command."""

    def test_init_command_help(self) -> None:
        """Test that init --help works."""
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        assert "Initialize a new workflow file" in result.output

    def test_init_creates_simple_workflow(self, tmp_path: Path) -> None:
        """Test initializing with simple template."""
        # Run in temp directory
        output_file = tmp_path / "my-workflow.yaml"
        result = runner.invoke(
            app,
            [
                "init",
                "my-workflow",
                "--output",
                str(output_file),
            ],
        )

        assert result.exit_code == 0
        assert output_file.exists()
        assert "Created workflow file" in result.output

        # Check content has the workflow name
        content = output_file.read_text()
        assert "my-workflow" in content

    def test_init_with_loop_template(self, tmp_path: Path) -> None:
        """Test initializing with loop template."""
        output_file = tmp_path / "loop-workflow.yaml"
        result = runner.invoke(
            app,
            [
                "init",
                "loop-workflow",
                "--template",
                "loop",
                "--output",
                str(output_file),
            ],
        )

        assert result.exit_code == 0
        assert output_file.exists()

        content = output_file.read_text()
        assert "loop-workflow" in content
        # Loop template should have generator and reviewer
        assert "generator" in content or "review" in content.lower()

    def test_init_with_human_gate_template(self, tmp_path: Path) -> None:
        """Test initializing with human-gate template."""
        output_file = tmp_path / "gate-workflow.yaml"
        result = runner.invoke(
            app,
            [
                "init",
                "gate-workflow",
                "--template",
                "human-gate",
                "--output",
                str(output_file),
            ],
        )

        assert result.exit_code == 0
        assert output_file.exists()

        content = output_file.read_text()
        assert "gate-workflow" in content
        # Human gate template should have human_gate type
        assert "human_gate" in content

    def test_init_invalid_template(self, tmp_path: Path) -> None:
        """Test init with invalid template name."""
        output_file = tmp_path / "workflow.yaml"
        result = runner.invoke(
            app,
            [
                "init",
                "workflow",
                "--template",
                "nonexistent",
                "--output",
                str(output_file),
            ],
        )

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_init_file_already_exists(self, tmp_path: Path) -> None:
        """Test init when file already exists."""
        output_file = tmp_path / "existing.yaml"
        output_file.write_text("existing content")

        result = runner.invoke(
            app,
            [
                "init",
                "existing",
                "--output",
                str(output_file),
            ],
        )

        assert result.exit_code != 0
        assert "exists" in result.output.lower()

        # Original content should be unchanged
        assert output_file.read_text() == "existing content"

    def test_init_default_output_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test init creates file with default name in current directory."""
        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["init", "test-workflow"])

        assert result.exit_code == 0
        # Should create test-workflow.yaml in current directory
        expected_file = tmp_path / "test-workflow.yaml"
        assert expected_file.exists()

    def test_init_short_template_option(self, tmp_path: Path) -> None:
        """Test init with short -t option."""
        output_file = tmp_path / "short-test.yaml"
        result = runner.invoke(
            app,
            [
                "init",
                "short-test",
                "-t",
                "loop",
                "-o",
                str(output_file),
            ],
        )

        assert result.exit_code == 0
        assert output_file.exists()


class TestInitHelperFunctions:
    """Tests for init helper functions."""

    def test_list_templates_returns_all(self) -> None:
        """Test that list_templates returns all templates."""
        from conductor.cli.init import list_templates

        templates = list_templates()
        template_names = [t.name for t in templates]

        assert "simple" in template_names
        assert "loop" in template_names
        assert "human-gate" in template_names

    def test_get_template_existing(self) -> None:
        """Test get_template with existing template."""
        from conductor.cli.init import get_template

        template = get_template("simple")
        assert template is not None
        assert template.name == "simple"
        assert template.filename == "simple.yaml"

    def test_get_template_nonexistent(self) -> None:
        """Test get_template with nonexistent template."""
        from conductor.cli.init import get_template

        template = get_template("nonexistent")
        assert template is None

    def test_render_template_substitutes_name(self) -> None:
        """Test that render_template substitutes the workflow name."""
        from conductor.cli.init import render_template

        content = render_template("simple", "my-awesome-workflow")
        assert "my-awesome-workflow" in content

    def test_render_template_invalid_raises(self) -> None:
        """Test that render_template raises for invalid template."""
        from conductor.cli.init import render_template

        with pytest.raises(ValueError, match="not found"):
            render_template("nonexistent", "test")

    def test_get_template_dir_exists(self) -> None:
        """Test that template directory exists."""
        from conductor.cli.init import get_template_dir

        template_dir = get_template_dir()
        assert template_dir.exists()
        assert template_dir.is_dir()

    def test_template_files_exist(self) -> None:
        """Test that all template files exist."""
        from conductor.cli.init import TEMPLATES, get_template_dir

        template_dir = get_template_dir()
        for template in TEMPLATES.values():
            template_path = template_dir / template.filename
            assert template_path.exists(), f"Template file missing: {template_path}"
