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
    """

    stdout: str
    stderr: str
    exit_code: int


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

        Renders command/args with Jinja2, spawns subprocess, captures output.

        Args:
            agent: Agent definition with type="script".
            context: Workflow context for template rendering.

        Returns:
            ScriptOutput with stdout, stderr, and exit_code.

        Raises:
            ExecutionError: If the script times out or cannot be started.
        """
        # Render command and args with Jinja2
        # command is guaranteed non-None by the model validator when type="script"
        assert agent.command is not None
        rendered_command = self.renderer.render(agent.command, context)
        rendered_args = [self.renderer.render(arg, context) for arg in agent.args]

        # Build environment (merge os.environ + agent.env)
        # Note: ${VAR:-default} patterns in agent.env are already resolved
        # by the config loader during YAML parsing.
        env = {**os.environ, **agent.env} if agent.env else None

        _verbose_log(f"  Script: {rendered_command} {' '.join(rendered_args)}")

        # Create subprocess
        try:
            process = await asyncio.create_subprocess_exec(
                rendered_command,
                *rendered_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=agent.working_dir,
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
                process.communicate(), timeout=timeout
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
        )
