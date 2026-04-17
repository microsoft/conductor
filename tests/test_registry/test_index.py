"""Tests for registry index loading and parsing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ruamel.yaml import YAML

from conductor.registry.config import RegistryEntry, RegistryType
from conductor.registry.errors import RegistryError
from conductor.registry.index import (
    RegistryIndex,
    WorkflowInfo,
    get_workflow_info,
    load_index,
    resolve_latest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_INDEX: dict = {
    "workflows": {
        "qa-bot": {
            "description": "Simple Q&A workflow",
            "path": "workflows/qa-bot.yaml",
            "versions": ["1.0.0", "1.1.0", "2.0.0"],
        },
        "code-review": {
            "description": "Multi-agent code review",
            "path": "workflows/code-review.yaml",
            "versions": ["0.3.0"],
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
        assert idx.workflows["qa-bot"].versions == ["1.0.0", "1.1.0", "2.0.0"]

    def test_load_json_fallback(self, tmp_path: Path) -> None:
        """index.json is loaded when index.yaml does not exist."""
        _write_json(tmp_path / "index.json", _SAMPLE_INDEX)
        idx = load_index(_path_entry(str(tmp_path)))

        assert "qa-bot" in idx.workflows
        assert idx.workflows["code-review"].versions == ["0.3.0"]

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

    @patch("conductor.registry.index.httpx.get")
    def test_fetch_yaml_main(self, mock_get: MagicMock) -> None:
        """Fetches index.yaml from main branch."""
        yaml = YAML(typ="safe")
        from io import StringIO

        stream = StringIO()
        yaml.dump(_SAMPLE_INDEX, stream)
        yaml_text = stream.getvalue()

        mock_get.return_value = _make_response(200, yaml_text)

        idx = load_index(_github_entry("myorg/myrepo"))
        assert "qa-bot" in idx.workflows

        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        assert "myorg/myrepo/main/index.yaml" in call_url

    @patch("conductor.registry.index.httpx.get")
    def test_fallback_to_master(self, mock_get: MagicMock) -> None:
        """Falls back to master branch when main returns 404."""
        yaml = YAML(typ="safe")
        from io import StringIO

        stream = StringIO()
        yaml.dump(_SAMPLE_INDEX, stream)
        yaml_text = stream.getvalue()

        # main returns 404, master returns 200
        mock_get.side_effect = [
            _make_response(404),
            _make_response(200, yaml_text),
        ]

        idx = load_index(_github_entry("myorg/myrepo"))
        assert "qa-bot" in idx.workflows
        assert mock_get.call_count == 2

        # First call was main, second was master
        calls = [c[0][0] for c in mock_get.call_args_list]
        assert "main/index.yaml" in calls[0]
        assert "master/index.yaml" in calls[1]

    @patch("conductor.registry.index.httpx.get")
    def test_fallback_to_json(self, mock_get: MagicMock) -> None:
        """Falls back to index.json when index.yaml not found on any branch."""
        json_text = json.dumps(_SAMPLE_INDEX)

        # yaml on main → 404, yaml on master → 404, json on main → 200
        mock_get.side_effect = [
            _make_response(404),
            _make_response(404),
            _make_response(200, json_text),
        ]

        idx = load_index(_github_entry("myorg/myrepo"))
        assert "qa-bot" in idx.workflows
        assert mock_get.call_count == 3

        calls = [c[0][0] for c in mock_get.call_args_list]
        assert "index.json" in calls[2]

    @patch("conductor.registry.index.httpx.get")
    def test_all_404_raises(self, mock_get: MagicMock) -> None:
        """RegistryError when all fetch attempts return 404."""
        mock_get.return_value = _make_response(404)

        with pytest.raises(RegistryError, match="No index.yaml or index.json"):
            load_index(_github_entry("myorg/myrepo"))

        # yaml(main, master) + json(main, master) = 4 attempts
        assert mock_get.call_count == 4

    @patch("conductor.registry.index.httpx.get")
    def test_network_error_raises(self, mock_get: MagicMock) -> None:
        """RegistryError on network failure."""
        import httpx as _httpx

        mock_get.side_effect = _httpx.ConnectError("connection refused")

        with pytest.raises(RegistryError, match="Failed to fetch"):
            load_index(_github_entry("myorg/myrepo"))


# ---------------------------------------------------------------------------
# Tests: resolve_latest
# ---------------------------------------------------------------------------


class TestResolveLatest:
    """Tests for resolve_latest."""

    def test_returns_last_version(self) -> None:
        """Returns the last version in the list."""
        idx = RegistryIndex(
            workflows={
                "wf": WorkflowInfo(path="w.yaml", versions=["1.0.0", "1.1.0", "2.0.0"]),
            }
        )
        assert resolve_latest(idx, "wf") == "2.0.0"

    def test_single_version(self) -> None:
        """Works with a single version."""
        idx = RegistryIndex(
            workflows={
                "wf": WorkflowInfo(path="w.yaml", versions=["0.1.0"]),
            }
        )
        assert resolve_latest(idx, "wf") == "0.1.0"

    def test_no_versions_raises(self) -> None:
        """RegistryError when the workflow has no versions."""
        idx = RegistryIndex(
            workflows={
                "wf": WorkflowInfo(path="w.yaml", versions=[]),
            }
        )
        with pytest.raises(RegistryError, match="no versions"):
            resolve_latest(idx, "wf")

    def test_workflow_not_found_raises(self) -> None:
        """RegistryError when the workflow doesn't exist."""
        idx = RegistryIndex(workflows={})
        with pytest.raises(RegistryError, match="not found"):
            resolve_latest(idx, "nope")


# ---------------------------------------------------------------------------
# Tests: get_workflow_info
# ---------------------------------------------------------------------------


class TestGetWorkflowInfo:
    """Tests for get_workflow_info."""

    def test_found(self) -> None:
        """Returns WorkflowInfo for an existing workflow."""
        info = WorkflowInfo(description="My workflow", path="workflows/my.yaml", versions=["1.0.0"])
        idx = RegistryIndex(workflows={"my-wf": info})

        result = get_workflow_info(idx, "my-wf")
        assert result.description == "My workflow"
        assert result.path == "workflows/my.yaml"
        assert result.versions == ["1.0.0"]

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
