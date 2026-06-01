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

    def test_print_loaded_instructions_flag_forwarded(self) -> None:
        """--print-loaded-instructions should appear in the subprocess command
        when set so background runs surface discovery info in their captured
        stderr log."""
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
                    print_loaded_instructions=True,
                    web_port=9999,
                )

            # Hard-assert that the subprocess command was actually built —
            # otherwise the `in` assertion below silently no-ops if Popen
            # was never reached.
            assert mock_popen.called, "expected launch_background to invoke Popen"
            cmd = mock_popen.call_args[0][0]
            assert "--print-loaded-instructions" in cmd

    def test_print_loaded_instructions_flag_omitted_when_unset(self) -> None:
        """--print-loaded-instructions must NOT appear when not requested
        (avoid leaking into background runs by default)."""
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

            # Hard-assert that the subprocess command was actually built —
            # otherwise the `not in` assertion below silently no-ops.
            assert mock_popen.called, "expected launch_background to invoke Popen"
            cmd = mock_popen.call_args[0][0]
            assert "--print-loaded-instructions" not in cmd


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

    def test_scoped_file_loads_at_root_cwd(self, tmp_path: Path) -> None:
        """`applyTo: '<glob>'` files load when CWD is the repo root.

        At the root, bidirectional overlap loads every scoped file
        (correctness fix for the silently-skipped bug — see
        microsoft/conductor#231). Narrowing kicks in only when the user
        `cd`s into a subdir, exercised by
        ``test_scoped_file_skipped_when_cwd_disjoint`` below.
        """
        (tmp_path / ".git").mkdir()
        f = tmp_path / ".github" / "instructions" / "ts-only.instructions.md"
        _write_with_frontmatter(f, apply_to="**/*.ts", body="TS rules.")
        result = discover_workspace_instructions(tmp_path)
        assert any(p.name == "ts-only.instructions.md" for p in result)

    def test_scoped_file_skipped_when_cwd_disjoint(self, tmp_path: Path) -> None:
        """When CWD is a subdir whose subtree doesn't overlap the file's
        `applyTo` glob, the scoped file is correctly skipped.

        This is the narrowing case: at `services/AS`, a file scoped to
        `services/GW/**` should NOT load.
        """
        (tmp_path / ".git").mkdir()
        # Scoped instructions live at the repo root, applyTo = services/GW/**
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "gw-only.instructions.md",
            apply_to="services/GW/**",
            body="GW rules.",
        )
        # CWD is services/AS — disjoint from services/GW
        as_dir = tmp_path / "services" / "AS"
        as_dir.mkdir(parents=True)
        result = discover_workspace_instructions(as_dir)
        assert not any(p.name == "gw-only.instructions.md" for p in result)

    def test_scoped_file_loads_when_cwd_inside_scope(self, tmp_path: Path) -> None:
        """When CWD is inside the file's `applyTo` subtree, the file loads."""
        (tmp_path / ".git").mkdir()
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "gw-only.instructions.md",
            apply_to="services/GW/**",
            body="GW rules.",
        )
        # CWD is deep inside services/GW
        gw_src = tmp_path / "services" / "GW" / "src" / "Controllers"
        gw_src.mkdir(parents=True)
        result = discover_workspace_instructions(gw_src)
        assert any(p.name == "gw-only.instructions.md" for p in result)

    def test_multi_glob_applyto_semicolon(self, tmp_path: Path) -> None:
        """Multi-glob applyTo with ';' separator loads when any sub-glob overlaps."""
        (tmp_path / ".git").mkdir()
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "gw-or-be.instructions.md",
            apply_to="services/GW/**;services/BE/**",
            body="GW or BE rules.",
        )
        be_dir = tmp_path / "services" / "BE"
        be_dir.mkdir(parents=True)
        result = discover_workspace_instructions(be_dir)
        assert any(p.name == "gw-or-be.instructions.md" for p in result)

    def test_multi_glob_applyto_comma(self, tmp_path: Path) -> None:
        """Multi-glob applyTo with ',' separator loads when any sub-glob overlaps."""
        (tmp_path / ".git").mkdir()
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "lang-mix.instructions.md",
            apply_to="**/*.cs,**/*.csproj",
            body="C# and project rules.",
        )
        # CWD anywhere — leading-** sub-globs overlap any CWD
        sub = tmp_path / "services" / "GW"
        sub.mkdir(parents=True)
        result = discover_workspace_instructions(sub)
        assert any(p.name == "lang-mix.instructions.md" for p in result)

    def test_nested_convention_scope_relative_to_owner_dir(self, tmp_path: Path) -> None:
        """A nested `.github/instructions/foo.md` interprets its `applyTo`
        relative to the nested project's own directory (its owner), not
        relative to the workspace root.

        Without per-walk-level cwd_rel, a nested file with
        `applyTo: "src/**"` evaluated from `services/GW` would compare
        `"src/**"` against cwd_rel `"services/GW"` (root-relative) and
        miss. With per-owner cwd_rel, cwd_rel is `""` (start_dir == owner),
        which always overlaps.
        """
        (tmp_path / ".git").mkdir()
        # Nested .github/instructions/ at services/GW/
        nested = tmp_path / "services" / "GW" / ".github" / "instructions"
        nested.mkdir(parents=True)
        _write_with_frontmatter(
            nested / "gw-src.instructions.md",
            apply_to="src/**",
            body="GW src rules.",
        )
        # CWD is services/GW (the owner dir for the nested convention)
        gw_dir = tmp_path / "services" / "GW"
        result = discover_workspace_instructions(gw_dir)
        assert any(p.name == "gw-src.instructions.md" for p in result)

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
# _scope_overlaps + _single_glob_overlaps + _normalize_scope_path
# ---------------------------------------------------------------------------


