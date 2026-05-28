"""Tests for ScriptExecutor.

Tests cover:
- Simple command execution with stdout capture
- Command with args
- Non-zero exit code capture
- Stderr capture
- Timeout handling
- Custom environment variables
- Working directory
- Jinja2 template rendering in command/args
- Command not found error
- Command resolution via shutil.which
"""

from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from conductor.config.schema import AgentDef
from conductor.exceptions import ExecutionError
from conductor.executor.script import ScriptExecutor, ScriptOutput


@pytest.fixture
def executor() -> ScriptExecutor:
    """Create a ScriptExecutor instance."""
    return ScriptExecutor()


class TestScriptOutput:
    """Tests for ScriptOutput dataclass."""

    def test_script_output_fields(self) -> None:
        """Test ScriptOutput has correct fields."""
        output = ScriptOutput(stdout="hello\n", stderr="", exit_code=0)
        assert output.stdout == "hello\n"
        assert output.stderr == ""
        assert output.exit_code == 0


class TestScriptExecutorBasic:
    """Tests for basic script execution."""

    @pytest.mark.asyncio
    async def test_simple_echo(self, executor: ScriptExecutor) -> None:
        """Test simple command captures stdout."""
        agent = AgentDef(
            name="test_echo",
            type="script",
            command=sys.executable,
            args=["-c", "print('hello')"],
        )
        output = await executor.execute(agent, {})
        assert output.stdout.strip() == "hello"
        assert output.exit_code == 0

    @pytest.mark.asyncio
    async def test_command_with_multiple_args(self, executor: ScriptExecutor) -> None:
        """Test command with multiple arguments."""
        agent = AgentDef(
            name="test_printf",
            type="script",
            command=sys.executable,
            args=[
                "-c",
                "import sys; print(sys.argv[1] + ' ' + sys.argv[2], end='')",
                "hello",
                "world",
            ],
        )
        output = await executor.execute(agent, {})
        assert output.stdout == "hello world"
        assert output.exit_code == 0

    @pytest.mark.asyncio
    async def test_failing_command_exit_code(self, executor: ScriptExecutor) -> None:
        """Test that non-zero exit code is captured correctly (not 0)."""
        agent = AgentDef(
            name="test_false",
            type="script",
            command=sys.executable,
            args=["-c", "import sys; sys.exit(1)"],
        )
        output = await executor.execute(agent, {})
        assert output.exit_code == 1
        assert output.exit_code != 0

    @pytest.mark.asyncio
    async def test_stderr_captured(self, executor: ScriptExecutor) -> None:
        """Test that stderr is captured separately from stdout."""
        agent = AgentDef(
            name="test_stderr",
            type="script",
            command=sys.executable,
            args=["-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        )
        output = await executor.execute(agent, {})
        assert "out" in output.stdout
        assert "err" in output.stderr


class TestScriptExecutorTimeout:
    """Tests for script timeout handling."""

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, executor: ScriptExecutor) -> None:
        """Test that timeout kills process and raises ExecutionError."""
        agent = AgentDef(
            name="test_timeout",
            type="script",
            command=sys.executable,
            args=["-c", "import time; time.sleep(10)"],
            timeout=1,
        )
        with pytest.raises(ExecutionError, match="timed out after 1s"):
            await executor.execute(agent, {})

    @pytest.mark.asyncio
    async def test_no_timeout_default(self, executor: ScriptExecutor) -> None:
        """Test that no timeout allows command to complete."""
        agent = AgentDef(
            name="test_quick",
            type="script",
            command=sys.executable,
            args=["-c", "print('fast')"],
        )
        output = await executor.execute(agent, {})
        assert output.exit_code == 0


class TestScriptExecutorEnvironment:
    """Tests for environment variable handling."""

    @pytest.mark.asyncio
    async def test_custom_env_passed(self, executor: ScriptExecutor) -> None:
        """Test that custom environment variables are passed to subprocess."""
        agent = AgentDef(
            name="test_env",
            type="script",
            command=sys.executable,
            args=["-c", "import os; print(os.environ['MY_TEST_VAR'])"],
            env={"MY_TEST_VAR": "custom_value"},
        )
        output = await executor.execute(agent, {})
        assert "custom_value" in output.stdout

    @pytest.mark.asyncio
    async def test_env_merges_with_os_environ(self, executor: ScriptExecutor) -> None:
        """Test that agent env merges with process environment."""
        agent = AgentDef(
            name="test_env_merge",
            type="script",
            command=sys.executable,
            args=["-c", "import os; print(os.environ.get('PATH', ''))"],
            env={"MY_EXTRA": "val"},
        )
        output = await executor.execute(agent, {})
        # PATH should still be available from os.environ
        assert output.stdout.strip() != ""

    @pytest.mark.asyncio
    async def test_env_values_not_jinja2_rendered(self, executor: ScriptExecutor) -> None:
        """Test that env values are passed as-is (not rendered through Jinja2 engine).

        This is intentional: env var values are static strings resolved by the
        YAML loader's ${VAR:-default} pass, not by the Jinja2 template engine.
        Jinja2 syntax in env values is treated as a literal string.
        """
        agent = AgentDef(
            name="test_env_no_render",
            type="script",
            command=sys.executable,
            args=["-c", "import os; print(os.environ['MY_VAR'])"],
            env={"MY_VAR": "{{ literal_braces }}"},
        )
        output = await executor.execute(agent, {"literal_braces": "should_not_appear"})
        # The env value is passed literally, not rendered through Jinja2
        assert "{{ literal_braces }}" in output.stdout


class TestScriptExecutorWorkingDir:
    """Tests for working directory handling."""

    @pytest.mark.asyncio
    async def test_working_dir_respected(self, executor: ScriptExecutor) -> None:
        """Test that working_dir is used by subprocess."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = AgentDef(
                name="test_cwd",
                type="script",
                command=sys.executable,
                args=["-c", "import os; print(os.getcwd())"],
                working_dir=tmpdir,
            )
            output = await executor.execute(agent, {})
            # Resolve symlinks for macOS /tmp -> /private/tmp
            assert os.path.realpath(output.stdout.strip()) == os.path.realpath(tmpdir)

    @pytest.mark.asyncio
    async def test_working_dir_with_jinja2_template(self, executor: ScriptExecutor) -> None:
        """Test that working_dir supports Jinja2 template rendering."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = AgentDef(
                name="test_cwd_tpl",
                type="script",
                command=sys.executable,
                args=["-c", "import os; print(os.getcwd())"],
                working_dir="{{ target_dir }}",
            )
            output = await executor.execute(agent, {"target_dir": tmpdir})
            assert os.path.realpath(output.stdout.strip()) == os.path.realpath(tmpdir)


