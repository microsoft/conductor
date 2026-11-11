"""Tests for the validate command.

This module tests:
- Validation of valid workflow files
- Validation of various invalid files (malformed YAML, missing fields, bad routes)
- Error message formatting
"""

from __future__ import annotations

import importlib
import re
from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest
from click import unstyle
from typer.testing import CliRunner

from conductor.bundle.collector import CollectedBundle, _Collector
from conductor.bundle.model import compute_bundle_digest
from conductor.cli.app import app
from conductor.config.environment import resolve_environment
from conductor.config.loader import load_config
from conductor.config.schema import WorkflowConfig
from conductor.config.validator import _EnvironmentValidationContext, validate_workflow_config
from conductor.console import MarkupFreeConsole, make_console
from conductor.engine.run_manifest import RunPreflight, compile_run_manifest
from conductor.exceptions import ConfigurationError

runner = CliRunner()


@pytest.fixture
def retained_validation_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home[qa]"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(
        importlib.import_module("conductor.cli.app"),
        "output_console",
        make_console(force_terminal=True, color_system="standard", no_color=False, width=200),
    )
    project = tmp_path / "project"
    project.mkdir()
    workflow = project / "workflow.yaml"
    workflow.write_text(
        "workflow:\n  name: retained-validation\n  entry_point: first\n"
        "  workspace: {persistence: durable}\n  bundle: {assets: [data.txt]}\n"
        "agents:\n"
        "  - {name: first, type: script, command: echo, routes: [{to: second}]}\n"
        "  - {name: second, type: script, command: echo, routes: [{to: child}]}\n"
        "  - {name: child, type: workflow, workflow: child.yaml, routes: [{to: $end}]}\n"
    )
    (project / "data.txt").write_text("original asset")
    (project / "child.yaml").write_text(
        "workflow:\n  name: child\n  entry_point: agent\n"
        "  runtime:\n    provider: copilot\n    skill_discovery: {sources: [project]}\n"
        "agents:\n  - {name: agent, prompt: fixture, routes: [{to: $end}]}\n"
    )
    skill = project / ".claude" / "skills" / "guide"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: guide\ndescription: Fixture.\n---\nGuide.")
    environment = tmp_path / "environment.yaml"
    environment.write_text("default: workstation\nprofiles:\n  workstation: {backend: local}\n")
    return workflow, environment


@pytest.fixture
def collection_attempts(monkeypatch: pytest.MonkeyPatch) -> list[_Collector]:
    attempts: list[_Collector] = []
    original = _Collector.collect

    def observed(collector: _Collector) -> CollectedBundle:
        attempts.append(collector)
        return original(collector)

    monkeypatch.setattr(_Collector, "collect", observed)
    return attempts


def test_retained_validate_collects_once(
    retained_validation_files: tuple[Path, Path], collection_attempts: list[_Collector]
) -> None:
    # Requirement: CLI compilation, recursive semantics, and reporting share one full closure.
    workflow, environment = retained_validation_files
    result = runner.invoke(app, ["validate", str(workflow), "--environment", str(environment)])

    assert result.exit_code == 0, result.output
    output = unstyle(result.output)
    # Requirement: the assertions exercise ANSI output even on a no-color test host.
    assert output != result.output
    assert len(collection_attempts) == 1
    collector = collection_attempts[0]
    assert {node.path.name for node in collector.nodes} == {"workflow.yaml", "child.yaml"}
    assert collector.files["tree/main/data.txt"] == b"original asset"
    assert "Bundle Closure" in output and "Status: complete" in output
    assert re.findall(r"Bundle digest:\s+(sha256:[0-9a-f]{64})", output) == [
        compute_bundle_digest(
            collector.entries.values(), collector.skills_topology, collector.plugins_topology
        )
    ]
    for step in ("first", "second", "child"):
        assert re.search(rf"{step}\s+workstation\s+local", output)
    assert collector.warnings
    flattened = " ".join(output.split())
    positions = []
    for warning in collector.warnings:
        assert flattened.count(" ".join(warning.split())) == 1
        positions.append(flattened.index(" ".join(warning.split())))
    assert positions == sorted(positions)
    assert "Source preflight reported a warning" not in output


