"""Tests for interrupt handler and CLI integration.

Tests for:
- --no-interactive CLI flag on run and resume commands
- Listener creation logic in run_workflow_async/resume_workflow_async
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

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
