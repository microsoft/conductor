"""Golden and invariant tests for the run bundle models.

These tests pin the model schemas (golden key sets), the digest
determinism and provenance invariance of :func:`compute_bundle_digest`,
and the failure modes of the frozen, ``extra="forbid"`` models. A schema
change must be a conscious, reviewed decision accompanied by an update to
the goldens here.
"""

from __future__ import annotations

import hashlib
from typing import Literal

import pytest
from pydantic import ValidationError

from conductor.bundle.model import (
    BundleDescriptor,
    BundleEntry,
    BundleEnvironmentLink,
    BundleManifest,
    BundleProvenance,
    GitProvenance,
    PluginProvenance,
    RegistryProvenance,
    compute_bundle_digest,
    serialize_manifest,
)
from conductor.digest import canonical_json, canonical_json_digest


def _file_entry(
    logical_path: str,
    *,
    origin_kind: Literal[
        "workflow",
        "include",
        "jinja_include",
        "subworkflow",
        "subworkflow_registry",
        "skill",
        "plugin",
        "asset",
    ] = "workflow",
    origin_detail: str = "workflow:root",
    digest: str = "sha256:" + "a" * 64,
) -> BundleEntry:
    return BundleEntry(
        logical_path=logical_path,
        kind="file",
        digest=digest,
        size=3,
        executable=False,
        link_target=None,
        origin_kind=origin_kind,
        origin_detail=origin_detail,
    )


def _manifest(
    entries: list[BundleEntry],
    skills_topology: dict[str, str] | None = None,
    plugins_topology: dict[str, str] | None = None,
) -> BundleManifest:
    return BundleManifest(
        version=1,
        bundle_digest=compute_bundle_digest(
            entries,
            skills_topology or {},
            plugins_topology or {},
        ),
        entries=tuple(entries),
        skills_topology=skills_topology or {},
        plugins_topology=plugins_topology or {},
    )


class TestGoldenKeys:
    """Schema drift on the persisted shapes must fail loudly."""

    def test_bundle_entry_keys(self) -> None:
        # Requirement: BundleEntry's serialized key set is frozen — a new
        # field changes the persisted bundle.json shape and must be a
        # reviewed golden update, not a silent drift.
        entry = _file_entry("tree/main/workflow.yaml")
        assert set(entry.model_dump(mode="json").keys()) == {
            "logical_path",
            "kind",
            "digest",
            "size",
            "executable",
            "link_target",
            "origin_kind",
            "origin_detail",
        }

    def test_bundle_manifest_keys(self) -> None:
        # Requirement: BundleManifest is the immutable CAS artifact — its
        # serialized key set (content zone only) is frozen.
        manifest = _manifest([_file_entry("tree/main/workflow.yaml")])
        assert set(manifest.model_dump(mode="json").keys()) == {
            "version",
            "bundle_digest",
            "entries",
            "skills_topology",
            "plugins_topology",
        }

    def test_bundle_descriptor_keys(self) -> None:
        # Requirement: BundleDescriptor is the ephemeral report — its key
        # set (digest + link fields + provenance) is frozen.
        descriptor = BundleDescriptor(
            bundle_digest="sha256:" + "b" * 64,
            run_manifest_digest=None,
            workflow_digest="sha256:" + "c" * 64,
            environment=None,
            provenance=BundleProvenance(
                git=None,
                registry=[],
                plugins=[],
            ),
            incomplete=[],
            warnings=[],
        )
        assert set(descriptor.model_dump(mode="json").keys()) == {
            "bundle_digest",
            "run_manifest_digest",
            "workflow_digest",
            "environment",
            "provenance",
            "incomplete",
            "warnings",
        }