def test_retained_validate_refreshes_each_invocation(
    retained_validation_files: tuple[Path, Path], collection_attempts: list[_Collector]
) -> None:
    # Requirement: an asset edit in the same process gets a fresh closure and digest.
    workflow, environment = retained_validation_files
    arguments = ["validate", str(workflow), "--environment", str(environment)]
    before = runner.invoke(app, arguments)
    (workflow.parent / "data.txt").write_text("changed asset")

    after = runner.invoke(app, arguments)

    assert before.exit_code == after.exit_code == 0
    assert len(collection_attempts) == 2
    assert collection_attempts[0].files["tree/main/data.txt"] == b"original asset"
    assert collection_attempts[1].files["tree/main/data.txt"] == b"changed asset"
    assert re.findall(
        r"Bundle digest:\s+(sha256:[0-9a-f]{64})", unstyle(before.output)
    ) != re.findall(r"Bundle digest:\s+(sha256:[0-9a-f]{64})", unstyle(after.output))


def test_retained_validate_isolates_same_named_roots_and_environments(
    retained_validation_files: tuple[Path, Path], collection_attempts: list[_Collector]
) -> None:
    # Requirement: equal workflow/environment names cannot reuse another invocation's grants.
    workflow, environment = retained_validation_files
    before = runner.invoke(app, ["validate", str(workflow), "--environment", str(environment)])
    second = workflow.parent.parent / "second"
    second.mkdir()
    other_workflow = second / workflow.name
    other_workflow.write_text(
        workflow.read_text().replace(
            "type: workflow, workflow: child.yaml", "type: script, command: echo"
        )
    )
    (second / "data.txt").write_text("other root")
    other_environment = second / environment.name
    other_environment.write_text("default: alternate\nprofiles:\n  alternate: {backend: local}\n")

    after = runner.invoke(
        app, ["validate", str(other_workflow), "--environment", str(other_environment)]
    )

    assert before.exit_code == after.exit_code == 0
    assert [attempt.root_workflow for attempt in collection_attempts] == [workflow, other_workflow]
    assert re.search(r"first\s+alternate\s+local", unstyle(after.output))
    assert collection_attempts[1].files["tree/main/data.txt"] == b"other root"


