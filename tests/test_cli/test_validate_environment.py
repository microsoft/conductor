"""Execution-environment behavior for ``conductor validate``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.cli.validate import validate_workflow
from conductor.config.environment import resolve_environment
from conductor.config.loader import load_config
from conductor.console import make_console
from conductor.engine.run_manifest import compile_run_manifest
from conductor.engine.workflow import WorkflowEngine

runner = CliRunner()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _write_environment(root: Path, name: str, profiles: list[str]) -> Path:
    path = root / ".conductor" / "environments" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["default: " + profiles[0], "profiles:"]
    for profile in profiles:
        lines.extend([f"  {profile}:", "    backend: local"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_workflow(root: Path, *, profile: str | None = "shell") -> Path:
    defaults = ""
    if profile is not None:
        defaults = f"""\
  defaults:
    execution:
      profile: {profile}
"""
    path = root / "workflow.yaml"
    path.write_text(
        f"""\
workflow:
  name: profiled
  entry_point: inspect
{defaults}agents:
  - name: inspect
    type: script
    command: echo
    args: [ok]
    routes:
      - to: $end
output:
  result: "{{{{ inspect.output.stdout }}}}"
""",
        encoding="utf-8",
    )
    return path


def _invoke_validate(path: Path, *args: str) -> tuple[int, str]:
    # Wide enough that long tmp paths (macOS, xdist) are not hard-wrapped mid-name.
    result = runner.invoke(app, ["validate", str(path), *args], env={"COLUMNS": "200"})
    return result.exit_code, result.output


def test_validate_with_environment_renders_resolved_section(tmp_path: Path) -> None:
    # Requirement: explicit validation reports environment identity, exact
    # path, effective default, and every step/profile/backend resolution.
    root = _repo(tmp_path)
    environment_path = _write_environment(root, "demo", ["shell"])
    workflow_path = _write_workflow(root)

    exit_code, output = _invoke_validate(workflow_path, "--environment", "demo")

    assert exit_code == 0
    flattened = output.replace("\n", " ")
    assert "Execution Resolution" in output
    assert "Environment" in output and "demo" in output
    assert "Source" in output and "project" in output
    assert str(environment_path) in flattened
    assert "Default profile" in output and "shell" in output
    assert "inspect" in output and "shell" in output and "local" in output
    assert "non-hermetic-compatibility" in output


def test_validate_run_manifest_parity(tmp_path: Path) -> None:
    # Requirement (Oracle Q1): validate renders the same compiled manifest an
    # engine constructs for the identical workflow and resolved environment.
    root = _repo(tmp_path)
    _write_environment(root, "demo", ["shell"])
    workflow_path = _write_workflow(root)
    config = load_config(workflow_path)
    resolved = resolve_environment("demo", workflow_dir=root)
    captured = []

    def capture_manifest(*args, **kwargs):
        manifest = compile_run_manifest(*args, **kwargs)
        captured.append(manifest)
        return manifest

    console = make_console(record=True, width=200)
    with patch("conductor.engine.run_manifest.compile_run_manifest", side_effect=capture_manifest):
        ok, _ = validate_workflow(workflow_path, console=console, environment="demo")

    assert ok is True
    assert len(captured) == 1
    engine = WorkflowEngine(
        config,
        provider=None,
        workflow_path=workflow_path,
        execution_environment=resolved,
    )
    assert captured[0].model_dump(mode="json") == engine._execution_resolver.manifest.model_dump(
        mode="json"
    )


class TestBareThreeLevels:
    """Requirements for ambient profile-reference cross-checking."""

    def test_no_refs_is_silent(self, tmp_path: Path) -> None:
        # Requirement: profile-less workflows add no ambient-validation output.
        root = _repo(tmp_path)
        _write_environment(root, "demo", ["shell"])
        exit_code, output = _invoke_validate(_write_workflow(root, profile=None))
        assert exit_code == 0
        assert "execution profile" not in output.lower()

    def test_resolves_everywhere_is_silent(self, tmp_path: Path) -> None:
        # Requirement: a ref present in every discovered environment is silent.
        root = _repo(tmp_path)
        _write_environment(root, "one", ["shell"])
        _write_environment(root, "two", ["shell", "batch"])
        exit_code, output = _invoke_validate(_write_workflow(root))
        assert exit_code == 0
        assert "absent from environment" not in output

    def test_resolves_in_some_warns_with_missing_environments(self, tmp_path: Path) -> None:
        # Requirement: partial resolution warns and names only environments
        # where the authored profile is absent.
        root = _repo(tmp_path)
        _write_environment(root, "has-shell", ["shell"])
        _write_environment(root, "missing-shell", ["batch"])
        exit_code, output = _invoke_validate(_write_workflow(root))
        assert exit_code == 0
        assert "absent from environment" in output
        assert "missing-shell" in output
        assert "has-shell" not in output

    def test_resolves_nowhere_is_error(self, tmp_path: Path) -> None:
        # Requirement: a ref absent from every discovered environment fails validation.
        root = _repo(tmp_path)
        _write_environment(root, "one", ["batch"])
        _write_environment(root, "two", ["other"])
        exit_code, output = _invoke_validate(_write_workflow(root))
        assert exit_code == 1
        assert "not defined in any discovered environment" in output
        assert "one" in output and "two" in output


def test_malformed_environment_warns_in_bare_mode(tmp_path: Path) -> None:
    # Requirement: malformed ambient documents warn, do not fail, and are
    # treated as non-resolving while healthy documents still resolve the ref.
    root = _repo(tmp_path)
    _write_environment(root, "good", ["shell"])
    malformed = root / ".conductor" / "environments" / "broken.yaml"
    malformed.write_text("profiles: [not-a-map]\n", encoding="utf-8")

    exit_code, output = _invoke_validate(_write_workflow(root))

    assert exit_code == 0
    assert "Skipping malformed environment document" in output
    assert "broken.yaml" in output.replace("\n", " ")
    assert "broken" in output
    assert "absent from environment" in output


def test_malformed_environment_fatal_with_flag(tmp_path: Path) -> None:
    # Requirement: selecting that same malformed document explicitly is fatal.
    root = _repo(tmp_path)
    malformed = root / ".conductor" / "environments" / "broken.yaml"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("profiles: [not-a-map]\n", encoding="utf-8")

    exit_code, output = _invoke_validate(_write_workflow(root), "--environment", "broken")

    assert exit_code == 1
    assert "Validation Failed" in output


def test_validate_from_subdirectory_resolves_root_project_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement (PR #551 review, blocking): invoked from a subdirectory with
    # a RELATIVE workflow path, validate must still find the repository root's
    # project document — and a same-named user document must not win.
    root = _repo(tmp_path)
    _write_environment(root, "prod", ["shell"])
    home = tmp_path / "isolated-home"
    (home / "environments").mkdir(parents=True)
    (home / "environments" / "prod.yaml").write_text(
        "default: user_only\nprofiles:\n  user_only:\n    backend: local\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))

    sub = root / "sub"
    sub.mkdir()
    workflow_path = _write_workflow(root)
    workflow_path.rename(sub / "workflow.yaml")

    monkeypatch.chdir(sub)
    exit_code, output = _invoke_validate(Path("workflow.yaml"), "--environment", "prod")

    assert exit_code == 0
    assert "Execution Resolution" in output
    assert "Source" in output and "project" in output


def test_validate_empty_environment_rejected(tmp_path: Path) -> None:
    # Requirement (PR #551 review): an explicitly empty --environment must
    # fail clearly instead of silently validating against the built-in
    # environment — run and validate must agree on what empty means.
    root = _repo(tmp_path)
    workflow_path = _write_workflow(root, profile=None)

    exit_code, output = _invoke_validate(workflow_path, "--environment", "")

    assert exit_code == 1
    assert "non-empty" in output


def test_profile_less_workflow_byte_identical_output(tmp_path: Path) -> None:
    # Requirement (Metis R4): adding a malformed ambient file cannot alter a
    # profile-less workflow's output, and environment discovery is never called.
    root = _repo(tmp_path)
    workflow_path = _write_workflow(root, profile=None)
    before_code, before = _invoke_validate(workflow_path)
    malformed = root / ".conductor" / "environments" / "broken.yaml"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("profiles: [not-a-map]\n", encoding="utf-8")

    with patch("conductor.config.environment.discover_all_environments") as discover:
        after_code, after = _invoke_validate(workflow_path)

    assert before_code == after_code == 0
    discover.assert_not_called()
    assert after == before