class TestDigestDeterminism:
    def test_digest_is_order_invariant(self) -> None:
        # Requirement: the bundle digest covers sorted entries and sorted
        # topologies — equal content collected in a different order must
        # produce an identical digest.
        entries_a = [
            _file_entry("tree/main/b.yaml"),
            _file_entry("tree/main/a.yaml"),
        ]
        entries_b = [
            _file_entry("tree/main/a.yaml"),
            _file_entry("tree/main/b.yaml"),
        ]
        digest_a = compute_bundle_digest(
            entries_a,
            {"skill-z": "tree/skills/z/SKILL.md", "skill-a": "tree/skills/a/SKILL.md"},
            {"plug-b": "tree/plugins/b/.claude-plugin/plugin.json"},
        )
        digest_b = compute_bundle_digest(
            entries_b,
            {"skill-a": "tree/skills/a/SKILL.md", "skill-z": "tree/skills/z/SKILL.md"},
            {"plug-b": "tree/plugins/b/.claude-plugin/plugin.json"},
        )
        assert digest_a == digest_b
        assert digest_a.startswith("sha256:")

    def test_content_change_changes_digest(self) -> None:
        # Requirement: the digest is a content digest — a single changed
        # byte in one entry must change it.
        entries_a = [_file_entry("tree/main/a.yaml", digest="sha256:" + "a" * 64)]
        entries_b = [_file_entry("tree/main/a.yaml", digest="sha256:" + "d" * 64)]
        assert compute_bundle_digest(entries_a, {}, {}) != compute_bundle_digest(entries_b, {}, {})


class TestProvenanceInvariance:
    def test_git_state_does_not_change_bundle_digest(self) -> None:
        # Requirement: provenance is never a digest input — identical
        # content bundled at a different git HEAD or with a different dirty
        # list must produce the same bundle_digest.
        entries = [_file_entry("tree/main/workflow.yaml")]
        skills: dict[str, str] = {}
        plugins: dict[str, str] = {}

        descriptor_at_head = BundleDescriptor(
            bundle_digest=compute_bundle_digest(entries, skills, plugins),
            run_manifest_digest="sha256:" + "1" * 64,
            workflow_digest="sha256:" + "2" * 64,
            environment=BundleEnvironmentLink(
                name="demo", source="project", digest="sha256:" + "3" * 64
            ),
            provenance=BundleProvenance(
                git=GitProvenance(
                    head_sha="aaaa1111",
                    remote="https://example.test/repo.git",
                    dirty=["tree/main/workflow.yaml"],
                ),
                registry=[RegistryProvenance(ref="wf@reg#v1", resolved_sha="bbbb2222")],
                plugins=[
                    PluginProvenance(name="prs", flavor="copilot", origin="cache", sha="cccc3333")
                ],
            ),
            incomplete=[],
            warnings=[],
        )
        descriptor_at_other_head = descriptor_at_head.model_copy(
            update={
                "provenance": BundleProvenance(
                    git=GitProvenance(
                        head_sha="dddd4444",
                        remote="https://example.test/repo.git",
                        dirty=[],
                    ),
                    registry=[RegistryProvenance(ref="wf@reg#v1", resolved_sha="bbbb2222")],
                    plugins=[
                        PluginProvenance(name="prs", flavor="copilot", origin="installed", sha=None)
                    ],
                ),
                "warnings": ["discovery-origin skill bundled"],
            }
        )

        assert descriptor_at_other_head.bundle_digest == descriptor_at_head.bundle_digest
        assert descriptor_at_other_head.provenance != descriptor_at_head.provenance


class TestSymlinkEntry:
    def test_symlink_entry_enforces_digest_rule(self) -> None:
        # Requirement: a symlink entry's digest is sha256 of the normalized
        # link_target (UTF-8), its size is 0 and executable is False.
        target = "tree/main/targets/review.md"
        entry = BundleEntry.for_symlink(
            "tree/main/links/review.md",
            target,
            origin_kind="include",
            origin_detail="include:prompts/review.md",
        )
        assert entry.kind == "symlink"
        assert entry.digest == ("sha256:" + hashlib.sha256(target.encode("utf-8")).hexdigest())
        assert entry.size == 0
        assert entry.executable is False
        assert entry.link_target == target

    def test_file_entry_rejects_link_target(self) -> None:
        # Requirement: link_target is set exactly for symlink entries.
        with pytest.raises(ValidationError, match="must not carry a link_target"):
            BundleEntry(
                logical_path="tree/main/a.yaml",
                kind="file",
                digest="sha256:" + "a" * 64,
                size=3,
                executable=False,
                link_target="tree/main/b.yaml",
                origin_kind="workflow",
                origin_detail="workflow:root",
            )

    def test_symlink_entry_requires_link_target(self) -> None:
        # Requirement: a symlink entry without a link_target is rejected.
        with pytest.raises(ValidationError, match="requires a link_target"):
            BundleEntry(
                logical_path="tree/main/a.yaml",
                kind="symlink",
                digest="sha256:" + "a" * 64,
                size=0,
                executable=False,
                link_target=None,
                origin_kind="workflow",
                origin_detail="workflow:root",
            )


