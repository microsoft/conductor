"""Tests for pinning: SHA for GitHub registries, content hash for path
registries, and drift detection without mutating any live catalogue
(DD6, E7-T5, E7-T10).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.mcp.serve.pinning import (
    Pin,
    pin_content,
    pin_content_file,
    pin_github_registry,
    recheck_content_pin,
    recheck_github_pin,
)
from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from tests.test_mcp.conftest import patch_github_network_to_raise

_FAKE_SHA = "a" * 40
_FAKE_SHA2 = "b" * 40


class TestPin:
    def test_as_str_sha(self) -> None:
        pin = Pin(kind="sha", value=_FAKE_SHA)
        assert pin.as_str() == f"sha:{_FAKE_SHA}"

    def test_as_str_hash(self) -> None:
        pin = Pin(kind="hash", value="deadbeef")
        assert pin.as_str() == "hash:deadbeef"

    def test_equality_requires_matching_kind(self) -> None:
        """A SHA and a content hash are never comparable -- even with an
        identical value string, differing `kind` must not compare equal."""
        sha_pin = Pin(kind="sha", value="abc123")
        hash_pin = Pin(kind="hash", value="abc123")
        assert sha_pin != hash_pin


class TestPinContent:
    def test_deterministic_for_same_bytes(self) -> None:
        assert pin_content(b"hello") == pin_content(b"hello")

    def test_differs_for_different_bytes(self) -> None:
        assert pin_content(b"hello") != pin_content(b"world")

    def test_kind_is_hash(self) -> None:
        assert pin_content(b"hello").kind == "hash"


class TestPinContentFile:
    def test_hashes_file_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "workflow.yaml"
        path.write_bytes(b"workflow:\n  name: qa-bot\n")

        pin = pin_content_file(path)
        assert pin == pin_content(b"workflow:\n  name: qa-bot\n")

    def test_missing_file_raises_oserror(self, tmp_path: Path) -> None:
        with pytest.raises(OSError):
            pin_content_file(tmp_path / "does-not-exist.yaml")

    def test_different_content_yields_different_pin(self, tmp_path: Path) -> None:
        one = tmp_path / "one.yaml"
        two = tmp_path / "two.yaml"
        one.write_bytes(b"name: one\n")
        two.write_bytes(b"name: two\n")
        assert pin_content_file(one) != pin_content_file(two)


class TestPinGithubRegistry:
    def test_rejects_non_github_entry(self) -> None:
        entry = RegistryEntry(type=RegistryType.path, source="/tmp/wherever")
        with pytest.raises(ValueError):
            pin_github_registry("official", entry)

    @patch("conductor.mcp.serve.pinning.materialize_to_sha")
    @patch("conductor.mcp.serve.pinning.resolve_ref")
    def test_online_resolves_via_materialize_to_sha(
        self, mock_resolve_ref: object, mock_materialize: object
    ) -> None:
        mock_resolve_ref.return_value = "main"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        pin = pin_github_registry("official", entry, ref=None, allow_network=True)

        assert pin == Pin(kind="sha", value=_FAKE_SHA)

    def test_offline_resolves_via_ref_pointer(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conductor.registry.cache import _write_ref_pointer

        _write_ref_pointer("official", "main", _FAKE_SHA)
        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        pin = pin_github_registry("official", entry, ref="main", allow_network=False)

        assert pin == Pin(kind="sha", value=_FAKE_SHA)

    def test_offline_with_no_pointer_raises_typed_error(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_github_network_to_raise(monkeypatch)
        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")

        with pytest.raises(RegistryError):
            pin_github_registry("official", entry, ref="main", allow_network=False)


class TestRecheckGithubPin:
    @patch("conductor.mcp.serve.pinning.materialize_to_sha")
    @patch("conductor.mcp.serve.pinning.resolve_ref")
    def test_no_drift_when_sha_unchanged(
        self, mock_resolve_ref: object, mock_materialize: object
    ) -> None:
        mock_resolve_ref.return_value = "main"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        original = Pin(kind="sha", value=_FAKE_SHA)
        report = recheck_github_pin("official", "review-pr", entry, "main", original)

        assert report.drifted is False
        assert report.current == original
        assert report.error is None

    @patch("conductor.mcp.serve.pinning.materialize_to_sha")
    @patch("conductor.mcp.serve.pinning.resolve_ref")
    def test_drift_detected_when_sha_changed(
        self, mock_resolve_ref: object, mock_materialize: object
    ) -> None:
        mock_resolve_ref.return_value = "main"  # type: ignore[union-attr]
        mock_materialize.return_value = _FAKE_SHA2  # type: ignore[union-attr]

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        original = Pin(kind="sha", value=_FAKE_SHA)
        report = recheck_github_pin("official", "review-pr", entry, "main", original)

        assert report.drifted is True
        assert report.current == Pin(kind="sha", value=_FAKE_SHA2)
        assert report.original == original

    def test_recheck_never_mutates_original(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-checking reports drift; it must never alter the caller's
        catalogue (DD6, DD3)."""
        from conductor.registry.cache import _write_ref_pointer

        _write_ref_pointer("official", "main", _FAKE_SHA2)
        patch_github_network_to_raise(monkeypatch)

        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        original = Pin(kind="sha", value=_FAKE_SHA)
        report = recheck_github_pin(
            "official", "review-pr", entry, "main", original, allow_network=False
        )

        # The report reflects the drift...
        assert report.drifted is True
        # ...but the caller's own reference to `original` is untouched.
        assert original == Pin(kind="sha", value=_FAKE_SHA)

    def test_unresolvable_ref_reports_error_not_drift(
        self, conductor_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_github_network_to_raise(monkeypatch)
        entry = RegistryEntry(type=RegistryType.github, source="myorg/workflows")
        original = Pin(kind="sha", value=_FAKE_SHA)

        report = recheck_github_pin(
            "official", "review-pr", entry, "main", original, allow_network=False
        )

        assert report.error is not None
        assert report.current is None
        assert report.drifted is False


class TestRecheckContentPin:
    def test_no_drift_when_unchanged(self, tmp_path: Path) -> None:
        path = tmp_path / "workflow.yaml"
        path.write_bytes(b"name: qa-bot\n")
        original = pin_content_file(path)

        report = recheck_content_pin("official", "qa-bot", path, original)

        assert report.drifted is False
        assert report.current == original

    def test_drift_detected_on_content_change(self, tmp_path: Path) -> None:
        path = tmp_path / "workflow.yaml"
        path.write_bytes(b"name: qa-bot\n")
        original = pin_content_file(path)

        path.write_bytes(b"name: qa-bot\nagents: []\n")
        report = recheck_content_pin("official", "qa-bot", path, original)

        assert report.drifted is True
        assert report.current != original

    def test_missing_file_reports_error_not_drift(self, tmp_path: Path) -> None:
        path = tmp_path / "workflow.yaml"
        path.write_bytes(b"name: qa-bot\n")
        original = pin_content_file(path)
        path.unlink()

        report = recheck_content_pin("official", "qa-bot", path, original)

        assert report.error is not None
        assert report.current is None
        assert report.drifted is False
