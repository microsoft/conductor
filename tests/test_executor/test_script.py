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

import asyncio
import os
import sys
import tempfile

import pytest

from conductor.config.schema import AgentDef
from conductor.exceptions import ExecutionError, TemplateError
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


# Child snippet that echoes whatever it reads from stdin straight to stdout.
_ECHO_STDIN = "import sys; sys.stdout.write(sys.stdin.read())"
# Child snippet that prints the number of characters it read from stdin.
_LEN_STDIN = "import sys; sys.stdout.write(str(len(sys.stdin.read())))"


class TestScriptExecutorStdin:
    """Tests for piping a rendered ``stdin`` payload to the subprocess (issue #18)."""

    @pytest.mark.asyncio
    async def test_stdin_round_trip(self, executor: ScriptExecutor) -> None:
        """A rendered stdin payload is delivered verbatim to the child."""
        agent = AgentDef(
            name="test_stdin",
            type="script",
            command=sys.executable,
            args=["-c", _ECHO_STDIN],
            stdin="hello from stdin",
        )
        output = await executor.execute(agent, {})
        assert output.stdout == "hello from stdin"
        assert output.exit_code == 0
        assert output.stdin_bytes == len("hello from stdin")

    @pytest.mark.asyncio
    async def test_stdin_jinja2_rendered_from_context(self, executor: ScriptExecutor) -> None:
        """The stdin field is a Jinja2 template rendered against the context."""
        agent = AgentDef(
            name="test_stdin_tpl",
            type="script",
            command=sys.executable,
            args=["-c", _ECHO_STDIN],
            stdin="{{ workflow.input.message }}",
        )
        context = {"workflow": {"input": {"message": "rendered payload"}}}
        output = await executor.execute(agent, context)
        assert output.stdout == "rendered payload"

    @pytest.mark.asyncio
    async def test_stdin_json_via_tojson_filter(self, executor: ScriptExecutor) -> None:
        """Structured data is handed off as valid JSON via the ``tojson`` filter."""
        agent = AgentDef(
            name="test_stdin_json",
            type="script",
            command=sys.executable,
            args=[
                "-c",
                "import sys, json; d = json.load(sys.stdin); "
                "print(d['name'], d['count'], len(d['items']))",
            ],
            stdin="{{ payload | tojson }}",
        )
        context = {"payload": {"name": "widget", "count": 3, "items": [1, 2, 3, 4]}}
        output = await executor.execute(agent, context)
        assert output.stdout.strip() == "widget 3 4"
        assert output.exit_code == 0

    @pytest.mark.asyncio
    async def test_stdin_large_payload_bypasses_arg_limits(self, executor: ScriptExecutor) -> None:
        """A multi-MB payload streams through stdin without deadlock or ARG_MAX.

        Passing this many bytes as a command-line argument would exceed the OS
        argument-length limit (~256 KB on macOS) and raise OSError; via stdin it
        is delivered intact. This is the core cross-platform fix for issue #18.
        """
        payload = "x" * (2 * 1024 * 1024)  # 2 MB, well beyond ARG_MAX
        agent = AgentDef(
            name="test_stdin_large",
            type="script",
            command=sys.executable,
            args=["-c", _LEN_STDIN],
            stdin="{{ blob }}",
        )
        output = await executor.execute(agent, {"blob": payload})
        assert output.stdout.strip() == str(len(payload))
        assert output.exit_code == 0
        assert output.stdin_bytes == len(payload)

    @pytest.mark.asyncio
    async def test_stdin_empty_string_pipes_immediate_eof(self, executor: ScriptExecutor) -> None:
        """An explicit empty string still pipes (sends immediate EOF), unlike omission."""
        agent = AgentDef(
            name="test_stdin_empty",
            type="script",
            command=sys.executable,
            args=["-c", _LEN_STDIN],
            stdin="",
        )
        output = await executor.execute(agent, {})
        assert output.stdout.strip() == "0"
        assert output.exit_code == 0
        # Present-but-empty is distinct from omitted: 0 bytes, not None.
        assert output.stdin_bytes == 0

    @pytest.mark.asyncio
    async def test_stdin_omitted_is_backwards_compatible(self, executor: ScriptExecutor) -> None:
        """Omitting stdin keeps legacy behavior: nothing piped, stdin_bytes is None."""
        agent = AgentDef(
            name="test_stdin_omitted",
            type="script",
            command=sys.executable,
            args=["-c", "print('no stdin')"],
        )
        output = await executor.execute(agent, {})
        assert output.stdout.strip() == "no stdin"
        assert output.exit_code == 0
        assert output.stdin_bytes is None

    @pytest.mark.asyncio
    async def test_stdin_coexists_with_args(self, executor: ScriptExecutor) -> None:
        """stdin and args are orthogonal — both reach the child when set."""
        agent = AgentDef(
            name="test_stdin_args",
            type="script",
            command=sys.executable,
            args=[
                "-c",
                "import sys; print(sys.argv[1]); sys.stdout.write(sys.stdin.read())",
                "from-args",
            ],
            stdin="from-stdin",
        )
        output = await executor.execute(agent, {})
        assert output.stdout == "from-args\nfrom-stdin"
        assert output.exit_code == 0

    @pytest.mark.asyncio
    async def test_stdin_utf8_payload_byte_count(self, executor: ScriptExecutor) -> None:
        """Non-ASCII payloads are UTF-8 encoded; stdin_bytes counts bytes, not chars."""
        payload = "café ☕ 日本語"
        agent = AgentDef(
            name="test_stdin_utf8",
            type="script",
            command=sys.executable,
            args=["-c", _ECHO_STDIN],
            stdin="{{ msg }}",
        )
        output = await executor.execute(agent, {"msg": payload})
        assert output.stdout == payload
        assert output.stdin_bytes == len(payload.encode("utf-8"))
        # Byte length exceeds character length for multi-byte code points.
        assert output.stdin_bytes > len(payload)

    @pytest.mark.asyncio
    async def test_stdin_large_bidirectional_no_deadlock(self, executor: ScriptExecutor) -> None:
        """A multi-MB payload in AND multi-MB out streams without deadlock.

        The other large-payload tests use a tiny stdout, which always fits the
        OS pipe buffer — so a regression to write-then-read piping would pass
        them yet deadlock here. The child echoes the full payload back, so both
        pipes exceed the buffer simultaneously. Wrapped in a timeout so a
        deadlock regression fails fast instead of hanging CI.
        """
        payload = "m" * (4 * 1024 * 1024)  # 4 MB each direction
        agent = AgentDef(
            name="test_stdin_bidi",
            type="script",
            command=sys.executable,
            args=["-c", _ECHO_STDIN],
            stdin="{{ blob }}",
        )
        output = await asyncio.wait_for(executor.execute(agent, {"blob": payload}), timeout=30)
        assert len(output.stdout) == len(payload)
        assert output.exit_code == 0
        assert output.stdin_bytes == len(payload)

    @pytest.mark.asyncio
    async def test_stdin_child_exits_without_reading(self, executor: ScriptExecutor) -> None:
        """A child that exits before reading a large stdin payload is handled cleanly.

        ``communicate`` lets asyncio absorb the resulting BrokenPipeError, so the
        step completes with the child's real exit code rather than crashing.
        """
        agent = AgentDef(
            name="test_stdin_early_exit",
            type="script",
            command=sys.executable,
            args=["-c", "import sys; sys.exit(3)"],  # never reads stdin
            stdin="y" * (2 * 1024 * 1024),
        )
        output = await executor.execute(agent, {})
        assert output.exit_code == 3
        # Byte count reflects the submitted payload even though the child read none.
        assert output.stdin_bytes == 2 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_stdin_timeout_while_writing(self, executor: ScriptExecutor) -> None:
        """Timeout fires cleanly even while a large stdin payload is mid-write."""
        agent = AgentDef(
            name="test_stdin_timeout",
            type="script",
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],  # sleeps, never drains stdin
            stdin="q" * (4 * 1024 * 1024),
            timeout=1,
        )
        with pytest.raises(ExecutionError, match="timed out after 1s"):
            await executor.execute(agent, {})

    @pytest.mark.asyncio
    async def test_stdin_invalid_utf8_raises_execution_error(
        self, executor: ScriptExecutor
    ) -> None:
        """A payload that can't be UTF-8 encoded raises a clear ExecutionError.

        Lone surrogates reach the context via upstream JSON (``json.loads`` of
        ``"\\ud800"``). The strict ``.encode`` must surface a named error, not a
        bare UnicodeEncodeError.
        """
        agent = AgentDef(
            name="test_stdin_bad_utf8",
            type="script",
            command=sys.executable,
            args=["-c", _ECHO_STDIN],
            stdin="{{ bad }}",
        )
        with pytest.raises(ExecutionError, match="not valid UTF-8"):
            await executor.execute(agent, {"bad": "\ud800"})

    @pytest.mark.asyncio
    async def test_stdin_render_failure_raises_template_error(
        self, executor: ScriptExecutor
    ) -> None:
        """An undefined variable in stdin fails like command/args (TemplateError)."""
        agent = AgentDef(
            name="test_stdin_bad_template",
            type="script",
            command=sys.executable,
            args=["-c", _ECHO_STDIN],
            stdin="{{ does_not_exist }}",
        )
        with pytest.raises(TemplateError):
            await executor.execute(agent, {})
