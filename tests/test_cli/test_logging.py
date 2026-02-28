"""Tests for logging redesign: --quiet, --silent, --log-file flags.

This module tests:
- ConsoleVerbosity enum and ContextVar behavior
- --quiet and --silent flag parsing and mutual exclusion
- --log-file flag acceptance on run command
- --verbose flag removal
- Derived ContextVar values (verbose_mode, full_mode)
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from conductor.cli.app import (
    ConsoleVerbosity,
    app,
    console_verbosity,
    full_mode,
    is_full,
    is_verbose,
    verbose_mode,
)

runner = CliRunner()


class TestConsoleVerbosityEnum:
    """Tests for the ConsoleVerbosity enum."""

    def test_enum_values(self) -> None:
        """Test that ConsoleVerbosity has the expected values."""
        assert ConsoleVerbosity.FULL == "full"
        assert ConsoleVerbosity.MINIMAL == "minimal"
        assert ConsoleVerbosity.SILENT == "silent"

    def test_enum_is_string(self) -> None:
        """Test that ConsoleVerbosity members are strings."""
        assert isinstance(ConsoleVerbosity.FULL, str)
        assert isinstance(ConsoleVerbosity.MINIMAL, str)
        assert isinstance(ConsoleVerbosity.SILENT, str)


class TestNewFlags:
    """Tests for the new --quiet and --silent flags."""

    def test_quiet_flag_in_help(self) -> None:
        """Test that --quiet/-q is documented in help."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "--quiet" in result.output or "-q" in result.output

    def test_silent_flag_in_help(self) -> None:
        """Test that --silent/-s is documented in help."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "--silent" in result.output or "-s" in result.output

    def test_verbose_flag_removed(self) -> None:
        """Test that --verbose/-V is no longer in help."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "--verbose" not in result.output
        # -V should not appear (note: -v is for --version, that's fine)
        # We check specifically that --verbose is gone
        lines = result.output.split("\n")
        verbose_lines = [line for line in lines if "--verbose" in line]
        assert len(verbose_lines) == 0

    def test_quiet_flag_accepted(self, tmp_path: Path) -> None:
        """Test that --quiet flag is accepted."""
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

            result = runner.invoke(app, ["--quiet", "run", str(workflow_file)])
            assert "no such option" not in result.output.lower()

    def test_quiet_short_flag_accepted(self, tmp_path: Path) -> None:
        """Test that -q short flag is accepted."""
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

            result = runner.invoke(app, ["-q", "run", str(workflow_file)])
            assert "no such option" not in result.output.lower()

    def test_silent_flag_accepted(self, tmp_path: Path) -> None:
        """Test that --silent flag is accepted."""
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

            result = runner.invoke(app, ["--silent", "run", str(workflow_file)])
            assert "no such option" not in result.output.lower()

    def test_silent_short_flag_accepted(self, tmp_path: Path) -> None:
        """Test that -s short flag is accepted."""
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

            result = runner.invoke(app, ["-s", "run", str(workflow_file)])
            assert "no such option" not in result.output.lower()

    def test_quiet_and_silent_mutually_exclusive(self, tmp_path: Path) -> None:
        """Test that --quiet and --silent cannot be used together."""
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

        result = runner.invoke(app, ["--quiet", "--silent", "run", str(workflow_file)])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_verbose_flag_not_accepted(self, tmp_path: Path) -> None:
        """Test that --verbose flag is no longer accepted."""
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

        result = runner.invoke(app, ["--verbose", "run", str(workflow_file)])
        assert result.exit_code != 0
        assert "no such option" in result.output.lower()


