"""Tests for interrupt handler and CLI integration.

Tests for:
- --no-interactive CLI flag on run and resume commands
- Listener creation logic in run_workflow_async/resume_workflow_async
- InterruptHandler UI: panel display, action selection, guidance, skip, stop, cancel
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from typer.testing import CliRunner

from conductor.cli.app import app
from conductor.exceptions import InterruptError
from conductor.gates.interrupt import (
    InterruptAction,
    InterruptHandler,
    InterruptResult,
)

runner = CliRunner()


def _write_workflow(tmp_path: Path) -> Path:
    """Create a minimal valid workflow file."""
    workflow_file = tmp_path / "test.yaml"
    workflow_file.write_text(
        "workflow:\n"
        "  name: test\n"
        "  entry_point: agent1\n"
        "agents:\n"
        "  - name: agent1\n"
        "    prompt: hello\n"
        "    routes:\n"
        "      - to: $end\n"
        "output:\n"
        "  result: '{{ agent1.output }}'\n"
    )
    return workflow_file


class TestNoInteractiveFlag:
    """Tests for --no-interactive CLI flag."""

    def test_run_accepts_no_interactive(self, tmp_path: pytest.TempPathFactory) -> None:
        """Verify --no-interactive is accepted on the run command."""
        # Create a minimal valid workflow file
        workflow_file = tmp_path / "test.yaml"  # type: ignore[operator]
        workflow_file.write_text(
            "workflow:\n"
            "  name: test\n"
            "  entry_point: agent1\n"
            "agents:\n"
            "  - name: agent1\n"
            "    prompt: hello\n"
        )

        # The command will fail because no provider is configured,
        # but it should NOT fail due to --no-interactive being unknown
        result = runner.invoke(
            app, ["run", str(workflow_file), "--no-interactive"], catch_exceptions=True
        )
        # Should not get "no such option" error
        assert "No such option" not in (result.output or "")

    def test_resume_accepts_no_interactive(self, tmp_path: pytest.TempPathFactory) -> None:
        """Verify --no-interactive is accepted on the resume command."""
        # Create a dummy workflow file
        workflow_file = tmp_path / "test.yaml"  # type: ignore[operator]
        workflow_file.write_text(
            "workflow:\n"
            "  name: test\n"
            "  entry_point: agent1\n"
            "agents:\n"
            "  - name: agent1\n"
            "    prompt: hello\n"
        )

        result = runner.invoke(
            app, ["resume", str(workflow_file), "--no-interactive"], catch_exceptions=True
        )
        # Should not get "no such option" error
        assert "No such option" not in (result.output or "")


class TestListenerCreation:
    """Tests for listener creation logic in run_workflow_async/resume_workflow_async.

    These tests call the real async functions with mocked dependencies to
    verify that the listener is created (or not) based on TTY state and
    --no-interactive flag.
    """

    @pytest.mark.asyncio
    async def test_no_listener_when_no_interactive(self, tmp_path: Path) -> None:
        """Verify no listener is created when --no-interactive is set."""
        from conductor.cli.run import run_workflow_async

        workflow_file = _write_workflow(tmp_path)

        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value={"result": "done"})
        mock_engine.config = MagicMock()
        mock_engine.config.workflow.cost.show_summary = False

        mock_registry = AsyncMock()
        mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
        mock_registry.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine) as mock_engine_cls,
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True

            await run_workflow_async(workflow_file, {}, no_interactive=True)

            # Engine should have been created with interrupt_event=None
            call_kwargs = mock_engine_cls.call_args
            assert call_kwargs[1]["interrupt_event"] is None

    @pytest.mark.asyncio
    async def test_no_listener_when_not_tty(self, tmp_path: Path) -> None:
        """Verify no listener is created when stdin is not a TTY."""
        from conductor.cli.run import run_workflow_async

        workflow_file = _write_workflow(tmp_path)

        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value={"result": "done"})
        mock_engine.config = MagicMock()
        mock_engine.config.workflow.cost.show_summary = False

        mock_registry = AsyncMock()
        mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
        mock_registry.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine) as mock_engine_cls,
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False

            await run_workflow_async(workflow_file, {}, no_interactive=False)

            # Engine should have been created with interrupt_event=None
            call_kwargs = mock_engine_cls.call_args
            assert call_kwargs[1]["interrupt_event"] is None

    @pytest.mark.asyncio
    async def test_listener_created_when_tty_and_interactive(self, tmp_path: Path) -> None:
        """Verify listener is created when stdin is TTY and interactive mode."""
        import asyncio

        from conductor.cli.run import run_workflow_async

        workflow_file = _write_workflow(tmp_path)

        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value={"result": "done"})
        mock_engine.config = MagicMock()
        mock_engine.config.workflow.cost.show_summary = False

        mock_registry = AsyncMock()
        mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
        mock_registry.__aexit__ = AsyncMock(return_value=False)

        mock_listener_instance = MagicMock()
        mock_listener_instance.start = AsyncMock()
        mock_listener_instance.stop = AsyncMock()

        with (
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine) as mock_engine_cls,
            patch("sys.stdin") as mock_stdin,
            patch(
                "conductor.interrupt.listener.KeyboardListener",
                return_value=mock_listener_instance,
            ) as mock_listener_cls,
        ):
            mock_stdin.isatty.return_value = True

            await run_workflow_async(workflow_file, {}, no_interactive=False)

            # Listener should have been created
            mock_listener_cls.assert_called_once()
            # And started + stopped
            mock_listener_instance.start.assert_called_once()
            mock_listener_instance.stop.assert_called_once()
            # Engine should have been created with a real asyncio.Event
            call_kwargs = mock_engine_cls.call_args
            assert isinstance(call_kwargs[1]["interrupt_event"], asyncio.Event)


class TestInterruptAction:
    """Tests for InterruptAction enum."""

    def test_action_values(self) -> None:
        """Verify all action enum values."""
        assert InterruptAction.CONTINUE == "continue_with_guidance"
        assert InterruptAction.SKIP == "skip_to_agent"
        assert InterruptAction.STOP == "stop"
        assert InterruptAction.CANCEL == "cancel"

    def test_action_is_string(self) -> None:
        """Verify actions are string enums for serialization."""
        assert isinstance(InterruptAction.CONTINUE, str)


class TestInterruptResult:
    """Tests for InterruptResult dataclass."""

    def test_defaults(self) -> None:
        """Verify default values for optional fields."""
        result = InterruptResult(action=InterruptAction.CANCEL)
        assert result.action == InterruptAction.CANCEL
        assert result.guidance is None
        assert result.skip_target is None

    def test_with_guidance(self) -> None:
        """Verify result with guidance text."""
        result = InterruptResult(
            action=InterruptAction.CONTINUE,
            guidance="Focus on Python 3",
        )
        assert result.guidance == "Focus on Python 3"

    def test_with_skip_target(self) -> None:
        """Verify result with skip target."""
        result = InterruptResult(
            action=InterruptAction.SKIP,
            skip_target="reviewer",
        )
        assert result.skip_target == "reviewer"


class TestInterruptError:
    """Tests for InterruptError exception."""

    def test_default_message(self) -> None:
        """Verify default error message."""
        err = InterruptError()
        assert "Workflow stopped by user interrupt" in str(err)

    def test_with_agent_name(self) -> None:
        """Verify agent_name is stored."""
        err = InterruptError(agent_name="summarizer")
        assert err.agent_name == "summarizer"

    def test_is_execution_error(self) -> None:
        """Verify InterruptError is a subclass of ExecutionError."""
        from conductor.exceptions import ExecutionError

        err = InterruptError()
        assert isinstance(err, ExecutionError)

    def test_custom_message(self) -> None:
        """Verify custom message is accepted."""
        err = InterruptError("Custom stop message", agent_name="agent1")
        assert "Custom stop message" in str(err)


class TestInterruptHandlerSkipGates:
    """Tests for InterruptHandler in skip_gates mode."""

    @pytest.mark.asyncio
    async def test_skip_gates_auto_cancels(self) -> None:
        """Verify skip_gates mode auto-selects cancel."""
        console = MagicMock()
        handler = InterruptHandler(console=console, skip_gates=True)

        result = await handler.handle_interrupt(
            current_agent="agent1",
            iteration=1,
            last_output_preview=None,
            available_agents=["agent1", "agent2"],
            accumulated_guidance=[],
        )

        assert result.action == InterruptAction.CANCEL
        # Verify the auto-cancel message was printed
        console.print.assert_called()
        printed_args = [str(call.args[0]) for call in console.print.call_args_list if call.args]
        assert any("Auto-cancelling" in arg for arg in printed_args)


class TestInterruptHandlerPanel:
    """Tests for InterruptHandler panel display."""

    @pytest.mark.asyncio
    async def test_panel_shows_agent_and_iteration(self) -> None:
        """Verify panel displays current agent and iteration."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        # Mock IntPrompt to select cancel (4)
        with patch.object(IntPrompt, "ask", return_value=4):
            await handler.handle_interrupt(
                current_agent="summarizer",
                iteration=5,
                last_output_preview=None,
                available_agents=["agent1"],
                accumulated_guidance=[],
            )

        # Find the Panel call
        panel_call = None
        for call in console.print.call_args_list:
            if call.args and isinstance(call.args[0], Panel):
                panel_call = call.args[0]
                break

        assert panel_call is not None
        panel_content = panel_call.renderable
        assert "summarizer" in panel_content
        assert "5" in panel_content

    @pytest.mark.asyncio
    async def test_panel_shows_output_preview(self) -> None:
        """Verify panel displays last output preview."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with patch.object(IntPrompt, "ask", return_value=4):
            await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview='{"summary": "Python is great"}',
                available_agents=[],
                accumulated_guidance=[],
            )

        panel_call = None
        for call in console.print.call_args_list:
            if call.args and isinstance(call.args[0], Panel):
                panel_call = call.args[0]
                break

        assert panel_call is not None
        assert "Python is great" in panel_call.renderable

    @pytest.mark.asyncio
    async def test_panel_truncates_long_output(self) -> None:
        """Verify output preview is truncated to 500 chars."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        long_output = "x" * 1000

        with patch.object(IntPrompt, "ask", return_value=4):
            await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=long_output,
                available_agents=[],
                accumulated_guidance=[],
            )

        panel_call = None
        for call in console.print.call_args_list:
            if call.args and isinstance(call.args[0], Panel):
                panel_call = call.args[0]
                break

        assert panel_call is not None
        # Should be truncated with "..."
        assert "..." in panel_call.renderable
        # Should not contain full 1000 chars
        assert "x" * 1000 not in panel_call.renderable

    @pytest.mark.asyncio
    async def test_panel_shows_accumulated_guidance(self) -> None:
        """Verify panel displays previously accumulated guidance."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with patch.object(IntPrompt, "ask", return_value=4):
            await handler.handle_interrupt(
                current_agent="agent1",
                iteration=2,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=["Focus on Python 3", "Use async patterns"],
            )

        panel_call = None
        for call in console.print.call_args_list:
            if call.args and isinstance(call.args[0], Panel):
                panel_call = call.args[0]
                break

        assert panel_call is not None
        assert "Focus on Python 3" in panel_call.renderable
        assert "Use async patterns" in panel_call.renderable

    @pytest.mark.asyncio
    async def test_panel_no_output_preview_when_none(self) -> None:
        """Verify panel omits output preview section when None."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with patch.object(IntPrompt, "ask", return_value=4):
            await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=[],
            )

        panel_call = None
        for call in console.print.call_args_list:
            if call.args and isinstance(call.args[0], Panel):
                panel_call = call.args[0]
                break

        assert panel_call is not None
        assert "Last Output Preview" not in panel_call.renderable

    @pytest.mark.asyncio
    async def test_panel_escapes_rich_markup_in_output_preview(self) -> None:
        """Verify Rich markup in output preview is escaped, not rendered."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with patch.object(IntPrompt, "ask", return_value=4):
            await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview='[red]error[/red] and [bold]text[/bold]',
                available_agents=[],
                accumulated_guidance=[],
            )

        panel_call = None
        for call in console.print.call_args_list:
            if call.args and isinstance(call.args[0], Panel):
                panel_call = call.args[0]
                break

        assert panel_call is not None
        content = panel_call.renderable
        # The raw markup tags should be escaped (rendered as literal text)
        from rich.markup import escape
        assert escape("[red]error[/red]") in content

    @pytest.mark.asyncio
    async def test_panel_escapes_rich_markup_in_guidance(self) -> None:
        """Verify Rich markup in accumulated guidance is escaped."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with patch.object(IntPrompt, "ask", return_value=4):
            await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=["[bold]inject markup[/bold]"],
            )

        panel_call = None
        for call in console.print.call_args_list:
            if call.args and isinstance(call.args[0], Panel):
                panel_call = call.args[0]
                break

        assert panel_call is not None
        content = panel_call.renderable
        from rich.markup import escape
        assert escape("[bold]inject markup[/bold]") in content


