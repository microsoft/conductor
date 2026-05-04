"""Tests for workspace instruction file discovery and loading."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from conductor.config.instructions import (
    INSTRUCTION_SIZE_WARNING_THRESHOLD,
    _find_git_root,
    build_instructions_preamble,
    discover_workspace_instructions,
    load_instruction_files,
)

# ---------------------------------------------------------------------------
# _find_git_root
# ---------------------------------------------------------------------------


class TestFindGitRoot:
    """Tests for _find_git_root()."""

    def test_finds_git_directory(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert _find_git_root(sub) == tmp_path

    def test_finds_git_file_worktree(self, tmp_path: Path) -> None:
        """Git worktrees use a .git file instead of a directory."""
        (tmp_path / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/foo")
        sub = tmp_path / "src"
        sub.mkdir()
        assert _find_git_root(sub) == tmp_path

    def test_returns_none_outside_git(self, tmp_path: Path) -> None:
        sub = tmp_path / "norepo"
        sub.mkdir()
        assert _find_git_root(sub) is None

    def test_git_root_is_start_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        assert _find_git_root(tmp_path) == tmp_path


# ---------------------------------------------------------------------------
# discover_workspace_instructions
# ---------------------------------------------------------------------------


class TestDiscoverWorkspaceInstructions:
    """Tests for discover_workspace_instructions()."""

    def test_discovers_agents_md(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("# Agent instructions")
        result = discover_workspace_instructions(tmp_path)
        assert len(result) == 1
        assert result[0].name == "AGENTS.md"

    def test_discovers_all_convention_files(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("agents")
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "copilot-instructions.md").write_text("copilot")
        (tmp_path / "CLAUDE.md").write_text("claude")

        result = discover_workspace_instructions(tmp_path)
        assert len(result) == 3
        # Deterministic order matches CONVENTION_FILES
        assert [p.name for p in result] == [
            "AGENTS.md",
            "copilot-instructions.md",
            "CLAUDE.md",
        ]

    def test_discovers_files_in_parent_directory(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("root agents")
        sub = tmp_path / "src" / "pkg"
        sub.mkdir(parents=True)

        result = discover_workspace_instructions(sub)
        assert len(result) == 1
        assert result[0] == tmp_path / "AGENTS.md"

    def test_closest_file_wins(self, tmp_path: Path) -> None:
        """If AGENTS.md exists at multiple levels, closest to start_dir wins."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("root agents")
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("local agents")

        result = discover_workspace_instructions(sub)
        assert len(result) == 1
        assert result[0] == sub / "AGENTS.md"

    def test_stops_at_git_root(self, tmp_path: Path) -> None:
        """Discovery should not walk above the git root."""
        (tmp_path / "AGENTS.md").write_text("above git root")
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / ".git").mkdir()

        result = discover_workspace_instructions(repo)
        assert len(result) == 0

    def test_no_git_repo_only_checks_start_dir(self, tmp_path: Path) -> None:
        """Without .git, discovery stops at start_dir."""
        (tmp_path / "AGENTS.md").write_text("parent")
        sub = tmp_path / "child"
        sub.mkdir()

        result = discover_workspace_instructions(sub)
        assert len(result) == 0

    def test_no_files_found(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        result = discover_workspace_instructions(tmp_path)
        assert result == []

    def test_mixed_levels(self, tmp_path: Path) -> None:
        """AGENTS.md in sub, CLAUDE.md in root — both found."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "CLAUDE.md").write_text("claude root")
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("agents local")

        result = discover_workspace_instructions(sub)
        assert len(result) == 2
        names = [p.name for p in result]
        assert "AGENTS.md" in names
        assert "CLAUDE.md" in names


# ---------------------------------------------------------------------------
# load_instruction_files
# ---------------------------------------------------------------------------


class TestLoadInstructionFiles:
    """Tests for load_instruction_files()."""

    def test_loads_single_file(self, tmp_path: Path) -> None:
        f = tmp_path / "AGENTS.md"
        f.write_text("# Instructions\nDo stuff.")
        result = load_instruction_files([f])
        assert "# Instructions from: AGENTS.md" in result
        assert "Do stuff." in result

    def test_loads_multiple_files(self, tmp_path: Path) -> None:
        a = tmp_path / "AGENTS.md"
        a.write_text("agents content")
        c = tmp_path / "CLAUDE.md"
        c.write_text("claude content")

        result = load_instruction_files([a, c])
        assert "agents content" in result
        assert "claude content" in result
        assert "---" in result  # separator

    def test_skips_empty_files(self, tmp_path: Path) -> None:
        a = tmp_path / "AGENTS.md"
        a.write_text("   \n  ")  # whitespace only
        result = load_instruction_files([a])
        assert result == ""

    def test_skips_unreadable_files(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        missing = tmp_path / "nonexistent.md"
        with caplog.at_level(logging.WARNING):
            result = load_instruction_files([missing])
        assert result == ""
        assert "Failed to read" in caplog.text

    def test_warns_on_large_content(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "large.md"
        f.write_text("x" * (INSTRUCTION_SIZE_WARNING_THRESHOLD + 1))
        with caplog.at_level(logging.WARNING):
            load_instruction_files([f])
        assert "workspace instructions" in caplog.text.lower()

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        f = tmp_path / "AGENTS.md"
        f.write_text("  content with spaces  \n\n")
        result = load_instruction_files([f])
        assert "content with spaces" in result


# ---------------------------------------------------------------------------
# build_instructions_preamble
# ---------------------------------------------------------------------------


class TestBuildInstructionsPreamble:
    """Tests for build_instructions_preamble()."""

    def test_returns_none_with_no_sources(self) -> None:
        result = build_instructions_preamble()
        assert result is None

    def test_auto_discovery(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("discovered instructions")

        result = build_instructions_preamble(auto_discover_dir=tmp_path)
        assert result is not None
        assert "discovered instructions" in result
        assert "<workspace_instructions>" in result
        assert "</workspace_instructions>" in result

    def test_yaml_instructions(self) -> None:
        result = build_instructions_preamble(
            yaml_instructions=["Always use Python 3.12", "Follow PEP 8"]
        )
        assert result is not None
        assert "Python 3.12" in result
        assert "PEP 8" in result

    def test_yaml_instructions_skip_empty(self) -> None:
        result = build_instructions_preamble(yaml_instructions=["content", "  ", ""])
        assert result is not None
        assert "content" in result

    def test_cli_instructions(self, tmp_path: Path) -> None:
        f = tmp_path / "custom.md"
        f.write_text("custom instructions")

        result = build_instructions_preamble(cli_instruction_paths=[str(f)])
        assert result is not None
        assert "custom instructions" in result

    def test_cli_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            build_instructions_preamble(cli_instruction_paths=[str(tmp_path / "missing.md")])

    def test_combines_all_sources(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("auto-discovered")
        custom = tmp_path / "custom.md"
        custom.write_text("cli-provided")

        result = build_instructions_preamble(
            auto_discover_dir=tmp_path,
            yaml_instructions=["yaml-inline"],
            cli_instruction_paths=[str(custom)],
        )
        assert result is not None
        assert "auto-discovered" in result
        assert "yaml-inline" in result
        assert "cli-provided" in result

    def test_precedence_order(self, tmp_path: Path) -> None:
        """Auto-discovered → YAML → CLI ordering is preserved."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("FIRST")
        custom = tmp_path / "custom.md"
        custom.write_text("THIRD")

        result = build_instructions_preamble(
            auto_discover_dir=tmp_path,
            yaml_instructions=["SECOND"],
            cli_instruction_paths=[str(custom)],
        )
        assert result is not None
        first_idx = result.index("FIRST")
        second_idx = result.index("SECOND")
        third_idx = result.index("THIRD")
        assert first_idx < second_idx < third_idx

    def test_wraps_with_tags(self, tmp_path: Path) -> None:
        result = build_instructions_preamble(
            yaml_instructions=["test content"],
        )
        assert result is not None
        assert result.startswith("<workspace_instructions>")
        assert result.rstrip().endswith("</workspace_instructions>")

    def test_no_discovery_when_dir_is_none(self, tmp_path: Path) -> None:
        """auto_discover_dir=None should skip discovery entirely."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("should not appear")

        result = build_instructions_preamble(auto_discover_dir=None)
        assert result is None


# ---------------------------------------------------------------------------
# AgentExecutor integration
# ---------------------------------------------------------------------------


class TestAgentExecutorInstructionsPreamble:
    """Tests for instructions preamble integration in AgentExecutor."""

    def test_preamble_prepended_to_prompt(self) -> None:
        """Instructions preamble is prepended to the rendered prompt."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.config.schema import AgentDef
        from conductor.executor.agent import AgentExecutor
        from conductor.providers.base import AgentOutput

        provider = MagicMock()
        provider.execute = AsyncMock(
            return_value=AgentOutput(content={"result": "ok"}, raw_response='{"result":"ok"}')
        )

        preamble = "<workspace_instructions>\nFollow PEP 8\n</workspace_instructions>\n\n"
        executor = AgentExecutor(
            provider,
            instructions_preamble=preamble,
        )

        agent = AgentDef(name="test", prompt="Do the thing.")

        import asyncio

        asyncio.run(executor.execute(agent, {}))

        # Verify the prompt passed to provider includes the preamble
        call_args = provider.execute.call_args
        rendered = call_args.kwargs.get("rendered_prompt") or call_args[1].get("rendered_prompt")
        assert rendered.startswith("<workspace_instructions>")
        assert "Follow PEP 8" in rendered
        assert "Do the thing." in rendered

    def test_no_preamble_when_none(self) -> None:
        """Without preamble, prompt is rendered normally."""
        from unittest.mock import AsyncMock, MagicMock

        from conductor.config.schema import AgentDef
        from conductor.executor.agent import AgentExecutor
        from conductor.providers.base import AgentOutput

        provider = MagicMock()
        provider.execute = AsyncMock(
            return_value=AgentOutput(content={"result": "ok"}, raw_response='{"result":"ok"}')
        )

        executor = AgentExecutor(provider, instructions_preamble=None)
        agent = AgentDef(name="test", prompt="Do the thing.")

        import asyncio

        asyncio.run(executor.execute(agent, {}))

        call_args = provider.execute.call_args
        rendered = call_args.kwargs.get("rendered_prompt") or call_args[1].get("rendered_prompt")
        assert rendered == "Do the thing."

    def test_render_prompt_includes_preamble(self) -> None:
        """render_prompt() should include the preamble for dry-run consistency."""
        from unittest.mock import MagicMock

        from conductor.config.schema import AgentDef
        from conductor.executor.agent import AgentExecutor

        provider = MagicMock()
        executor = AgentExecutor(
            provider,
            instructions_preamble="PREAMBLE\n\n",
        )
        agent = AgentDef(name="test", prompt="Hello {{ name }}")
        result = executor.render_prompt(agent, {"name": "World"})
        assert result == "PREAMBLE\n\nHello World"

    def test_render_prompt_without_preamble(self) -> None:
        from unittest.mock import MagicMock

        from conductor.config.schema import AgentDef
        from conductor.executor.agent import AgentExecutor

        provider = MagicMock()
        executor = AgentExecutor(provider)
        agent = AgentDef(name="test", prompt="Hello {{ name }}")
        result = executor.render_prompt(agent, {"name": "World"})
        assert result == "Hello World"