class TestScriptExecutorTemplating:
    """Tests for Jinja2 template rendering in command/args."""

    @pytest.mark.asyncio
    async def test_template_in_command(self, executor: ScriptExecutor) -> None:
        """Test Jinja2 template rendering in command field."""
        agent = AgentDef(
            name="test_cmd_tpl",
            type="script",
            command="{{ cmd }}",
            args=["-c", "print('ok')"],
        )
        output = await executor.execute(agent, {"cmd": sys.executable})
        assert output.exit_code == 0

    @pytest.mark.asyncio
    async def test_template_in_args(self, executor: ScriptExecutor) -> None:
        """Test Jinja2 template rendering in args."""
        agent = AgentDef(
            name="test_args_tpl",
            type="script",
            command=sys.executable,
            args=["-c", "print('{{ greeting }}')"],
        )
        output = await executor.execute(agent, {"greeting": "hi there"})
        assert "hi there" in output.stdout

    @pytest.mark.asyncio
    async def test_template_with_workflow_context(self, executor: ScriptExecutor) -> None:
        """Test template rendering with nested workflow context."""
        agent = AgentDef(
            name="test_ctx_tpl",
            type="script",
            command=sys.executable,
            args=["-c", "print('{{ workflow.input.message }}')"],
        )
        context = {"workflow": {"input": {"message": "from workflow"}}}
        output = await executor.execute(agent, context)
        assert "from workflow" in output.stdout


class TestScriptExecutorErrors:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_command_not_found(self, executor: ScriptExecutor) -> None:
        """Test that command not found raises ExecutionError."""
        agent = AgentDef(
            name="test_notfound",
            type="script",
            command="definitely_not_a_real_command_xyz123",
        )
        with pytest.raises(ExecutionError, match="command not found"):
            await executor.execute(agent, {})

    @pytest.mark.asyncio
    async def test_specific_exit_code(self, executor: ScriptExecutor) -> None:
        """Test that specific exit codes are captured correctly."""
        agent = AgentDef(
            name="test_exit42",
            type="script",
            command=sys.executable,
            args=["-c", "import sys; sys.exit(42)"],
        )
        output = await executor.execute(agent, {})
        assert output.exit_code == 42