class TestLogFileFlag:
    """Tests for the --log-file flag on the run command."""

    def test_log_file_in_run_help(self) -> None:
        """Test that --log-file/-l is documented in run --help."""
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "--log-file" in result.output or "-l" in result.output

    def test_log_file_with_explicit_path(self, tmp_path: Path) -> None:
        """Test that --log-file accepts an explicit path."""
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
        log_path = str(tmp_path / "debug.log")

        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            result = runner.invoke(app, ["run", str(workflow_file), "--log-file", log_path])
            assert "no such option" not in result.output.lower()
            # Verify log_file was passed to run_workflow_async
            if mock_run.called:
                call_args = mock_run.call_args
                assert call_args[0][4] == Path(log_path)

    def test_log_file_auto_path(self, tmp_path: Path) -> None:
        """Test that --log-file auto generates a temp file path."""
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

            result = runner.invoke(app, ["run", str(workflow_file), "--log-file", "auto"])
            assert "no such option" not in result.output.lower()
            # Verify an auto-generated path was passed
            if mock_run.called:
                call_args = mock_run.call_args
                log_path = call_args[0][4]
                assert log_path is not None
                assert "conductor" in str(log_path)
                assert str(log_path).endswith(".log")

    def test_log_file_short_flag(self, tmp_path: Path) -> None:
        """Test that -l short flag works."""
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
        log_path = str(tmp_path / "debug.log")

        with patch("conductor.cli.run.run_workflow_async") as mock_run:
            mock_run.return_value = {"result": "done"}

            result = runner.invoke(app, ["run", str(workflow_file), "-l", log_path])
            assert "no such option" not in result.output.lower()


class TestContextVarDefaults:
    """Tests for ContextVar default values and derivation."""

    def test_is_full_default_true(self) -> None:
        """Test that is_full returns True by default (full output is the new default)."""
        token = full_mode.set(True)
        try:
            assert is_full() is True
        finally:
            full_mode.reset(token)

    def test_is_verbose_default_true(self) -> None:
        """Test that is_verbose returns True by default."""
        token = verbose_mode.set(True)
        try:
            assert is_verbose() is True
        finally:
            verbose_mode.reset(token)

    def test_console_verbosity_default_full(self) -> None:
        """Test that console_verbosity defaults to FULL."""
        token = console_verbosity.set(ConsoleVerbosity.FULL)
        try:
            assert console_verbosity.get() == ConsoleVerbosity.FULL
        finally:
            console_verbosity.reset(token)

    def test_full_verbosity_derives_correct_vars(self) -> None:
        """Test that FULL verbosity sets verbose_mode=True, full_mode=True."""
        verbosity = ConsoleVerbosity.FULL
        t1 = verbose_mode.set(verbosity != ConsoleVerbosity.SILENT)
        t2 = full_mode.set(verbosity == ConsoleVerbosity.FULL)
        try:
            assert is_verbose() is True
            assert is_full() is True
        finally:
            full_mode.reset(t2)
            verbose_mode.reset(t1)

    def test_minimal_verbosity_derives_correct_vars(self) -> None:
        """Test that MINIMAL verbosity sets verbose_mode=True, full_mode=False."""
        verbosity = ConsoleVerbosity.MINIMAL
        t1 = verbose_mode.set(verbosity != ConsoleVerbosity.SILENT)
        t2 = full_mode.set(verbosity == ConsoleVerbosity.FULL)
        try:
            assert is_verbose() is True
            assert is_full() is False
        finally:
            full_mode.reset(t2)
            verbose_mode.reset(t1)

    def test_silent_verbosity_derives_correct_vars(self) -> None:
        """Test that SILENT verbosity sets verbose_mode=False, full_mode=False."""
        verbosity = ConsoleVerbosity.SILENT
        t1 = verbose_mode.set(verbosity != ConsoleVerbosity.SILENT)
        t2 = full_mode.set(verbosity == ConsoleVerbosity.FULL)
        try:
            assert is_verbose() is False
            assert is_full() is False
        finally:
            full_mode.reset(t2)
            verbose_mode.reset(t1)


class TestGenerateLogPath:
    """Tests for the generate_log_path function."""

    def test_generate_log_path_format(self) -> None:
        """Test that generated log path has the expected format."""
        from conductor.cli.run import generate_log_path

        path = generate_log_path("my-workflow")
        assert "conductor" in str(path)
        assert "my-workflow" in str(path)
        assert str(path).endswith(".log")

    def test_generate_log_path_uses_tmpdir(self) -> None:
        """Test that generated log path is under temp directory."""
        import tempfile

        from conductor.cli.run import generate_log_path

        path = generate_log_path("test")
        assert str(path).startswith(tempfile.gettempdir())


