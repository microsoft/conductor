"""Tests for registry index loading and parsing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ruamel.yaml import YAML

from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError, RegistryNotFoundError
from conductor.registry.index import (
    RegistryIndex,
    WorkflowInfo,
    get_workflow_info,
    load_index,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_INDEX: dict = {
    "workflows": {
        "qa-bot": {
            "description": "Simple Q&A workflow",
            "path": "workflows/qa-bot.yaml",
        },
        "code-review": {
            "description": "Multi-agent code review",
            "path": "workflows/code-review.yaml",
        },
    }
}


def _write_yaml(path: Path, data: dict) -> None:
    yaml = YAML(typ="safe")
    with open(path, "w") as f:
        yaml.dump(data, f)


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _path_entry(source: str) -> RegistryEntry:
    return RegistryEntry(type=RegistryType.path, source=source)


def _github_entry(source: str = "owner/repo") -> RegistryEntry:
    return RegistryEntry(type=RegistryType.github, source=source)


def _make_response(status_code: int = 200, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# Tests: load_index — local path registries
# ---------------------------------------------------------------------------


class TestLoadPathIndex:
    """Tests for loading index from local path registries."""

    def test_load_yaml(self, tmp_path: Path) -> None:
        """index.yaml is loaded and parsed correctly."""
        _write_yaml(tmp_path / "index.yaml", _SAMPLE_INDEX)
        idx = load_index(_path_entry(str(tmp_path)))

        assert "qa-bot" in idx.workflows
        assert "code-review" in idx.workflows
        assert idx.workflows["qa-bot"].description == "Simple Q&A workflow"
        assert idx.workflows["qa-bot"].path == "workflows/qa-bot.yaml"

    def test_load_json_fallback(self, tmp_path: Path) -> None:
        """index.json is loaded when index.yaml does not exist."""
        _write_json(tmp_path / "index.json", _SAMPLE_INDEX)
        idx = load_index(_path_entry(str(tmp_path)))

        assert "qa-bot" in idx.workflows
        assert idx.workflows["code-review"].path == "workflows/code-review.yaml"

    def test_yaml_preferred_over_json(self, tmp_path: Path) -> None:
        """index.yaml takes priority when both files exist."""
        yaml_data = {
            "workflows": {
                "from-yaml": {
                    "description": "from yaml",
                    "path": "workflows/yaml.yaml",
                }
            }
        }
        json_data = {
            "workflows": {
                "from-json": {
                    "description": "from json",
                    "path": "workflows/json.yaml",
                }
            }
        }
        _write_yaml(tmp_path / "index.yaml", yaml_data)
        _write_json(tmp_path / "index.json", json_data)

        idx = load_index(_path_entry(str(tmp_path)))
        assert "from-yaml" in idx.workflows
        assert "from-json" not in idx.workflows

    def test_missing_index_raises(self, tmp_path: Path) -> None:
        """RegistryError raised when neither index file exists."""
        with pytest.raises(RegistryError, match="No index.yaml or index.json"):
            load_index(_path_entry(str(tmp_path)))

    def test_nonexistent_directory_raises(self, tmp_path: Path) -> None:
        """RegistryError raised when the source dir does not exist."""
        with pytest.raises(RegistryError, match="does not exist"):
            load_index(_path_entry(str(tmp_path / "nope")))

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        """RegistryError raised for invalid YAML content."""
        (tmp_path / "index.yaml").write_text("a: [unterminated", encoding="utf-8")
        with pytest.raises(RegistryError, match="Failed to parse"):
            load_index(_path_entry(str(tmp_path)))

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        """RegistryError raised for invalid JSON content."""
        (tmp_path / "index.json").write_text("{bad json", encoding="utf-8")
        with pytest.raises(RegistryError, match="Failed to parse"):
            load_index(_path_entry(str(tmp_path)))

    def test_malformed_schema_raises(self, tmp_path: Path) -> None:
        """RegistryError raised when data doesn't match expected schema."""
        # workflows entries missing required 'path' field
        bad_data = {"workflows": {"broken": {"description": "no path"}}}
        _write_yaml(tmp_path / "index.yaml", bad_data)
        with pytest.raises(RegistryError, match="Malformed"):
            load_index(_path_entry(str(tmp_path)))

    def test_non_mapping_yaml_raises(self, tmp_path: Path) -> None:
        """RegistryError raised when YAML top level is not a mapping."""
        (tmp_path / "index.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(RegistryError, match="expected a mapping"):
            load_index(_path_entry(str(tmp_path)))

    def test_empty_workflows(self, tmp_path: Path) -> None:
        """An index with no workflows is valid."""
        _write_yaml(tmp_path / "index.yaml", {"workflows": {}})
        idx = load_index(_path_entry(str(tmp_path)))
        assert idx.workflows == {}


# ---------------------------------------------------------------------------
# Tests: load_index — GitHub registries
# ---------------------------------------------------------------------------


class TestLoadGitHubIndex:
    """Tests for loading index from GitHub registries."""

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="abc123def456")
    @patch("conductor.registry.github.get_default_branch", return_value="main")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_fetch_yaml_default_branch(
        self,
        _mock_parse: MagicMock,
        mock_default_branch: MagicMock,
        mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """Without a ref, queries the default branch and resolves to a SHA."""
        yaml = YAML(typ="safe")
        from io import StringIO

        stream = StringIO()
        yaml.dump(_SAMPLE_INDEX, stream)
        yaml_text = stream.getvalue()

        mock_fetch.return_value = yaml_text

        idx = load_index(_github_entry("myorg/myrepo"))
        assert "qa-bot" in idx.workflows

        mock_default_branch.assert_called_once_with("myorg", "myrepo")
        mock_resolve.assert_called_once_with("myorg", "myrepo", "main")
        mock_fetch.assert_called_once_with("myorg", "myrepo", "index.yaml", ref="abc123def456")

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="deadbeef00000")
    @patch("conductor.registry.github.get_default_branch", return_value="trunk")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_latest_ref_uses_default_branch(
        self,
        _mock_parse: MagicMock,
        mock_default_branch: MagicMock,
        mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """Passing ref='latest' is equivalent to no ref — uses default branch."""
        json_text = json.dumps(_SAMPLE_INDEX)
        # yaml fails, json succeeds
        mock_fetch.side_effect = [RegistryNotFoundError("not found"), json_text]

        idx = load_index(_github_entry("myorg/myrepo"), ref="latest")
        assert "qa-bot" in idx.workflows

        mock_default_branch.assert_called_once_with("myorg", "myrepo")
        mock_resolve.assert_called_once_with("myorg", "myrepo", "trunk")

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="tagsha9876543")
    @patch("conductor.registry.github.get_default_branch")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_explicit_ref_passes_through(
        self,
        _mock_parse: MagicMock,
        mock_default_branch: MagicMock,
        mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """Explicit ref is resolved directly without consulting default branch."""
        yaml = YAML(typ="safe")
        from io import StringIO

        stream = StringIO()
        yaml.dump(_SAMPLE_INDEX, stream)
        mock_fetch.return_value = stream.getvalue()

        idx = load_index(_github_entry("myorg/myrepo"), ref="v1.2.3")
        assert "qa-bot" in idx.workflows

        # Default branch should NOT be queried for an explicit ref
        mock_default_branch.assert_not_called()
        mock_resolve.assert_called_once_with("myorg", "myrepo", "v1.2.3")
        # Resolved SHA — not the original ref — must be passed to fetch_file_text
        mock_fetch.assert_called_once_with("myorg", "myrepo", "index.yaml", ref="tagsha9876543")

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="sha1234567890")
    @patch("conductor.registry.github.get_default_branch", return_value="main")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_fallback_to_json(
        self,
        _mock_parse: MagicMock,
        _mock_default_branch: MagicMock,
        _mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """Falls back to index.json when index.yaml is not found at the SHA."""
        json_text = json.dumps(_SAMPLE_INDEX)
        mock_fetch.side_effect = [RegistryNotFoundError("not found"), json_text]

        idx = load_index(_github_entry("myorg/myrepo"))
        assert "qa-bot" in idx.workflows
        assert mock_fetch.call_count == 2
        # Both attempts use the resolved SHA
        for call in mock_fetch.call_args_list:
            assert call.kwargs["ref"] == "sha1234567890"

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="sha1234567890")
    @patch("conductor.registry.github.get_default_branch", return_value="main")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_all_404_raises(
        self,
        _mock_parse: MagicMock,
        _mock_default_branch: MagicMock,
        _mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """RegistryNotFoundError when neither index file is found at the SHA."""
        mock_fetch.side_effect = RegistryNotFoundError("not found")

        with pytest.raises(RegistryNotFoundError, match="No index.yaml or index.json"):
            load_index(_github_entry("myorg/myrepo"))

        # Only one attempt per filename — no branch fallback any more
        assert mock_fetch.call_count == 2

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="sha1234567890")
    @patch("conductor.registry.github.get_default_branch", return_value="main")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_network_error_raises(
        self,
        _mock_parse: MagicMock,
        _mock_default_branch: MagicMock,
        _mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """RegistryError on network failure propagates immediately, not collapsed to 'no index'."""
        mock_fetch.side_effect = RegistryError("Failed to fetch: connection refused")

        with pytest.raises(RegistryError, match="connection refused"):
            load_index(_github_entry("myorg/myrepo"))

    @patch("conductor.registry.github.resolve_ref_to_sha")
    @patch("conductor.registry.github.get_default_branch")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_resolve_ref_failure_propagates(
        self,
        _mock_parse: MagicMock,
        _mock_default_branch: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """If ref cannot be resolved to a SHA, the error propagates."""
        mock_resolve.side_effect = RegistryError("ref not found")

        with pytest.raises(RegistryError, match="ref not found"):
            load_index(_github_entry("myorg/myrepo"), ref="nonexistent")

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="sha1234567890")
    @patch("conductor.registry.github.get_default_branch", return_value="main")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_load_index_propagates_auth_error(
        self,
        _mock_parse: MagicMock,
        _mock_default_branch: MagicMock,
        _mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """A non-404 RegistryError (e.g. HTTP 403 auth/rate-limit) on the first
        filename propagates immediately rather than being swallowed and reported
        as 'no index file found'."""
        mock_fetch.side_effect = RegistryError(
            "Fetching myorg/myrepo/index.yaml at ref sha1234567890: HTTP 403. "
            "GitHub API rate limit may be exceeded. Try again later."
        )

        with pytest.raises(RegistryError, match="HTTP 403") as exc_info:
            load_index(_github_entry("myorg/myrepo"))

        assert "No index.yaml or index.json" not in str(exc_info.value)
        # Only the first filename should be tried — the loop must NOT fall through
        assert mock_fetch.call_count == 1

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="sha1234567890")
    @patch("conductor.registry.github.get_default_branch", return_value="main")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_load_index_propagates_rate_limit(
        self,
        _mock_parse: MagicMock,
        _mock_default_branch: MagicMock,
        _mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """An HTTP 429 rate-limit error propagates immediately."""
        mock_fetch.side_effect = RegistryError(
            "Fetching myorg/myrepo/index.yaml at ref sha1234567890: HTTP 429. "
            "GitHub API rate limit may be exceeded. Try again later."
        )

        with pytest.raises(RegistryError, match="HTTP 429") as exc_info:
            load_index(_github_entry("myorg/myrepo"))

        assert "No index.yaml or index.json" not in str(exc_info.value)
        assert mock_fetch.call_count == 1

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="sha1234567890")
    @patch("conductor.registry.github.get_default_branch", return_value="main")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_load_index_falls_back_yaml_to_json_on_404(
        self,
        _mock_parse: MagicMock,
        _mock_default_branch: MagicMock,
        _mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """A 404 (RegistryNotFoundError) on index.yaml falls through to index.json."""
        json_text = json.dumps(_SAMPLE_INDEX)
        mock_fetch.side_effect = [RegistryNotFoundError("not found"), json_text]

        idx = load_index(_github_entry("myorg/myrepo"))
        assert "qa-bot" in idx.workflows
        assert mock_fetch.call_count == 2

    @patch("conductor.registry.github.fetch_file_text")
    @patch("conductor.registry.github.resolve_ref_to_sha", return_value="sha1234567890")
    @patch("conductor.registry.github.get_default_branch", return_value="main")
    @patch("conductor.registry.github.parse_github_source", return_value=("myorg", "myrepo"))
    def test_load_index_404_for_both_raises_not_found(
        self,
        _mock_parse: MagicMock,
        _mock_default_branch: MagicMock,
        _mock_resolve: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """When both yaml and json 404, the final error is a RegistryNotFoundError."""
        mock_fetch.side_effect = RegistryNotFoundError("not found")

        with pytest.raises(RegistryNotFoundError, match="No index.yaml or index.json"):
            load_index(_github_entry("myorg/myrepo"))

        assert mock_fetch.call_count == 2


# ---------------------------------------------------------------------------
# Tests: legacy schema compatibility
# ---------------------------------------------------------------------------


class TestLegacySchema:
    """Tests for back-compat with older index schemas."""

    def test_index_with_legacy_versions_field_is_ignored(self) -> None:
        """Indexes that still include a legacy 'versions' field parse without error."""
        legacy = {
            "workflows": {
                "wf": {
                    "description": "legacy entry",
                    "path": "workflows/wf.yaml",
                    "versions": ["1.0.0"],
                }
            }
        }
        idx = RegistryIndex.model_validate(legacy)
        assert "wf" in idx.workflows
        assert idx.workflows["wf"].path == "workflows/wf.yaml"
        assert not hasattr(idx.workflows["wf"], "versions")


# ---------------------------------------------------------------------------
# Tests: get_workflow_info
# ---------------------------------------------------------------------------


class TestGetWorkflowInfo:
    """Tests for get_workflow_info."""

    def test_found(self) -> None:
        """Returns WorkflowInfo for an existing workflow."""
        info = WorkflowInfo(description="My workflow", path="workflows/my.yaml")
        idx = RegistryIndex(workflows={"my-wf": info})

        result = get_workflow_info(idx, "my-wf")
        assert result.description == "My workflow"
        assert result.path == "workflows/my.yaml"

    def test_not_found_raises(self) -> None:
        """RegistryError when the workflow is not in the index."""
        idx = RegistryIndex(
            workflows={
                "alpha": WorkflowInfo(path="a.yaml"),
                "beta": WorkflowInfo(path="b.yaml"),
            }
        )
        with pytest.raises(RegistryError, match="not found") as exc_info:
            get_workflow_info(idx, "gamma")
        # Suggestion lists available workflows
        assert "alpha" in str(exc_info.value.suggestion)
        assert "beta" in str(exc_info.value.suggestion)
