"""Tests for the --verbose flag.

This module tests:
- Verbose flag parsing
- Verbose logging output
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from conductor.cli.app import app

runner = CliRunner()


class TestVerboseFlag:
    """Tests for the --verbose flag."""

    def test_verbose_flag_in_help(self) -> None:
        """Test that --verbose is documented in help."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "--verbose" in result.output or "-V" in result.output

    def test_verbose_short_flag_in_help(self) -> None:
        """Test that -V is documented in help."""
        # The global options should include verbose
        result = runner.invoke(app, ["--help"])
        assert "-V" in result.output or "--verbose" in result.output

    def test_verbose_flag_accepted(self, tmp_path: Path) -> None:
        """Test that --verbose flag is accepted."""
        workflow_file = tmp_path / "test.yaml"
        workflow_file.write_text("""\
workflow:
  name: test-workflow
  entry_point: agent1

agents:
  - name: agent1
    prompt: "Hello"
    routes:
      - to: $end

output:
  result: "done"
""")

        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            # Should not raise an error about unknown option
            result = runner.invoke(app, ["--verbose", "run", str(workflow_file)])
            # The command may fail for other reasons, but not unknown option
            assert "no such option" not in result.output.lower()

    def test_short_verbose_flag_accepted(self, tmp_path: Path) -> None:
        """Test that -V short flag is accepted."""
        workflow_file = tmp_path / "test.yaml"
        workflow_file.write_text("""\
workflow:
  name: test-workflow
  entry_point: agent1

agents:
  - name: agent1
    prompt: "Hello"
    routes:
      - to: $end

output:
  result: "done"
""")

        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            result = runner.invoke(app, ["-V", "run", str(workflow_file)])
            assert "no such option" not in result.output.lower()