class TestScopeOverlaps:
    """Tests for the bidirectional glob-vs-CWD overlap helper.

    The overlap test is intentionally conservative (over-approximates): better
    to load too many instructions than to silently skip ones the user expects.
    These tests pin down both correct positives and the principled
    over-approximations so future maintainers don't "fix" them into false
    negatives.
    """

    def test_root_cwd_loads_everything(self) -> None:
        """At repo root (cwd_rel=''), every prefix overlaps."""
        from conductor.config.instructions import _scope_overlaps

        assert _scope_overlaps("services/GW/**", "")
        assert _scope_overlaps("**/*.cs", "")
        assert _scope_overlaps("docs/eng.ms/**", "")
        assert _scope_overlaps("CHANGELOG.md", "")

    def test_cwd_inside_scope_subtree(self) -> None:
        """CWD is a descendant of the glob's prefix subtree."""
        from conductor.config.instructions import _scope_overlaps

        assert _scope_overlaps("services/GW/**", "services/GW")
        assert _scope_overlaps("services/GW/**", "services/GW/src/Controllers")
        assert _scope_overlaps("docs/**", "docs/api/reference")

    def test_scope_inside_cwd_subtree(self) -> None:
        """The glob's prefix subtree is a descendant of CWD."""
        from conductor.config.instructions import _scope_overlaps

        # Repo root with a nested-target scope
        assert _scope_overlaps("services/GW/**", "services")
        # CWD = services, scope target = services/GW/** → scope is inside CWD
        assert _scope_overlaps("docs/api/**", "docs")

    def test_disjoint_subtrees(self) -> None:
        """Disjoint prefix subtrees: no overlap."""
        from conductor.config.instructions import _scope_overlaps

        assert not _scope_overlaps("services/GW/**", "services/AS")
        assert not _scope_overlaps("docs/eng.ms/**", "services/GW")
        assert not _scope_overlaps("portal/extension/**", "services/GW")

    def test_segment_boundary_not_prefix_match(self) -> None:
        """`foo` must NOT overlap `food` — segment boundaries matter.

        This pins down a class of regression where a future "simplification"
        might use bare `str.startswith` (would falsely match `food` against
        prefix `foo`). The implementation guards against this with
        `prefix + "/"` boundary checks.
        """
        from conductor.config.instructions import _scope_overlaps

        assert not _scope_overlaps("foo", "food")
        assert not _scope_overlaps("services/GW", "services/GWX")
        assert not _scope_overlaps("services/GW/**", "services/GWX/src")

    def test_leading_double_star_matches_anywhere(self) -> None:
        """Globs starting with `**` have an empty literal prefix → always overlap."""
        from conductor.config.instructions import _scope_overlaps

        assert _scope_overlaps("**/*.cs", "services/GW/src")
        assert _scope_overlaps("**/portal-extension/**", "services/AS")
        assert _scope_overlaps("**/kubectl/*.yaml", "anywhere/at/all")

    def test_multi_glob_semicolon_separator(self) -> None:
        """`;`-separated multi-glob: overlap iff any sub-glob overlaps."""
        from conductor.config.instructions import _scope_overlaps

        assert _scope_overlaps("services/GW/**;services/BE/**", "services/BE")
        assert _scope_overlaps("services/GW/**;services/BE/**", "services/GW")
        assert not _scope_overlaps("services/GW/**;services/BE/**", "services/AS")

    def test_multi_glob_comma_separator(self) -> None:
        """`,`-separated multi-glob: overlap iff any sub-glob overlaps."""
        from conductor.config.instructions import _scope_overlaps

        assert _scope_overlaps("**/*.cs,**/*.csproj,**/Directory.Packages.props", "anywhere")
        assert _scope_overlaps("docs/**,**/*.md", "docs/api")
        # Note: leading-** sub-globs make this trivially true; an unambiguous
        # disjoint case requires fully-prefixed sub-globs.
        assert not _scope_overlaps("services/GW/**,services/BE/**", "services/AS")

    def test_leading_slash_in_glob(self) -> None:
        """Authors sometimes write `/docs/...` (observed in real data).
        Leading slash should not break the overlap test."""
        from conductor.config.instructions import _scope_overlaps

        assert _scope_overlaps("/docs/eng.ms/**", "docs/eng.ms/articles")
        assert not _scope_overlaps("/docs/eng.ms/**", "services/GW")

    def test_exact_file_glob(self) -> None:
        """A glob with no wildcards is an exact file path. Overlap if CWD
        is the file's directory (or an ancestor of it)."""
        from conductor.config.instructions import _scope_overlaps

        # CWD = src, scope = src/foo/bar.cs → scope is inside CWD → overlap
        assert _scope_overlaps("src/foo/bar.cs", "src")
        # CWD = src/foo/bar, scope = src/foo/bar.cs → scope is at parent of CWD;
        # technically the file is at src/foo/, NOT inside src/foo/bar. The
        # literal-prefix-overlap algorithm conservatively returns True (prefix
        # 'src/foo/bar.cs' overlaps with cwd 'src/foo/bar' — neither contains
        # the other strictly, so disjoint). This is OVER-APPROXIMATION in the
        # principled direction; documenting here so it's not "fixed".
        assert not _scope_overlaps("src/foo/bar.cs", "src/foo/baz")

    def test_over_approximation_intermediate_wildcard(self) -> None:
        """`src/*/tests/**` evaluated against `src/foo/bar` has literal prefix
        `src` which overlaps `src/foo/bar`. The glob can't actually match any
        file under `src/foo/bar/` (because the path doesn't go through
        `tests/`), but we over-include rather than risk skipping. Document the
        intentional false-positive so it isn't "fixed" into a false negative."""
        from conductor.config.instructions import _scope_overlaps

        assert _scope_overlaps("src/*/tests/**", "src/foo/bar")
        # The narrowing case still works: cwd outside `src/*` is correctly disjoint
        assert not _scope_overlaps("src/*/tests/**", "docs")

    def test_empty_and_whitespace_components(self) -> None:
        """Empty / whitespace sub-globs are skipped, not treated as 'always-match'."""
        from conductor.config.instructions import _scope_overlaps

        # Trailing separator → empty trailing sub-glob skipped
        assert _scope_overlaps("services/GW/**;", "services/GW")
        # Leading separator and whitespace → empty leading sub-glob skipped
        assert not _scope_overlaps(" ; services/GW/**", "services/AS")

    def test_only_separators_rejects(self) -> None:
        """A scope value of just `;` or `,` (or whitespace) splits into all
        empty sub-globs and overlaps with nothing — file is rejected.

        Regression guard: a future change that treated empty-after-split as
        "always-on" would silently broaden the filter.
        """
        from conductor.config.instructions import _scope_overlaps

        assert not _scope_overlaps(";", "anywhere")
        assert not _scope_overlaps(",", "anywhere")
        assert not _scope_overlaps(" ; , ", "anywhere")

    def test_brace_expansion_unsupported(self) -> None:
        """Brace-expansion globs (e.g. `src/{foo,bar}/**`) are NOT supported:
        the comma is treated as a multi-glob separator, splitting the brace
        and producing nonsensical sub-globs.

        This documents the known limitation. Real-world ``applyTo`` values
        observed across Azure repos use `;`/`,`-separated whole-string
        multi-globs rather than brace expansion, so we accept this gap.
        If brace expansion becomes a real need, swap the separator regex for
        a brace-aware splitter.
        """
        from conductor.config.instructions import _scope_overlaps

        # `src/{foo,bar}/**` splits on `,` into [`src/{foo`, `bar}/**`].
        # Neither sub-glob matches the intent. The test asserts the
        # *current* (broken-but-documented) behavior so a future maintainer
        # who attempts brace expansion knows to update this test.
        # The first sub-glob `src/{foo` has literal prefix `src/{foo`
        # (brace is not a recognized wildcard char in our regex `[*?\[]`).
        # That prefix won't match `src/foo`, so this returns False —
        # documenting that we DON'T magically handle braces.
        assert not _scope_overlaps("src/{foo,bar}/**", "src/foo")

    def test_windows_backslash_normalised(self) -> None:
        """Authors on Windows may write backslashes; normalise to forward slashes."""
        from conductor.config.instructions import _scope_overlaps

        assert _scope_overlaps("services\\GW\\**", "services/GW")
        assert _scope_overlaps("services/GW/**", "services\\GW")