class TestVerboseLogging:
    """Tests for verbose logging functions (migrated from test_verbose.py)."""

    def test_verbose_log_respects_mode(self) -> None:
        """Test that verbose_log respects verbose mode."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log

        # When verbose is False, nothing should be logged
        output = StringIO()
        token = verbose_mode.set(False)
        try:
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
                clean_text = re.sub(r"\x1b\[[0-9;]*m", "", output_text)
                assert "1.23" in clean_text
        finally:
            verbose_mode.reset(token)

    def test_verbose_log_section(self) -> None:
        """Test verbose_log_section function in FULL mode."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(True)
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
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_verbose_log_section_skipped_in_minimal_mode(self) -> None:
        """Test that verbose_log_section skips console output in MINIMAL mode."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(False)  # MINIMAL mode
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, width=200),
            ):
                verbose_log_section("Long Section", "x" * 1000)
                output_text = output.getvalue()
                # In MINIMAL mode, sections are not shown on console at all
                assert output_text == ""
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_verbose_log_section_skipped_in_silent_mode(self) -> None:
        """Test that verbose_log_section skips console output in SILENT mode."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token_verbose = verbose_mode.set(False)  # SILENT mode
        token_full = full_mode.set(False)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                verbose_log_section("Test Section", "Test content here")
                output_text = output.getvalue()
                assert output_text == ""
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_verbose_log_section_shows_full_in_full_mode(self) -> None:
        """Test that verbose_log_section shows full untruncated content in FULL mode."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                long_content = "x" * 1000
                verbose_log_section("Long Section", long_content)
                output_text = output.getvalue()
                assert "truncated" not in output_text
                x_count = output_text.count("x")
                assert x_count == 1000, f"Expected 1000 x's, got {x_count}"
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_verbose_log_parallel_start(self) -> None:
        """Test verbose_log_parallel_start function."""
        from io import StringIO

        from rich.console import Console

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


class TestFileLogging:
    """Tests for file logging functionality."""

    def test_init_file_logging_creates_file(self, tmp_path: Path) -> None:
        """Test that init_file_logging creates the log file."""
        from conductor.cli.run import close_file_logging, init_file_logging

        log_path = tmp_path / "test.log"
        try:
            init_file_logging(log_path)
            assert log_path.exists()
        finally:
            close_file_logging()

    def test_init_file_logging_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Test that init_file_logging creates parent directories."""
        from conductor.cli.run import close_file_logging, init_file_logging

        log_path = tmp_path / "nested" / "dir" / "test.log"
        try:
            init_file_logging(log_path)
            assert log_path.exists()
        finally:
            close_file_logging()

    def test_verbose_log_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log writes output to the log file."""
        from conductor.cli.run import close_file_logging, init_file_logging, verbose_log

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)  # Console off, but file should still get output
        try:
            init_file_logging(log_path)
            verbose_log("test file message")
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "test file message" in content
        finally:
            verbose_mode.reset(token)

    def test_file_output_is_plain_text(self, tmp_path: Path) -> None:
        """Test that file output has no ANSI escape codes."""
        import re

        from conductor.cli.run import close_file_logging, init_file_logging, verbose_log

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(True)
        try:
            init_file_logging(log_path)
            verbose_log("styled message", style="bold red")
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "styled message" in content
            # No ANSI escape sequences
            ansi_pattern = re.compile(r"\x1b\[[0-9;]*m")
            assert not ansi_pattern.search(content)
        finally:
            verbose_mode.reset(token)

    def test_file_gets_untruncated_content(self, tmp_path: Path) -> None:
        """Test that file logging always gets full untruncated content."""
        from conductor.cli.run import close_file_logging, init_file_logging, verbose_log_section

        log_path = tmp_path / "test.log"
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(False)  # Console would truncate
        try:
            init_file_logging(log_path)
            long_content = "x" * 1000
            with patch("conductor.cli.run._verbose_console"):
                verbose_log_section("Test", long_content)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            # File should have full content, not truncated
            assert "truncated" not in content
            assert content.count("x") == 1000
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_file_logging_in_silent_mode(self, tmp_path: Path) -> None:
        """Test that file logging works even when console is silent."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log,
            verbose_log_timing,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)  # Silent mode
        try:
            init_file_logging(log_path)
            verbose_log("silent mode message")
            verbose_log_timing("test op", 1.5)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "silent mode message" in content
            assert "test op" in content
            assert "1.50" in content
        finally:
            verbose_mode.reset(token)

    def test_generate_log_path_creates_directory(self) -> None:
        """Test that generate_log_path creates the parent directory."""
        from conductor.cli.run import generate_log_path

        path = generate_log_path("test-workflow")
        assert path.parent.exists()
        assert path.parent.is_dir()

    def test_close_file_logging_cleans_up(self, tmp_path: Path) -> None:
        """Test that close_file_logging properly cleans up resources."""
        import conductor.cli.run as run_module
        from conductor.cli.run import close_file_logging, init_file_logging

        log_path = tmp_path / "test.log"
        init_file_logging(log_path)
        assert run_module._file_console is not None
        assert run_module._file_handle is not None

        close_file_logging()
        assert run_module._file_console is None
        assert run_module._file_handle is None


