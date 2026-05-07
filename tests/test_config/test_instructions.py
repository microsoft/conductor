"""Tests for workspace instruction file discovery and loading."""

from __future__ import annotations

import logging
import os
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
# Wrap / unwrap roundtrip
# ---------------------------------------------------------------------------


class TestWrapUnwrapPreamble:
    """Tests for _wrap_preamble and _unwrap_preamble roundtrip."""

    def test_roundtrip(self) -> None:
        from conductor.config.instructions import _unwrap_preamble, _wrap_preamble

        inner = "Follow PEP 8.\n\n---\n\nUse pytest for tests."
        wrapped = _wrap_preamble(inner)
        assert "<workspace_instructions>" in wrapped
        assert "</workspace_instructions>" in wrapped
        unwrapped = _unwrap_preamble(wrapped)
        assert unwrapped == inner

    def test_unwrap_passthrough(self) -> None:
        """Unwrapping a string without tags returns it stripped."""
        from conductor.config.instructions import _unwrap_preamble

        result = _unwrap_preamble("plain text")
        assert result == "plain text"


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


# ---------------------------------------------------------------------------
# Sub-workflow instruction merging
# ---------------------------------------------------------------------------


class TestSubWorkflowInstructionMerging:
    """Tests for sub-workflow instruction preamble merging in WorkflowEngine."""

    @pytest.mark.asyncio
    async def test_subworkflow_inherits_parent_preamble(self, tmp_path: Path) -> None:
        """Sub-workflow should inherit the parent's instructions preamble."""
        import textwrap

        from conductor.config.schema import (
            AgentDef,
            ContextConfig,
            LimitsConfig,
            RouteDef,
            RuntimeConfig,
            WorkflowConfig,
            WorkflowDef,
        )
        from conductor.engine.workflow import WorkflowEngine
        from conductor.providers.copilot import CopilotProvider

        # Create sub-workflow YAML (no instructions of its own)
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            textwrap.dedent("""\
            workflow:
              name: sub
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
            agents:
              - name: inner
                prompt: "Do inner work"
                routes:
                  - to: "$end"
            output:
              result: "{{ inner.output.result }}"
            """),
            encoding="utf-8",
        )

        parent_path = tmp_path / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="step",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="step",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ step.output.result }}"},
        )

        prompts_seen: list[str] = []

        def mock_handler(agent, prompt, context):
            prompts_seen.append(prompt)
            return {"result": "done"}

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(
            config,
            provider,
            workflow_path=parent_path,
            instructions_preamble="PARENT_PREAMBLE\n\n",
        )
        await engine.run({})

        # The inner agent's prompt should include the parent preamble
        assert len(prompts_seen) == 1
        assert "PARENT_PREAMBLE" in prompts_seen[0]

    @pytest.mark.asyncio
    async def test_subworkflow_merges_own_instructions(self, tmp_path: Path) -> None:
        """Sub-workflow with its own instructions field should merge with parent preamble.

        The merged result should contain a single <workspace_instructions> block,
        not nested tags.
        """
        import textwrap

        from conductor.config.instructions import _wrap_preamble
        from conductor.config.schema import (
            AgentDef,
            ContextConfig,
            LimitsConfig,
            RouteDef,
            RuntimeConfig,
            WorkflowConfig,
            WorkflowDef,
        )
        from conductor.engine.workflow import WorkflowEngine
        from conductor.providers.copilot import CopilotProvider

        # Create sub-workflow YAML with its own instructions
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            textwrap.dedent("""\
            workflow:
              name: sub
              entry_point: inner
              runtime:
                provider: copilot
              limits:
                max_iterations: 5
              instructions:
                - "SUB_INSTRUCTION"
            agents:
              - name: inner
                prompt: "Do inner work"
                routes:
                  - to: "$end"
            output:
              result: "{{ inner.output.result }}"
            """),
            encoding="utf-8",
        )

        parent_path = tmp_path / "parent.yaml"
        parent_path.write_text("dummy", encoding="utf-8")

        config = WorkflowConfig(
            workflow=WorkflowDef(
                name="parent",
                entry_point="step",
                runtime=RuntimeConfig(provider="copilot"),
                context=ContextConfig(mode="accumulate"),
                limits=LimitsConfig(max_iterations=10),
            ),
            agents=[
                AgentDef(
                    name="step",
                    type="workflow",
                    workflow="sub.yaml",
                    routes=[RouteDef(to="$end")],
                ),
            ],
            output={"result": "{{ step.output.result }}"},
        )

        prompts_seen: list[str] = []

        def mock_handler(agent, prompt, context):
            prompts_seen.append(prompt)
            return {"result": "done"}

        # Pass a properly wrapped preamble (as build_instructions_preamble would produce)
        parent_preamble = _wrap_preamble("PARENT_CONTENT")

        provider = CopilotProvider(mock_handler=mock_handler)
        engine = WorkflowEngine(
            config,
            provider,
            workflow_path=parent_path,
            instructions_preamble=parent_preamble,
        )
        await engine.run({})

        # Inner agent should see both parent and sub instruction content
        assert len(prompts_seen) == 1
        prompt = prompts_seen[0]
        assert "PARENT_CONTENT" in prompt
        assert "SUB_INSTRUCTION" in prompt

        # Critically: only ONE set of <workspace_instructions> tags (not nested)
        assert prompt.count("<workspace_instructions>") == 1
        assert prompt.count("</workspace_instructions>") == 1


