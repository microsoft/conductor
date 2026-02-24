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
"""

from __future__ import annotations

import os
import tempfile

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
        """Test simple echo command captures stdout."""
        agent = AgentDef(name="test_echo", type="script", command="echo", args=["hello"])
        output = await executor.execute(agent, {})
        assert output.stdout == "hello\n"
        assert output.stderr == ""
        assert output.exit_code == 0

    @pytest.mark.asyncio
    async def test_command_with_multiple_args(self, executor: ScriptExecutor) -> None:
        """Test command with multiple arguments."""
        agent = AgentDef(
            name="test_printf", type="script", command="printf", args=["%s %s", "hello", "world"]
        )
        output = await executor.execute(agent, {})
        assert output.stdout == "hello world"
        assert output.exit_code == 0

    @pytest.mark.asyncio
    async def test_failing_command_exit_code(self, executor: ScriptExecutor) -> None:
        """Test that non-zero exit code is captured correctly (not 0)."""
        agent = AgentDef(name="test_false", type="script", command="false")
        output = await executor.execute(agent, {})
        assert output.exit_code == 1
        assert output.exit_code != 0

    @pytest.mark.asyncio
    async def test_stderr_captured(self, executor: ScriptExecutor) -> None:
        """Test that stderr is captured separately from stdout."""
        agent = AgentDef(
            name="test_stderr",
            type="script",
            command="sh",
            args=["-c", "echo out; echo err >&2"],
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
            name="test_timeout", type="script", command="sleep", args=["10"], timeout=1
        )
        with pytest.raises(ExecutionError, match="timed out after 1s"):
            await executor.execute(agent, {})

    @pytest.mark.asyncio
    async def test_no_timeout_default(self, executor: ScriptExecutor) -> None:
        """Test that no timeout allows command to complete."""
        agent = AgentDef(name="test_quick", type="script", command="echo", args=["fast"])
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
            command="sh",
            args=["-c", "echo $MY_TEST_VAR"],
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
            command="sh",
            args=["-c", "echo $PATH"],
            env={"MY_EXTRA": "val"},
        )
        output = await executor.execute(agent, {})
        # PATH should still be available from os.environ
        assert output.stdout.strip() != ""


class TestScriptExecutorWorkingDir:
    """Tests for working directory handling."""

    @pytest.mark.asyncio
    async def test_working_dir_respected(self, executor: ScriptExecutor) -> None:
        """Test that working_dir is used by subprocess."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = AgentDef(
                name="test_cwd",
                type="script",
                command="pwd",
                working_dir=tmpdir,
            )
            output = await executor.execute(agent, {})
            # Resolve symlinks for macOS /tmp -> /private/tmp
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
        )
        output = await executor.execute(agent, {"cmd": "echo"})
        assert output.exit_code == 0

    @pytest.mark.asyncio
    async def test_template_in_args(self, executor: ScriptExecutor) -> None:
        """Test Jinja2 template rendering in args."""
        agent = AgentDef(
            name="test_args_tpl",
            type="script",
            command="echo",
            args=["{{ greeting }}"],
        )
        output = await executor.execute(agent, {"greeting": "hi there"})
        assert "hi there" in output.stdout

    @pytest.mark.asyncio
    async def test_template_with_workflow_context(self, executor: ScriptExecutor) -> None:
        """Test template rendering with nested workflow context."""
        agent = AgentDef(
            name="test_ctx_tpl",
            type="script",
            command="echo",
            args=["{{ workflow.input.message }}"],
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
            command="sh",
            args=["-c", "exit 42"],
        )
        output = await executor.execute(agent, {})
        assert output.exit_code == 42