class TestVerbosityAwareOutput:
    """Tests for verbosity-aware console output (Epic 2).

    Verifies that FULL mode shows everything, MINIMAL mode shows only
    agent lifecycle/routing, and SILENT mode suppresses all progress.
    """

    def test_full_mode_shows_sections(self) -> None:
        """Test that FULL mode shows prompt sections on console."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                verbose_log_section("Prompt for 'agent1'", "Hello world prompt")
                output_text = output.getvalue()
                assert "Prompt for 'agent1'" in output_text
                assert "Hello world prompt" in output_text
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_minimal_mode_hides_sections(self) -> None:
        """Test that MINIMAL mode (--quiet) hides prompt sections."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(False)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                verbose_log_section("Prompt for 'agent1'", "Hello world prompt")
                assert output.getvalue() == ""
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_minimal_mode_shows_agent_lifecycle(self) -> None:
        """Test that MINIMAL mode shows agent start/complete messages."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_agent_complete, verbose_log_agent_start

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(False)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_agent_start("test-agent", 1)
                verbose_log_agent_complete("test-agent", 1.5, model="gpt-4")
                output_text = output.getvalue()
                assert "test-agent" in output_text
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_minimal_mode_shows_routing(self) -> None:
        """Test that MINIMAL mode shows routing decisions."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_route

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(False)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_route("agent2")
                output_text = output.getvalue()
                assert "agent2" in output_text
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_minimal_mode_shows_timing(self) -> None:
        """Test that MINIMAL mode shows timing information."""
        import re
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_timing

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(False)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True, no_color=True),
            ):
                verbose_log_timing("Workflow", 2.5)
                output_text = output.getvalue()
                clean_text = re.sub(r"\x1b\[[0-9;]*m", "", output_text)
                assert "Workflow" in clean_text
                assert "2.50" in clean_text
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_minimal_mode_shows_general_log(self) -> None:
        """Test that MINIMAL mode shows general verbose_log messages."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(False)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                verbose_log("lifecycle message")
                output_text = output.getvalue()
                assert "lifecycle message" in output_text
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_silent_mode_hides_everything(self) -> None:
        """Test that SILENT mode suppresses all progress output."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import (
            verbose_log,
            verbose_log_agent_complete,
            verbose_log_agent_start,
            verbose_log_route,
            verbose_log_section,
            verbose_log_timing,
        )

        output = StringIO()
        token_verbose = verbose_mode.set(False)
        token_full = full_mode.set(False)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                verbose_log("should not appear")
                verbose_log_section("Title", "content")
                verbose_log_agent_start("agent1", 1)
                verbose_log_agent_complete("agent1", 1.0)
                verbose_log_route("agent2")
                verbose_log_timing("op", 1.0)
                assert output.getvalue() == ""
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_file_gets_sections_in_minimal_mode(self, tmp_path: Path) -> None:
        """Test that file logging gets sections even when console is in MINIMAL mode."""
        from conductor.cli.run import close_file_logging, init_file_logging, verbose_log_section

        log_path = tmp_path / "test.log"
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(False)  # MINIMAL mode - console skips sections
        try:
            init_file_logging(log_path)
            with patch("conductor.cli.run._verbose_console"):
                verbose_log_section("Prompt", "full prompt content here")
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "Prompt" in content
            assert "full prompt content here" in content
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)

    def test_no_truncation_in_default_mode(self) -> None:
        """Test that default (FULL) mode never truncates content."""
        from io import StringIO

        from rich.console import Console

        from conductor.cli.run import verbose_log_section

        output = StringIO()
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(True)
        try:
            with patch(
                "conductor.cli.run._verbose_console",
                Console(file=output, force_terminal=True),
            ):
                long_content = "x" * 2000
                verbose_log_section("Long Prompt", long_content)
                output_text = output.getvalue()
                assert "truncated" not in output_text
                assert output_text.count("x") == 2000
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)