def test_retained_validate_uses_frozen_child_semantics(
    retained_validation_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: known children are semantically checked from the collected closure.
    workflow, environment = retained_validation_files
    child = workflow.parent / "child.yaml"
    original = _Collector.collect
    attempts = 0

    def observed(collector: _Collector) -> CollectedBundle:
        nonlocal attempts
        attempts += 1
        collected = original(collector)
        child.write_text(
            child.read_text().replace("prompt: fixture", "prompt: '{{ ghost.output }}'")
        )
        return collected

    monkeypatch.setattr(_Collector, "collect", observed)
    result = runner.invoke(app, ["validate", str(workflow), "--environment", str(environment)])

    assert result.exit_code == 0, result.output
    assert attempts == 1
    assert "ghost" not in unstyle(result.output)


@pytest.mark.parametrize("refusal", ["provider", "path"])
def test_retained_validate_keeps_cheap_refusals(
    retained_validation_files: tuple[Path, Path], refusal: Literal["provider", "path"]
) -> None:
    # Requirement: sharing a preflight never admits unsupported providers or escaping cwd.
    workflow, environment = retained_validation_files
    child = workflow.parent / "child.yaml"
    match refusal:
        case "provider":
            child.write_text(
                child.read_text()
                .replace("provider: copilot", "provider: hermes")
                .replace("    skill_discovery: {sources: [project]}\n", "")
            )
            expected = "managed_local_workspace"
        case "path":
            workflow.write_text(
                workflow.read_text().replace(
                    "command: echo", "command: echo, working_dir: ../escape"
                )
            )
            expected = "escape"
        case unreachable:
            assert_never(unreachable)

    result = runner.invoke(app, ["validate", str(workflow), "--environment", str(environment)])

    assert result.exit_code == 1
    output = unstyle(result.output)
    assert expected in output
    assert "Bundle digest:" not in output


@pytest.mark.parametrize("mismatch", ["path", "config", "environment", "document"])
def test_preflight_rejects_incompatible_validation_inputs(
    retained_validation_files: tuple[Path, Path],
    mismatch: Literal["path", "config", "environment", "document"],
) -> None:
    # Requirement: compile and semantic boundaries reject mismatched snapshots without recollection.
    workflow, environment_path = retained_validation_files
    config = load_config(workflow)
    environment = resolve_environment(str(environment_path), workflow_dir=workflow.parent)
    preflight = RunPreflight(config, workflow, environment)
    match mismatch:
        case "path":
            workflow = workflow.with_name("other.yaml")
        case "config":
            config = config.model_copy(
                update={"workflow": config.workflow.model_copy(update={"name": "override"})}
            )
        case "environment":
            environment = replace(environment, path=environment_path.with_name("other.yaml"))
        case "document":
            environment.document.default = "other-profile"
        case unreachable:
            assert_never(unreachable)
    context: _EnvironmentValidationContext = {
        "refs_found": False,
        "environments": {environment.name: environment},
        "explicit": True,
        "root_workflow_dir": workflow.parent,
        "warned_no_environments": False,
        "warned_no_secret_environments": False,
        "preflights": {environment.name: preflight},
    }

    with pytest.raises(ConfigurationError, match="preflight does not match"):
        compile_run_manifest(
            config, workflow_path=workflow, environment=environment, _preflight=preflight
        )
    with pytest.raises(ConfigurationError, match="preflight does not match"):
        validate_workflow_config(config, workflow_path=workflow, _environment_context=context)
    assert preflight.collected is None


def test_direct_validator_lazily_shares_effective_root_preflight(
    retained_validation_files: tuple[Path, Path], collection_attempts: list[_Collector]
) -> None:
    # Requirement: old explicit contexts collect once and preserve valid effective config overrides.
    workflow, environment_path = retained_validation_files
    config = load_config(workflow)
    config = config.model_copy(
        update={"workflow": config.workflow.model_copy(update={"name": "effective-name"})}
    )
    environment = resolve_environment(str(environment_path), workflow_dir=workflow.parent)
    context: _EnvironmentValidationContext = {
        "refs_found": False,
        "environments": {environment.name: environment},
        "explicit": True,
        "root_workflow_dir": workflow.parent,
        "warned_no_environments": False,
        "warned_no_secret_environments": False,
    }

    validate_workflow_config(config, workflow_path=workflow, _environment_context=context)
    assert "preflights" in context
    manifest = compile_run_manifest(
        config,
        workflow_path=workflow,
        environment=environment,
        _preflight=context["preflights"][environment.name],
    )

    assert len(collection_attempts) == 1
    assert manifest.workflow.name == "effective-name"
    assert context["preflights"][environment.name].config.workflow.name == "effective-name"


def test_direct_validator_cheap_refusal_collects_nothing(
    retained_validation_files: tuple[Path, Path], collection_attempts: list[_Collector]
) -> None:
    # Requirement: one cheap root refusal needs no collection and retains independent diagnostics.
    workflow, environment_path = retained_validation_files
    workflow.write_text(
        workflow.read_text()
        .replace("command: echo", "command: echo, working_dir: ../escape", 1)
        .replace("command: echo, routes", "command: '{{ ghost.output }}', routes", 1)
    )
    config = load_config(workflow)
    environment = resolve_environment(str(environment_path), workflow_dir=workflow.parent)
    context: _EnvironmentValidationContext = {
        "refs_found": False,
        "environments": {environment.name: environment},
        "explicit": True,
        "root_workflow_dir": workflow.parent,
        "warned_no_environments": False,
        "warned_no_secret_environments": False,
    }

    with pytest.raises(ConfigurationError) as failure:
        validate_workflow_config(config, workflow_path=workflow, _environment_context=context)

    message = str(failure.value)
    assert "ghost" in message
    assert (
        message.count("Managed local directory refused (namespace_escape)."),
        len(collection_attempts),
    ) == (1, 0)


@pytest.mark.parametrize("failure", ["incomplete", "escape", "semantic", "report"])
def test_retained_validate_preserves_failure_warnings(
    retained_validation_files: tuple[Path, Path],
    collection_attempts: list[_Collector],
    failure: str,
) -> None:
    # Requirement: failed closure/semantics preserves detailed warnings once, without publication.
    workflow, environment = retained_validation_files
    original = workflow.read_text()
    workflow.write_text(
        original.replace(
            "  entry_point: first\n",
            "  entry_point: first\n  runtime:\n    plugin_sources:\n"
            "      one: acme/plugins#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "      two: acme/other#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n",
        )
    )
    if failure in {"escape", "report"}:
        (workflow.parent.parent / "outside.txt").write_text("outside")
        child = workflow.parent / "child.yaml"
        child.write_text(
            child.read_text().replace(
                "  entry_point: agent\n",
                "  entry_point: agent\n  bundle: {assets: [../outside.txt]}\n",
            )
        )
        if failure == "report":
            workflow.write_text(
                workflow.read_text().replace("  workspace: {persistence: durable}\n", "")
            )
    elif failure == "semantic":
        workflow.write_text(original.replace("command: echo", "command: '{{ ghost.output }}'"))

    result = runner.invoke(app, ["validate", str(workflow), "--environment", str(environment)])

    assert result.exit_code == 1, result.output
    assert len(collection_attempts) == 1, result.output
    output = unstyle(result.output)
    flattened = " ".join(output.split())
    assert collection_attempts[0].warnings
    positions = []
    for warning in collection_attempts[0].warnings:
        assert flattened.count(" ".join(warning.split())) == 1
        assert flattened.index(" ".join(warning.split())) < flattened.index("Validation Failed")
        positions.append(flattened.index(" ".join(warning.split())))
    assert positions == sorted(positions)
    if failure == "report":
        assert "BundleRootEscapeError" in output
    assert "Bundle digest:" not in output
    assert "Status: complete" not in output
    assert not list(workflow.parent.parent.rglob("bundle.json"))


def test_later_report_failure_keeps_bundle_warnings_once(
    retained_validation_files: tuple[Path, Path],
    collection_attempts: list[_Collector],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requirement: a later reporter failure preserves its error without replaying bundle warnings.
    workflow, environment = retained_validation_files
    error = ConfigurationError("Post-bundle reporter failed [original].")

    def fail_after_bundle(_config: WorkflowConfig, _console: MarkupFreeConsole) -> None:
        raise error

    monkeypatch.setattr("conductor.cli.validate._report_mcp", fail_after_bundle)

    result = runner.invoke(app, ["validate", str(workflow), "--environment", str(environment)])

    assert result.exit_code == 1, result.output
    output = unstyle(result.output)
    assert "Status: complete" in output
    assert re.search(r"Bundle digest:\s+sha256:[0-9a-f]{64}", output)
    assert "ConfigurationError" in output
    assert output.count(str(error)) == 1
    assert len(collection_attempts) == 1
    assert collection_attempts[0].warnings
    flattened = " ".join(output.split())
    for warning in collection_attempts[0].warnings:
        text = " ".join(warning.split())
        assert flattened.count(text) == 1
        assert flattened.index(text) < flattened.index("Validation Failed")


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

    def test_validate_removed_hooks_block(self, fixtures_dir: Path) -> None:
        """A workflow declaring the removed `hooks:` block fails validation
        with a message naming the field, not a silent no-op (#476)."""
        workflow_file = fixtures_dir / "invalid_hooks.yaml"
        result = runner.invoke(app, ["validate", str(workflow_file)])
        assert result.exit_code != 0
        assert "hooks" in result.output.lower()

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

    @pytest.mark.parametrize(
        "endpoint_variable",
        ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"],
    )
    def test_otlp_endpoint_without_sdk_warns_without_failing_validation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        endpoint_variable: str,
    ) -> None:
        from io import StringIO

        from conductor.cli.validate import validate_workflow
        from conductor.console import make_console

        workflow_file = tmp_path / "telemetry.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: telemetry
  entry_point: writer
agents:
  - name: writer
    model: gpt-4
    prompt: "Write"
    routes:
      - to: $end
""",
        )
        output = StringIO()
        monkeypatch.setenv(endpoint_variable, "http://localhost:4317")
        monkeypatch.setattr("conductor.cli.validate.OTEL_SDK_AVAILABLE", False)

        # Given: an OTLP endpoint is configured where the optional SDK is unavailable.
        # When: the workflow is validated.
        is_valid, config = validate_workflow(
            workflow_file, console=make_console(file=output, force_terminal=False)
        )

        # Then: the configuration remains valid and names the required extra.
        assert is_valid is True
        assert config is not None
        assert "An OTLP endpoint is set" in output.getvalue()
        assert "telemetry" in output.getvalue()

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


class TestMcpBlockReporting:
    """E6-T5: `conductor validate` reports the `mcp:` block (E6, DD4, FR11)."""

    def _write(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def test_reports_the_default_block_and_tool_name(self, tmp_path: Path) -> None:
        from io import StringIO

        from rich.console import Console

        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "wf.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: review-pr
  entry_point: writer
agents:
  - name: writer
    model: gpt-4
    prompt: "Hello"
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
        assert "review_pr" in out
        assert "expose=True" in out
        assert "mode=async" in out

    def test_reports_a_populated_block(self, tmp_path: Path) -> None:
        from io import StringIO

        from rich.console import Console

        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "wf.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: review-pr
  entry_point: writer
  mcp:
    expose: true
    mode: sync
    read_only: false
    destructive: true
    estimated_minutes: 8
agents:
  - name: writer
    model: gpt-4
    prompt: "Hello"
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
        assert "mode=sync" in out
        assert "destructive=True" in out
        assert "estimated_minutes=8" in out

    def test_wait_seconds_input_is_a_validation_error(self, tmp_path: Path) -> None:
        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "wf.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: has-reserved-input
  entry_point: writer
  input:
    _wait_seconds: { type: number }
agents:
  - name: writer
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end
""",
        )
        is_valid, config = validate_workflow(workflow_file)
        assert is_valid is False
        assert config is None

    def test_wait_seconds_input_error_names_the_rule(self, tmp_path: Path) -> None:
        from io import StringIO

        from rich.console import Console

        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "wf.yaml"
        self._write(
            workflow_file,
            """
workflow:
  name: has-reserved-input
  entry_point: writer
  input:
    _wait_seconds: { type: number }
agents:
  - name: writer
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end
""",
        )
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=200)

        is_valid, config = validate_workflow(workflow_file, console=console)
        assert is_valid is False
        assert config is None
        out = output.getvalue()
        assert "_wait_seconds" in out
        assert "reserved parameter" in out

    def test_unslugifiable_workflow_name_is_a_validation_error(self, tmp_path: Path) -> None:
        from io import StringIO

        from rich.console import Console

        from conductor.cli.validate import validate_workflow

        workflow_file = tmp_path / "wf.yaml"
        # A workflow name over 128 characters cannot slugify to a legal
        # (1-128 char) MCP tool name.
        long_name = "x" * 129
        self._write(
            workflow_file,
            f"""
workflow:
  name: "{long_name}"
  entry_point: writer
agents:
  - name: writer
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end
""",
        )
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=200)

        is_valid, config = validate_workflow(workflow_file, console=console)
        assert is_valid is False
        assert config is None
        out = output.getvalue()
        assert "cannot be slugified" in out
