"""Script execution for Conductor workflow steps.

This module provides the ScriptExecutor class for running shell commands
as workflow steps, capturing stdout/stderr and exit codes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

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
        error: Optional structured error envelope.

            Populated when the script wrote a well-formed envelope to the
            file referenced by ``$CONDUCTOR_ERROR_OUT``, or when the engine
            synthesized an ``internal.script_error`` envelope because the
            script exited non-zero, opted into error routing (via ``raises``
            or any ``on_error`` route), and produced no envelope of its own.
            ``None`` for legacy success/exit-code routing.
    """

    stdout: str
    stderr: str
    exit_code: int
    error: dict[str, Any] | None = field(default=None)


def _node_uses_error_routing(agent: AgentDef) -> bool:
    """True if the script node opts into error envelopes via ``raises``/``on_error``.

    Backward compatibility: legacy workflows route on ``exit_code`` and
    must NOT see a synthesized ``internal.script_error`` envelope when
    they haven't opted in. Any presence of ``raises`` or an ``on_error``
    field on any route counts as opting in.
    """
    if agent.raises:
        return True
    if agent.routes:
        for route in agent.routes:
            if route.on_error is not None:
                return True
    return False


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
        rendered_working_dir = (
            self.renderer.render(agent.working_dir, context) if agent.working_dir else None
        )

        # Build environment (merge os.environ + agent.env)
        # Note: ${VAR:-default} patterns in agent.env are already resolved
        # by the config loader during YAML parsing.
        # Always set PYTHONUTF8=1 so child Python processes use UTF-8 encoding
        # instead of the system default (cp1252 on Windows), preventing garbled
        # Unicode characters in script output.
        base_env = {**os.environ, "PYTHONUTF8": "1"}
        env = {**base_env, **agent.env} if agent.env else base_env

        # Error envelope contract: allocate a temp file the script can write
        # a typed error envelope to, and expose its path via the
        # CONDUCTOR_ERROR_OUT env var. Set this AFTER agent.env merge so the
        # user cannot accidentally (or deliberately) override it. Caller is
        # responsible for reading + deleting in the finally block.
        fd, error_path = tempfile.mkstemp(prefix="conductor-err-", suffix=".json")
        os.close(fd)
        env["CONDUCTOR_ERROR_OUT"] = error_path

        _verbose_log(f"  Script: {rendered_command} {' '.join(rendered_args)}")

        envelope: dict[str, Any] | None = None
        try:
            # Create subprocess
            try:
                process = await asyncio.create_subprocess_exec(
                    rendered_command,
                    *rendered_args,
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
            exit_code = process.returncode

            envelope = _read_error_envelope(error_path, agent.name)
            if envelope is None and exit_code != 0 and _node_uses_error_routing(agent):
                envelope = _synthesize_script_error(
                    exit_code=exit_code,
                    stderr_tail=stderr_text,
                    command=rendered_command,
                )

            return ScriptOutput(
                stdout=stdout_text,
                stderr=stderr_text,
                exit_code=exit_code,
                error=envelope,
            )
        finally:
            # Always remove the temp file — even on TimeoutError or
            # FileNotFoundError above. The script's contract is to write
            # once; lingering files would leak across runs.
            with contextlib.suppress(OSError):
                os.unlink(error_path)


def _read_error_envelope(path: str, node_name: str) -> dict[str, Any] | None:
    """Read and coerce the envelope file the script may have written.

    Returns ``None`` if the file is missing, empty, unreadable, or
    contains a malformed envelope. A malformed envelope is downgraded to
    an ``internal.schema_violation`` envelope so the engine still sees
    a typed failure (rather than silently dropping the script's signal).
    """
    # Lazy import: package __init__ pulls in workflow → circular.
    from conductor.engine.errors import (  # noqa: PLC0415
        EnvelopeValidationError,
        coerce_envelope,
        make_schema_violation,
    )

    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        envelope = make_schema_violation(
            node_name=node_name,
            source="script",
            original_message=f"CONDUCTOR_ERROR_OUT file is not valid JSON: {exc}",
        )
        return cast("dict[str, Any]", envelope)
    try:
        return cast("dict[str, Any]", coerce_envelope(parsed))
    except EnvelopeValidationError as exc:
        envelope = make_schema_violation(
            node_name=node_name,
            source="script",
            original_message=f"Malformed envelope in CONDUCTOR_ERROR_OUT: {exc}",
        )
        return cast("dict[str, Any]", envelope)


def _synthesize_script_error(*, exit_code: int, stderr_tail: str, command: str) -> dict[str, Any]:
    """Wrap the make_script_error helper with the same lazy-import dance."""
    from conductor.engine.errors import make_script_error  # noqa: PLC0415

    # Truncate stderr so we don't drag megabytes of compiler noise into the envelope.
    tail = stderr_tail[-2000:] if len(stderr_tail) > 2000 else stderr_tail
    return cast(
        "dict[str, Any]",
        make_script_error(exit_code=exit_code, stderr_tail=tail, command=command),
    )