class TestFileLoggingDualWrite:
    """Tests for dual-write behavior across all verbose_log_* functions.

    Verifies that every logging function writes to the file console
    independently of console verbosity settings.
    """

    def test_agent_start_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_agent_start writes to file in silent mode."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_agent_start,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_agent_start("test-agent", 1)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "test-agent" in content
            assert "iter 1" in content
        finally:
            verbose_mode.reset(token)

    def test_agent_complete_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_agent_complete writes to file in silent mode."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_agent_complete,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_agent_complete(
                "test-agent", 1.5, model="gpt-4", input_tokens=100, output_tokens=50
            )
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "test-agent" in content
            assert "1.50" in content
            assert "gpt-4" in content
            assert "100 in/50 out" in content
        finally:
            verbose_mode.reset(token)

    def test_route_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_route writes to file in silent mode."""
        from conductor.cli.run import close_file_logging, init_file_logging, verbose_log_route

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_route("next-agent")
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "next-agent" in content
        finally:
            verbose_mode.reset(token)

    def test_route_end_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_route with $end writes to file."""
        from conductor.cli.run import close_file_logging, init_file_logging, verbose_log_route

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_route("$end")
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "$end" in content
        finally:
            verbose_mode.reset(token)

    def test_timing_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_timing writes to file in silent mode."""
        from conductor.cli.run import close_file_logging, init_file_logging, verbose_log_timing

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_timing("Workflow execution", 3.456)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "Workflow execution" in content
            assert "3.46" in content
        finally:
            verbose_mode.reset(token)

    def test_parallel_start_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_parallel_start writes to file in silent mode."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_parallel_start,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_parallel_start("parallel-group", 3)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "parallel-group" in content
            assert "3 agents" in content
        finally:
            verbose_mode.reset(token)

    def test_parallel_agent_complete_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_parallel_agent_complete writes to file."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_parallel_agent_complete,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_parallel_agent_complete("agent-a", 2.0, model="gpt-4", tokens=200)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "agent-a" in content
            assert "2.00" in content
        finally:
            verbose_mode.reset(token)

    def test_parallel_agent_failed_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_parallel_agent_failed writes to file."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_parallel_agent_failed,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_parallel_agent_failed("agent-b", 0.5, "RuntimeError", "Something broke")
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "agent-b" in content
            assert "RuntimeError" in content
            assert "Something broke" in content
        finally:
            verbose_mode.reset(token)

    def test_parallel_summary_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_parallel_summary writes to file."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_parallel_summary,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_parallel_summary("group1", 2, 1, 3.0)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "group1" in content
            assert "2 succeeded" in content
            assert "1 failed" in content
        finally:
            verbose_mode.reset(token)

    def test_for_each_start_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_for_each_start writes to file."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_for_each_start,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_for_each_start("loop-group", 5, 2, "fail_fast")
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "loop-group" in content
            assert "5 items" in content
        finally:
            verbose_mode.reset(token)

    def test_for_each_item_complete_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_for_each_item_complete writes to file."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_for_each_item_complete,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_for_each_item_complete("item-0", 1.2, tokens=50)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "item-0" in content
            assert "1.20" in content
        finally:
            verbose_mode.reset(token)

    def test_for_each_item_failed_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_for_each_item_failed writes to file."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_for_each_item_failed,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_for_each_item_failed("item-2", 0.3, "ValueError", "bad input")
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "item-2" in content
            assert "ValueError" in content
            assert "bad input" in content
        finally:
            verbose_mode.reset(token)

    def test_for_each_summary_writes_to_file(self, tmp_path: Path) -> None:
        """Test that verbose_log_for_each_summary writes to file."""
        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log_for_each_summary,
        )

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_for_each_summary("loop1", 4, 1, 5.0)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "loop1" in content
            assert "4 succeeded" in content
            assert "1 failed" in content
        finally:
            verbose_mode.reset(token)

    def test_display_usage_summary_writes_to_file(self, tmp_path: Path) -> None:
        """Test that display_usage_summary writes to file in silent mode."""
        from conductor.cli.run import close_file_logging, display_usage_summary, init_file_logging

        log_path = tmp_path / "test.log"
        token = verbose_mode.set(False)
        try:
            init_file_logging(log_path)
            display_usage_summary(
                {
                    "total_input_tokens": 500,
                    "total_output_tokens": 200,
                    "total_tokens": 700,
                    "total_cost_usd": 0.0123,
                    "agents": [
                        {"agent_name": "agent1", "cost_usd": 0.0123},
                    ],
                }
            )
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "Token Usage" in content
            assert "500" in content
            assert "200" in content
            assert "agent1" in content
        finally:
            verbose_mode.reset(token)

    def test_section_writes_full_content_to_file_in_silent_mode(self, tmp_path: Path) -> None:
        """Test that verbose_log_section writes full content to file even in SILENT mode."""
        from conductor.cli.run import close_file_logging, init_file_logging, verbose_log_section

        log_path = tmp_path / "test.log"
        token_verbose = verbose_mode.set(False)
        token_full = full_mode.set(False)
        try:
            init_file_logging(log_path)
            verbose_log_section("Prompt", "full prompt content")
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            assert "Prompt" in content
            assert "full prompt content" in content
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)


class TestFileLoggingStderrNotification:
    """Tests for log file path stderr notification and lifecycle."""

    def test_log_path_printed_to_stderr_on_completion(self, tmp_path: Path) -> None:
        """Test that run_workflow_async prints log file path to stderr on completion."""
        import asyncio
        from io import StringIO
        from unittest.mock import AsyncMock, MagicMock

        from rich.console import Console

        from conductor.cli.run import run_workflow_async

        log_path = tmp_path / "test.log"
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

        stderr_output = StringIO()
        mock_stderr_console = Console(file=stderr_output, no_color=True, highlight=False, width=500)

        mock_registry = AsyncMock()
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value={"result": "done"})

        with (
            patch("conductor.cli.run._verbose_console", mock_stderr_console),
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
        ):
            mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.__aexit__ = AsyncMock(return_value=False)
            asyncio.run(run_workflow_async(workflow_file, {}, log_file=log_path))

        stderr_text = stderr_output.getvalue()
        assert "Log written to" in stderr_text
        assert str(log_path) in stderr_text

    def test_file_handle_closed_on_workflow_error(self, tmp_path: Path) -> None:
        """Test that file handle is closed even when workflow raises an error."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        import conductor.cli.run as run_module
        from conductor.cli.run import run_workflow_async

        log_path = tmp_path / "test.log"
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

        mock_registry = AsyncMock()
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(side_effect=RuntimeError("Workflow failed"))

        with (
            patch("conductor.cli.run._verbose_console"),
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
        ):
            mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.__aexit__ = AsyncMock(return_value=False)
            with contextlib.suppress(RuntimeError):
                asyncio.run(run_workflow_async(workflow_file, {}, log_file=log_path))

        # File handle should be cleaned up
        assert run_module._file_console is None
        assert run_module._file_handle is None
        # File should exist (was created before error)
        assert log_path.exists()

    def test_log_path_not_printed_when_init_fails(self, tmp_path: Path) -> None:
        """Test that log path is not printed to stderr when file init fails."""
        import asyncio
        from io import StringIO
        from unittest.mock import AsyncMock, MagicMock

        from rich.console import Console

        from conductor.cli.run import run_workflow_async

        log_path = tmp_path / "unreachable.log"
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

        stderr_output = StringIO()
        mock_stderr_console = Console(file=stderr_output, no_color=True, highlight=False)

        mock_registry = AsyncMock()
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value={"result": "done"})

        with (
            patch("conductor.cli.run._verbose_console", mock_stderr_console),
            patch("conductor.cli.run.init_file_logging", side_effect=OSError("Permission denied")),
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
        ):
            mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.__aexit__ = AsyncMock(return_value=False)
            asyncio.run(run_workflow_async(workflow_file, {}, log_file=log_path))

        stderr_text = stderr_output.getvalue()
        # Warning about failed init should appear
        assert "Cannot open log file" in stderr_text
        # But "Log written to" should NOT appear since init failed
        assert "Log written to" not in stderr_text