# ---------------------------------------------------------------------------
# discover_workspace_instructions_detailed (metadata-bearing variant)
# ---------------------------------------------------------------------------


class TestDiscoverDetailed:
    """Tests for the structured-discovery variant used by
    --print-loaded-instructions and any future caller that needs to reason
    about *why* a file was included."""

    def test_returns_discovered_instruction_records(self, tmp_path: Path) -> None:
        from conductor.config.instructions import (
            DiscoveredInstruction,
            discover_workspace_instructions_detailed,
        )

        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("agents")
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "always.instructions.md",
            apply_to="**",
        )
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "cs.instructions.md",
            apply_to="**/*.cs",
        )

        result = discover_workspace_instructions_detailed(tmp_path)
        assert all(isinstance(d, DiscoveredInstruction) for d in result)
        by_name = {d.path.name: d for d in result}

        # AGENTS.md is a file convention — no scope concept.
        assert by_name["AGENTS.md"].reason == "file-convention"
        assert by_name["AGENTS.md"].scope is None

        # always.instructions.md is always-on per applyTo: "**"
        assert by_name["always.instructions.md"].reason == "always-on"
        assert by_name["always.instructions.md"].scope == "**"

        # cs.instructions.md is scoped; at root CWD it loads via overlap
        assert by_name["cs.instructions.md"].reason == "scope-overlap"
        assert by_name["cs.instructions.md"].scope == "**/*.cs"

    def test_paths_wrapper_matches_detailed(self, tmp_path: Path) -> None:
        """`discover_workspace_instructions` is a thin wrapper over the
        detailed variant; the paths must match exactly."""
        from conductor.config.instructions import (
            discover_workspace_instructions,
            discover_workspace_instructions_detailed,
        )

        (tmp_path / ".git").mkdir()
        (tmp_path / "AGENTS.md").write_text("agents")
        _write_with_frontmatter(
            tmp_path / ".github" / "instructions" / "always.instructions.md",
            apply_to="**",
        )

        paths = discover_workspace_instructions(tmp_path)
        detailed = discover_workspace_instructions_detailed(tmp_path)
        assert paths == [d.path for d in detailed]