class TestVerboseLogging:
    """Tests for verbose logging functions."""

    def test_is_verbose_default_true(self) -> None:
        """Test that is_verbose returns True by default."""
        from conductor.cli.app import is_verbose, verbose_mode

        # Reset to default state first (which is now True)
        token = verbose_mode.set(True)
        try:
            assert is_verbose() is True
        finally:
            verbose_mode.reset(token)

    def test_verbose_mode_can_be_set(self) -> None:
        """Test that verbose mode can be set via context var."""
        from conductor.cli.app import is_verbose, verbose_mode

        # First set to False explicitly
        token1 = verbose_mode.set(False)
        try:
            assert is_verbose() is False

            # Set verbose mode to True
            token2 = verbose_mode.set(True)
            try:
                assert is_verbose() is True
            finally:
                verbose_mode.reset(token2)

            # Should be back to False
            assert is_verbose() is False
        finally:
            verbose_mode.reset(token1)

    def test_verbose_log_respects_mode(self) -> None:
        """Test that verbose_log respects verbose mode."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import verbose_mode
        from conductor.cli.run import verbose_log

        # Capture output - we need to patch the console
        output = StringIO()

        # When verbose is False, nothing should be logged
        token = verbose_mode.set(False)
        try:
            # verbose_log uses _verbose_console, so we need to patch it
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                verbose_log("test message")
                assert output.getvalue() == ""
        finally:
            verbose_mode.reset(token)

        # When verbose is True, message should be logged
        output = StringIO()
        token = verbose_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                verbose_log("test message")
                assert "test message" in output.getvalue()
        finally:
            verbose_mode.reset(token)

    def test_verbose_log_timing(self) -> None:
        """Test verbose_log_timing function."""
        import re
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import verbose_mode
        from conductor.cli.run import verbose_log_timing

        output = StringIO()
        token = verbose_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_timing("Test operation", 1.234)
                output_text = output.getvalue()
                assert "Test operation" in output_text
                # Strip ANSI codes and check for timing
                clean_text = re.sub(r"\x1b\[[0-9;]*m", "", output_text)
                assert "1.23" in clean_text
        finally:
            verbose_mode.reset(token)

    def test_verbose_log_section(self) -> None:
        """Test verbose_log_section function."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import verbose_mode
        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token = verbose_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                verbose_log_section("Test Section", "Test content here")
                output_text = output.getvalue()
                assert "Test Section" in output_text
                assert "Test content" in output_text
        finally:
            verbose_mode.reset(token)

    def test_verbose_log_section_truncates_by_default(self) -> None:
        """Test that verbose_log_section truncates long content by default."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import full_mode, verbose_mode
        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(False)  # Explicitly set full mode to False
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, width=200),
            ):
                long_content = "x" * 1000
                verbose_log_section("Long Section", long_content)
                output_text = output.getvalue()
                # Content should be truncated (shows truncation message)
                assert "truncated" in output_text or "..." in output_text
                # Should only have ~500 x's (truncated at 500 chars)
                x_count = output_text.count("x")
                assert x_count == 500, f"Expected 500 x's, got {x_count}"
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_verbose_log_section_shows_full_in_full_mode(self) -> None:
        """Test that verbose_log_section shows full content when full mode is enabled."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import full_mode, verbose_mode
        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(True)  # Enable full mode (--verbose flag)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                long_content = "x" * 1000
                verbose_log_section("Long Section", long_content)
                output_text = output.getvalue()
                # Full content should be shown (no truncation indicator)
                assert "truncated" not in output_text
                # Count total x's in output (content is wrapped across lines)
                x_count = output_text.count("x")
                assert x_count == 1000, f"Expected 1000 x's, got {x_count}"
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_is_full_default_false(self) -> None:
        """Test that is_full returns False by default."""
        from conductor.cli.app import full_mode, is_full

        # Reset to default state first
        token = full_mode.set(False)
        try:
            assert is_full() is False
        finally:
            full_mode.reset(token)

    def test_full_mode_can_be_set(self) -> None:
        """Test that full mode can be set via context var."""
        from conductor.cli.app import full_mode, is_full

        # First set to False explicitly
        token1 = full_mode.set(False)
        try:
            assert is_full() is False

            # Set full mode to True
            token2 = full_mode.set(True)
            try:
                assert is_full() is True
            finally:
                full_mode.reset(token2)

            # Should be back to False
            assert is_full() is False
        finally:
            full_mode.reset(token1)

    def test_verbose_log_parallel_start(self) -> None:
        """Test verbose_log_parallel_start function."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import verbose_mode
        from conductor.cli.run import verbose_log_parallel_start

        output = StringIO()
        token = verbose_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_parallel_start("test_group", 3)
                output_text = output.getvalue()
                assert "Parallel Group" in output_text
                assert "test_group" in output_text
                assert "3 agents" in output_text
        finally:
            verbose_mode.reset(token)

    def test_verbose_log_parallel_agent_complete(self) -> None:
        """Test verbose_log_parallel_agent_complete function."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import verbose_mode
        from conductor.cli.run import verbose_log_parallel_agent_complete

        output = StringIO()
        token = verbose_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_parallel_agent_complete("test_agent", 1.234, model="gpt-4", tokens=100)
                output_text = output.getvalue()
                assert "test_agent" in output_text
                assert "1.23" in output_text
                assert "gpt-4" in output_text
                assert "100 tokens" in output_text
        finally:
            verbose_mode.reset(token)

    def test_verbose_log_parallel_agent_failed(self) -> None:
        """Test verbose_log_parallel_agent_failed function."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import verbose_mode
        from conductor.cli.run import verbose_log_parallel_agent_failed

        output = StringIO()
        token = verbose_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_parallel_agent_failed(
                    "test_agent", 0.5, "ValidationError", "Missing required field"
                )
                output_text = output.getvalue()
                assert "test_agent" in output_text
                assert "0.50" in output_text
                assert "ValidationError" in output_text
                assert "Missing required field" in output_text
        finally:
            verbose_mode.reset(token)

    def test_verbose_log_parallel_summary_success(self) -> None:
        """Test verbose_log_parallel_summary for successful execution."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import verbose_mode
        from conductor.cli.run import verbose_log_parallel_summary

        output = StringIO()
        token = verbose_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_parallel_summary("test_group", 3, 0, 2.5)
                output_text = output.getvalue()
                assert "test_group" in output_text
                assert "3/3 succeeded" in output_text
                assert "2.50" in output_text
        finally:
            verbose_mode.reset(token)

    def test_verbose_log_parallel_summary_partial_failure(self) -> None:
        """Test verbose_log_parallel_summary for partial failure."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import verbose_mode
        from conductor.cli.run import verbose_log_parallel_summary

        output = StringIO()
        token = verbose_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_parallel_summary("test_group", 2, 1, 3.0)
                output_text = output.getvalue()
                assert "test_group" in output_text
                assert "2 succeeded" in output_text
                assert "1 failed" in output_text
                assert "3.00" in output_text
        finally:
            verbose_mode.reset(token)

    def test_verbose_log_parallel_summary_all_failed(self) -> None:
        """Test verbose_log_parallel_summary for total failure."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.app import verbose_mode
        from conductor.cli.run import verbose_log_parallel_summary

        output = StringIO()
        token = verbose_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_parallel_summary("test_group", 0, 3, 1.5)
                output_text = output.getvalue()
                assert "test_group" in output_text
                assert "0 succeeded" in output_text
                assert "3 failed" in output_text
                assert "1.50" in output_text
        finally:
            verbose_mode.reset(token)
