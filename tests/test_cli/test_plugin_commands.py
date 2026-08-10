"""Tests for the ``conductor plugin`` CLI subcommand group.

``fetch`` is the only command in the tool that clones, which is what lets
``conductor validate`` stay off the network. ``list`` reads the cache and
reports the state a run would start from.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the plugin cache at a temp directory, never the real ``~``."""
    from conductor.plugins.fetch import clear_resolution_memo

    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor-home"))
    clear_resolution_memo()


def _plugin(root: Path, name: str, *, agents: list[str] | None = None) -> Path:
    """Minimal plugin tree — the CLI only needs it to resolve."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / ".claude-plugin"
    manifest.mkdir(parents=True, exist_ok=True)
    (manifest / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    for agent in agents or []:
        agents_dir = root / "agents"
        agents_dir.mkdir(exist_ok=True)
        (agents_dir / f"{agent}.agent.md").write_text(
            f"---\nname: {agent}\ndescription: Does a thing.\n---\n\nYou act.\n",
            encoding="utf-8",
        )
    return root


def _catalog(root: Path, name: str, plugins: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / ".claude-plugin"
    manifest.mkdir(parents=True, exist_ok=True)
    (manifest / "marketplace.json").write_text(
        json.dumps(
            {
                "name": name,
                "metadata": {},
                "plugins": [{"name": k, "source": v} for k, v in plugins.items()],
            }
        ),
        encoding="utf-8",
    )
    return root


def _git_repo(root: Path) -> str:
    for command in (
        ["git", "init", "-q", "-b", "main", "."],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-qm", "init"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _workflow(path: Path, sources: dict[str, str], plugins: list[str]) -> Path:
    source_lines = "\n".join(f"      {name}: {value}" for name, value in sources.items())
    plugin_lines = "\n".join(f"      - {entry}" for entry in plugins)
    path.write_text(
        "workflow:\n"
        "  name: demo\n"
        "  entry_point: worker\n"
        "  runtime:\n"
        "    provider: copilot\n"
        + (f"    plugin_sources:\n{source_lines}\n" if sources else "")
        + (f"    plugins:\n{plugin_lines}\n" if plugins else "")
        + "agents:\n"
        "  - name: worker\n"
        '    prompt: "Do it."\n'
        "    output:\n"
        "      result:\n"
        "        type: string\n"
        "output:\n"
        '  result: "{{ worker.output.result }}"\n',
        encoding="utf-8",
    )
    return path


class TestHelp:
    def test_group_is_registered(self) -> None:
        result = runner.invoke(app, ["plugin", "--help"])
        assert result.exit_code == 0
        assert "fetch" in result.output
        assert "list" in result.output

    def test_there_is_no_update_verb(self) -> None:
        """Floating refs self-update and pinned ones are meant not to."""
        result = runner.invoke(app, ["plugin", "update"])
        assert result.exit_code != 0


class TestFetch:
    def test_acquires_a_git_source(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _plugin(repo / "prs", "prs")
        _catalog(repo, "acme", {"prs": "./prs"})
        sha = _git_repo(repo)
        workflow = _workflow(tmp_path / "wf.yaml", {"acme": f"file://{repo}"}, ["prs@acme"])

        result = runner.invoke(app, ["plugin", "fetch", str(workflow)])

        assert result.exit_code == 0, result.output
        assert sha[:12] in result.output
        assert "newly fetched" in result.output

    def test_a_second_fetch_reports_a_cache_hit(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _plugin(repo / "prs", "prs")
        _catalog(repo, "acme", {"prs": "./prs"})
        _git_repo(repo)
        workflow = _workflow(tmp_path / "wf.yaml", {"acme": f"file://{repo}"}, ["prs@acme"])
        runner.invoke(app, ["plugin", "fetch", str(workflow)])

        result = runner.invoke(app, ["plugin", "fetch", str(workflow)])

        assert result.exit_code == 0
        assert "(0 newly fetched)" in result.output

    def test_reports_a_workflow_with_no_sources(self, tmp_path: Path) -> None:
        workflow = _workflow(tmp_path / "wf.yaml", {}, [])

        result = runner.invoke(app, ["plugin", "fetch", str(workflow)])

        assert result.exit_code == 0
        assert "declares no plugin sources" in result.output

    def test_an_unreachable_source_fails(self, tmp_path: Path) -> None:
        workflow = _workflow(
            tmp_path / "wf.yaml", {"acme": f"file://{tmp_path}/missing"}, ["prs@acme"]
        )

        result = runner.invoke(app, ["plugin", "fetch", str(workflow)])

        assert result.exit_code == 1

    def test_a_missing_workflow_file_fails(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["plugin", "fetch", str(tmp_path / "nope.yaml")])

        assert result.exit_code == 1
        assert "not found" in result.output


class TestList:
    def test_reports_sources_and_component_counts(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _plugin(repo / "prs", "prs", agents=["code-reviewer"])
        _catalog(repo, "acme", {"prs": "./prs"})
        workflow = _workflow(tmp_path / "wf.yaml", {"acme": f"file://{repo}"}, ["prs@acme"])
        _git_repo(repo)
        runner.invoke(app, ["plugin", "fetch", str(workflow)])

        result = runner.invoke(app, ["plugin", "list", str(workflow)])

        assert result.exit_code == 0, result.output
        assert "acme" in result.output
        assert "prs:code-reviewer" in result.output

    def test_never_fetches(self, tmp_path: Path) -> None:
        """The whole reason ``fetch`` is a separate verb."""
        repo = tmp_path / "repo"
        _plugin(repo / "prs", "prs")
        _catalog(repo, "acme", {"prs": "./prs"})
        _git_repo(repo)
        workflow = _workflow(tmp_path / "wf.yaml", {"acme": f"file://{repo}"}, ["prs@acme"])

        result = runner.invoke(app, ["plugin", "list", str(workflow)])

        assert result.exit_code == 1
        assert "conductor plugin fetch" in result.output

    def test_works_with_a_local_source(self, tmp_path: Path) -> None:
        _plugin(tmp_path / "vendor" / "mine", "mine", agents=["helper"])
        workflow = _workflow(tmp_path / "wf.yaml", {"local": "./vendor/mine"}, ["mine@local"])

        result = runner.invoke(app, ["plugin", "list", str(workflow)])

        assert result.exit_code == 0, result.output
        assert "mine:helper" in result.output

    def test_reports_a_workflow_enabling_no_plugins(self, tmp_path: Path) -> None:
        workflow = _workflow(tmp_path / "wf.yaml", {}, [])

        result = runner.invoke(app, ["plugin", "list", str(workflow)])

        assert result.exit_code == 0
        assert "No agent in this workflow enables plugins" in result.output

    def test_lists_a_plain_path_plugin_without_sources(self, tmp_path: Path) -> None:
        _plugin(tmp_path / "tools" / "mine", "mine")
        workflow = _workflow(tmp_path / "wf.yaml", {}, ["./tools/mine"])

        result = runner.invoke(app, ["plugin", "list", str(workflow)])

        assert result.exit_code == 0, result.output
        assert "./tools/mine" in result.output


class TestRunPrefetch:
    """``conductor run`` acquires sources before the engine starts.

    Up front rather than lazily for three reasons: a cold cache means a
    clone, which would otherwise stall the first agent to reference the
    plugin; a failure is a configuration problem and should read as one;
    and resolving every source together lets them be fetched at once.
    """

    def test_returns_the_marketplace_table(self, tmp_path: Path) -> None:
        import asyncio

        from conductor.cli.run import _prefetch_plugin_sources
        from conductor.config.loader import load_workflow

        repo = tmp_path / "repo"
        _plugin(repo / "prs", "prs")
        _catalog(repo, "acme", {"prs": "./prs"})
        _git_repo(repo)
        workflow = _workflow(tmp_path / "wf.yaml", {"acme": f"file://{repo}"}, ["prs@acme"])
        config = load_workflow(workflow)

        table = asyncio.run(_prefetch_plugin_sources(config, workflow))

        assert list(table) == ["acme"]
        assert "prs" in table["acme"].plugins

    def test_is_a_no_op_without_declared_sources(self, tmp_path: Path) -> None:
        import asyncio

        from conductor.cli.run import _prefetch_plugin_sources
        from conductor.config.loader import load_workflow

        workflow = _workflow(tmp_path / "wf.yaml", {}, [])
        config = load_workflow(workflow)

        assert asyncio.run(_prefetch_plugin_sources(config, workflow)) == {}

    def test_a_bad_source_fails_before_the_engine(self, tmp_path: Path) -> None:
        import asyncio

        from conductor.cli.run import _prefetch_plugin_sources
        from conductor.config.loader import load_workflow
        from conductor.plugins.errors import PluginError

        workflow = _workflow(
            tmp_path / "wf.yaml", {"acme": f"file://{tmp_path}/missing"}, ["prs@acme"]
        )
        config = load_workflow(workflow)

        with pytest.raises(PluginError):
            asyncio.run(_prefetch_plugin_sources(config, workflow))
