"""Tests for the catalogue builder: the exposure ladder, the three-tier
schema ladder, zero-network resolution (NFR1), never-silently-dropped
degraded exposure (NFR2), and the direct-vs-discovery decision
(FR2, FR3, NFR1, NFR2, DD4, DD6, DD10, E7-T6, E7-T9).
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest

from conductor.mcp.serve.catalogue import build_catalogue
from conductor.mcp.serve.options import ServeOptions
from conductor.registry.config import RegistriesConfig, RegistryEntry, RegistryType
from tests.test_mcp.conftest import (
    patch_github_network_to_raise,
    populate_github_warm_cache,
    write_path_registry,
)

_FAKE_SHA = "c" * 40

_REVIEW_PR_YAML = """\
workflow:
  name: review-pr
  description: Reviews a pull request across correctness, tests, and security.
  entry_point: worker
  input:
    pr_number:
      type: number
      required: true
      description: The PR number to review
  mcp:
    mode: async
    destructive: true
    estimated_minutes: 8
agents:
  - name: worker
    prompt: "Review PR {{ pr_number }}"
    output:
      result:
        type: string
output:
  result: "{{ worker.output.result }}"
"""


def _unexposed_yaml(name: str = "internal-helper") -> str:
    return textwrap.dedent(
        f"""\
        workflow:
          name: {name}
          description: An internal helper, not meant to be called directly.
          entry_point: worker
          mcp:
            expose: false
        agents:
          - name: worker
            prompt: "Do the thing."
            output:
              result:
                type: string
        output:
          result: "{{{{ worker.output.result }}}}"
        """
    )


def _simple_yaml(name: str) -> str:
    return textwrap.dedent(
        f"""\
        workflow:
          name: {name}
          description: A simple workflow named {name}.
          entry_point: worker
        agents:
          - name: worker
            prompt: "Do the thing."
            output:
              result:
                type: string
        output:
          result: "{{{{ worker.output.result }}}}"
        """
    )


def _registries_config(**registries: RegistryEntry) -> RegistriesConfig:
    return RegistriesConfig(registries=registries)


# ---------------------------------------------------------------------------
# Zero-network / basic build
# ---------------------------------------------------------------------------


class TestBasicBuild:
    def test_builds_from_path_registry_with_zero_network_io(self, tmp_path: Path) -> None:
        """Path registries never touch the network at all -- the simplest
        possible proof of NFR1's "zero network I/O" half."""
        entry = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        config = _registries_config(official=entry)

        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        assert len(catalogue.entries) == 1
        entry_out = catalogue.entries[0]
        assert entry_out.tool_name == "review_pr"
        assert entry_out.registry == "official"
        assert entry_out.workflow == "review-pr"
        assert entry_out.resolution_tier == "parsed"

    def test_catalogue_is_immutable_dataclass(self, tmp_path: Path) -> None:
        entry = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        with pytest.raises((AttributeError, TypeError)):
            catalogue.mode = "discovery"  # type: ignore[misc]

    def test_no_registries_no_workflow_dirs_yields_empty_catalogue(self) -> None:
        catalogue = build_catalogue(
            ServeOptions(), registries_config=RegistriesConfig(), allow_network=False
        )
        assert catalogue.entries == ()
        assert catalogue.mode == "direct"


# ---------------------------------------------------------------------------
# Exposure ladder (DD4, FR2)
# ---------------------------------------------------------------------------