class TestFrozenAndForbid:
    def test_mutation_of_frozen_model_raises(self) -> None:
        # Requirement: bundle models are frozen — mutating a field after
        # construction must raise, never silently re-bind.
        entry = _file_entry("tree/main/a.yaml")
        with pytest.raises(ValidationError):
            entry.size = 99  # type: ignore[misc]

    def test_extra_key_rejected(self) -> None:
        # Requirement: models forbid unknown keys, so a schema drift at the
        # serialization boundary fails loudly instead of being dropped.
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BundleEntry(
                logical_path="tree/main/a.yaml",
                kind="file",
                digest="sha256:" + "a" * 64,
                size=3,
                executable=False,
                link_target=None,
                origin_kind="workflow",
                origin_detail="workflow:root",
                host_path="/etc/passwd",  # type: ignore[call-arg]
            )

    def test_absolute_logical_path_rejected(self) -> None:
        # Requirement: logical_path is POSIX-notation relative to the bundle
        # root — a host-absolute path is a machine-dependent leak and is
        # rejected by the validator.
        with pytest.raises(ValidationError, match="absolute"):
            _file_entry("/etc/passwd")


class TestLogicalPathHardening:
    """The validator rejects every traversal or absolute path spelling."""

    @pytest.mark.parametrize(
        "logical_path",
        [
            "../x",
            "a/../../x",
            "",
            "a//b",
            "a/./b",
            ".",
            "C:\\x",
            "C:/x",
            "\\\\server\\x",
            "/abs",
            "a\\b",
        ],
    )
    def test_malicious_logical_path_rejected(self, logical_path: str) -> None:
        # Requirement: logical_path is a normalized relative POSIX path — the
        # validator rejects parent traversal, empty/dot segments, backslashes
        # (POSIX-only notation), and POSIX/Windows absolute spellings.
        with pytest.raises(ValidationError, match="logical_path"):
            _file_entry(logical_path)

    def test_normal_logical_path_accepted(self) -> None:
        # Requirement: ordinary normalized relative paths keep working.
        entry = _file_entry("tree/main/x")
        assert entry.logical_path == "tree/main/x"


class TestSerializeManifest:
    def test_round_trip(self) -> None:
        # Requirement: serialize_manifest output parses back into an equal
        # model — the bytes stored as bundle.json are lossless.
        manifest = _manifest(
            [_file_entry("tree/main/workflow.yaml")],
            skills_topology={"conductor": "tree/skills/conductor/SKILL.md"},
        )
        parsed = BundleManifest.model_validate_json(serialize_manifest(manifest))
        assert parsed == manifest

    def test_byte_stable(self) -> None:
        # Requirement: serialization is deterministic — the same model
        # serializes to identical bytes on every call and every host.
        manifest = _manifest(
            [_file_entry("tree/main/b.yaml"), _file_entry("tree/main/a.yaml")],
            plugins_topology={"prs": "tree/plugins/prs/.claude-plugin/plugin.json"},
        )
        assert serialize_manifest(manifest) == serialize_manifest(manifest)


class TestCanonicalJson:
    def test_canonical_json_matches_serialize_manifest_form(self) -> None:
        # Requirement: conductor.digest.canonical_json is the single canonical
        # serializer — its output equals serialize_manifest's bytes for the
        # same payload, so the store sentinel and every digest agree.
        manifest = _manifest([_file_entry("tree/main/workflow.yaml")])
        payload = manifest.model_dump(mode="json")
        assert serialize_manifest(manifest) == canonical_json(payload)

    def test_canonical_json_digest_golden(self) -> None:
        # Requirement: canonical_json_digest of a fixed payload is
        # byte-stable — the golden value below was computed with the original
        # implementation and must survive the canonical_json extraction.
        payload = {"b": [1, 2, {"k": "v"}], "a": {"x": 1, "nested": {"z": "é中"}}}
        golden = '{"a":{"nested":{"z":"\\u00e9\\u4e2d"},"x":1},"b":[1,2,{"k":"v"}]}'
        assert canonical_json(payload) == golden
        assert canonical_json_digest(payload) == (
            "sha256:3d9b047d1912e8d0138c15ace5971013a3b50d111ff79b4102b37b65b8aa43aa"
        )
