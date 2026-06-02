"""Tests for conductor.registry.github module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from conductor.registry.errors import RegistryError, RegistryNotFoundError
from conductor.registry.github import (
    fetch_file,
    fetch_file_text,
    get_default_branch,
    list_directory,
    list_tags,
    parse_github_source,
    resolve_ref_to_sha,
)


def _mock_response(
    status_code: int = 200,
    content: bytes = b"",
    json_data: object = None,
    links: dict[str, dict[str, str]] | None = None,
) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.content = content
    if json_data is not None:
        resp.json.return_value = json_data
    resp.links = links or {}
    return resp


# --- fetch_file ---


class TestFetchFile:
    @patch("conductor.registry.github.httpx.get")
    def test_success(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(content=b"hello world")
        result = fetch_file("owner", "repo", "path/to/file.txt", ref="v1.0")

        assert result == b"hello world"
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert "raw.githubusercontent.com/owner/repo/v1.0/path/to/file.txt" in call_args[0][0]

    @patch("conductor.registry.github.httpx.get")
    def test_404_raises_registry_error(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(status_code=404)

        with pytest.raises(RegistryError, match="not found"):
            fetch_file("owner", "repo", "missing.txt")

    @patch("conductor.registry.github.httpx.get")
    def test_404_raises_registry_not_found_error(self, mock_get: MagicMock) -> None:
        """404 specifically raises RegistryNotFoundError (subclass of RegistryError)."""
        mock_get.return_value = _mock_response(status_code=404)

        with pytest.raises(RegistryNotFoundError, match="not found"):
            fetch_file("owner", "repo", "missing.txt")

    @patch("conductor.registry.github.httpx.get")
    def test_timeout_raises_registry_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = httpx.TimeoutException("timed out")

        with pytest.raises(RegistryError, match="Timeout"):
            fetch_file("owner", "repo", "file.txt")

    @patch("conductor.registry.github.httpx.get")
    def test_403_rate_limit(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(status_code=403)

        with pytest.raises(RegistryError, match="rate limit"):
            fetch_file("owner", "repo", "file.txt")


# --- fetch_file_text ---


class TestFetchFileText:
    @patch("conductor.registry.github.httpx.get")
    def test_returns_decoded_string(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(content="café résumé".encode())
        result = fetch_file_text("owner", "repo", "readme.md")

        assert result == "café résumé"
        assert isinstance(result, str)


# --- list_tags ---


class TestListTags:
    @patch("conductor.registry.github.httpx.get")
    def test_success(self, mock_get: MagicMock) -> None:
        tags_json = [{"name": "v2.0"}, {"name": "v1.1"}, {"name": "v1.0"}]
        mock_get.return_value = _mock_response(json_data=tags_json)

        result = list_tags("owner", "repo")

        assert result == ["v2.0", "v1.1", "v1.0"]
        call_args = mock_get.call_args
        assert "api.github.com/repos/owner/repo/tags" in call_args[0][0]

    @patch("conductor.registry.github.httpx.get")
    def test_error_handling(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(status_code=404)

        with pytest.raises(RegistryError, match="not found"):
            list_tags("owner", "repo")

    @patch("conductor.registry.github.httpx.get")
    def test_http_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = httpx.HTTPError("connection failed")

        with pytest.raises(RegistryError, match="HTTP error"):
            list_tags("owner", "repo")

    @patch("conductor.registry.github.httpx.get")
    def test_pagination_follows_link_header(self, mock_get: MagicMock) -> None:
        page1 = _mock_response(
            json_data=[{"name": "v3.0"}, {"name": "v2.0"}],
            links={"next": {"url": "https://api.github.com/repos/owner/repo/tags?page=2"}},
        )
        page2 = _mock_response(json_data=[{"name": "v1.0"}])
        mock_get.side_effect = [page1, page2]

        result = list_tags("owner", "repo")

        assert result == ["v3.0", "v2.0", "v1.0"]
        assert mock_get.call_count == 2
        # Second call should use the next URL from the Link header
        assert mock_get.call_args_list[1][0][0] == (
            "https://api.github.com/repos/owner/repo/tags?page=2"
        )

    @patch("conductor.registry.github.httpx.get")
    def test_pagination_three_pages(self, mock_get: MagicMock) -> None:
        page1 = _mock_response(
            json_data=[{"name": "a"}],
            links={"next": {"url": "https://api.github.com/p2"}},
        )
        page2 = _mock_response(
            json_data=[{"name": "b"}],
            links={"next": {"url": "https://api.github.com/p3"}},
        )
        page3 = _mock_response(json_data=[{"name": "c"}])
        mock_get.side_effect = [page1, page2, page3]

        result = list_tags("owner", "repo")

        assert result == ["a", "b", "c"]
        assert mock_get.call_count == 3


# --- get_default_branch ---


class TestGetDefaultBranch:
    @patch("conductor.registry.github.httpx.get")
    def test_success(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(json_data={"default_branch": "main"})

        result = get_default_branch("owner", "repo")

        assert result == "main"
        call_args = mock_get.call_args
        assert "api.github.com/repos/owner/repo" in call_args[0][0]

    @patch("conductor.registry.github.httpx.get")
    def test_returns_master(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(json_data={"default_branch": "master"})
        assert get_default_branch("owner", "repo") == "master"

    @patch("conductor.registry.github.httpx.get")
    def test_404_raises_registry_error(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(status_code=404)

        with pytest.raises(RegistryError, match="not found"):
            get_default_branch("owner", "missing-repo")

    @patch("conductor.registry.github.httpx.get")
    def test_timeout_raises_registry_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = httpx.TimeoutException("timed out")

        with pytest.raises(RegistryError, match="Timeout"):
            get_default_branch("owner", "repo")


# --- resolve_ref_to_sha ---


class TestResolveRefToSha:
    FULL_SHA = "abc1234567890abcdef1234567890abcdef12345"

    @patch("conductor.registry.github.httpx.get")
    def test_resolves_branch(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(json_data={"sha": self.FULL_SHA})

        result = resolve_ref_to_sha("owner", "repo", "main")

        assert result == self.FULL_SHA
        call_args = mock_get.call_args
        assert "api.github.com/repos/owner/repo/commits/main" in call_args[0][0]

    @patch("conductor.registry.github.httpx.get")
    def test_resolves_tag(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(json_data={"sha": self.FULL_SHA})

        result = resolve_ref_to_sha("owner", "repo", "v1.0.0")

        assert result == self.FULL_SHA
        assert "commits/v1.0.0" in mock_get.call_args[0][0]

    @patch("conductor.registry.github.httpx.get")
    def test_resolves_short_sha(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(json_data={"sha": self.FULL_SHA})

        result = resolve_ref_to_sha("owner", "repo", "abc1234")

        assert result == self.FULL_SHA
        assert "commits/abc1234" in mock_get.call_args[0][0]

    @patch("conductor.registry.github.httpx.get")
    def test_404_raises_registry_error_with_suggestion(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(status_code=404)

        with pytest.raises(RegistryError, match="not found") as exc_info:
            resolve_ref_to_sha("owner", "repo", "nonexistent-branch")

        assert exc_info.value.suggestion is not None
        assert "gh auth login" in exc_info.value.suggestion

    @patch("conductor.registry.github.httpx.get")
    def test_http_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = httpx.HTTPError("connection failed")

        with pytest.raises(RegistryError, match="HTTP error"):
            resolve_ref_to_sha("owner", "repo", "main")


# --- list_directory ---


class TestListDirectory:
    @patch("conductor.registry.github.httpx.get")
    def test_success(self, mock_get: MagicMock) -> None:
        contents = [
            {"name": "workflow.yaml", "type": "file"},
            {"name": "tools.yaml", "type": "file"},
            {"name": "subdir", "type": "dir"},
        ]
        mock_get.return_value = _mock_response(json_data=contents)

        result = list_directory("owner", "repo", "workflows")

        assert result == ["workflow.yaml", "tools.yaml"]
        assert "subdir" not in result

    @patch("conductor.registry.github.httpx.get")
    def test_404_raises_registry_error(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(status_code=404)

        with pytest.raises(RegistryError, match="not found"):
            list_directory("owner", "repo", "nonexistent")

    @patch("conductor.registry.github.httpx.get")
    def test_single_file_raises_error(self, mock_get: MagicMock) -> None:
        # Contents API returns a single object (not a list) when path is a file
        mock_get.return_value = _mock_response(json_data={"name": "file.txt", "type": "file"})

        with pytest.raises(RegistryError, match="Expected a directory"):
            list_directory("owner", "repo", "file.txt")


# --- parse_github_source ---


class TestParseGithubSource:
    def test_valid(self) -> None:
        assert parse_github_source("microsoft/conductor") == ("microsoft", "conductor")

    def test_invalid_no_slash(self) -> None:
        with pytest.raises(RegistryError, match="Invalid GitHub source"):
            parse_github_source("just-a-name")

    def test_invalid_too_many_parts(self) -> None:
        with pytest.raises(RegistryError, match="Invalid GitHub source"):
            parse_github_source("a/b/c")

    def test_invalid_empty_parts(self) -> None:
        with pytest.raises(RegistryError, match="Invalid GitHub source"):
            parse_github_source("/repo")

        with pytest.raises(RegistryError, match="Invalid GitHub source"):
            parse_github_source("owner/")