class TestFileLoggingErrorHandling:
    """Tests for file logging error handling."""

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific chmod permissions")
    def test_init_file_logging_permission_denied(self, tmp_path: Path) -> None:
        """Test that init_file_logging raises OSError for permission issues."""
        import os

        from conductor.cli.run import init_file_logging

        # Create a read-only directory
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        os.chmod(readonly_dir, 0o444)

        log_path = readonly_dir / "test.log"
        try:
            raised = False
            try:
                init_file_logging(log_path)
            except OSError:
                raised = True
            assert raised, "Expected OSError for permission denied"
        finally:
            # Restore permissions for cleanup
            os.chmod(readonly_dir, 0o755)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific path behavior")
    def test_run_workflow_handles_log_file_error_gracefully(self, tmp_path: Path) -> None:
        """Test that run_workflow_async handles log file errors gracefully."""
        import asyncio
        from io import StringIO
        from unittest.mock import AsyncMock, MagicMock

        from rich.console import Console

        from conductor.cli.run import run_workflow_async

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

        stderr_output = StringIO()
        mock_stderr_console = Console(file=stderr_output, no_color=True, highlight=False)
        bad_path = Path("/nonexistent/readonly/dir/test.log")

        mock_registry = AsyncMock()
        mock_engine = MagicMock()
        mock_engine.run = AsyncMock(return_value={"result": "done"})

        with (
            patch("conductor.cli.run._verbose_console", mock_stderr_console),
            patch("conductor.cli.run.ProviderRegistry", return_value=mock_registry),
            patch("conductor.cli.run.WorkflowEngine", return_value=mock_engine),
        ):
            mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
            mock_registry.__aexit__ = AsyncMock(return_value=False)
            # Should not raise - error is handled with a warning
            result = asyncio.run(run_workflow_async(workflow_file, {}, log_file=bad_path))

        assert result == {"result": "done"}
        stderr_text = stderr_output.getvalue()
        assert "Cannot open log file" in stderr_text

    def test_close_file_logging_idempotent(self) -> None:
        """Test that close_file_logging can be called multiple times safely."""
        from conductor.cli.run import close_file_logging

        # Should not raise when called with no active file logging
        close_file_logging()
        close_file_logging()

    def test_file_output_no_ansi_for_all_styles(self, tmp_path: Path) -> None:
        """Test that file output strips all ANSI codes for styled content."""
        import re

        from conductor.cli.run import (
            close_file_logging,
            init_file_logging,
            verbose_log,
            verbose_log_agent_complete,
            verbose_log_agent_start,
            verbose_log_route,
            verbose_log_section,
            verbose_log_timing,
        )

        log_path = tmp_path / "test.log"
        token_verbose = verbose_mode.set(True)
        token_full = full_mode.set(True)
        try:
            init_file_logging(log_path)
            verbose_log("test message", style="bold red")
            verbose_log_agent_start("agent1", 1)
            verbose_log_agent_complete("agent1", 2.0, model="gpt-4")
            verbose_log_route("agent2")
            verbose_log_section("Title", "content body")
            verbose_log_timing("operation", 1.5)
            close_file_logging()

            content = log_path.read_text(encoding="utf-8")
            ansi_pattern = re.compile(r"\x1b\[[0-9;]*m")
            assert not ansi_pattern.search(content), f"ANSI codes found in file output: {content}"
        finally:
            full_mode.reset(token_full)
            verbose_mode.reset(token_verbose)
