"""Integration tests for the registry system.

Tests the full flow: configure registry → resolve ref → fetch workflow → get cached path.
Uses local path registries to avoid network dependencies.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.config.loader import load_config as load_workflow_config
from conductor.registry.cache import fetch_workflow, resolve_and_fetch
from conductor.registry.config import (
    RegistryType,
    add_registry,
    load_config,
    remove_registry,
)
from conductor.registry.errors import RegistryError
from conductor.registry.resolver import resolve_ref

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CONDUCTOR_HOME at a temp directory and return its path."""
    home = tmp_path / "conductor_home"
    home.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


def _create_local_registry(
    root: Path,
    workflows: dict[str, dict],
    *,
    sibling_files: dict[str, dict[str, str]] | None = None,
) -> Path:
    """Build a minimal local registry directory.

    Args:
        root: Parent directory (e.g. tmp_path).
        workflows: Mapping of workflow-name → dict with keys
            ``description``, ``path``, and ``content`` (the YAML text of the
            workflow file).
        sibling_files: Optional mapping of workflow-name → dict of
            filename → content for extra files alongside the workflow. A
            filename may itself be a nested relative path (e.g.
            ``"prompts/nested/note.md"``) — its parent directories are
            created as needed, matching a GitHub registry's recursive
            asset acquisition (path registries read the source tree
            directly, so nesting requires no special handling here, but
            the helper still needs to create the intermediate parents).

    Returns:
        Path to the registry root directory.
    """
    from ruamel.yaml import YAML

    registry_dir = root / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)

    index_data: dict = {"workflows": {}}
    for name, info in workflows.items():
        index_data["workflows"][name] = {
            "description": info.get("description", ""),
            "path": info["path"],
        }

        wf_path = registry_dir / info["path"]
        wf_path.parent.mkdir(parents=True, exist_ok=True)
        wf_path.write_text(info["content"])

        if sibling_files and name in sibling_files:
            for fname, fcontent in sibling_files[name].items():
                asset_path = wf_path.parent / fname
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                asset_path.write_text(fcontent)

    yaml = YAML()
    with open(registry_dir / "index.yaml", "w") as f:
        yaml.dump(index_data, f)

    return registry_dir


_SIMPLE_WORKFLOW = """\
name: test-workflow
agents:
  helper:
    model: copilot
    instructions: Say hello
steps:
  - agent: helper
"""


# ---------------------------------------------------------------------------
# Full local registry flow
# ---------------------------------------------------------------------------


class TestFullLocalFlow:
    """Configure → resolve → fetch → verify cached content."""

    def test_local_registry_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "hello": {
                    "description": "A greeting workflow",
                    "path": "hello/workflow.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("my-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)

        # Path registries don't accept refs — use the bare name with explicit registry.
        ref = resolve_ref("hello@my-reg")
        assert ref.kind == "registry"
        assert ref.workflow == "hello"
        assert ref.registry_name == "my-reg"
        assert ref.ref is None
        assert ref.registry_entry is not None

        cached_path = fetch_workflow("my-reg", ref.registry_entry, "hello")
        assert cached_path.exists()
        assert cached_path.name == "workflow.yaml"
        assert "test-workflow" in cached_path.read_text()


# ---------------------------------------------------------------------------
# Default registry flow
# ---------------------------------------------------------------------------


class TestDefaultRegistryFlow:
    """Resolve using the default registry (no @registry in ref)."""

    def test_resolve_via_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "greeter": {
                    "description": "Greet someone",
                    "path": "greeter.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("default-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)

        ref = resolve_ref("greeter")
        assert ref.kind == "registry"
        assert ref.registry_name == "default-reg"
        assert ref.workflow == "greeter"

        cached = fetch_workflow("default-reg", ref.registry_entry, "greeter")
        assert cached.exists()
        assert "test-workflow" in cached.read_text()


# ---------------------------------------------------------------------------
# Path registries reject refs
# ---------------------------------------------------------------------------