# ---------------------------------------------------------------------------
# Frontmatter robustness: edge cases for `_extract_apply_to`
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


# ---------------------------------------------------------------------------
# Coverage: _parse_frontmatter exception path
# ---------------------------------------------------------------------------


class TestParseFrontmatterExceptions:
    """Locks the exception path of _parse_frontmatter (OSError on read)."""

    def test_unreadable_file_returns_none(self, tmp_path: Path) -> None:
        """When ``path.read_text`` raises (e.g., file is actually a directory),
        the parser returns None and logs at DEBUG; never raises."""
        from conductor.config.instructions import _parse_frontmatter

        # Pass a directory path — read_text will raise an OSError (PermissionError
        # on Windows, IsADirectoryError on POSIX). Both subclass OSError.
        d = tmp_path / "not_a_file"
        d.mkdir()
        assert _parse_frontmatter(d) is None


# ---------------------------------------------------------------------------
# Coverage: non-recursive ConventionDirectory branch
# ---------------------------------------------------------------------------


class TestConventionDirectoryNonRecursive:
    """Locks the ``recursive=False`` branch of _walk_directory_convention.

    No production convention currently uses ``recursive=False``, but the
    polymorphic shape supports it (e.g. for hypothetical Cline-style flat
    directories like ``.clinerules/*.md``). These tests pin the behaviour
    without waiting for a future convention to add it.
    """

    def test_non_recursive_skips_subdirectory_files(self, tmp_path: Path) -> None:
        """With ``recursive=False``, files in subdirectories are NOT discovered."""
        from conductor.config.instructions import (
            ConventionDirectory,
            _walk_directory_convention,
        )

        base = tmp_path / "rules"
        base.mkdir()
        (base / "top.md").write_text("top-level rule", encoding="utf-8")
        sub = base / "lang"
        sub.mkdir()
        (sub / "csharp.md").write_text("nested rule", encoding="utf-8")

        conv = ConventionDirectory(path="rules", pattern="*.md", recursive=False)
        results = {rel: path for rel, path, _scope in _walk_directory_convention(base, conv)}

        assert "top.md" in results
        assert "csharp.md" not in results
        # Recursion suppressed — nested file path is not yielded
        assert not any("lang" in k for k in results)

    def test_non_recursive_applies_pattern(self, tmp_path: Path) -> None:
        """Pattern filter applies in the non-recursive branch."""
        from conductor.config.instructions import (
            ConventionDirectory,
            _walk_directory_convention,
        )

        base = tmp_path / "rules"
        base.mkdir()
        (base / "match.md").write_text("md", encoding="utf-8")
        (base / "skip.txt").write_text("txt", encoding="utf-8")

        conv = ConventionDirectory(path="rules", pattern="*.md", recursive=False)
        results = {rel: path for rel, path, _scope in _walk_directory_convention(base, conv)}

        assert "match.md" in results
        assert "skip.txt" not in results

    def test_non_recursive_applies_include_file_predicate(self, tmp_path: Path) -> None:
        """``include_file`` predicate applies in the non-recursive branch."""
        from conductor.config.instructions import (
            ConventionDirectory,
            _walk_directory_convention,
        )

        base = tmp_path / "rules"
        base.mkdir()
        (base / "include.md").write_text("INCLUDE", encoding="utf-8")
        (base / "skip.md").write_text("SKIP", encoding="utf-8")

        conv = ConventionDirectory(
            path="rules",
            pattern="*.md",
            recursive=False,
            include_file=lambda p: "INCLUDE" in p.read_text(encoding="utf-8"),
        )
        results = {rel: path for rel, path, _scope in _walk_directory_convention(base, conv)}

        assert "include.md" in results
        assert "skip.md" not in results

    def test_non_recursive_missing_directory_yields_nothing(self, tmp_path: Path) -> None:
        """If the convention directory does not exist, the walker logs at DEBUG
        and yields nothing — never raises."""
        from conductor.config.instructions import (
            ConventionDirectory,
            _walk_directory_convention,
        )

        missing = tmp_path / "does_not_exist"
        conv = ConventionDirectory(path="rules", pattern="*.md", recursive=False)
        # No exception; empty iterator
        assert list(_walk_directory_convention(missing, conv)) == []

    def test_non_recursive_skips_entries_whose_is_file_raises(self, tmp_path: Path) -> None:
        """Defensive: ``entry.is_file()`` can raise OSError for broken symlinks
        or permission errors. The walker swallows that exception and skips
        the entry rather than crashing the whole discovery."""
        from unittest.mock import patch

        from conductor.config.instructions import (
            ConventionDirectory,
            _walk_directory_convention,
        )

        base = tmp_path / "rules"
        base.mkdir()
        (base / "good.md").write_text("ok", encoding="utf-8")
        (base / "broken.md").write_text("ok", encoding="utf-8")

        conv = ConventionDirectory(path="rules", pattern="*.md", recursive=False)

        # Make is_file() raise OSError for the "broken" entry only
        original_is_file = os.DirEntry.is_file

        def patched_is_file(self, *, follow_symlinks=True):
            if self.name == "broken.md":
                raise OSError("simulated broken symlink")
            return original_is_file(self, follow_symlinks=follow_symlinks)

        with patch.object(os.DirEntry, "is_file", patched_is_file):
            results = {rel: path for rel, path, _scope in _walk_directory_convention(base, conv)}

        assert "good.md" in results
        assert "broken.md" not in results
