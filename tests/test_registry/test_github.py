"""Tests for conductor.registry.github module."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import httpx
import pytest

from conductor.registry.errors import RegistryError, RegistryNotFoundError
from conductor.registry.github import (
    _get_auth_token,
    fetch_file,
    fetch_file_text,
    get_default_branch,
    list_directory,
    list_files_recursive,
    list_tags,
    parse_github_source,
    resolve_ref_to_sha,
)


@pytest.fixture(autouse=True)
def _stub_auth_token(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock_run = MagicMock(return_value=subprocess.CompletedProcess([], 1, "", ""))
    monkeypatch.setattr("conductor.registry.github.subprocess.run", mock_run)
    return mock_run


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


class TestGithubAuthentication:
    @pytest.mark.parametrize("gh_host", [None, "github.com", "github.enterprise.example"])
    def test_token_lookup_selects_github_com_without_changing_environment(
        self, gh_host: str | None, monkeypatch: pytest.MonkeyPatch, _stub_auth_token: MagicMock
    ) -> None:
        if gh_host is None:
            monkeypatch.delenv("GH_HOST", raising=False)
        else:
            monkeypatch.setenv("GH_HOST", gh_host)
        environment = dict(os.environ)
        _stub_auth_token.return_value = subprocess.CompletedProcess(
            [], 0, " github-com-token \n", ""
        )

        assert _get_auth_token() == "github-com-token"
        _stub_auth_token.assert_called_once_with(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert dict(os.environ) == environment

    @pytest.mark.parametrize(
        ("returncode", "stdout"),
        [(1, "github-com-token\n"), (0, ""), (0, " \n\t ")],
    )
    def test_unavailable_token_returns_none(
        self, _stub_auth_token: MagicMock, returncode: int, stdout: str
    ) -> None:
        _stub_auth_token.return_value = subprocess.CompletedProcess([], returncode, stdout, "")

        assert _get_auth_token() is None

    @pytest.mark.parametrize(
        "failure",
        [FileNotFoundError("gh"), subprocess.TimeoutExpired(["gh", "auth", "token"], 5)],
    )
    def test_missing_cli_or_timeout_returns_none(
        self, _stub_auth_token: MagicMock, failure: Exception
    ) -> None:
        _stub_auth_token.side_effect = failure

        assert _get_auth_token() is None

    @pytest.mark.parametrize("request_kind", ["api", "raw"])
    @pytest.mark.parametrize("authenticated", [True, False])
    def test_http_requests_use_only_the_github_com_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _stub_auth_token: MagicMock,
        request_kind: str,
        authenticated: bool,
    ) -> None:
        monkeypatch.setenv("GH_HOST", "github.enterprise.example")
        _stub_auth_token.return_value = subprocess.CompletedProcess(
            [], 0 if authenticated else 1, " github-com-token \n", ""
        )
        with patch("conductor.registry.github.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(
                content=b"workflow", json_data={"default_branch": "main"}
            )
            if request_kind == "api":
                assert get_default_branch("owner", "repo") == "main"
                assert mock_get.call_args.args[0] == "https://api.github.com/repos/owner/repo"
                assert mock_get.call_args.kwargs["headers"]["Accept"] == (
                    "application/vnd.github.v3+json"
                )
            else:
                assert fetch_file("owner", "repo", "workflow.yaml") == b"workflow"
                assert mock_get.call_args.args[0].startswith("https://raw.githubusercontent.com/")

        headers = mock_get.call_args.kwargs["headers"]
        if authenticated:
            assert headers["Authorization"] == "Bearer github-com-token"
        else:
            assert "Authorization" not in headers
        _stub_auth_token.assert_called_once_with(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=5,
        )


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

    @patch("conductor.registry.github.httpx.get")
    def test_path_with_hash_is_percent_encoded(self, mock_get: MagicMock) -> None:
        """A '#' in the path must not be treated as a URL fragment delimiter.

        Regression for issue #530: an unescaped '#' truncates the request at
        'prompts/plan.md', silently fetching the wrong file (or the parent
        file's content again) instead of 'prompts/plan.md#draft'.
        """
        mock_get.return_value = _mock_response(content=b"draft content")
        result = fetch_file("owner", "repo", "prompts/plan.md#draft", ref="v1.0")

        assert result == b"draft content"
        requested_url = mock_get.call_args[0][0]
        assert requested_url == (
            "https://raw.githubusercontent.com/owner/repo/v1.0/prompts/plan.md%23draft"
        )

    @patch("conductor.registry.github.httpx.get")
    def test_path_with_question_mark_is_percent_encoded(self, mock_get: MagicMock) -> None:
        """A '?' in the path must not be treated as a query-string delimiter."""
        mock_get.return_value = _mock_response(content=b"query-like content")
        result = fetch_file("owner", "repo", "assets/report?v2.md", ref="v1.0")

        assert result == b"query-like content"
        requested_url = mock_get.call_args[0][0]
        assert requested_url == (
            "https://raw.githubusercontent.com/owner/repo/v1.0/assets/report%3Fv2.md"
        )

    @patch("conductor.registry.github.httpx.get")
    def test_path_with_literal_percent_is_percent_encoded(self, mock_get: MagicMock) -> None:
        """A literal '%' must be re-escaped, not passed through as an existing escape."""
        mock_get.return_value = _mock_response(content=b"100% done")
        result = fetch_file("owner", "repo", "notes/100%done.md", ref="v1.0")

        assert result == b"100% done"
        requested_url = mock_get.call_args[0][0]
        assert requested_url == (
            "https://raw.githubusercontent.com/owner/repo/v1.0/notes/100%25done.md"
        )

    @patch("conductor.registry.github.httpx.get")
    def test_path_separators_are_not_escaped(self, mock_get: MagicMock) -> None:
        """Path separators are preserved; only segment names are percent-encoded."""
        mock_get.return_value = _mock_response(content=b"nested")
        result = fetch_file("owner", "repo", "a/b/c#weird?name%.md", ref="v1.0")

        assert result == b"nested"
        requested_url = mock_get.call_args[0][0]
        assert requested_url == (
            "https://raw.githubusercontent.com/owner/repo/v1.0/a/b/c%23weird%3Fname%25.md"
        )


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
        assert "gh auth login --hostname github.com" in exc_info.value.suggestion

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


# --- list_files_recursive ---


def _tree_entry(path: str, mode: str, entry_type: str, sha: str | None = "s") -> dict:
    """Build one Git Trees API entry dict."""
    return {"path": path, "mode": mode, "type": entry_type, "sha": sha}


def _tree_response(
    entries: list[dict], *, truncated: bool = False, tree_sha: str = "sha"
) -> MagicMock:
    return _mock_response(json_data={"sha": tree_sha, "tree": entries, "truncated": truncated})


class TestListFilesRecursive:
    _REF = "f" * 40

    @patch("conductor.registry.github.httpx.get")
    def test_root_level_workflow_lists_whole_repo(self, mock_get: MagicMock) -> None:
        """A root-directory listing (a repo-root workflow) walks from the
        commit SHA directly — one call for the root, no directory-segment
        resolution calls first."""
        mock_get.return_value = _tree_response(
            [
                _tree_entry("workflow.yaml", "100644", "blob"),
                _tree_entry("README.md", "100644", "blob"),
            ]
        )

        result = list_files_recursive("owner", "repo", "", ref=self._REF)

        assert result == ["README.md", "workflow.yaml"]
        assert mock_get.call_count == 1
        called_url = mock_get.call_args_list[0][0][0]
        assert called_url.endswith(f"/git/trees/{self._REF}")

    @patch("conductor.registry.github.httpx.get")
    def test_deterministic_sorted_paths_regardless_of_input_order(
        self, mock_get: MagicMock
    ) -> None:
        mock_get.return_value = _tree_response(
            [
                _tree_entry("z.yaml", "100644", "blob"),
                _tree_entry("a.yaml", "100644", "blob"),
                _tree_entry("m.yaml", "100644", "blob"),
            ]
        )

        result = list_files_recursive("owner", "repo", ".", ref=self._REF)

        assert result == ["a.yaml", "m.yaml", "z.yaml"]

    @patch("conductor.registry.github.httpx.get")
    def test_multiple_nesting_levels(self, mock_get: MagicMock) -> None:
        """Listing a two-level-deep directory resolves each path segment,
        then walks the target directory's own nested subtree."""
        root_tree = _tree_response(
            [_tree_entry("workflows", "040000", "tree", sha="TREE_WORKFLOWS")]
        )
        workflows_tree = _tree_response(
            [_tree_entry("foo", "040000", "tree", sha="TREE_FOO")], tree_sha="TREE_WORKFLOWS"
        )
        foo_tree = _tree_response(
            [
                _tree_entry("plan.yaml", "100644", "blob"),
                _tree_entry("prompts", "040000", "tree", sha="TREE_PROMPTS"),
            ],
            tree_sha="TREE_FOO",
        )
        prompts_tree = _tree_response(
            [_tree_entry("plan.md", "100644", "blob")], tree_sha="TREE_PROMPTS"
        )
        mock_get.side_effect = [root_tree, workflows_tree, foo_tree, prompts_tree]

        result = list_files_recursive("owner", "repo", "workflows/foo", ref=self._REF)

        assert result == [
            "workflows/foo/plan.yaml",
            "workflows/foo/prompts/plan.md",
        ]
        assert mock_get.call_count == 4
        # The first two calls resolve directory segments from the commit SHA
        # itself, then from the "workflows" tree SHA — never re-touching ref.
        assert mock_get.call_args_list[0][0][0].endswith(f"/git/trees/{self._REF}")
        assert mock_get.call_args_list[1][0][0].endswith("/git/trees/TREE_WORKFLOWS")
        assert mock_get.call_args_list[2][0][0].endswith("/git/trees/TREE_FOO")
        assert mock_get.call_args_list[3][0][0].endswith("/git/trees/TREE_PROMPTS")

    @patch("conductor.registry.github.httpx.get")
    def test_regular_and_executable_blobs_included(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _tree_response(
            [
                _tree_entry("script.sh", "100755", "blob"),
                _tree_entry("data.json", "100644", "blob"),
            ]
        )

        result = list_files_recursive("owner", "repo", "", ref=self._REF)

        assert result == ["data.json", "script.sh"]

    @patch("conductor.registry.github.httpx.get")
    def test_symlinks_excluded(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _tree_response(
            [
                _tree_entry("real.yaml", "100644", "blob"),
                _tree_entry("alias.yaml", "120000", "blob"),
            ]
        )

        result = list_files_recursive("owner", "repo", "", ref=self._REF)

        assert result == ["real.yaml"]
        assert "alias.yaml" not in result

    @patch("conductor.registry.github.httpx.get")
    def test_submodules_excluded(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _tree_response(
            [
                _tree_entry("real.yaml", "100644", "blob"),
                _tree_entry("vendor/lib", "160000", "commit"),
            ]
        )

        result = list_files_recursive("owner", "repo", "", ref=self._REF)

        assert result == ["real.yaml"]

    @patch("conductor.registry.github.httpx.get")
    def test_repeated_tree_contents_at_different_paths(self, mock_get: MagicMock) -> None:
        """Two sibling directories sharing an identical filename must not
        be conflated — each resolves to its own repo-relative path."""
        root_tree = _tree_response(
            [
                _tree_entry("alpha", "040000", "tree", sha="TREE_ALPHA"),
                _tree_entry("beta", "040000", "tree", sha="TREE_BETA"),
            ]
        )
        alpha_tree = _tree_response(
            [_tree_entry("note.md", "100644", "blob")], tree_sha="TREE_ALPHA"
        )
        beta_tree = _tree_response([_tree_entry("note.md", "100644", "blob")], tree_sha="TREE_BETA")
        mock_get.side_effect = [root_tree, alpha_tree, beta_tree]

        result = list_files_recursive("owner", "repo", "", ref=self._REF)

        assert result == ["alpha/note.md", "beta/note.md"]

    @patch("conductor.registry.github.httpx.get")
    def test_missing_directory_raises_not_found(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _tree_response([_tree_entry("other", "040000", "tree", sha="X")])

        with pytest.raises(RegistryNotFoundError, match="not found"):
            list_files_recursive("owner", "repo", "workflows", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_path_segment_that_is_a_file_not_a_directory_raises_not_found(
        self, mock_get: MagicMock
    ) -> None:
        mock_get.return_value = _tree_response(
            [_tree_entry("workflows", "100644", "blob", sha=None)]
        )

        with pytest.raises(RegistryNotFoundError):
            list_files_recursive("owner", "repo", "workflows/foo", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_malformed_response_not_an_object_raises(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(json_data=["not", "an", "object"])

        with pytest.raises(RegistryError, match="Malformed response"):
            list_files_recursive("owner", "repo", "", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_malformed_response_missing_tree_key_raises(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(json_data={"sha": "x"})

        with pytest.raises(RegistryError, match="Malformed response"):
            list_files_recursive("owner", "repo", "", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_malformed_entry_missing_path_raises(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(
            json_data={"sha": "x", "tree": [{"mode": "100644", "type": "blob"}], "truncated": False}
        )

        with pytest.raises(RegistryError, match="Malformed response"):
            list_files_recursive("owner", "repo", "", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_malformed_tree_entry_missing_sha_raises(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(
            json_data={
                "sha": "x",
                "tree": [{"path": "sub", "mode": "040000", "type": "tree"}],
                "truncated": False,
            }
        )

        with pytest.raises(RegistryError, match="Malformed response"):
            list_files_recursive("owner", "repo", "", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_truncated_response_raises(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _tree_response(
            [_tree_entry("workflow.yaml", "100644", "blob")], truncated=True
        )

        with pytest.raises(RegistryError, match="truncated"):
            list_files_recursive("owner", "repo", "", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_timeout_raises_registry_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = httpx.TimeoutException("timed out")

        with pytest.raises(RegistryError, match="Timeout"):
            list_files_recursive("owner", "repo", "", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_http_error_raises_registry_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = httpx.HTTPError("connection failed")

        with pytest.raises(RegistryError, match="HTTP error"):
            list_files_recursive("owner", "repo", "", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_404_on_nested_directory_raises_registry_error(self, mock_get: MagicMock) -> None:
        root_tree = _tree_response([_tree_entry("sub", "040000", "tree", sha="TREE_SUB")])
        mock_get.side_effect = [root_tree, _mock_response(status_code=404)]

        with pytest.raises(RegistryError, match="not found"):
            list_files_recursive("owner", "repo", "", ref=self._REF)

    @patch("conductor.registry.github.httpx.get")
    def test_traversal_pinned_to_commit_and_tree_identities(self, mock_get: MagicMock) -> None:
        """Every request after the first uses a *tree* SHA discovered from
        the previous response — never the original ref/branch name — so
        traversal stays pinned to one immutable commit throughout."""
        root_tree = _tree_response([_tree_entry("sub", "040000", "tree", sha="TREE_SUB")])
        sub_tree = _tree_response([_tree_entry("file.yaml", "100644", "blob")], tree_sha="TREE_SUB")
        mock_get.side_effect = [root_tree, sub_tree]

        result = list_files_recursive("owner", "repo", "", ref=self._REF)

        assert result == ["sub/file.yaml"]
        urls = [call[0][0] for call in mock_get.call_args_list]
        assert urls[0].endswith(f"/git/trees/{self._REF}")
        assert urls[1].endswith("/git/trees/TREE_SUB")
        assert self._REF not in urls[1]

    @patch("conductor.registry.github.httpx.get")
    def test_no_auth_header_needed_without_local_gh_cli(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _tree_response([_tree_entry("workflow.yaml", "100644", "blob")])

        list_files_recursive("owner", "repo", "", ref=self._REF)

        headers = mock_get.call_args.kwargs["headers"]
        assert "Authorization" not in headers


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