# ---------------------------------------------------------------------------
# bg_runner flag forwarding
# ---------------------------------------------------------------------------


class TestBgRunnerInstructionFlags:
    """Tests for --workspace-instructions and --instructions forwarding in bg_runner."""

    def test_workspace_instructions_flag_forwarded(self) -> None:
        """--workspace-instructions should appear in the subprocess command."""
        import contextlib
        from unittest.mock import patch

        from conductor.cli.bg_runner import launch_background

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen") as mock_popen,
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
        ):
            mock_popen.return_value.pid = 12345

            with contextlib.suppress(Exception):
                launch_background(
                    workflow_path=Path("test.yaml"),
                    inputs={},
                    workspace_instructions=True,
                    web_port=9999,
                )

            if mock_popen.called:
                cmd = mock_popen.call_args[0][0]
                assert "--workspace-instructions" in cmd

    def test_cli_instructions_forwarded(self) -> None:
        """--instructions paths should appear in the subprocess command."""
        import contextlib
        from unittest.mock import patch

        from conductor.cli.bg_runner import launch_background

        with (
            patch("conductor.cli.bg_runner.subprocess.Popen") as mock_popen,
            patch("conductor.cli.bg_runner._wait_for_server", return_value=True),
        ):
            mock_popen.return_value.pid = 12345

            with contextlib.suppress(Exception):
                launch_background(
                    workflow_path=Path("test.yaml"),
                    inputs={},
                    cli_instructions=["AGENTS.md", "CLAUDE.md"],
                    web_port=9999,
                )

            if mock_popen.called:
                cmd = mock_popen.call_args[0][0]
                # Should have --instructions AGENTS.md --instructions CLAUDE.md
                instr_indices = [i for i, x in enumerate(cmd) if x == "--instructions"]
                assert len(instr_indices) == 2
                assert cmd[instr_indices[0] + 1] == "AGENTS.md"
                assert cmd[instr_indices[1] + 1] == "CLAUDE.md"


# ---------------------------------------------------------------------------
# Directory-convention discovery: .github/instructions/*.instructions.md
# ---------------------------------------------------------------------------


