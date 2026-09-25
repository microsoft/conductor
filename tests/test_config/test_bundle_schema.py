"""Tests for the ``workflow.bundle:`` schema block.

Covers:
- ``BundleConfig`` model defaults and field preservation.
- Full block parsing (assets + additional_roots) with glob strings preserved verbatim.
- Absent ``bundle:`` block on ``WorkflowDef`` evaluates to ``None``.
- ``assets`` validation: relative paths only; absolute paths and '~' prefixes are rejected.
- ``additional_roots`` validation: accepts relative, absolute, and '~' path strings.
- Non-empty / whitespace-only validation for both ``assets`` and ``additional_roots``.
- ``extra="forbid"`` rejection of unknown keys.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conductor.config.schema import BundleConfig, WorkflowDef


class TestBundleConfigDefaultsAndParsing:
    """Requirement: BundleConfig defaults to empty lists; WorkflowDef.bundle defaults to None."""

    def test_defaults(self) -> None:
        # Requirement: BundleConfig default instance has empty assets and additional_roots lists.
        config = BundleConfig()
        assert config.assets == []
        assert config.additional_roots == []

    def test_full_block_parses_verbatim(self) -> None:
        # Requirement: Full bundle block parses and preserves globs and paths verbatim.
        config = BundleConfig(
            assets=["scripts/*.sh", "data/**/*.json", ".github/**", "docs/README.md"],
            additional_roots=[
                "/opt/shared/libs",
                "~/team-roots",
                "relative/vendor",
                "C:\\data\\root",
            ],
        )
        assert config.assets == [
            "scripts/*.sh",
            "data/**/*.json",
            ".github/**",
            "docs/README.md",
        ]
        assert config.additional_roots == [
            "/opt/shared/libs",
            "~/team-roots",
            "relative/vendor",
            "C:\\data\\root",
        ]

    def test_absent_block_is_none_on_workflow_def(self) -> None:
        # Requirement: Absent bundle block on WorkflowDef is None (not a default instance).
        wf = WorkflowDef(name="my-workflow", entry_point="agent1")
        assert wf.bundle is None

    def test_explicit_bundle_wired_on_workflow_def(self) -> None:
        # Requirement: An explicitly supplied bundle block is accessible on WorkflowDef.bundle.
        bundle = BundleConfig(
            assets=["prompts/*.md"],
            additional_roots=["/var/roots"],
        )
        wf = WorkflowDef(name="my-workflow", entry_point="agent1", bundle=bundle)
        assert wf.bundle is not None
        assert wf.bundle.assets == ["prompts/*.md"]
        assert wf.bundle.additional_roots == ["/var/roots"]


class TestBundleConfigAssetsValidation:
    """Requirement: assets must be non-empty strings relative to workflow dir (no absolute or ~)."""

    @pytest.mark.parametrize(
        "absolute_asset",
        [
            "/etc/hosts",
            "/var/data/assets.json",
            "C:\\project\\data.txt",
            "C:/project/data.txt",
            "\\\\server\\share\\asset.txt",
            "\\absolute\\posix\\or\\win",
        ],
    )
    def test_absolute_asset_rejected(self, absolute_asset: str) -> None:
        # Requirement: Absolute paths in assets must raise ValidationError naming bundle.assets.
        with pytest.raises(ValidationError) as exc_info:
            BundleConfig(assets=[absolute_asset])
        msg = str(exc_info.value)
        assert "bundle.assets" in msg
        assert "relative path" in msg

    @pytest.mark.parametrize(
        "tilde_asset",
        [
            "~",
            "~/data.json",
            "~user/documents/file.txt",
        ],
    )
    def test_tilde_asset_rejected(self, tilde_asset: str) -> None:
        # Requirement: '~' prefixed paths in assets must raise ValidationError naming bundle.assets.
        with pytest.raises(ValidationError) as exc_info:
            BundleConfig(assets=[tilde_asset])
        msg = str(exc_info.value)
        assert "bundle.assets" in msg
        assert "relative path" in msg

    @pytest.mark.parametrize(
        "empty_asset",
        [
            "",
            "   ",
            "\t\n",
        ],
    )
    def test_empty_or_whitespace_asset_rejected(self, empty_asset: str) -> None:
        # Requirement: Empty or whitespace-only asset entries must raise ValidationError.
        with pytest.raises(ValidationError) as exc_info:
            BundleConfig(assets=[empty_asset])
        msg = str(exc_info.value)
        assert "bundle.assets" in msg
        assert "non-empty" in msg


class TestBundleConfigAdditionalRootsValidation:
    """Requirement: additional_roots accepts relative, absolute, and ~ paths, rejecting empty."""

    def test_additional_roots_accepts_absolute_and_tilde_and_relative(self) -> None:
        # Requirement: additional_roots accepts '~', POSIX/Win absolute, and relative paths.
        config = BundleConfig(
            additional_roots=[
                "~/.conductor/roots",
                "/shared/data",
                "C:\\roots\\shared",
                "relative/subdir",
                "../sibling/root",
            ]
        )
        assert len(config.additional_roots) == 5

    @pytest.mark.parametrize(
        "empty_root",
        [
            "",
            "   ",
            "\t\n",
        ],
    )
    def test_empty_or_whitespace_additional_root_rejected(self, empty_root: str) -> None:
        # Requirement: Empty or whitespace-only additional_roots entries must raise ValidationError.
        with pytest.raises(ValidationError) as exc_info:
            BundleConfig(additional_roots=[empty_root])
        msg = str(exc_info.value)
        assert "bundle.additional_roots" in msg
        assert "non-empty" in msg


class TestBundleConfigExtraForbid:
    """Requirement: extra='forbid' rejects unknown fields."""

    def test_unknown_field_rejected(self) -> None:
        # Requirement: Extra / misspelled keys on BundleConfig raise ValidationError.
        with pytest.raises(ValidationError) as exc_info:
            BundleConfig(asset=["test/*.txt"])  # type: ignore[call-arg]
        assert "Extra inputs are not permitted" in str(exc_info.value)