class TestInterruptHandlerActions:
    """Tests for InterruptHandler action selection flows."""

    @pytest.mark.asyncio
    async def test_cancel_action(self) -> None:
        """Verify cancel action returns CANCEL result."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with patch.object(IntPrompt, "ask", return_value=4):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.CANCEL
        assert result.guidance is None
        assert result.skip_target is None

    @pytest.mark.asyncio
    async def test_stop_action(self) -> None:
        """Verify stop action returns STOP result."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with patch.object(IntPrompt, "ask", return_value=3):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.STOP

    @pytest.mark.asyncio
    async def test_continue_with_guidance(self) -> None:
        """Verify continue action collects and returns guidance."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with (
            patch.object(IntPrompt, "ask", return_value=1),
            patch.object(Prompt, "ask", return_value="Focus on Python 3.12+"),
        ):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.CONTINUE
        assert result.guidance == "Focus on Python 3.12+"

    @pytest.mark.asyncio
    async def test_continue_with_empty_guidance_cancels(self) -> None:
        """Verify empty guidance text results in cancel."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with (
            patch.object(IntPrompt, "ask", return_value=1),
            patch.object(Prompt, "ask", return_value="   "),
        ):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.CANCEL

    @pytest.mark.asyncio
    async def test_continue_guidance_is_stripped(self) -> None:
        """Verify guidance with leading/trailing whitespace is stripped before storing."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with (
            patch.object(IntPrompt, "ask", return_value=1),
            patch.object(Prompt, "ask", return_value="  helpful note  "),
        ):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.CONTINUE
        assert result.guidance == "helpful note"

    @pytest.mark.asyncio
    async def test_skip_to_agent_by_name(self) -> None:
        """Verify skip action with agent name selection."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with (
            patch.object(IntPrompt, "ask", return_value=2),
            patch.object(Prompt, "ask", return_value="reviewer"),
        ):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=["researcher", "reviewer", "summarizer"],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.SKIP
        assert result.skip_target == "reviewer"

    @pytest.mark.asyncio
    async def test_skip_to_agent_by_number(self) -> None:
        """Verify skip action with numeric agent selection."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with (
            patch.object(IntPrompt, "ask", return_value=2),
            patch.object(Prompt, "ask", return_value="2"),
        ):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=["researcher", "reviewer", "summarizer"],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.SKIP
        assert result.skip_target == "reviewer"

    @pytest.mark.asyncio
    async def test_skip_invalid_then_valid(self) -> None:
        """Verify skip re-prompts on invalid agent name then accepts valid one."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        # First call to IntPrompt selects "skip" (2)
        # First Prompt.ask returns invalid name, second returns valid name
        with (
            patch.object(IntPrompt, "ask", return_value=2),
            patch.object(Prompt, "ask", side_effect=["nonexistent", "reviewer"]),
        ):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=["researcher", "reviewer"],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.SKIP
        assert result.skip_target == "reviewer"
        # Verify error message was printed for invalid name
        printed_args = [str(call.args[0]) for call in console.print.call_args_list if call.args]
        assert any("not found" in arg for arg in printed_args)

    @pytest.mark.asyncio
    async def test_skip_back_returns_to_menu(self) -> None:
        """Verify 'back' in skip selection returns to main menu."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        # First IntPrompt: skip (2), then Prompt returns 'back',
        # Second IntPrompt: cancel (4)
        int_prompt_calls = iter([2, 4])
        with (
            patch.object(IntPrompt, "ask", side_effect=int_prompt_calls),
            patch.object(Prompt, "ask", return_value="back"),
        ):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=["researcher", "reviewer"],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.CANCEL

    @pytest.mark.asyncio
    async def test_skip_no_available_agents(self) -> None:
        """Verify skip with no available agents returns to menu."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        # First IntPrompt: skip (2) — no agents available, back to menu
        # Second IntPrompt: cancel (4)
        int_prompt_calls = iter([2, 4])
        with patch.object(IntPrompt, "ask", side_effect=int_prompt_calls):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.CANCEL

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_during_action_cancels(self) -> None:
        """Verify KeyboardInterrupt during action selection returns cancel."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with patch.object(IntPrompt, "ask", side_effect=KeyboardInterrupt):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.CANCEL

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_during_guidance_cancels(self) -> None:
        """Verify KeyboardInterrupt during guidance input returns cancel."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        with (
            patch.object(IntPrompt, "ask", return_value=1),
            patch.object(Prompt, "ask", side_effect=KeyboardInterrupt),
        ):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=[],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.CANCEL

    @pytest.mark.asyncio
    async def test_eof_during_skip_returns_to_menu(self) -> None:
        """Verify EOFError during skip agent input returns to menu."""
        console = MagicMock()
        handler = InterruptHandler(console=console)

        # First IntPrompt: skip (2), Prompt raises EOFError
        # Second IntPrompt: cancel (4)
        int_prompt_calls = iter([2, 4])
        with (
            patch.object(IntPrompt, "ask", side_effect=int_prompt_calls),
            patch.object(Prompt, "ask", side_effect=EOFError),
        ):
            result = await handler.handle_interrupt(
                current_agent="agent1",
                iteration=1,
                last_output_preview=None,
                available_agents=["agent2"],
                accumulated_guidance=[],
            )

        assert result.action == InterruptAction.CANCEL
