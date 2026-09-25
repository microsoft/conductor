"""Tests for ``conductor bundle build`` — the run-bundle CI priming verb.

Covers the happy path (digest, store publication, rebuild reuse, cwd
independence), registry-reference builds against a warm cache, the typed
failure modes (dynamic Jinja include, missing declared additional root,
unknown registry), and the CLI-level help smoke.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.registry.cache import (
    CACHE_LAYOUT_VERSION,
    _readiness_marker_payload,
    _sentinel_path,
)

runner = CliRunner()

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

# Provider-free workflow: a single set step, so no provider credential or
# network access is ever needed.
_SET_WORKFLOW = """\
workflow:
  name: bundle-smoke
  entry_point: mark
agents:
  - name: mark
    type: set
    value: "42"
    routes:
      - to: $end
output:
  answer: "{{ mark.output }}"
"""

_PROMPTED_WORKFLOW = """\
workflow:
  name: bundle-prompted
  entry_point: agent
agents:
  - name: agent
    prompt: !file prompt.md
    routes:
      - to: $end
"""

_FAKE_SHA = "a" * 40


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _bundle_digest(output: str) -> str:
    """Extract the bundle digest — the first ``sha256:`` line of the report."""
    match = _DIGEST_RE.search(_flattened(output))
    assert match is not None, f"no sha256 digest in output:\n{output}"
    return match.group(0)


def _flattened(output: str) -> str:
    """Whitespace-stripped output: Rich wraps long paths across lines."""
    return "".join(output.split())


def _store_dir(digest: str) -> Path:
    from conductor.bundle.store import bundle_store_path

    return bundle_store_path(digest)


class TestBuildSuccess:
    def test_build_set_step_workflow_publishes_store(self, tmp_path: Path) -> None:
        # Requirement: a provider-free workflow builds with exit 0, prints the
        # digest and store path, and the store directory holds bundle.json.
        workflow = _write(tmp_path, "w.yaml", _SET_WORKFLOW)

        result = runner.invoke(app, ["bundle", "build", str(workflow)])

        assert result.exit_code == 0, result.output
        flat = _flattened(result.output)
        assert "Bundlebuildcomplete" in flat
        digest = _bundle_digest(result.output)
        store = _store_dir(digest)
        assert store.is_dir()
        assert (store / "bundle.json").is_file()
        assert (store / "bundle.tar.gz").is_file()
        assert _flattened(str(store)) in flat
        # Counts per origin kind and link fields are part of the report.
        assert "workflow:1" in flat
        assert "Runmanifest:" in flat
        assert "Workflow:" in flat
        assert "local/default(builtin)" in flat

    def test_rebuild_reuses_store_directory(self, tmp_path: Path) -> None:
        # Requirement: identical content produces an identical digest, and the
        # second build reports the existing store directory as reused.
        workflow = _write(tmp_path, "w.yaml", _SET_WORKFLOW)
        first = runner.invoke(app, ["bundle", "build", str(workflow)])
        assert first.exit_code == 0, first.output

        second = runner.invoke(app, ["bundle", "build", str(workflow)])

        assert second.exit_code == 0, second.output
        assert _bundle_digest(second.output) == _bundle_digest(first.output)
        assert "(reused)" in _flattened(second.output)

    def test_rebuild_of_invalid_store_dir_does_not_report_reused(self, tmp_path: Path) -> None:
        # Requirement: a store directory that exists but is invalid (its
        # bundle.json unreadable) is rebuilt, and the report must NOT claim
        # "(reused)" — the previous directory was discarded, not reused.
        workflow = _write(tmp_path, "w.yaml", _SET_WORKFLOW)
        first = runner.invoke(app, ["bundle", "build", str(workflow)])
        assert first.exit_code == 0, first.output
        store = _store_dir(_bundle_digest(first.output))
        (store / "bundle.json").write_text("not json", encoding="utf-8")

        second = runner.invoke(app, ["bundle", "build", str(workflow)])

        assert second.exit_code == 0, second.output
        assert _bundle_digest(second.output) == _bundle_digest(first.output)
        assert "(reused)" not in _flattened(second.output)
        parsed = json.loads((store / "bundle.json").read_text(encoding="utf-8"))
        assert parsed["bundle_digest"] == _bundle_digest(second.output)

    def test_build_from_different_cwd_keeps_digest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: the digest depends on the workflow content, not the
        # caller's working directory (bundles are relocatable artifacts).
        workflow = _write(tmp_path, "w.yaml", _SET_WORKFLOW)
        baseline = runner.invoke(app, ["bundle", "build", str(workflow)])
        assert baseline.exit_code == 0, baseline.output

        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        moved = runner.invoke(app, ["bundle", "build", str(workflow)])

        assert moved.exit_code == 0, moved.output
        assert _bundle_digest(moved.output) == _bundle_digest(baseline.output)

    def test_build_with_environment_document(self, tmp_path: Path) -> None:
        # Requirement: --environment accepts a path to a document and the
        # report links its identity instead of the built-in environment.
        workflow = _write(tmp_path, "w.yaml", _SET_WORKFLOW)
        env = _write(
            tmp_path,
            "demo.yaml",
            "default: default\nprofiles:\n  default:\n    backend: local\n",
        )

        result = runner.invoke(app, ["bundle", "build", str(workflow), "--environment", str(env)])

        assert result.exit_code == 0, result.output
        assert "demo(path)" in _flattened(result.output)


class TestRegistryBuild:
    def _warm_cache(self, home: Path) -> None:
        """Pre-populate the ad-hoc registry cache as if a fetch had completed."""
        base = home / "cache" / "registries"
        sha_root = base / "_adhoc" / "acme" / "flows" / _FAKE_SHA[:12]
        workflow_path = sha_root / "qa-bot.yaml"
        workflow_path.parent.mkdir(parents=True)
        workflow_path.write_text(_SET_WORKFLOW, encoding="utf-8")
        meta = base / "_adhoc" / "acme" / "flows" / "_meta" / _FAKE_SHA[:12]
        meta.mkdir(parents=True)
        (meta / "source.json").write_text(
            json.dumps(
                {
                    "cache_layout_version": CACHE_LAYOUT_VERSION,
                    "registry_type": "github",
                    "source": "acme/flows",
                    "full_sha": _FAKE_SHA,
                }
            ),
            encoding="utf-8",
        )
        (meta / "index.yaml").write_text(
            "workflows:\n  qa-bot:\n    description: ''\n    path: qa-bot.yaml\n",
            encoding="utf-8",
        )
        sentinel = _sentinel_path("_adhoc/acme/flows", _FAKE_SHA, "qa-bot")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(_readiness_marker_payload(), encoding="utf-8")

    def test_registry_ref_build_records_resolved_sha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requirement: a pinned registry reference builds offline against a
        # warm cache, and the descriptor provenance carries the resolved SHA.
        home = tmp_path / "home"
        monkeypatch.setenv("CONDUCTOR_HOME", str(home))
        self._warm_cache(home)
        # A pinned full-SHA ref resolves without the GitHub API; stand in for
        # the /commits/{sha} round trip with the faithful answer.
        monkeypatch.setattr(
            "conductor.registry.version_resolver.resolve_ref_to_sha",
            lambda owner, repo, ref: ref.lower(),
        )
        captured: dict[str, Any] = {}

        def spy_report(descriptor: Any, **kwargs: Any) -> None:
            captured["descriptor"] = descriptor

        monkeypatch.setattr("conductor.cli.bundle._print_report", spy_report)

        result = runner.invoke(app, ["bundle", "build", f"qa-bot@acme/flows#{_FAKE_SHA}"])

        assert result.exit_code == 0, result.output
        descriptor = captured["descriptor"]
        assert _store_dir(descriptor.bundle_digest).is_dir()
        provenance = descriptor.provenance.registry
        assert len(provenance) == 1
        assert provenance[0].ref == f"qa-bot@acme/flows#{_FAKE_SHA}"
        assert provenance[0].resolved_sha == _FAKE_SHA


class TestBuildFailures:
    def test_dynamic_jinja_include_fails_naming_the_file(self, tmp_path: Path) -> None:
        # Requirement: a dynamic Jinja include in a file-backed prompt is a
        # hard error (exit 1) whose message names the offending file, the
        # Jinja construct, and the line number.
        workflow = _write(tmp_path, "w.yaml", _PROMPTED_WORKFLOW)
        _write(tmp_path, "prompt.md", "line\n{% include target %}")

        result = runner.invoke(app, ["bundle", "build", str(workflow)])

        assert result.exit_code == 1
        output = result.stderr or result.output
        flat = _flattened(output)
        assert "prompt.md" in flat
        assert "include" in flat
        assert "atline2" in flat

    def test_missing_declared_additional_root_fails(self, tmp_path: Path) -> None:
        # Requirement: a declared additional_root that does not exist aborts
        # the build with exit 1 and names the missing root.
        workflow = _write(
            tmp_path,
            "w.yaml",
            _SET_WORKFLOW.replace(
                "workflow:\n  name: bundle-smoke\n",
                "workflow:\n  name: bundle-smoke\n"
                "  bundle:\n"
                "    additional_roots: [missing-root]\n",
            ),
        )

        result = runner.invoke(app, ["bundle", "build", str(workflow)])

        assert result.exit_code == 1
        output = result.stderr or result.output
        assert "missing-root" in _flattened(output)

    def test_unknown_registry_fails(self, tmp_path: Path) -> None:
        # Requirement: a registry reference naming an unconfigured registry is
        # a hard error with exit 1, not a traceback.
        result = runner.invoke(app, ["bundle", "build", f"qa-bot@nowhere#{_FAKE_SHA}"])

        assert result.exit_code == 1
        output = result.stderr or result.output
        assert "Error" in _flattened(output)


class TestHelp:
    def test_bundle_help_exits_zero(self) -> None:
        # Requirement: the group uses no_args_is_help and renders help.
        result = runner.invoke(app, ["bundle", "--help"])

        assert result.exit_code == 0
        assert "Usage" in result.output
        assert "build" in result.output

    def test_bundle_no_args_prints_help(self) -> None:
        # Requirement: invoking the group bare prints its help text.
        result = runner.invoke(app, ["bundle"])

        assert "Usage" in result.output
        assert "build" in result.output
