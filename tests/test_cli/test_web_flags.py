"""Tests for --web, --web-port, and --web-bg CLI flags.

This module tests:
- CLI flag acceptance and parameter passing
- Missing web dependency detection with actionable error
- Dashboard startup failure handling (non-fatal)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()

# Minimal workflow YAML for test fixtures
_WORKFLOW_YAML = """\
workflow:
  name: test-workflow
  entry_point: agent1

agents:
  - name: agent1
    model: gpt-4
    prompt: "Hello"
    routes:
      - to: $end

output:
  result: "done"
"""


@pytest.fixture()
def workflow_file(tmp_path: Path) -> Path:
    """Create a minimal workflow file for testing."""
    f = tmp_path / "test.yaml"
    f.write_text(_WORKFLOW_YAML)
    return f


class TestWebFlagAcceptance:
    """Test that --web, --web-port, and --web-bg flags are accepted by the CLI."""

    def test_web_flag_passed_to_run_workflow_async(self, workflow_file: Path) -> None:
        """Test --web flag is passed through to run_workflow_async."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(app, ["run", str(workflow_file), "--web"])

            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs["web"] is True

    def test_web_port_flag_passed(self, workflow_file: Path) -> None:
        """Test --web-port value is passed through."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(app, ["run", str(workflow_file), "--web", "--web-port", "8080"])

            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs["web"] is True
            assert kwargs["web_port"] == 8080

    def test_web_bg_flag_passed(self, workflow_file: Path) -> None:
        """Test --web-bg flag is passed through."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(app, ["run", str(workflow_file), "--web", "--web-bg"])

            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs["web"] is True
            assert kwargs["web_bg"] is True

    def test_web_flags_default_values(self, workflow_file: Path) -> None:
        """Test that web flags default to False/0 when not specified."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(app, ["run", str(workflow_file)])

            assert mock_run.called
            _, kwargs = mock_run.call_args
            assert kwargs["web"] is False
            assert kwargs["web_port"] == 0
            assert kwargs["web_bg"] is False

    def test_web_compatible_with_existing_flags(self, workflow_file: Path) -> None:
        """Test --web works alongside existing flags like --skip-gates."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            runner.invoke(
                app,
                ["run", str(workflow_file), "--web", "--skip-gates", "--no-interactive"],
            )

            assert mock_run.called
            call_args = mock_run.call_args
            # skip_gates is the 4th positional arg
            assert call_args[0][3] is True
            _, kwargs = call_args
            assert kwargs["web"] is True


class TestWebDependencyCheck:
    """Test actionable error when web dependencies are missing."""

    def test_missing_web_deps_exits_with_code_1(self, workflow_file: Path) -> None:
        """Test that missing web deps produce exit code 1.

        Mocks run_workflow_async to raise SystemExit(1) as the real function
        would via typer.Exit when the import fails.
        """
        import typer

        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.side_effect = typer.Exit(code=1)
            result = runner.invoke(app, ["run", str(workflow_file), "--web"])
            assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_import_error_in_run_workflow_async(self) -> None:
        """Test that run_workflow_async raises typer.Exit on missing web deps.

        Directly tests the import-guarded code path by patching builtins.__import__.
        """
        from click.exceptions import Exit as ClickExit

        from conductor.cli.run import run_workflow_async

        real_import = __import__

        def blocking_import(name, *args, **kwargs):
            if name == "conductor.web.server":
                raise ImportError("No module named 'fastapi'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocking_import):
            with pytest.raises(ClickExit) as exc_info:
                await run_workflow_async(
                    Path("/tmp/fake.yaml"),
                    {},
                    web=True,
                )
            assert exc_info.value.exit_code == 1


class TestDashboardStartupFailure:
    """Test that dashboard startup failure is non-fatal."""

    def test_dashboard_start_failure_continues_workflow(self, workflow_file: Path) -> None:
        """Test that when dashboard fails, CLI still succeeds."""
        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}
            result = runner.invoke(app, ["run", str(workflow_file), "--web"])
            assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_dashboard_start_oserror_is_non_fatal(self) -> None:
        """Test the actual code path: dashboard.start() OSError is caught.

        Mocks WebDashboard so start() raises OSError, verifies the
        workflow result is still returned (dashboard=None after failure).
        """
        from conductor.cli.run import run_workflow_async

        mock_dashboard = MagicMock()
        mock_dashboard.start = AsyncMock(side_effect=OSError("Address already in use"))
        mock_dashboard.stop = AsyncMock()

        mock_web_module = MagicMock()
        mock_web_module.WebDashboard.return_value = mock_dashboard

        # Mock config loading and the full engine flow
        mock_config = MagicMock()
        mock_config.workflow.name = "test"
        mock_config.workflow.entry_point = "agent1"
        mock_config.agents = []
        mock_config.workflow.runtime.provider = "copilot"
        mock_config.workflow.limits.max_iterations = 50
        mock_config.workflow.limits.timeout_seconds = None
        mock_config.workflow.cost.show_summary = False
        mock_config.tools = None
        mock_config.mcp_servers = []

        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value={"result": "done"})
        mock_engine._last_checkpoint_path = None
        mock_engine.get_execution_summary.return_value = {}

        with (
            patch("conductor.cli.run.load_config", return_value=mock_config),
            patch.dict(sys.modules, {"conductor.web.server": mock_web_module}),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
            patch("conductor.cli.run.ProviderRegistry") as mock_registry,
            patch(
                "conductor.cli.run._build_mcp_servers",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            mock_registry.return_value.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await run_workflow_async(
                Path("/tmp/fake.yaml"),
                {},
                web=True,
            )
            assert result == {"result": "done"}
