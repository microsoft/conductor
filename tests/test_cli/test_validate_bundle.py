"""Bundle-closure reporting for ``conductor validate --environment``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import conductor.bundle.collector as collector_module
from conductor.cli.app import app

runner = CliRunner()

_SET_WORKFLOW = """\
workflow:
  name: bundle-validation
  entry_point: mark
agents:
  - name: mark
    type: set
    value: "'ok'"
    routes:
      - to: $end
output:
  result: "{{ mark.output }}"
"""


def _write_environment(root: Path) -> Path:
    environment = root / "environment.yaml"
    environment.write_text("default: local\nprofiles:\n  local:\n    backend: local\n")
    return environment


def _invoke_validate(workflow: Path, environment: Path | None = None) -> tuple[int, str]:
    arguments = ["validate", str(workflow)]
    if environment is not None:
        arguments.extend(["--environment", str(environment)])
    result = runner.invoke(app, arguments)
    return result.exit_code, result.output


def test_complete_closure_reports_digest_counts_and_roots(tmp_path: Path) -> None:
    # Requirement: a complete closure reports its digest, origin counts, size, roots, and state.
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _SET_WORKFLOW.replace(
            "  entry_point: mark\n",
            "  entry_point: mark\n  bundle:\n    assets: [data.txt]\n",
        )
    )
    (tmp_path / "data.txt").write_text("payload")

    exit_code, output = _invoke_validate(workflow, _write_environment(tmp_path))

    assert exit_code == 0
    assert "Bundle Closure" in output
    assert "Status: complete" in output.replace("\n", " ")
    assert "Bundle digest:" in output and "sha256:" in output
    assert "Origin" in output and "workflow" in output and "asset" in output
    assert "Total size:" in output and "Roots:" in output
    assert "tree/main" in output
    assert "Dirty:" in output


def test_incomplete_plugin_source_omits_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: an uncached plugin source is non-fatal and never exposes a partial digest.
    home = tmp_path / "home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        _SET_WORKFLOW.replace(
            "  entry_point: mark\n",
            "  entry_point: mark\n"
            "  runtime:\n"
            "    plugin_sources:\n"
            "      acme: acme/plugins#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        )
    )

    exit_code, output = _invoke_validate(workflow, _write_environment(tmp_path))

    assert exit_code == 0
    assert "Bundle Closure" in output
    assert "Status: incomplete" in output.replace("\n", " ")
    assert "acme" in output
    assert "Bundle digest:" not in output


def test_dynamic_include_is_validation_error(tmp_path: Path) -> None:
    # Requirement: a dynamic Jinja include makes explicit-environment validation fail.
    prompt = tmp_path / "prompt.md"
    prompt.write_text("{% include template_name %}")
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
workflow:
  name: dynamic-include
  entry_point: agent
agents:
  - name: agent
    prompt: !file prompt.md
    routes:
      - to: $end
"""
    )

    exit_code, output = _invoke_validate(workflow, _write_environment(tmp_path))

    assert exit_code == 1
    assert "BundleDynamicTemplateError" in output
    assert "Validation Failed" in output


def test_bundle_caps_error_fails_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: collector caps remain validation errors rather than incomplete closures.
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_SET_WORKFLOW)
    monkeypatch.setattr(collector_module, "MAX_BUNDLE_ENTRIES", 0)

    exit_code, output = _invoke_validate(workflow, _write_environment(tmp_path))

    assert exit_code == 1
    assert "BundleCapsError" in output
    assert "Validation Failed" in output


def test_bare_validate_byte_identical_without_environment(tmp_path: Path) -> None:
    # Requirement: bare validation adds no bundle output and performs no bundle collection I/O.
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_SET_WORKFLOW)
    before_code, before_output = _invoke_validate(workflow)

    with patch("conductor.bundle.collector.collect_bundle") as collect_spy:
        after_code, after_output = _invoke_validate(workflow)

    assert before_code == after_code == 0
    assert after_output == before_output
    assert "Bundle Closure" not in after_output
    collect_spy.assert_not_called()


def test_environment_reports_closure_without_bundle_block(tmp_path: Path) -> None:
    # Requirement: closure reporting is enabled by the environment flag, not by a bundle block.
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_SET_WORKFLOW)

    exit_code, output = _invoke_validate(workflow, _write_environment(tmp_path))

    assert exit_code == 0
    assert "Bundle Closure" in output
    assert "Status: complete" in output.replace("\n", " ")
    assert "Bundle digest:" in output