class TestScriptExecutorCommandResolution:
    """Tests for command resolution via ``shutil.which`` in rendered_command.

    Forward-slash paths, missing Windows extensions, and bare command names
    are resolved against PATH/PATHEXT. Relative paths containing a separator
    are left untouched so they resolve against ``working_dir``. Resolution is
    non-destructive: when ``which`` returns ``None`` the rendered command is
    used as-is.
    """

    @pytest.mark.asyncio
    async def test_bare_name_resolved_via_which(self, executor: ScriptExecutor) -> None:
        """A bare command name is resolved to the executable ``which`` finds."""
        agent = AgentDef(
            name="test_bare",
            type="script",
            command="python",
            args=["-c", "print('hello')"],
        )
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"hello\n", b"")
        mock_process.returncode = 0

        with (
            patch(
                "conductor.executor.script.shutil.which",
                return_value="/resolved/bin/python",
            ) as mock_which,
            patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec,
        ):
            await executor.execute(agent, {})

        mock_which.assert_called_once_with("python")
        assert mock_exec.call_args[0][0] == "/resolved/bin/python"

    @pytest.mark.asyncio
    async def test_absolute_path_resolved_via_which(self, executor: ScriptExecutor) -> None:
        """An absolute path (incl. forward slashes) is resolved via ``which``."""
        agent = AgentDef(
            name="test_abs",
            type="script",
            command="C:/Python314/python",
            args=["-c", "print('hello')"],
        )
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"hello\n", b"")
        mock_process.returncode = 0

        with (
            patch(
                "conductor.executor.script.shutil.which",
                return_value="C:\\Python314\\python.EXE",
            ) as mock_which,
            patch("conductor.executor.script.os.path.isabs", return_value=True),
            patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec,
        ):
            await executor.execute(agent, {})

        mock_which.assert_called_once_with("C:/Python314/python")
        assert mock_exec.call_args[0][0] == "C:\\Python314\\python.EXE"

    @pytest.mark.asyncio
    async def test_which_none_falls_back_to_rendered(self, executor: ScriptExecutor) -> None:
        """When ``which`` cannot resolve, the rendered command is used as-is."""
        agent = AgentDef(
            name="test_fallback",
            type="script",
            command="python",
            args=["-c", "print('hello')"],
        )
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"hello\n", b"")
        mock_process.returncode = 0

        with (
            patch("conductor.executor.script.shutil.which", return_value=None),
            patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec,
        ):
            await executor.execute(agent, {})

        assert mock_exec.call_args[0][0] == "python"

    @pytest.mark.asyncio
    async def test_relative_path_with_separator_not_resolved(
        self, executor: ScriptExecutor
    ) -> None:
        """A relative path with a separator is left untouched (working_dir semantics)."""
        agent = AgentDef(
            name="test_relative",
            type="script",
            command="./scripts/run.sh",
            args=[],
        )
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"", b"")
        mock_process.returncode = 0

        with (
            patch("conductor.executor.script.shutil.which") as mock_which,
            patch("conductor.executor.script.os.path.isabs", return_value=False),
            patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec,
        ):
            await executor.execute(agent, {})

        mock_which.assert_not_called()
        assert mock_exec.call_args[0][0] == "./scripts/run.sh"

    @pytest.mark.asyncio
    async def test_args_not_resolved(self, executor: ScriptExecutor) -> None:
        """Args are never passed through ``which`` (may contain URLs or flags with /)."""
        agent = AgentDef(
            name="test_args_preserve",
            type="script",
            command="python",
            args=["-c", "print('hello')", "https://example.com/api/v1"],
        )
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"hello\n", b"")
        mock_process.returncode = 0

        with (
            patch("conductor.executor.script.shutil.which", return_value="/bin/python"),
            patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec,
        ):
            await executor.execute(agent, {})

        called_args = mock_exec.call_args[0][1:]
        assert "https://example.com/api/v1" in called_args

    @pytest.mark.asyncio
    async def test_file_not_found_includes_hint_on_windows(self, executor: ScriptExecutor) -> None:
        """FileNotFoundError on Windows includes a path-resolution hint."""
        agent = AgentDef(
            name="test_hint",
            type="script",
            command="C:/nonexistent/python.exe",
        )
        with (
            patch("conductor.executor.script.sys") as mock_sys,
            patch("conductor.executor.script.shutil.which", return_value=None),
            patch("conductor.executor.script.os.path.isabs", return_value=True),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError("not found"),
            ),
        ):
            mock_sys.platform = "win32"
            with pytest.raises(ExecutionError, match="Hint: on Windows") as exc_info:
                await executor.execute(agent, {})

        error_msg = str(exc_info.value)
        assert "C:/nonexistent/python.exe" in error_msg
        assert "working_dir=cwd" in error_msg

    @pytest.mark.asyncio
    async def test_file_not_found_no_hint_on_linux(self, executor: ScriptExecutor) -> None:
        """FileNotFoundError on Linux does not include the Windows hint."""
        agent = AgentDef(
            name="test_no_hint",
            type="script",
            command="/usr/local/bin/nonexistent",
        )
        with (
            patch("conductor.executor.script.sys") as mock_sys,
            patch("conductor.executor.script.shutil.which", return_value=None),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError("not found"),
            ),
        ):
            mock_sys.platform = "linux"
            with pytest.raises(ExecutionError, match="command not found") as exc_info:
                await executor.execute(agent, {})

        assert "Hint" not in str(exc_info.value)
