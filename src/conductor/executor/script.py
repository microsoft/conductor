"""Script execution for Conductor workflow steps.

This module provides the ScriptExecutor class for running shell commands
as workflow steps, capturing stdout/stderr and exit codes.
"""

from __future__ import annotations

# These module imports are patch anchors for the existing test suite. The
# local backend uses the same module singletons, so patches applied here reach
# the extracted subprocess implementation.
import os  # noqa: F401
import shutil  # noqa: F401
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from conductor.exceptions import ConfigurationError, ExecutionError
from conductor.execution import (
    CommandResult,
    CommandSpec,
    LocalRunnerBackend,
    ResolvedExecutionSpec,
    RunnerBackend,
    WorkspaceLease,
)
from conductor.execution.errors import ExecutionSpecError
from conductor.executor.template import TemplateRenderer


def _verbose_log(message: str, style: str = "dim") -> None:
    """Log a verbose message via the CLI run module.

    Uses a deferred import to avoid a circular import between executor.script
    and cli.run (cli.run imports WorkflowEngine which imports executor modules).
    """
    from conductor.cli.run import verbose_log

    verbose_log(message, style)


if TYPE_CHECKING:
    from conductor.config.schema import ScriptStepDef


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
    """Executes script steps via a batch-capable runner backend.

    Renders command and argument fields via the template renderer, validates
    and counts UTF-8 stdin payloads, delegates process execution to a
    :class:`~conductor.execution.RunnerBackend` (defaulting to
    :class:`~conductor.execution.LocalRunnerBackend`), and maps data-shaped
    outcomes back to the existing :class:`ScriptOutput` /
    :class:`~conductor.exceptions.ExecutionError` contract.

    Example::

        executor = ScriptExecutor()
        output = await executor.execute(agent, context)
        print(output.stdout, output.exit_code)
    """

    def __init__(self, backend: RunnerBackend | None = None) -> None:
        """Initialize the executor with a batch-capable runner backend."""
        self.renderer = TemplateRenderer()
        self._backend = backend or LocalRunnerBackend()
        if not self._backend.capabilities().batch:
            raise ExecutionError("Script execution backend does not support batch commands")

    async def execute(
        self,
        agent: ScriptStepDef,
        context: dict[str, Any],
        *,
        lease: WorkspaceLease | None = None,
        backend: RunnerBackend | None = None,
        secret_env: dict[str, str] | None = None,
        execution: ResolvedExecutionSpec | None = None,
        inherit_control_environment: bool = True,
    ) -> ScriptOutput:
        """Execute a script step.

        Renders command, argument, and working directory fields via the template
        renderer, validates and encodes any UTF-8 stdin payload, delegates command
        execution to the runner backend with the optional workspace lease, and
        maps data-shaped backend outcomes back to :class:`ScriptOutput` or raises
        :class:`~conductor.exceptions.ExecutionError`.

        Args:
            agent: Agent definition with ``type="script"``.
            context: Workflow context for template rendering.
            lease: Optional :class:`~conductor.execution.WorkspaceLease` threaded
                through to the backend's ``run_command``, or ``None`` when the
                caller has none (default None).
            backend: Optional per-call backend override. When omitted, uses the
                backend supplied at construction.
            secret_env: Optional resolved secret deliveries (environment
                variable name → plaintext value) merged verbatim into the
                command's environment. ``None`` (the default) keeps the
                pre-secrets behavior exactly.
            execution: Optional resolved container execution payload for the
                command, or ``None`` for default (local) execution. The
                executor only carries the payload into :class:`CommandSpec`;
                the compiled run manifest guarantees backend/payload
                consistency. The local backend ignores the payload. When a
                container backend receives ``None`` (a manifest/engine wiring
                mismatch), it raises
                :class:`~conductor.execution.errors.ExecutionSpecError`, which
                this method translates into
                :class:`~conductor.exceptions.ConfigurationError` so engine
                and CLI layers keep their familiar error vocabulary.
            inherit_control_environment: Effective inheritance policy from the
                compiled run manifest. When False, the backend runs the command
                on a minimal environment plus ``env`` instead of merging over
                the control process's environment. Defaults to True (the local
                backend's long-standing behavior).

        Returns:
            :class:`ScriptOutput` with stdout, stderr, exit_code, and stdin_bytes.

        Raises:
            ExecutionError: If the script times out, cannot be started, or the
                stdin payload is not valid UTF-8.
        """
        # Render command and args with Jinja2
        # command is guaranteed non-None by the model validator when type="script"
        assert agent.command is not None
        rendered_command = self.renderer.render(agent.command, context)
        rendered_args = [self.renderer.render(arg, context) for arg in agent.args]
        rendered_working_dir = (
            self.renderer.render(agent.working_dir, context) if agent.working_dir else None
        )
        # Declared ``env`` values pass through verbatim — they are NOT
        # Jinja-rendered, matching the long-standing contract. Secret
        # deliveries merge on top verbatim as well (a secret value containing
        # ``{{`` must reach the child byte-identically). A name collision
        # between the two is rejected as a ``ConfigurationError`` by the
        # engine's ``_execute_script`` before this call, so the plain update
        # here cannot silently shadow an authored variable.
        merged_env = dict(agent.env)
        if secret_env:
            merged_env.update(secret_env)

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

        execution_backend = backend or self._backend
        if not execution_backend.capabilities().batch:
            raise ExecutionError("Script execution backend does not support batch commands")

        spec = CommandSpec(
            command=rendered_command,
            args=tuple(rendered_args),
            working_dir=rendered_working_dir,
            env=merged_env,
            inherit_control_environment=inherit_control_environment,
            stdin=stdin_payload,
            timeout=agent.timeout,
            execution=execution,
            # Step identity travels with the payload as one unit: a local run
            # must produce the byte-identical legacy spec (name=None), while a
            # container run gets the step name for realm-side labels/names.
            name=agent.name if execution is not None else None,
        )
        try:
            result = await execution_backend.run_command(
                spec,
                lease,
                diagnostics=self._make_diagnostics(),
            )
        except ExecutionSpecError as exc:
            # The execution leaf speaks its own specification-error type; the
            # engine/CLI boundary translates it into the familiar
            # ConfigurationError while preserving the original as __cause__.
            raise ConfigurationError(str(exc)) from exc

        if result.outcome == "command_not_found":
            cause = self._reconstruct_start_error(result)
            hint = ""
            if sys.platform == "win32":
                hint = (
                    " Hint: on Windows, include the file extension (e.g. .exe) "
                    "or use an absolute path."
                )
            raise ExecutionError(
                f"Script '{agent.name}': command not found: '{result.resolved_command}'"
                f" (working_dir={rendered_working_dir or 'cwd'}){hint}",
                agent_name=agent.name,
                suggestion=f"Ensure '{result.resolved_command}' is installed and on PATH",
            ) from cause
        if result.outcome == "start_failed":
            cause = self._reconstruct_start_error(result)
            raise ExecutionError(
                f"Script '{agent.name}' failed to start: {cause}",
                agent_name=agent.name,
            ) from cause
        if result.outcome == "timed_out":
            raise ExecutionError(
                f"Script '{agent.name}' timed out after {spec.timeout}s",
                agent_name=agent.name,
            ) from None

        assert result.exit_code is not None
        return ScriptOutput(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            stdin_bytes=len(stdin_payload) if stdin_payload is not None else None,
        )

    @staticmethod
    def _make_diagnostics() -> Callable[[str], None]:
        """Adapt backend diagnostics to the existing verbose logger.

        Backend diagnostics carry the child's stderr, so the text is scrubbed
        through the run-scoped redactor (when one is active in the current
        context) before reaching the verbose console / ``--log-file``: a
        script that prints a delivered credential to stderr would otherwise
        write it to both. The live subprocess result is untouched.
        """

        def _log_diagnostic(message: str) -> None:
            from conductor import redaction

            redactor = redaction.current()
            if redactor is not None and redactor.active:
                message = redactor.scrub(message)
            _verbose_log(message)

        return _log_diagnostic

    @staticmethod
    def _reconstruct_start_error(result: CommandResult) -> OSError:
        """Reconstruct the spawn exception used as ``ExecutionError.__cause__``."""
        start_error = result.start_error
        assert start_error is not None
        cause_type = FileNotFoundError if start_error.kind == "file_not_found" else OSError
        if start_error.errno is not None:
            return cause_type(start_error.errno, start_error.message, start_error.filename)
        return cause_type(start_error.message)
