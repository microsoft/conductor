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
import sys
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


class TestScriptErrorEnvelope:
    """Tests for the ``CONDUCTOR_ERROR_OUT`` envelope contract."""

    @pytest.mark.asyncio
    async def test_no_envelope_when_file_empty(self, executor: ScriptExecutor) -> None:
        """A script that exits 0 and writes nothing produces no envelope."""
        agent = AgentDef(
            name="quiet",
            type="script",
            command=sys.executable,
            args=["-c", "print('ok')"],
            raises=["external.x"],  # opted in, but didn't raise anything
        )
        output = await executor.execute(agent, {})
        assert output.exit_code == 0
        assert output.error is None

    @pytest.mark.asyncio
    async def test_well_formed_envelope_is_surfaced(self, executor: ScriptExecutor) -> None:
        """A script writes a JSON envelope to ``$CONDUCTOR_ERROR_OUT``."""
        script = (
            "import json, os\n"
            "with open(os.environ['CONDUCTOR_ERROR_OUT'], 'w', encoding='utf-8') as f:\n"
            "    json.dump({"
            "'conductor_error': True, "
            "'kind': 'external.git.fetch_failed', "
            "'message': 'remote unreachable', "
            "'details': {'remote': 'origin'}"
            "}, f)\n"
        )
        agent = AgentDef(
            name="fetch",
            type="script",
            command=sys.executable,
            args=["-c", script],
            raises=["external.git.fetch_failed"],
        )
        output = await executor.execute(agent, {})
        assert output.error is not None
        assert output.error["kind"] == "external.git.fetch_failed"
        assert output.error["message"] == "remote unreachable"
        assert output.error["details"] == {"remote": "origin"}

    @pytest.mark.asyncio
    async def test_user_env_cannot_override_envelope_path(self, executor: ScriptExecutor) -> None:
        """``CONDUCTOR_ERROR_OUT`` in ``agent.env`` is overridden by the executor."""
        # The script reads $CONDUCTOR_ERROR_OUT and writes there. If the
        # executor's value wins, the test passes (envelope shows up). If the
        # user override won, the file would be created in a path the executor
        # doesn't know about, so output.error stays None.
        script = (
            "import json, os\n"
            "with open(os.environ['CONDUCTOR_ERROR_OUT'], 'w', encoding='utf-8') as f:\n"
            "    json.dump({"
            "'conductor_error': True, "
            "'kind': 'external.x', "
            "'message': 'm'"
            "}, f)\n"
        )
        agent = AgentDef(
            name="hijack",
            type="script",
            command=sys.executable,
            args=["-c", script],
            env={"CONDUCTOR_ERROR_OUT": "/this/path/does/not/exist/should/not/win"},
            raises=["external.x"],
        )
        output = await executor.execute(agent, {})
        assert output.error is not None
        assert output.error["kind"] == "external.x"

    @pytest.mark.asyncio
    async def test_synthesize_script_error_when_opted_in(self, executor: ScriptExecutor) -> None:
        """Non-zero exit + no envelope + opt-in → synthesized ``internal.script_error``."""
        agent = AgentDef(
            name="boom",
            type="script",
            command=sys.executable,
            args=["-c", "import sys; sys.stderr.write('kaboom\\n'); sys.exit(7)"],
            raises=["external.x"],
        )
        output = await executor.execute(agent, {})
        assert output.exit_code == 7
        assert output.error is not None
        assert output.error["kind"] == "internal.script_error"
        assert output.error["details"]["exit_code"] == 7

    @pytest.mark.asyncio
    async def test_legacy_nonzero_exit_without_optin_has_no_envelope(
        self, executor: ScriptExecutor
    ) -> None:
        """Legacy workflows that route on ``exit_code`` are not surprised by an envelope."""
        agent = AgentDef(
            name="legacy",
            type="script",
            command=sys.executable,
            args=["-c", "import sys; sys.exit(3)"],
        )
        output = await executor.execute(agent, {})
        assert output.exit_code == 3
        assert output.error is None

    @pytest.mark.asyncio
    async def test_malformed_envelope_downgraded_to_schema_violation(
        self, executor: ScriptExecutor
    ) -> None:
        """An envelope missing ``kind`` becomes ``internal.schema_violation``."""
        script = (
            "import json, os\n"
            "with open(os.environ['CONDUCTOR_ERROR_OUT'], 'w', encoding='utf-8') as f:\n"
            "    json.dump({'conductor_error': True, 'message': 'no kind here'}, f)\n"
        )
        agent = AgentDef(
            name="bad",
            type="script",
            command=sys.executable,
            args=["-c", script],
            raises=["external.x"],
        )
        output = await executor.execute(agent, {})
        assert output.error is not None
        assert output.error["kind"] == "internal.schema_violation"

    @pytest.mark.asyncio
    async def test_envelope_temp_file_is_cleaned_up(self, executor: ScriptExecutor) -> None:
        """The temp file is removed after the script runs (success path)."""
        # Capture the path the script saw and check it doesn't exist after.
        capture_path = os.path.join(tempfile.gettempdir(), "conductor-test-capture.txt")
        script = (
            "import os\n"
            f"with open(r'{capture_path}', 'w') as f:\n"
            "    f.write(os.environ['CONDUCTOR_ERROR_OUT'])\n"
        )
        try:
            agent = AgentDef(
                name="capture",
                type="script",
                command=sys.executable,
                args=["-c", script],
            )
            await executor.execute(agent, {})
            with open(capture_path) as f:
                error_path = f.read().strip()
            assert error_path  # we got a path
            assert not os.path.exists(error_path), "temp envelope file should be removed"
        finally:
            if os.path.exists(capture_path):
                os.unlink(capture_path)
