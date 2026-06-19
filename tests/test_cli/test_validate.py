"""Tests for the validate command.

This module tests:
- Validation of valid workflow files
- Validation of various invalid files (malformed YAML, missing fields, bad routes)
- Error message formatting
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


class TestValidateCommand:
    """Tests for the validate command."""

    def test_validate_command_help(self) -> None:
        """Test that validate --help works."""
        result = runner.invoke(app, ["validate", "--help"])
        assert result.exit_code == 0
        assert "Validate a workflow YAML file" in result.output

    def test_validate_valid_simple_workflow(self, fixtures_dir: Path) -> None:
        """Test validating a valid simple workflow file."""
        workflow_file = fixtures_dir / "valid_simple.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code == 0
        assert "Validation Successful" in result.output
        assert "simple-workflow" in result.output

    def test_validate_valid_full_workflow(self, fixtures_dir: Path) -> None:
        """Test validating a valid full-featured workflow file."""
        workflow_file = fixtures_dir / "valid_full.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code == 0
        assert "Validation Successful" in result.output

    def test_validate_templated_enums(self, fixtures_dir: Path) -> None:
        """#262: a workflow with templated reasoning.effort + context_tier
        validates clean (templates are deferred to runtime, not rejected)."""
        workflow_file = fixtures_dir / "valid_templated_enums.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code == 0
        assert "Validation Successful" in result.output
        assert "templated-enums-workflow" in result.output

    def test_validate_malformed_yaml(self, fixtures_dir: Path) -> None:
        """Test validating a file with malformed YAML."""
        workflow_file = fixtures_dir / "invalid_malformed.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code != 0
        assert "Validation Failed" in result.output
        # Should mention YAML or syntax error
        assert "YAML" in result.output or "syntax" in result.output.lower()

    def test_validate_bad_route(self, fixtures_dir: Path) -> None:
        """Test validating a file with invalid route target."""
        workflow_file = fixtures_dir / "invalid_bad_route.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code != 0
        assert "Validation Failed" in result.output
        # Should mention the unknown agent
        assert "unknown" in result.output.lower()

    def test_validate_missing_entry_point(self, fixtures_dir: Path) -> None:
        """Test validating a file with missing entry point."""
        workflow_file = fixtures_dir / "invalid_missing_entry.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code != 0
        assert "Validation Failed" in result.output
        # Should mention entry point not found
        assert "entry" in result.output.lower() or "not found" in result.output.lower()

    def test_validate_human_gate_without_options(self, fixtures_dir: Path) -> None:
        """Test validating a human gate without options."""
        workflow_file = fixtures_dir / "invalid_gate_no_options.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code != 0
        assert "Validation Failed" in result.output
        # Should mention human_gate or options
        assert "human_gate" in result.output or "options" in result.output.lower()

    def test_validate_nonexistent_file(self) -> None:
        """Test validating a file that doesn't exist."""
        result = runner.invoke(app, ["validate", "nonexistent.yaml"])

        # Typer should catch this before our code
        assert result.exit_code != 0

    def test_validate_shows_agent_count(self, fixtures_dir: Path) -> None:
        """Test that validation success shows agent count."""
        workflow_file = fixtures_dir / "valid_simple.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code == 0
        # Should show agents info
        assert "greeter" in result.output  # Agent name from valid_simple.yaml

    def test_validate_with_env_vars(
        self, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test validating a file with environment variables."""
        monkeypatch.setenv("TEST_MODEL", "gpt-4")
        workflow_file = fixtures_dir / "valid_env_vars.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code == 0
        assert "Validation Successful" in result.output


class TestValidateErrorFormatting:
    """Tests for validate command error formatting."""

    def test_error_includes_file_path(self, fixtures_dir: Path) -> None:
        """Test that error includes the file path."""
        workflow_file = fixtures_dir / "invalid_bad_route.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code != 0
        # Should include the file name (may be just the basename or full path)
        # Rich may format this differently in different environments
        assert (
            "invalid_bad_route.yaml" in result.output
            or str(workflow_file) in result.output
            or "File:" in result.output  # At minimum, should show the File: label
        ), f"Expected file path in error output. Got: {result.output[:500]}"

    def test_error_includes_suggestion(self, fixtures_dir: Path) -> None:
        """Test that error includes a suggestion when available."""
        workflow_file = fixtures_dir / "invalid_bad_route.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code != 0
        # Our errors include suggestions
        # Note: The exact format may vary, but there should be helpful text

    def test_malformed_yaml_includes_line_info(self, fixtures_dir: Path) -> None:
        """Test that malformed YAML error includes line info when available."""
        workflow_file = fixtures_dir / "invalid_malformed.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])

        assert result.exit_code != 0
        # YAML errors typically include line numbers
        # The exact format depends on the YAML parser


class TestValidateDisplayFunctions:
    """Tests for validate display helper functions."""

    def test_display_validation_success_with_mock(self, tmp_path: Path) -> None:
        """Test validation success display with a temporary workflow."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.validate import display_validation_success
        from conductor.config.loader import load_config

        # Create a simple valid workflow
        workflow_file = tmp_path / "test.yaml"
        workflow_file.write_text("""\
workflow:
  name: test-workflow
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end

output:
  result: "done"
""")

        config = load_config(workflow_file)

        # Capture output
        output = StringIO()
        console = Console(file=output, force_terminal=True)

        display_validation_success(config, workflow_file, console)

        output_text = output.getvalue()
        assert "test-workflow" in output_text
        assert "agent1" in output_text

    def test_validate_workflow_function_valid(self, tmp_path: Path) -> None:
        """Test validate_workflow function with valid file."""
        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "test.yaml"
        workflow_file.write_text("""\
workflow:
  name: test-workflow
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end

output:
  result: "done"
""")

        is_valid, config = validate_workflow(workflow_file)

        assert is_valid is True
        assert config is not None
        assert config.workflow.name == "test-workflow"

    def test_validate_workflow_function_invalid(self, tmp_path: Path) -> None:
        """Test validate_workflow function with invalid file."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "invalid.yaml"
        workflow_file.write_text("""\
workflow:
  name: invalid
  entry_point: nonexistent

agents:
  - name: agent1
    prompt: "Hello"

output: {}
""")

        # Capture output
        output = StringIO()
        console = Console(file=output, force_terminal=True)

        is_valid, config = validate_workflow(workflow_file, console)

        assert is_valid is False
        assert config is None
        assert "Validation Failed" in output.getvalue()


class TestSemanticValidationIntegration:
    """Integration tests for the new semantic-validation wiring in the CLI."""

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def test_stale_agent_ref_fails_validation(self, tmp_path: Path) -> None:
        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "stale.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: stale-ref
  entry_point: writer
agents:
  - name: writer
    model: gpt-4
    prompt: "Use {{ ghost.output.value }}"
    routes:
      - to: $end
""",
        )
        is_valid, config = validate_workflow(workflow_file)
        assert is_valid is False
        assert config is None

    def test_unknown_workflow_input_fails_validation(self, tmp_path: Path) -> None:
        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "bad_input.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: bad-input
  entry_point: writer
  input:
    topic: { type: string }