def _write_with_frontmatter(
    path: Path,
    *,
    apply_to: str | None = "**",
    body: str = "Coding rules.",
    description: str | None = None,
    no_frontmatter: bool = False,
    raw: str | None = None,
    encoding: str = "utf-8",
) -> None:
    """Helper: write a `.instructions.md` file with the given frontmatter setup.

    * ``raw`` — write bytes/string verbatim (overrides everything)
    * ``no_frontmatter`` — write ``body`` without any ``---`` block
    * ``apply_to=None`` — emit a frontmatter block with no ``applyTo`` key
    * ``apply_to="<glob>"`` — emit a frontmatter block with that ``applyTo`` value
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding=encoding)
        return
    if no_frontmatter:
        path.write_text(body, encoding=encoding)
        return
    lines = ["---"]
    if description is not None:
        lines.append(f"description: '{description}'")
    if apply_to is not None:
        lines.append(f"applyTo: '{apply_to}'")
    lines.append("---")
    lines.append(body)
    path.write_text("\n".join(lines) + "\n", encoding=encoding)


class TestDiscoverGithubInstructionsDir:
    """Tests for `.github/instructions/*.instructions.md` directory discovery."""

    def test_discovers_always_on_file(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "style.instructions.md"
        _write_with_frontmatter(f, apply_to="**", body="Use four-space indents.")
        result = discover_workspace_instructions(tmp_path)
        assert any(p.name == "style.instructions.md" for p in result)

    def test_skips_scoped_file(self, tmp_path: Path) -> None:
        """`applyTo: '<other glob>'` is scoped per the docs and SHOULD NOT be loaded."""
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "ts-only.instructions.md"
        _write_with_frontmatter(f, apply_to="**/*.ts", body="TS rules.")
        result = discover_workspace_instructions(tmp_path)
        assert not any(p.name == "ts-only.instructions.md" for p in result)

    def test_skips_no_frontmatter(self, tmp_path: Path) -> None:
        """Files without any frontmatter are 'manual-attach' per the docs → SKIP."""
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "plain.instructions.md"
        _write_with_frontmatter(f, no_frontmatter=True, body="Plain markdown.")
        result = discover_workspace_instructions(tmp_path)
        assert not any(p.name == "plain.instructions.md" for p in result)

    def test_skips_no_apply_to(self, tmp_path: Path) -> None:
        """Frontmatter present but no `applyTo` key → manual-attach default → SKIP."""
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "desc-only.instructions.md"
        _write_with_frontmatter(f, apply_to=None, description="Just description")
        result = discover_workspace_instructions(tmp_path)
        assert not any(p.name == "desc-only.instructions.md" for p in result)

    def test_recursive_subdirs(self, tmp_path: Path) -> None:
        """Files in nested subdirectories are discovered (recursive=True default)."""
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "lang" / "csharp.instructions.md"
        _write_with_frontmatter(f, apply_to="**", body="C# rules.")
        result = discover_workspace_instructions(tmp_path)
        assert any(p.parent.name == "lang" and p.name == "csharp.instructions.md" for p in result)

    def test_closest_wins_per_relative_path(self, tmp_path: Path) -> None:
        """When the same relative path exists at multiple levels, closest wins."""
        (tmp_path / ".git").mkdir()
        # root-level instructions
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "lang" / "csharp.instructions.md",
            apply_to="**",
            body="ROOT_CSHARP",
        )
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "style.instructions.md",
            apply_to="**",
            body="ROOT_STYLE",
        )
        # subproject-level overrides csharp only
        sub = tmp_path / "subproject"
        sub.mkdir()
        _write_with_frontmatter(
            sub / ".github" / "instructions" / "lang" / "csharp.instructions.md",
            apply_to="**",
            body="SUB_CSHARP",
        )

        result = discover_workspace_instructions(sub)
        # Build a {rel-path-within-dir: file} map for easier assertion
        by_rel = {}
        for p in result:
            # Find the .github/instructions ancestor
            parts = p.parts
            try:
                idx = parts.index("instructions")
            except ValueError:
                continue
            if idx >= 1 and parts[idx - 1] == ".github":
                rel = "/".join(parts[idx + 1 :])
                by_rel[rel] = p

        # Subproject's csharp wins; root's style is loaded since no override
        assert (
            by_rel["lang/csharp.instructions.md"]
            .read_text(encoding="utf-8")
            .endswith("SUB_CSHARP\n")
        )
        assert by_rel["style.instructions.md"].read_text(encoding="utf-8").endswith("ROOT_STYLE\n")

    def test_root_only_file_loads_when_subproject_missing(self, tmp_path: Path) -> None:
        """When the subproject has no `.github/instructions/`, root's files load."""
        (tmp_path / ".git").mkdir()
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "global.instructions.md",
            apply_to="**",
        )
        sub = tmp_path / "subproject"
        sub.mkdir()
        result = discover_workspace_instructions(sub)
        assert any(p.name == "global.instructions.md" for p in result)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Symlink creation requires elevation on Windows",
    )
    def test_symlinked_directory_not_traversed(self, tmp_path: Path) -> None:
        """Symlinked directories inside `.github/instructions/` are NOT traversed.

        Prevents symlink loops and out-of-tree expansion. Symlinked instruction
        FILES are still treated as regular files (different policy)."""
        (tmp_path / ".git").mkdir()
        instr_dir = tmp_path / ".github" / "instructions"
        instr_dir.mkdir(parents=True)
        # Real file directly under the convention dir
        _write_with_frontmatter(instr_dir / "real.instructions.md", apply_to="**")
        # Out-of-tree directory containing a tempting file
        outside = tmp_path / "outside"
        outside.mkdir()
        _write_with_frontmatter(outside / "leak.instructions.md", apply_to="**")
        # Symlink the outside dir into the convention dir
        os.symlink(outside, instr_dir / "outside_link", target_is_directory=True)

        result = discover_workspace_instructions(tmp_path)
        names = [p.name for p in result]
        assert "real.instructions.md" in names
        assert "leak.instructions.md" not in names

    def test_directory_and_file_conventions_coexist(self, tmp_path: Path) -> None:
        """File conventions (AGENTS.md) and directory conventions both load."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("agents content")
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "style.instructions.md",
            apply_to="**",
        )
        result = discover_workspace_instructions(tmp_path)
        names = [p.name for p in result]
        assert "AGENTS.md" in names
        assert "style.instructions.md" in names
        # File conventions appear first (declaration order in CONVENTIONS).
        assert names.index("AGENTS.md") < names.index("style.instructions.md")

    def test_pattern_must_match(self, tmp_path: Path) -> None:
        """Files in `.github/instructions/` that don't match the `*.instructions.md`
        pattern are NOT picked up (e.g. plain `.md`)."""
        (tmp_path / ".git").mkdir()
        # Wrong extension: .md, not .instructions.md
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "foo.md",
            apply_to="**",
            body="should not load",
        )
        result = discover_workspace_instructions(tmp_path)
        assert not any(p.name == "foo.md" for p in result)


# ---------------------------------------------------------------------------
# Frontmatter robustness: edge cases for `_is_always_on_instructions_file`
# ---------------------------------------------------------------------------


class TestFrontmatterRobustness:
    """Edge cases for the YAML frontmatter parser used by the directory convention."""

    def test_crlf_line_endings(self, tmp_path: Path) -> None:
        """Windows-authored files with CRLF are still parsed correctly."""
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "win.instructions.md"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"---\r\napplyTo: '**'\r\n---\r\nWindows file.\r\n")
        result = discover_workspace_instructions(tmp_path)
        assert any(p.name == "win.instructions.md" for p in result)

    def test_utf8_bom_handling(self, tmp_path: Path) -> None:
        """A leading UTF-8 BOM does not break frontmatter parsing."""
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "bom.instructions.md"
        f.parent.mkdir(parents=True)
        # BOM (\xef\xbb\xbf) followed by frontmatter
        f.write_bytes(b"\xef\xbb\xbf---\napplyTo: '**'\n---\nBOM file.\n")
        result = discover_workspace_instructions(tmp_path)
        assert any(p.name == "bom.instructions.md" for p in result)

    def test_malformed_yaml_skipped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed YAML in frontmatter logs a warning and skips, not crashes."""
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "bad.instructions.md"
        f.parent.mkdir(parents=True)
        # Invalid YAML: unclosed quote
        f.write_text("---\napplyTo: '**\n---\nBody.\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            result = discover_workspace_instructions(tmp_path)
        assert not any(p.name == "bad.instructions.md" for p in result)
        assert "Failed to parse frontmatter" in caplog.text

    def test_non_dict_yaml_skipped(self, tmp_path: Path) -> None:
        """Frontmatter that parses to a non-dict (list/scalar/empty) is skipped safely."""
        (tmp_path / ".git").mkdir()
        # Frontmatter that parses to a YAML list (not a dict)
        list_fm = tmp_path / ".github" / "instructions" / "list.instructions.md"
        list_fm.parent.mkdir(parents=True)
        list_fm.write_text("---\n- a\n- b\n---\nBody.\n", encoding="utf-8")
        # Frontmatter that parses to an empty doc
        empty_fm = tmp_path / ".github" / "instructions" / "empty.instructions.md"
        empty_fm.write_text("---\n\n---\nBody.\n", encoding="utf-8")
        # Frontmatter that parses to a scalar
        scalar_fm = tmp_path / ".github" / "instructions" / "scalar.instructions.md"
        scalar_fm.write_text("---\nhello\n---\nBody.\n", encoding="utf-8")

        result = discover_workspace_instructions(tmp_path)
        names = [p.name for p in result]
        assert "list.instructions.md" not in names
        assert "empty.instructions.md" not in names
        assert "scalar.instructions.md" not in names

    def test_closing_delimiter_at_eof(self, tmp_path: Path) -> None:
        """Frontmatter with closing `---` at EOF (no trailing newline) parses correctly.

        Some authors do not end files with a trailing newline. The tolerant
        regex handles both `\\Z` and `\\r?\\n` after the closing delimiter.
        """
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "eof.instructions.md"
        f.parent.mkdir(parents=True)
        # No body, no trailing newline
        f.write_text("---\napplyTo: '**'\n---", encoding="utf-8")
        result = discover_workspace_instructions(tmp_path)
        assert any(p.name == "eof.instructions.md" for p in result)


# ---------------------------------------------------------------------------
# Backward compatibility: CONVENTION_FILES alias
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """Locks the public-import contract for downstream consumers of
    ``CONVENTION_FILES`` predating the polymorphic refactor."""

    def test_convention_files_remains_importable(self) -> None:
        """`CONVENTION_FILES` was module-public before the refactor and must
        keep working for any downstream code that imports it directly."""
        from conductor.config.instructions import CONVENTION_FILES

        assert CONVENTION_FILES == [
            "AGENTS.md",
            ".github/copilot-instructions.md",
            "CLAUDE.md",
        ]

    def test_convention_files_projects_from_conventions(self) -> None:
        """`CONVENTION_FILES` must reflect every `ConventionFile` entry in
        `CONVENTIONS`, in the same order — so adding a new file convention
        automatically updates the alias."""
        from conductor.config.instructions import (
            CONVENTION_FILES,
            CONVENTIONS,
            ConventionFile,
        )

        expected = [c.path for c in CONVENTIONS if isinstance(c, ConventionFile)]
        assert expected == CONVENTION_FILES


# ---------------------------------------------------------------------------
# UTF-8 BOM handling in load_instruction_files
# ---------------------------------------------------------------------------


class TestLoadInstructionFilesBom:
    """Locks BOM handling in the reader path (not just the frontmatter parser)."""

    def test_bom_stripped_from_loaded_content(self, tmp_path: Path) -> None:
        """A BOM-authored instruction file must not leak `\\ufeff` into the
        prompt content emitted by `load_instruction_files()`.
        """
        from conductor.config.instructions import load_instruction_files

        f = tmp_path / "AGENTS.md"
        # BOM (\xef\xbb\xbf) followed by content
        f.write_bytes(b"\xef\xbb\xbfAgent rules.\n")
        result = load_instruction_files([f])
        assert "\ufeff" not in result
        assert "Agent rules." in result