class TestPathRegistryRefs:
    """Path registries do not support refs and raise on non-empty refs."""

    def test_fetch_with_ref_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "wf": {
                    "description": "",
                    "path": "wf.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("p-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        ref = resolve_ref("wf")
        assert ref.registry_entry is not None

        with pytest.raises(RegistryError, match="Path registries do not support refs"):
            fetch_workflow("p-reg", ref.registry_entry, "wf", ref="v1.0.0")


# ---------------------------------------------------------------------------
# Cache reuse
# ---------------------------------------------------------------------------


class TestCacheReuse:
    """Second fetch of the same ref returns the cached path without re-copying."""

    def test_second_fetch_returns_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "cached-wf": {
                    "description": "Cached workflow",
                    "path": "cached-wf.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("cache-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        ref = resolve_ref("cached-wf")
        assert ref.registry_entry is not None

        path1 = fetch_workflow("cache-reg", ref.registry_entry, "cached-wf")
        path2 = fetch_workflow("cache-reg", ref.registry_entry, "cached-wf")

        assert path1 == path2
        assert path1.exists()
        # Path registries don't use cache — returns source directly.
        assert str(path1).startswith(str(reg_dir))


# ---------------------------------------------------------------------------
# Sibling files
# ---------------------------------------------------------------------------


class TestSiblingFiles:
    """Sibling files in the workflow directory are present alongside the workflow."""

    def test_siblings_alongside_workflow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "with-siblings": {
                    "description": "Has extra files",
                    "path": "with-siblings/workflow.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
            sibling_files={
                "with-siblings": {
                    "prompt.txt": "You are a helpful assistant.",
                    "schema.json": '{"type": "object"}',
                },
            },
        )

        add_registry("sib-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        ref = resolve_ref("with-siblings")
        assert ref.registry_entry is not None

        cached = fetch_workflow("sib-reg", ref.registry_entry, "with-siblings")
        cache_dir = cached.parent

        assert (cache_dir / "prompt.txt").exists()
        assert (cache_dir / "prompt.txt").read_text() == "You are a helpful assistant."
        assert (cache_dir / "schema.json").exists()
        assert (cache_dir / "schema.json").read_text() == '{"type": "object"}'

    def test_nested_siblings_preserve_relative_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path registries already read the source tree directly — a
        nested asset (not just an immediate sibling) resolves at its
        original relative path with no caching involved, confirming
        issue #530's recursive-acquisition change to the GitHub path does
        not alter path-registry behavior."""
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "issue-triage": {
                    "description": "Nested assets",
                    "path": "workflows/issue-triage.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
            sibling_files={
                "issue-triage": {
                    "prompts/issue-triage.md": "You triage issues carefully.",
                    "scripts/lib/deep/note.md": "Deeply nested note.",
                },
            },
        )

        add_registry("nested-reg", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        ref = resolve_ref("issue-triage")
        assert ref.registry_entry is not None

        cached = fetch_workflow("nested-reg", ref.registry_entry, "issue-triage")
        cache_dir = cached.parent

        assert (cache_dir / "prompts" / "issue-triage.md").read_text() == (
            "You triage issues carefully."
        )
        assert (cache_dir / "scripts" / "lib" / "deep" / "note.md").read_text() == (
            "Deeply nested note."
        )
        assert str(cached).startswith(str(reg_dir))


# ---------------------------------------------------------------------------
# CLI round-trip
# ---------------------------------------------------------------------------


class TestCLIRoundTrip:
    """Exercise the CLI commands for add → list → list <name> → remove."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_home(tmp_path, monkeypatch)
        self._tmp_path = tmp_path

    def test_full_cli_lifecycle(self) -> None:
        reg_dir = _create_local_registry(
            self._tmp_path,
            {
                "demo": {
                    "description": "Demo workflow",
                    "path": "demo.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        result = runner.invoke(
            app, ["registry", "add", "test-reg", str(reg_dir), "--type", "path", "--default"]
        )
        assert result.exit_code == 0, result.output
        assert "added" in result.output

        result = runner.invoke(app, ["registry", "list"])
        assert result.exit_code == 0, result.output
        assert "test-reg" in result.output
        assert "✓" in result.output

        result = runner.invoke(app, ["registry", "list", "test-reg"])
        assert result.exit_code == 0, result.output
        assert "demo" in result.output
        assert "Demo workflow" in result.output

        result = runner.invoke(app, ["registry", "remove", "test-reg"])
        assert result.exit_code == 0, result.output
        assert "removed" in result.output

        result = runner.invoke(app, ["registry", "list"])
        assert result.exit_code == 0, result.output
        assert "No registries configured" in result.output

    def test_add_list_remove_multiple(self) -> None:
        """Add two registries, list both, remove one, verify the other remains."""
        reg1 = _create_local_registry(
            self._tmp_path / "r1_parent",
            {
                "wf-a": {
                    "description": "A",
                    "path": "a.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )
        reg2 = _create_local_registry(
            self._tmp_path / "r2_parent",
            {
                "wf-b": {
                    "description": "B",
                    "path": "b.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        runner.invoke(app, ["registry", "add", "reg-a", str(reg1), "--type", "path"])
        runner.invoke(app, ["registry", "add", "reg-b", str(reg2), "--type", "path"])

        result = runner.invoke(app, ["registry", "list"])
        assert "reg-a" in result.output
        assert "reg-b" in result.output

        runner.invoke(app, ["registry", "remove", "reg-a"])

        result = runner.invoke(app, ["registry", "list"])
        assert "reg-a" not in result.output
        assert "reg-b" in result.output

    def test_set_default_and_resolve(self) -> None:
        """Set a default registry and resolve a bare workflow name."""
        reg_dir = _create_local_registry(
            self._tmp_path,
            {
                "auto": {
                    "description": "Auto-resolved",
                    "path": "auto.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        runner.invoke(app, ["registry", "add", "def-reg", str(reg_dir), "--type", "path"])
        runner.invoke(app, ["registry", "set-default", "def-reg"])

        config = load_config()
        assert config.default == "def-reg"

        ref = resolve_ref("auto")
        assert ref.kind == "registry"
        assert ref.registry_name == "def-reg"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Additional edge-case coverage for the integration flow."""

    def test_remove_default_clears_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removing the default registry clears the default setting."""
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "wf": {
                    "description": "",
                    "path": "wf.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )

        add_registry("gone", str(reg_dir), registry_type=RegistryType.path, set_default=True)
        assert load_config().default == "gone"

        remove_registry("gone")
        assert load_config().default is None

    def test_file_path_takes_precedence_over_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing file on disk resolves as 'file', not 'registry'."""
        _setup_home(tmp_path, monkeypatch)

        local_file = tmp_path / "my-workflow.yaml"
        local_file.write_text(_SIMPLE_WORKFLOW)

        ref = resolve_ref(str(local_file))
        assert ref.kind == "file"
        assert ref.path == local_file


# ---------------------------------------------------------------------------
# Ad-hoc references (workflow@owner/repo[#ref])
# ---------------------------------------------------------------------------


class TestAdhocRefIntegration:
    """End-to-end tests for ad-hoc registry references — no pre-installation."""

    def test_resolve_then_fetch_via_resolve_and_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real resolve_ref + mocked fetch produces a cached file under _adhoc/."""
        from unittest.mock import patch

        from conductor.registry.cache import resolve_and_fetch

        home = _setup_home(tmp_path, monkeypatch)

        # Pre-populate a "fetched" workflow in the expected adhoc cache dir.
        # In the real flow, _fetch_github would write here; we short-circuit
        # by mocking materialize_to_sha + load_index + _fetch_github.
        # Adhoc cache layout: <base>/_adhoc/<owner>/<repo>/<sha[:12]>/<repo_path>
        fake_sha = "c" * 40
        sha_dir = home / "cache" / "registries" / "_adhoc" / "myorg" / "team-a" / fake_sha[:12]

        def fake_fetch_github(entry, workflow_path, sha, dest_dir):
            target = dest_dir / workflow_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_SIMPLE_WORKFLOW)

        from conductor.registry.index import RegistryIndex, WorkflowInfo

        fake_index = RegistryIndex(
            workflows={
                "analysis": WorkflowInfo(description="", path="analysis.yaml"),
            }
        )

        with (
            patch("conductor.registry.cache.materialize_to_sha", return_value=fake_sha),
            patch("conductor.registry.cache.resolve_ref", return_value="v1.0.0"),
            patch("conductor.registry.cache.load_index", return_value=fake_index),
            patch("conductor.registry.cache._fetch_github", side_effect=fake_fetch_github),
        ):
            # No registry configured — but ad-hoc form works anyway
            resolved = resolve_ref("analysis@myorg/team-a#v1.0.0")
            assert resolved.kind == "adhoc"
            assert resolved.adhoc_owner == "myorg"
            assert resolved.adhoc_repo == "team-a"

            cached = resolve_and_fetch(resolved)

        assert cached.exists()
        assert cached.parent == sha_dir
        assert "test-workflow" in cached.read_text()

    def test_adhoc_works_with_no_registries_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ad-hoc resolution does not consult registry config."""
        _setup_home(tmp_path, monkeypatch)
        # No add_registry() call — config is empty.

        # Without ad-hoc, this would raise "No default registry configured"
        # because there's no '/' in the registry slot. With ad-hoc + '/' in
        # the slot, no config lookup happens.
        resolved = resolve_ref("analysis@myorg/team-a#v1.0.0")
        assert resolved.kind == "adhoc"
        assert resolved.adhoc_owner == "myorg"
        assert resolved.adhoc_repo == "team-a"
        assert resolved.workflow == "analysis"

    def test_adhoc_coexists_with_named_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A named registry and ad-hoc refs to a different repo both work."""
        _setup_home(tmp_path, monkeypatch)

        reg_dir = _create_local_registry(
            tmp_path,
            {
                "named-wf": {
                    "description": "",
                    "path": "named.yaml",
                    "content": _SIMPLE_WORKFLOW,
                },
            },
        )
        add_registry("team-a", str(reg_dir), registry_type=RegistryType.path, set_default=False)

        # Named ref → registry kind, looks up "team-a" in config
        named = resolve_ref("named-wf@team-a")
        assert named.kind == "registry"
        assert named.registry_name == "team-a"

        # Ad-hoc ref → adhoc kind, no config lookup (note '/' in registry slot)
        adhoc = resolve_ref("analysis@otherorg/team-a#v1.0.0")
        assert adhoc.kind == "adhoc"
        assert adhoc.adhoc_owner == "otherorg"
        assert adhoc.adhoc_repo == "team-a"


# ---------------------------------------------------------------------------
# Nested registry assets (issue #530)
# ---------------------------------------------------------------------------


def _build_git_trees(files: dict[str, bytes], root_sha: str) -> dict[str, list[dict]]:
    """Build a ``tree_sha -> tree-entries`` mapping for a synthetic repo.

    ``root_sha`` doubles as the tree identity for the repository root, so a
    request for ``git/trees/<root_sha>`` (which GitHub resolves
    automatically from a commit SHA to its root tree) is served identically
    to any other directory's own tree SHA.
    """
    dir_children: dict[str, dict[str, tuple[str, str, str]]] = {}

    def _tree_sha(dirpath: str) -> str:
        # No '/' in the synthetic identity: a real tree SHA never contains
        # one, and `_get()` below extracts it from the URL's final path
        # segment, which a literal '/' would corrupt.
        return root_sha if dirpath == "" else f"tree_{dirpath.replace('/', '__')}"

    for path in files:
        parts = path.split("/")
        for i in range(len(parts)):
            parent = "/".join(parts[:i])
            name = parts[i]
            dir_children.setdefault(parent, {})
            if i == len(parts) - 1:
                dir_children[parent][name] = ("100644", "blob", f"blob:{path}")
            else:
                child_dir = "/".join(parts[: i + 1])
                dir_children[parent][name] = ("040000", "tree", _tree_sha(child_dir))

    trees: dict[str, list[dict]] = {}
    for dirpath, children in dir_children.items():
        trees[_tree_sha(dirpath)] = [
            {"path": name, "mode": mode, "type": typ, "sha": sha}
            for name, (mode, typ, sha) in sorted(children.items())
        ]
    return trees


def _make_github_http_get(
    *,
    owner: str,
    repo: str,
    sha: str,
    default_branch: str,
    files: dict[str, bytes],
    unavailable_paths: frozenset[str] = frozenset(),
) -> tuple[object, list[str]]:
    """Build an ``httpx.get`` stand-in serving a synthetic GitHub repo.

    Backs ``GET /repos/{owner}/{repo}`` (default branch), ``.../commits/{ref}``
    (ref → SHA resolution), ``.../git/trees/{tree_sha}`` (the Git Trees API
    :func:`~conductor.registry.github.list_files_recursive` walks), and
    ``raw.githubusercontent.com/...`` (file downloads) entirely from
    *files* — no real network access. Returns ``(get_fn, downloaded_paths)``;
    the second element records every repo-relative path actually downloaded
    via the raw endpoint, so a test can assert a file outside the workflow's
    containing directory was never fetched.
    """
    import httpx as httpx_module

    trees = _build_git_trees(files, sha)
    repo_prefix = f"https://api.github.com/repos/{owner}/{repo}"
    raw_prefix = f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/"
    downloaded_paths: list[str] = []

    def _response(status_code: int = 200, json_data: object = None, content: bytes = b"") -> object:
        resp = MagicMock(spec=httpx_module.Response)
        resp.status_code = status_code
        resp.is_success = 200 <= status_code < 300
        resp.content = content
        resp.links = {}
        if json_data is not None:
            resp.json.return_value = json_data
        return resp

    def _get(url: str, *_args: object, **_kwargs: object) -> object:
        if url == repo_prefix:
            return _response(json_data={"default_branch": default_branch})
        if url.startswith(f"{repo_prefix}/commits/"):
            return _response(json_data={"sha": sha})
        if url.startswith(f"{repo_prefix}/git/trees/"):
            tree_sha = url.rsplit("/", 1)[-1]
            entries = trees.get(tree_sha)
            if entries is None:
                return _response(status_code=404)
            return _response(json_data={"sha": tree_sha, "tree": entries, "truncated": False})
        if url.startswith(raw_prefix):
            repo_path = url[len(raw_prefix) :]
            if repo_path in unavailable_paths:
                return _response(status_code=404)
            content = files.get(repo_path)
            if content is None:
                return _response(status_code=404)
            downloaded_paths.append(repo_path)
            return _response(content=content)
        raise AssertionError(f"Unexpected GitHub URL requested in test: {url}")

    return _get, downloaded_paths


_ISSUE_TRIAGE_WORKFLOW_YAML = """\
workflow:
  name: issue-triage
  entry_point: triage
agents:
  - name: triage
    model: gpt-4
    prompt: !file prompts/issue-triage.md
    routes:
      - to: labeler
  - name: labeler
    model: gpt-4
    prompt: !file scripts/lib/deep/note.md
    routes:
      - to: $end
"""


@pytest.mark.parametrize("named", [True, False], ids=["named", "adhoc"])
@pytest.mark.parametrize("pinned", [True, False], ids=["full-sha", "default-branch"])
def test_github_registry_auth_ignores_enterprise_gh_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool, pinned: bool
) -> None:
    _setup_home(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_HOST", "github.enterprise.example")
    environment = dict(os.environ)
    owner, repo, sha = "acme", "workflows", "d" * 40
    files = {
        "index.yaml": (
            b"workflows:\n"
            b"  issue-triage:\n"
            b"    description: Triage issues\n"
            b"    path: workflows/issue-triage.yaml\n"
        ),
        "workflows/issue-triage.yaml": _ISSUE_TRIAGE_WORKFLOW_YAML.encode(),
        "workflows/prompts/issue-triage.md": b"You triage issues carefully.\n",
        "workflows/scripts/lib/deep/note.md": b"Deeply nested guidance.\n",
    }
    mock_get, downloaded = _make_github_http_get(
        owner=owner, repo=repo, sha=sha, default_branch="main", files=files
    )
    if named:
        add_registry("acme-wf", f"{owner}/{repo}", registry_type=RegistryType.github)
    registry = "acme-wf" if named else f"{owner}/{repo}"
    suffix = f"#{sha}" if pinned else ""
    credential = "github-com-token"
    with (
        patch(
            "conductor.registry.github.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, f" {credential}\n", ""),
        ) as mock_run,
        patch("conductor.registry.github.httpx.get", side_effect=mock_get) as mock_http,
    ):
        cached = resolve_and_fetch(resolve_ref(f"issue-triage@{registry}{suffix}"))

    assert cached.read_bytes() == files["workflows/issue-triage.yaml"]
    assert (cached.parent / "prompts" / "issue-triage.md").read_bytes() == (
        files["workflows/prompts/issue-triage.md"]
    )
    assert "workflows/issue-triage.yaml" in downloaded
    urls = [call.args[0] for call in mock_http.call_args_list]
    assert any(url.startswith("https://api.github.com/") for url in urls)
    assert any(url.startswith("https://raw.githubusercontent.com/") for url in urls)
    assert any(url.endswith(f"/commits/{sha if pinned else 'main'}") for url in urls)
    assert (f"https://api.github.com/repos/{owner}/{repo}" in urls) is not pinned
    assert mock_run.call_count == mock_http.call_count
    for call in mock_run.call_args_list:
        assert call.args == (["gh", "auth", "token", "--hostname", "github.com"],)
        assert call.kwargs == {"capture_output": True, "text": True, "timeout": 5}
    for call in mock_http.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == f"Bearer {credential}"
    assert dict(os.environ) == environment


class TestNestedRegistryAssetsRegression:
    """End-to-end reproduction of issue #530 against a GitHub registry,
    HTTP-mocked at the ``httpx`` boundary so real reference resolution,
    recursive fetching, staging/promotion, and ``load_config()`` all run
    for real — only the network transport is faked."""

    _OWNER = "acme"
    _REPO = "workflows"
    _SHA = "d" * 40

    def _files(self) -> dict[str, bytes]:
        return {
            "index.yaml": (
                b"workflows:\n"
                b"  issue-triage:\n"
                b"    description: Triage a new issue\n"
                b"    path: workflows/issue-triage.yaml\n"
            ),
            "workflows/issue-triage.yaml": _ISSUE_TRIAGE_WORKFLOW_YAML.encode("utf-8"),
            "workflows/prompts/issue-triage.md": b"You triage issues carefully.\n",
            "workflows/scripts/triage.sh": b"#!/bin/sh\necho triage\n",
            "workflows/data/labels.json": b'{"labels": ["bug", "feature"]}',
            "workflows/scripts/lib/deep/note.md": b"Deeply nested guidance.\n",
            # Lives outside the workflow's containing directory ("workflows/")
            # and must never be listed or downloaded.
            "other/unrelated.yaml": b"name: unrelated\n",
        }

    def _configure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = _setup_home(tmp_path, monkeypatch)
        # No local `gh` CLI dependency for this test.
        monkeypatch.setattr("conductor.registry.github._get_auth_token", lambda: None)
        add_registry(
            "acme-wf",
            f"{self._OWNER}/{self._REPO}",
            registry_type=RegistryType.github,
            set_default=True,
        )
        return home

    def test_loads_nested_prompt_and_preserves_asset_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configure(tmp_path, monkeypatch)
        files = self._files()
        mock_get, downloaded = _make_github_http_get(
            owner=self._OWNER,
            repo=self._REPO,
            sha=self._SHA,
            default_branch="main",
            files=files,
        )

        with patch("conductor.registry.github.httpx.get", side_effect=mock_get):
            ref = resolve_ref("issue-triage@acme-wf")
            assert ref.kind == "registry"
            assert ref.registry_entry is not None
            cached = fetch_workflow("acme-wf", ref.registry_entry, "issue-triage")

        assert cached.name == "issue-triage.yaml"
        cache_dir = cached.parent

        # Every nested asset survives at its original repo-relative layout.
        assert (cache_dir / "prompts" / "issue-triage.md").read_bytes() == (
            files["workflows/prompts/issue-triage.md"]
        )
        assert (cache_dir / "scripts" / "triage.sh").read_bytes() == (
            files["workflows/scripts/triage.sh"]
        )
        assert (cache_dir / "data" / "labels.json").read_bytes() == (
            files["workflows/data/labels.json"]
        )
        assert (cache_dir / "scripts" / "lib" / "deep" / "note.md").read_bytes() == (
            files["workflows/scripts/lib/deep/note.md"]
        )
        # The file outside the workflow's containing directory must never
        # have been downloaded.
        assert "other/unrelated.yaml" not in downloaded
        assert not (cache_dir.parent / "other").exists()

        # The real loader resolves the nested !file prompts through the
        # cached, mirrored layout.
        config = load_workflow_config(cached)
        assert config.agents[0].prompt == "You triage issues carefully.\n"
        assert config.agents[1].prompt == "Deeply nested guidance.\n"

    def test_named_and_adhoc_references_both_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both a named-registry reference and an ad-hoc reference reach
        the same recursive fetch pipeline and end up in distinct caches."""
        from conductor.registry.cache import resolve_and_fetch

        self._configure(tmp_path, monkeypatch)
        files = self._files()
        mock_get, _downloaded = _make_github_http_get(
            owner=self._OWNER,
            repo=self._REPO,
            sha=self._SHA,
            default_branch="main",
            files=files,
        )

        with patch("conductor.registry.github.httpx.get", side_effect=mock_get):
            named = resolve_and_fetch(resolve_ref("issue-triage@acme-wf"))
            adhoc = resolve_and_fetch(resolve_ref(f"issue-triage@{self._OWNER}/{self._REPO}"))

        assert named != adhoc
        assert (named.parent / "prompts" / "issue-triage.md").is_file()
        assert (adhoc.parent / "prompts" / "issue-triage.md").is_file()

    def test_loads_from_unrelated_caller_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """!file resolution is relative to the cached workflow file, not cwd."""
        import os

        self._configure(tmp_path, monkeypatch)
        files = self._files()
        mock_get, _downloaded = _make_github_http_get(
            owner=self._OWNER,
            repo=self._REPO,
            sha=self._SHA,
            default_branch="main",
            files=files,
        )

        with patch("conductor.registry.github.httpx.get", side_effect=mock_get):
            ref = resolve_ref("issue-triage@acme-wf")
            assert ref.registry_entry is not None
            cached = fetch_workflow("acme-wf", ref.registry_entry, "issue-triage")

        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        original_cwd = os.getcwd()
        try:
            os.chdir(elsewhere)
            config = load_workflow_config(cached)
        finally:
            os.chdir(original_cwd)

        assert config.agents[0].prompt == "You triage issues carefully.\n"

    def test_conductor_validate_succeeds_via_registry_reference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configure(tmp_path, monkeypatch)
        files = self._files()
        mock_get, _downloaded = _make_github_http_get(
            owner=self._OWNER,
            repo=self._REPO,
            sha=self._SHA,
            default_branch="main",
            files=files,
        )

        with patch("conductor.registry.github.httpx.get", side_effect=mock_get):
            result = runner.invoke(app, ["validate", "issue-triage@acme-wf"])

        assert result.exit_code == 0, result.output

    def test_conductor_validate_fails_when_a_nested_asset_cannot_be_downloaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure downloading even an unused regular file aborts the
        whole fetch (strict completeness, issue #530) and surfaces the
        affected path to the user rather than silently proceeding."""
        self._configure(tmp_path, monkeypatch)
        files = self._files()
        failing_path = "workflows/scripts/lib/deep/note.md"
        mock_get, _downloaded = _make_github_http_get(
            owner=self._OWNER,
            repo=self._REPO,
            sha=self._SHA,
            default_branch="main",
            files=files,
            unavailable_paths=frozenset({failing_path}),
        )

        with patch("conductor.registry.github.httpx.get", side_effect=mock_get):
            result = runner.invoke(app, ["validate", "issue-triage@acme-wf"])

        assert result.exit_code != 0
        assert failing_path in result.output