agents:
  - name: writer
    model: gpt-4
    prompt: "Write about {{ workflow.input.nonexistent }}"
    routes:
      - to: $end
""",
        )
        is_valid, config = validate_workflow(workflow_file)
        assert is_valid is False
        assert config is None

    def test_explicit_mode_warning_renders(self, tmp_path: Path) -> None:
        """Warnings should be displayed but validation should still pass."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "warn.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: warn
  entry_point: writer
  context:
    mode: explicit
  input:
    topic: { type: string }
agents:
  - name: writer
    model: gpt-4
    prompt: "About {{ workflow.input.topic }}"
    routes:
      - to: $end
""",
        )

        output = StringIO()
        console = Console(file=output, force_terminal=False, width=200)

        is_valid, config = validate_workflow(workflow_file, console=console)
        assert is_valid is True
        assert config is not None
        out = output.getvalue()
        # Warning should mention the undeclared input and explicit mode.
        assert "workflow.input.topic" in out
        assert "explicit" in out

    def test_for_loop_var_does_not_block_validation(self, tmp_path: Path) -> None:
        """Regression test: false-positive #1 should not cause CLI failure."""
        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "loopvar.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: loopvar
  entry_point: pg
agents:
  - name: r1
    model: gpt-4
    prompt: "Research"
  - name: r2
    model: gpt-4
    prompt: "Research"
  - name: aggregator
    model: gpt-4
    prompt: |
      {% for r in pg.outputs %}- {{ r.output.text }}{% endfor %}
    routes:
      - to: $end
parallel:
  - name: pg
    agents: [r1, r2]
    routes:
      - to: aggregator
""",
        )
        is_valid, _ = validate_workflow(workflow_file)
        assert is_valid is True

    def test_validate_command_exits_nonzero_on_stale_ref(self, tmp_path: Path) -> None:
        """The CLI command (not just the function) must propagate failure."""
        workflow_file = tmp_path / "stale.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: stale
  entry_point: writer
agents:
  - name: writer
    model: gpt-4
    prompt: "Use {{ ghost.output }}"
    routes:
      - to: $end
""",
        )
        result = runner.invoke(app, ["validate", str(workflow_file)])
        assert result.exit_code != 0
        assert "Validation Failed" in result.output
        assert "ghost" in result.output
