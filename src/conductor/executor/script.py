"""Script execution for Conductor workflow steps.

This module provides the ScriptExecutor class for running shell commands
as workflow steps, capturing stdout/stderr and exit codes.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from conductor.exceptions import ExecutionError
from conductor.executor.template import TemplateRenderer


def _verbose_log(message: str, style: str = "dim") -> None:
    """Log a verbose message via the CLI run module.

    Uses a deferred import to avoid a circular import between executor.script
    and cli.run (cli.run imports WorkflowEngine which imports executor modules).
    """
    from conductor.cli.run import verbose_log

    verbose_log(message, style)


if TYPE_CHECKING:
    from conductor.config.schema import AgentDef


@dataclass
class ScriptOutput:
    """Result of a script step execution.

    Attributes:
        stdout: Captured standard output as text.
        stderr: Captured standard error as text.
        exit_code: Process exit code.
        stdin_bytes: Number of UTF-8 bytes in the stdin payload submitted to
            the child (the child may read fewer if it exits early), or
            ``None`` when no ``stdin`` payload was configured (stdin inherited).
    """

    stdout: str
    stderr: str
    exit_code: int
    stdin_bytes: int | None = None


class ScriptExecutor:
    """Executes script steps via asyncio subprocess.

    Handles command/args template rendering, environment merging,
    working directory, timeout enforcement, and output capture.

    Example:
        >>> executor = ScriptExecutor()
        >>> output = await executor.execute(agent, context)
        >>> print(output.stdout, output.exit_code)
    """

    def __init__(self) -> None:
        """Initialize the ScriptExecutor with a template renderer."""
        self.renderer = TemplateRenderer()

    async def execute(
        self,
        agent: AgentDef,
        context: dict[str, Any],
    ) -> ScriptOutput:
        """Execute a script step.

        Renders command/args with Jinja2, spawns the subprocess, and captures
        output. If ``agent.stdin`` is set, the rendered payload is piped to the
        child's stdin as UTF-8 via ``communicate`` (which streams stdin while
        draining stdout/stderr, so large payloads can't deadlock the pipe);
        routing the payload through stdin also keeps it off the command line
        and clear of OS argv length limits. Otherwise the child inherits the
        parent's stdin.

        Args:
            agent: Agent definition with type="script".
            context: Workflow context for template rendering.

        Returns:
            ScriptOutput with stdout, stderr, exit_code, and stdin_bytes.

        Raises:
            ExecutionError: If the script times out or cannot be started.
        """
        # Render command and args with Jinja2
        # command is guaranteed non-None by the model validator when type="script"
        assert agent.command is not None
        rendered_command = self.renderer.render(agent.command, context)
        rendered_args = [self.renderer.render(arg, context) for arg in agent.args]
        rendered_working_dir = (
            self.renderer.render(agent.working_dir, context) if agent.working_dir else None
        )

        # Render the optional stdin payload. ``None`` means "inherit the
        # parent's stdin" (the legacy behavior); any string — including an
        # empty one — means "pipe this to the child", so we check
        # ``is not None`` rather than truthiness. Routing the payload through
        # stdin (rather than argv) is what keeps it clear of OS command-line
        # length limits — Windows caps the command line at ~32 KB; POSIX
        # ARG_MAX is larger. We write it via ``communicate(input=...)``, which
        # feeds stdin concurrently with draining stdout/stderr so a large
        # payload can't deadlock the pipe.
        stdin_payload: bytes | None = None
        if agent.stdin is not None:
            rendered_stdin = self.renderer.render(agent.stdin, context)
            try:
                stdin_payload = rendered_stdin.encode("utf-8")
            except UnicodeEncodeError as exc:
                # Strict encode (unlike the lenient ``decode(errors="replace")``
                # on output) — surface a clear, named error instead of a bare
                # codec traceback. Do NOT use ``errors="replace"`` here: that
                # would silently corrupt the payload delivered to the child.
                raise ExecutionError(
                    f"Script '{agent.name}': stdin payload is not valid UTF-8 ({exc})",
                    agent_name=agent.name,
                    suggestion=(
                        "The rendered stdin contains characters that cannot be "
                        "UTF-8 encoded (e.g. unpaired surrogates from upstream "
                        "JSON). Sanitize the value or render it through the "
                        "'tojson' filter."
                    ),
                ) from exc

        # Build environment (merge os.environ + agent.env)
        # Note: ${VAR:-default} patterns in agent.env are already resolved
        # by the config loader during YAML parsing.
        # Always set PYTHONUTF8=1 so child Python processes use UTF-8 encoding
        # instead of the system default (cp1252 on Windows), preventing garbled
        # Unicode characters in script output.
        base_env = {**os.environ, "PYTHONUTF8": "1"}
        env = {**base_env, **agent.env} if agent.env else base_env

        _verbose_log(f"  Script: {rendered_command} {' '.join(rendered_args)}")
        if stdin_payload is not None:
            _verbose_log(f"  Script stdin: {len(stdin_payload)} bytes")

        # Create subprocess
        try:
            process = await asyncio.create_subprocess_exec(
                rendered_command,
                *rendered_args,
                stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=rendered_working_dir,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ExecutionError(
                f"Script '{agent.name}': command not found: '{rendered_command}'",
                agent_name=agent.name,
                suggestion=f"Ensure '{rendered_command}' is installed and in PATH",
            ) from exc
        except OSError as e:
            raise ExecutionError(
                f"Script '{agent.name}' failed to start: {e}",
                agent_name=agent.name,
            ) from e

        # Wait with optional per-script timeout
        timeout = agent.timeout
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=stdin_payload), timeout=timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ExecutionError(
                f"Script '{agent.name}' timed out after {timeout}s",
                agent_name=agent.name,
            ) from None

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        if stderr_text:
            _verbose_log(f"  Script stderr: {stderr_text.strip()}")

        # IMPORTANT: process.returncode is guaranteed non-None after communicate().
        # Do NOT use `process.returncode or 0` — 0 is falsy in Python.
        assert process.returncode is not None
        return ScriptOutput(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=process.returncode,
            stdin_bytes=len(stdin_payload) if stdin_payload is not None else None,
        )