class TestExposureLadder:
    def test_default_on_exposes_workflow_with_no_mcp_block(self, tmp_path: Path) -> None:
        entry = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        assert len(catalogue.entries) == 1

    def test_mcp_expose_false_hides_workflow(self, tmp_path: Path) -> None:
        entry = write_path_registry(
            tmp_path, name="official", workflows={"internal-helper": _unexposed_yaml()}
        )
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        assert catalogue.entries == ()

    def test_deny_beats_allow(self, tmp_path: Path) -> None:
        """Rung 1 (--deny) outranks rung 2 (--allow): a workflow matched
        by both must still be excluded."""
        entry = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        config = _registries_config(official=entry)
        options = ServeOptions(allow=("review-*",), deny=("review-*",))

        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert catalogue.entries == ()

    def test_allow_overrides_mcp_expose_false(self, tmp_path: Path) -> None:
        """Rung 2 (--allow) outranks rung 3 (mcp.expose): a workflow the
        author marked expose: false must still be exposed when an
        operator's --allow explicitly names it."""
        entry = write_path_registry(
            tmp_path, name="official", workflows={"internal-helper": _unexposed_yaml()}
        )
        config = _registries_config(official=entry)
        options = ServeOptions(allow=("internal-*",))

        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert len(catalogue.entries) == 1
        assert catalogue.entries[0].workflow == "internal-helper"

    def test_allow_mode_excludes_non_matching_workflows(self, tmp_path: Path) -> None:
        entry = write_path_registry(
            tmp_path,
            name="official",
            workflows={"review-pr": _REVIEW_PR_YAML, "merge-pr": _simple_yaml("merge-pr")},
        )
        config = _registries_config(official=entry)
        options = ServeOptions(allow=("review-*",))

        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert [e.workflow for e in catalogue.entries] == ["review-pr"]

    def test_registry_flag_excludes_non_candidate_registries_entirely(self, tmp_path: Path) -> None:
        """`--registry` operates one level above the ladder: a registry
        outside the selected set contributes nothing, regardless of any
        `--allow`/`--deny` that would otherwise have matched a workflow in
        it."""
        official = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        team = write_path_registry(tmp_path, name="team", workflows={"review-pr": _REVIEW_PR_YAML})
        config = _registries_config(official=official, team=team)
        options = ServeOptions(registries=("official",))

        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert [e.registry for e in catalogue.entries] == ["official"]

    def test_registry_flag_is_glob_capable(self, tmp_path: Path) -> None:
        official = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        other = write_path_registry(
            tmp_path, name="other", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        config = _registries_config(official=official, other=other)
        options = ServeOptions(registries=("off*",))

        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert [e.registry for e in catalogue.entries] == ["official"]

    def test_deny_glob_excludes_matching_workflows(self, tmp_path: Path) -> None:
        entry = write_path_registry(
            tmp_path,
            name="official",
            workflows={"review-pr": _REVIEW_PR_YAML, "merge-pr": _simple_yaml("merge-pr")},
        )
        config = _registries_config(official=entry)
        options = ServeOptions(deny=("merge-*",))

        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert [e.workflow for e in catalogue.entries] == ["review-pr"]


# ---------------------------------------------------------------------------
# NFR2: never silently dropped for an environmental reason
# ---------------------------------------------------------------------------


class TestNeverSilentlyDropped:
    def test_missing_env_var_still_exposed_with_permissive_schema(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent(
            """\
            workflow:
              name: broken-env
              description: ${SOME_VAR_THAT_IS_DEFINITELY_NOT_SET_XYZ}
              entry_point: worker
            agents:
              - name: worker
                prompt: hi
                output:
                  result:
                    type: string
            output:
              result: "{{ worker.output.result }}"
            """
        )
        entry = write_path_registry(tmp_path, name="official", workflows={"broken-env": yaml_text})
        config = _registries_config(official=entry)

        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        assert len(catalogue.entries) == 1
        exposed = catalogue.entries[0]
        assert exposed.resolution_tier == "degraded"
        assert exposed.tool.inputSchema == {
            "type": "object",
            "properties": {
                "_wait_seconds": exposed.tool.inputSchema["properties"]["_wait_seconds"],
            },
        }
        assert "could not be resolved" in exposed.tool.description

    def test_unresolvable_parent_file_tag_still_exposed(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent(
            """\
            workflow:
              name: broken-file
              entry_point: worker
              instructions:
                - !file ../outside-the-registry.md
            agents:
              - name: worker
                prompt: hi
                output:
                  result:
                    type: string
            output:
              result: "{{ worker.output.result }}"
            """
        )
        entry = write_path_registry(tmp_path, name="official", workflows={"broken-file": yaml_text})
        config = _registries_config(official=entry)

        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        assert len(catalogue.entries) == 1
        exposed = catalogue.entries[0]
        assert exposed.resolution_tier == "degraded"
        assert "could not be resolved" in exposed.tool.description

    def test_degraded_workflow_still_gets_a_pin(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent(
            """\
            workflow:
              name: broken-env
              description: ${SOME_VAR_THAT_IS_DEFINITELY_NOT_SET_XYZ}
              entry_point: worker
            agents:
              - name: worker
                prompt: hi
                output:
                  result:
                    type: string
            output:
              result: "{{ worker.output.result }}"
            """
        )
        entry = write_path_registry(tmp_path, name="official", workflows={"broken-env": yaml_text})
        config = _registries_config(official=entry)

        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        assert catalogue.entries[0].pin is not None


# ---------------------------------------------------------------------------
# Reserved `_wait_seconds` collision (FR10)
# ---------------------------------------------------------------------------


class TestReservedInputRejection:
    def test_wait_seconds_input_rejected_and_logged(self, tmp_path: Path) -> None:
        yaml_text = textwrap.dedent(
            """\
            workflow:
              name: broken-reserved
              entry_point: worker
              input:
                _wait_seconds:
                  type: number
                  required: false
            agents:
              - name: worker
                prompt: hi
                output:
                  result:
                    type: string
            output:
              result: "{{ worker.output.result }}"
            """
        )
        entry = write_path_registry(
            tmp_path, name="official", workflows={"broken-reserved": yaml_text}
        )
        config = _registries_config(official=entry)

        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        assert catalogue.entries == ()
        assert len(catalogue.rejected) == 1
        assert catalogue.rejected[0].workflow == "broken-reserved"
        assert "_wait_seconds" in catalogue.rejected[0].reason


# ---------------------------------------------------------------------------
# Cross-registry collision (DD10) -- acceptance criterion
# ---------------------------------------------------------------------------


class TestCollisionAcceptanceCriterion:
    def test_two_registries_publishing_one_slug_yield_two_qualified_names(
        self, tmp_path: Path
    ) -> None:
        official = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        team = write_path_registry(tmp_path, name="team", workflows={"review-pr": _REVIEW_PR_YAML})
        config = _registries_config(official=official, team=team)

        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        names = {e.tool_name for e in catalogue.entries}
        assert names == {"official_review_pr", "team_review_pr"}
        assert len(catalogue.collisions) == 1


# ---------------------------------------------------------------------------
# --workflow-dir
# ---------------------------------------------------------------------------


class TestWorkflowDir:
    def test_workflow_dir_exposes_yaml_files(self, tmp_path: Path) -> None:
        directory = tmp_path / "adhoc"
        directory.mkdir()
        (directory / "review-pr.yaml").write_text(_REVIEW_PR_YAML, encoding="utf-8")

        options = ServeOptions(workflow_dirs=(directory,))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )

        assert len(catalogue.entries) == 1
        assert catalogue.entries[0].tool_name == "review_pr"
        assert catalogue.entries[0].registry == f"dir:{directory.name}"

    def test_workflow_dir_ignores_non_yaml_files(self, tmp_path: Path) -> None:
        directory = tmp_path / "adhoc"
        directory.mkdir()
        (directory / "review-pr.yaml").write_text(_REVIEW_PR_YAML, encoding="utf-8")
        (directory / "README.md").write_text("not a workflow", encoding="utf-8")

        options = ServeOptions(workflow_dirs=(directory,))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )

        assert len(catalogue.entries) == 1

    def test_workflow_dir_nonexistent_directory_does_not_crash(self, tmp_path: Path) -> None:
        options = ServeOptions(workflow_dirs=(tmp_path / "does-not-exist",))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )
        assert catalogue.entries == ()


# ---------------------------------------------------------------------------
# Discovery threshold decision (FR9)
# ---------------------------------------------------------------------------


class TestDiscoveryThreshold:
    def test_direct_mode_under_the_cap(self, tmp_path: Path) -> None:
        entry = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        config = _registries_config(official=entry)
        options = ServeOptions(max_direct_tools=25)

        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert catalogue.mode == "direct"

    def test_discovery_mode_over_the_cap(self, tmp_path: Path) -> None:
        workflows = {f"workflow-{i}": _simple_yaml(f"workflow-{i}") for i in range(5)}
        entry = write_path_registry(tmp_path, name="official", workflows=workflows)
        config = _registries_config(official=entry)
        options = ServeOptions(max_direct_tools=3)

        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert catalogue.mode == "discovery"
        # E7 only decides the mode -- the entries themselves are still
        # generated regardless (E12 acts on `mode`, it does not live here).
        assert len(catalogue.entries) == 5

    def test_exactly_at_the_cap_is_direct(self, tmp_path: Path) -> None:
        workflows = {f"workflow-{i}": _simple_yaml(f"workflow-{i}") for i in range(3)}
        entry = write_path_registry(tmp_path, name="official", workflows=workflows)
        config = _registries_config(official=entry)
        options = ServeOptions(max_direct_tools=3)

        catalogue = build_catalogue(options, registries_config=config, allow_network=False)
        assert catalogue.mode == "direct"


# ---------------------------------------------------------------------------
# Schema ladder tiers + NFR1 (GitHub registry, warm cache, offline)
# ---------------------------------------------------------------------------


class TestSchemaLadderTiersOffline:
    """Exercises the three-tier ladder against a GitHub registry whose
    cache has been pre-warmed, with `registry/github.py` patched to raise
    on every call -- the load-bearing NFR1 proof for the catalogue
    builder, mirroring E5-T5's own load-bearing test for the cache layer
    it is built on.
    """

    def test_tier1_index_provided_schema(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        index_yaml = textwrap.dedent(
            """\
            workflows:
              qa-bot:
                description: Simple Q&A
                path: workflows/qa-bot.yaml
                input:
                  question:
                    type: string
                    required: true
                    description: The question to ask
                mcp:
                  mode: sync
                  read_only: true
            """
        )
        populate_github_warm_cache(
            conductor_home,
            registry_name="official",
            sha=_FAKE_SHA,
            registry_source="myorg/workflows",
            index_yaml=index_yaml,
        )
        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        assert len(catalogue.entries) == 1
        exposed = catalogue.entries[0]
        assert exposed.resolution_tier == "index"
        assert exposed.pin.kind == "sha"
        assert exposed.pin.value == _FAKE_SHA
        assert exposed.tool.inputSchema["properties"]["question"]["type"] == "string"

    def test_tier2_sha_keyed_parse_cache(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.config.schema import InputDef, McpConfig
        from conductor.registry.cache import ParsedToolInfo, save_parsed_tools

        index_yaml = textwrap.dedent(
            """\
            workflows:
              qa-bot:
                description: ""
                path: workflows/qa-bot.yaml
            """
        )
        populate_github_warm_cache(
            conductor_home,
            registry_name="official",
            sha=_FAKE_SHA,
            registry_source="myorg/workflows",
            index_yaml=index_yaml,
        )
        save_parsed_tools(
            "official",
            _FAKE_SHA,
            {
                "qa-bot": ParsedToolInfo(
                    description="Simple Q&A (from cache)",
                    input={"question": InputDef(type="string", required=True)},
                    mcp=McpConfig(mode="sync"),
                )
            },
        )
        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        assert len(catalogue.entries) == 1
        exposed = catalogue.entries[0]
        assert exposed.resolution_tier == "cache"
        assert "from cache" in exposed.tool.description

    def test_tier3_fetch_and_parse_from_mirrored_file(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No index-provided schema and no parse cache: the workflow file
        itself, already mirrored into the SHA root by a prior online
        fetch, is read and parsed -- still with zero network I/O."""
        index_yaml = textwrap.dedent(
            """\
            workflows:
              qa-bot:
                description: ""
                path: workflows/qa-bot.yaml
            """
        )
        populate_github_warm_cache(
            conductor_home,
            registry_name="official",
            sha=_FAKE_SHA,
            registry_source="myorg/workflows",
            index_yaml=index_yaml,
            workflow_files={"workflows/qa-bot.yaml": _REVIEW_PR_YAML.encode()},
        )
        # Mark the workflow as fully cached (readiness sentinel) so
        # fetch_workflow's cache-hit path is taken without a network call.
        meta_dir = conductor_home / "cache" / "registries" / "official" / "_meta" / _FAKE_SHA[:12]
        (meta_dir / "qa-bot.complete").write_text("", encoding="utf-8")

        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        assert len(catalogue.entries) == 1
        exposed = catalogue.entries[0]
        assert exposed.resolution_tier == "parsed"
        assert exposed.tool.inputSchema["properties"]["pr_number"]["type"] == "number"

    def test_zero_network_build_completes_well_under_two_seconds(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NFR1: cold-start to first `tools/list` response <= 2s with a
        warm registry cache, with zero network I/O."""
        index_yaml = textwrap.dedent(
            """\
            workflows:
              qa-bot:
                description: Simple Q&A
                path: workflows/qa-bot.yaml
                input:
                  question:
                    type: string
                    required: true
            """
        )
        populate_github_warm_cache(
            conductor_home,
            registry_name="official",
            sha=_FAKE_SHA,
            registry_source="myorg/workflows",
            index_yaml=index_yaml,
        )
        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        config = _registries_config(official=entry)

        start = time.monotonic()
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        elapsed = time.monotonic() - start

        assert len(catalogue.entries) == 1
        assert elapsed < 2.0

    def test_unresolved_floating_ref_registry_is_skipped_not_fatal(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registry that cannot be resolved offline (no warm ref pointer)
        must not abort the whole catalogue build -- it is skipped, exactly
        like any other environmental failure at the registry level."""
        patch_github_network_to_raise(monkeypatch)
        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        config = _registries_config(official=entry)

        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        assert catalogue.entries == ()

    def test_default_allow_network_path_uses_warm_cache_without_touching_network(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NFR1: the *default* production path (``allow_network=True``,
        the caller-facing default) must consult the warm cache first and
        never resolve the ref online when the cache already answers --
        not just the explicit ``allow_network=False`` path exercised
        above."""
        index_yaml = textwrap.dedent(
            """\
            workflows:
              qa-bot:
                description: Simple Q&A
                path: workflows/qa-bot.yaml
                input:
                  question:
                    type: string
                    required: true
                mcp:
                  mode: sync
            """
        )
        populate_github_warm_cache(
            conductor_home,
            registry_name="official",
            sha=_FAKE_SHA,
            registry_source="myorg/workflows",
            index_yaml=index_yaml,
        )
        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        config = _registries_config(official=entry)

        # No `allow_network=` override here -- this is the default an
        # operator gets from `conductor mcp serve`.
        catalogue = build_catalogue(ServeOptions(), registries_config=config)

        assert len(catalogue.entries) == 1
        assert catalogue.entries[0].resolution_tier == "index"

    def test_repointed_registry_source_does_not_serve_stale_cached_index(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: a warm cache populated for one repository must not
        be served for a registry name later repointed at a *different*
        repository. Without validating cached ``source.json`` metadata
        (source, SHA, type, cache layout) against the current
        ``RegistryEntry`` before accepting the cached index, the old
        repository's ``qa-bot`` workflow would keep being served under
        the ``official`` name with no warning or network refresh."""
        index_yaml = textwrap.dedent(
            """\
            workflows:
              qa-bot:
                description: ""
                path: workflows/qa-bot.yaml
            """
        )
        populate_github_warm_cache(
            conductor_home,
            registry_name="official",
            sha=_FAKE_SHA,
            registry_source="myorg/old-workflows",
            index_yaml=index_yaml,
        )
        patch_github_network_to_raise(monkeypatch)

        # The registry name "official" now points at a different
        # repository than the one the warm cache was populated for.
        entry = RegistryEntry(type=RegistryType.github, source="myorg/new-workflows")
        config = _registries_config(official=entry)

        # Offline: there is no network to refresh from, so the stale
        # cache must be rejected outright (registry skipped), never
        # silently served as if it were the new repository's index.
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        assert catalogue.entries == ()


# ---------------------------------------------------------------------------
# Partial tier-1 index metadata (FR2/DD4)
# ---------------------------------------------------------------------------


class TestPartialIndexMetadata:
    """An index declaring only one of `input`/`mcp` must resolve the other
    independently through the lower tiers -- never defaulting the missing
    field, and never discarding the one it did declare."""

    def test_input_without_mcp_does_not_default_to_exposed(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        index_yaml = textwrap.dedent(
            """\
            workflows:
              qa-bot:
                description: Simple Q&A
                path: workflows/qa-bot.yaml
                input:
                  question:
                    type: string
                    required: true
            """
        )
        populate_github_warm_cache(
            conductor_home,
            registry_name="official",
            sha=_FAKE_SHA,
            registry_source="myorg/workflows",
            index_yaml=index_yaml,
            workflow_files={
                "workflows/qa-bot.yaml": _unexposed_yaml("qa-bot").encode(),
            },
        )
        meta_dir = conductor_home / "cache" / "registries" / "official" / "_meta" / _FAKE_SHA[:12]
        (meta_dir / "qa-bot.complete").write_text("", encoding="utf-8")
        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        # The workflow file itself declares `mcp.expose: false`; the index
        # only declares `input`, so `mcp` must fall through to the parsed
        # file rather than silently defaulting to exposed.
        assert catalogue.entries == ()

    def test_mcp_without_input_is_not_discarded(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        index_yaml = textwrap.dedent(
            """\
            workflows:
              qa-bot:
                description: Simple Q&A
                path: workflows/qa-bot.yaml
                mcp:
                  read_only: true
            """
        )
        populate_github_warm_cache(
            conductor_home,
            registry_name="official",
            sha=_FAKE_SHA,
            registry_source="myorg/workflows",
            index_yaml=index_yaml,
            workflow_files={"workflows/qa-bot.yaml": _REVIEW_PR_YAML.encode()},
        )
        meta_dir = conductor_home / "cache" / "registries" / "official" / "_meta" / _FAKE_SHA[:12]
        (meta_dir / "qa-bot.complete").write_text("", encoding="utf-8")
        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        assert len(catalogue.entries) == 1
        exposed = catalogue.entries[0]
        # The index-declared `mcp.read_only` must survive, and `input`
        # must come from the parsed file rather than being empty.
        assert exposed.tool.annotations.readOnlyHint is True
        assert "pr_number" in exposed.tool.inputSchema["properties"]


# ---------------------------------------------------------------------------
# Duplicate source identity across --workflow-dir entries
# ---------------------------------------------------------------------------


class TestDuplicateSourceIdentity:
    def test_two_dirs_with_same_basename_and_file_both_survive(self, tmp_path: Path) -> None:
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        adhoc_a = root_a / "adhoc"
        adhoc_b = root_b / "adhoc"
        adhoc_a.mkdir(parents=True)
        adhoc_b.mkdir(parents=True)
        (adhoc_a / "review-pr.yaml").write_text(_simple_yaml("review-pr-a"), encoding="utf-8")
        (adhoc_b / "review-pr.yaml").write_text(_simple_yaml("review-pr-b"), encoding="utf-8")

        options = ServeOptions(workflow_dirs=(adhoc_a, adhoc_b))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )

        # Both directories share a basename ("adhoc") and both expose a
        # "review-pr" file -- without a source discriminator, one
        # candidate would silently overwrite the other.
        assert len(catalogue.entries) == 2


# ---------------------------------------------------------------------------
# Names derived from WorkflowDef.name (FR3)
# ---------------------------------------------------------------------------


class TestNameDerivedFromDeclaredName:
    def test_workflow_dir_tool_name_uses_declared_name_not_filename(self, tmp_path: Path) -> None:
        directory = tmp_path / "adhoc"
        directory.mkdir()
        # Filename deliberately differs from the workflow's declared name.
        (directory / "on-disk-filename.yaml").write_text(
            _simple_yaml("declared-name"), encoding="utf-8"
        )

        options = ServeOptions(workflow_dirs=(directory,))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )

        assert len(catalogue.entries) == 1
        # Matches `conductor validate`'s slugify_workflow_name("declared-name").
        assert catalogue.entries[0].tool_name == "declared_name"

    def test_tier2_cache_hit_preserves_declared_name_across_builds(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for FR3 across cache tiers: a first build resolves
        the workflow via tier 3 (fetch-and-parse) and publishes its
        declared name ``review-pr`` even though the registry index key is
        ``qa-bot``, and persists that name into the SHA-keyed parse cache
        (``ParsedToolInfo.name``). A second, independent build must hit
        the tier-2 cache and publish the *same* declared name -- not fall
        back to the index key ``qa-bot``, which would silently change the
        published tool name between runs.
        """
        index_yaml = textwrap.dedent(
            """\
            workflows:
              qa-bot:
                description: ""
                path: workflows/qa-bot.yaml
            """
        )
        populate_github_warm_cache(
            conductor_home,
            registry_name="official",
            sha=_FAKE_SHA,
            registry_source="myorg/workflows",
            index_yaml=index_yaml,
            workflow_files={"workflows/qa-bot.yaml": _REVIEW_PR_YAML.encode()},
        )
        meta_dir = conductor_home / "cache" / "registries" / "official" / "_meta" / _FAKE_SHA[:12]
        (meta_dir / "qa-bot.complete").write_text("", encoding="utf-8")
        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        config = _registries_config(official=entry)

        first = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        assert len(first.entries) == 1
        assert first.entries[0].resolution_tier == "parsed"
        assert first.entries[0].tool_name == "review_pr"

        second = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)
        assert len(second.entries) == 1
        assert second.entries[0].resolution_tier == "cache"
        assert second.entries[0].tool_name == "review_pr"

    def test_degraded_workflow_dir_entry_falls_back_to_filename_stem(self, tmp_path: Path) -> None:
        directory = tmp_path / "adhoc"
        directory.mkdir()
        (directory / "broken.yaml").write_text("not: [valid", encoding="utf-8")

        options = ServeOptions(workflow_dirs=(directory,))
        catalogue = build_catalogue(
            options, registries_config=RegistriesConfig(), allow_network=False
        )

        assert len(catalogue.entries) == 1
        assert catalogue.entries[0].tool_name == "broken"
        assert catalogue.entries[0].resolution_tier == "degraded"


# ---------------------------------------------------------------------------
# Immutability (DD3)
# ---------------------------------------------------------------------------


class TestCatalogueImmutability:
    def test_reverse_map_is_read_only(self, tmp_path: Path) -> None:
        entry = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        with pytest.raises(TypeError):
            catalogue.reverse["hacked"] = ("official", "review-pr")  # type: ignore[index]

    def test_tools_returns_defensive_copies_of_input_schema(self, tmp_path: Path) -> None:
        entry = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        first_call = catalogue.tools()
        first_call[0].inputSchema["properties"]["injected"] = {"type": "string"}

        second_call = catalogue.tools()
        assert "injected" not in second_call[0].inputSchema["properties"]
        assert "injected" not in catalogue.entries[0].tool.inputSchema["properties"]

    def test_mutating_entries_tool_does_not_corrupt_canonical_catalogue(
        self, tmp_path: Path
    ) -> None:
        """Regression: ``catalogue.entries`` must not expose the
        catalogue's canonical ``Tool`` objects directly -- mutating an
        entry's ``tool.inputSchema`` obtained this way must not corrupt
        what subsequent ``entries``/``tools()`` calls return."""
        entry = write_path_registry(
            tmp_path, name="official", workflows={"review-pr": _REVIEW_PR_YAML}
        )
        config = _registries_config(official=entry)
        catalogue = build_catalogue(ServeOptions(), registries_config=config, allow_network=False)

        catalogue.entries[0].tool.inputSchema["properties"]["injected"] = {"type": "string"}

        assert "injected" not in catalogue.entries[0].tool.inputSchema["properties"]
        assert "injected" not in catalogue.tools()[0].inputSchema["properties"]


# ---------------------------------------------------------------------------
# Startup deadline enforcement for --workflow-dir (NFR1)
# ---------------------------------------------------------------------------


class TestWorkflowDirDeadline:
    def test_exhausted_deadline_degrades_without_parsing(self, tmp_path: Path) -> None:
        directory = tmp_path / "adhoc"
        directory.mkdir()
        (directory / "review-pr.yaml").write_text(_REVIEW_PR_YAML, encoding="utf-8")

        options = ServeOptions(workflow_dirs=(directory,))
        catalogue = build_catalogue(
            options,
            registries_config=RegistriesConfig(),
            allow_network=False,
            schema_resolution_deadline=0.0,
        )

        assert len(catalogue.entries) == 1
        exposed = catalogue.entries[0]
        assert exposed.resolution_tier == "degraded"
        assert "deadline" in exposed.tool.description

    def test_deadline_bounds_a_parse_that_runs_past_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A parse that is still in flight when the remaining budget runs
        out must degrade the workflow and return promptly -- not run to
        completion and return a ``parsed`` result regardless of how long
        it took (the bug: only an *already*-expired deadline, checked
        before the call, was ever enforced)."""
        import conductor.mcp.serve.catalogue as catalogue_module

        directory = tmp_path / "adhoc"
        directory.mkdir()
        (directory / "review-pr.yaml").write_text(_REVIEW_PR_YAML, encoding="utf-8")

        real_load_config = catalogue_module.load_config

        def _slow_load_config(path: Path) -> object:
            time.sleep(0.5)
            return real_load_config(path)

        monkeypatch.setattr(catalogue_module, "load_config", _slow_load_config)

        options = ServeOptions(workflow_dirs=(directory,))
        start = time.monotonic()
        catalogue = build_catalogue(
            options,
            registries_config=RegistriesConfig(),
            allow_network=False,
            schema_resolution_deadline=0.05,
        )
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, "build_catalogue waited for the slow parse instead of bounding it"
        assert len(catalogue.entries) == 1
        exposed = catalogue.entries[0]
        assert exposed.resolution_tier == "degraded"
        assert "budget" in exposed.tool.description
