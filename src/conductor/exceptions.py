"""Exception hierarchy for Conductor.

This module defines all custom exceptions used throughout the application.
All exceptions inherit from ConductorError and support optional suggestions
to help users resolve issues.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from conductor.error_envelope import ErrorEnvelope


class ConductorError(Exception):
    """Base exception for all Conductor errors.

    All custom exceptions in the application inherit from this class.
    Supports optional file path, line number, and suggestion to help
    users understand what went wrong and how to fix it.

    Attributes:
        suggestion: Optional actionable advice for resolving the error.
        file_path: Optional path to the file where the error occurred.
        line_number: Optional line number where the error occurred.
    """

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
    ) -> None:
        """Initialize a ConductorError.

        Args:
            message: The error message describing what went wrong.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
        """
        self.suggestion = suggestion
        self.file_path = file_path
        self.line_number = line_number
        super().__init__(message)

    def __str__(self) -> str:
        """Format the error message with location and suggestion."""
        msg = super().__str__()

        # Add location info if available
        if self.file_path or self.line_number:
            location_parts = []
            if self.file_path:
                location_parts.append(f"File: {self.file_path}")
            if self.line_number:
                location_parts.append(f"Line: {self.line_number}")
            location = ", ".join(location_parts)
            msg += f"\n\n📍 Location: {location}"

        if self.suggestion:
            msg += f"\n\n💡 Suggestion: {self.suggestion}"
        return msg

    @property
    def error_type(self) -> str:
        """Return the type name for display purposes."""
        return self.__class__.__name__


class ConfigurationError(ConductorError):
    """Raised when workflow configuration is invalid.

    This includes malformed YAML, missing required fields, or invalid
    configuration values.

    Attributes:
        field_path: Optional path to the invalid field (e.g., 'workflow.limits.max_iterations').
    """

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
        field_path: str | None = None,
    ) -> None:
        """Initialize a ConfigurationError.

        Args:
            message: The error message describing what went wrong.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
            field_path: Optional path to the invalid configuration field.
        """
        self.field_path = field_path

        # Auto-generate suggestion for common configuration errors
        if suggestion is None:
            suggestion = self._generate_suggestion(message, field_path)

        super().__init__(message, suggestion, file_path, line_number)

    def _generate_suggestion(self, message: str, field_path: str | None) -> str | None:
        """Generate helpful suggestions based on error message and field."""
        msg_lower = message.lower()

        if "entry_point" in msg_lower:
            return "Ensure entry_point matches an agent name defined in the 'agents' list"

        if "route" in msg_lower and "unknown agent" in msg_lower:
            return "Check that all route targets match agent names or use '$end' to terminate"

        if "required" in msg_lower:
            return f"Add the missing required field{' at ' + field_path if field_path else ''}"

        if "type" in msg_lower or "validation" in msg_lower:
            return "Check the field type matches the expected schema type"

        return None

    def __str__(self) -> str:
        """Format the error message with field path, location and suggestion."""
        msg = self.args[0] if self.args else ""

        # Add field path if available
        if self.field_path:
            msg += f"\n\n📋 Field: {self.field_path}"

        # Add location info if available
        if self.file_path or self.line_number:
            location_parts = []
            if self.file_path:
                location_parts.append(f"File: {self.file_path}")
            if self.line_number:
                location_parts.append(f"Line: {self.line_number}")
            location = ", ".join(location_parts)
            msg += f"\n\n📍 Location: {location}"

        if self.suggestion:
            msg += f"\n\n💡 Suggestion: {self.suggestion}"
        return msg


class ValidationError(ConductorError):
    """Raised when data validation fails.

    This includes Pydantic validation errors, schema mismatches, and
    cross-field validation failures.
    """

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
        field_name: str | None = None,
        expected_type: str | None = None,
        actual_value: str | None = None,
    ) -> None:
        """Initialize a ValidationError.

        Args:
            message: The error message describing what went wrong.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
            field_name: Optional name of the field that failed validation.
            expected_type: Optional expected type for the field.
            actual_value: Optional string representation of the actual value.
        """
        self.field_name = field_name
        self.expected_type = expected_type
        self.actual_value = actual_value

        # Auto-generate suggestion for validation errors
        if suggestion is None and expected_type:
            suggestion = f"Expected type '{expected_type}'"
            if actual_value:
                suggestion += f", but got '{actual_value}'"

        super().__init__(message, suggestion, file_path, line_number)


class TemplateError(ConductorError):
    """Raised when Jinja2 template rendering fails.

    This includes undefined variables, syntax errors, and filter errors
    in template expressions.
    """

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
        template_string: str | None = None,
        undefined_variable: str | None = None,
    ) -> None:
        """Initialize a TemplateError.

        Args:
            message: The error message describing what went wrong.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
            template_string: Optional original template string.
            undefined_variable: Optional name of the undefined variable.
        """
        self.template_string = template_string
        self.undefined_variable = undefined_variable

        # Auto-generate suggestion for template errors
        if suggestion is None:
            if undefined_variable:
                suggestion = (
                    f"Variable '{undefined_variable}' is not defined. "
                    "Check available context variables: workflow.input.*, agent_name.output.*"
                )
            elif "syntax" in message.lower():
                suggestion = (
                    "Check Jinja2 template syntax: ensure {{ }} are balanced "
                    "and filters use | correctly"
                )

        super().__init__(message, suggestion, file_path, line_number)


class ProviderError(ConductorError):
    """Raised when an agent provider encounters an error.

    This includes SDK initialization failures, API errors, and
    connection issues with the underlying provider.
    """

    # HTTP status codes that should NOT be retried
    NON_RETRYABLE_CODES = {400, 401, 403, 404, 422}

    # HTTP status codes that SHOULD be retried
    RETRYABLE_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
        status_code: int | None = None,
        provider_name: str | None = None,
        is_retryable: bool | None = None,
    ) -> None:
        """Initialize a ProviderError.

        Args:
            message: The error message describing what went wrong.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
            status_code: Optional HTTP status code from the provider.
            provider_name: Optional name of the provider.
            is_retryable: Optional override for retryability. If None, determined by status_code.
        """
        self.status_code = status_code
        self.provider_name = provider_name

        # Determine retryability
        if is_retryable is not None:
            self._is_retryable = is_retryable
        elif status_code is not None:
            self._is_retryable = status_code in self.RETRYABLE_CODES
        else:
            # Connection errors are generally retryable
            self._is_retryable = "connection" in message.lower() or "timeout" in message.lower()

        # Auto-generate suggestion based on status code
        if suggestion is None:
            suggestion = self._generate_suggestion(message, status_code)

        super().__init__(message, suggestion, file_path, line_number)

    def _generate_suggestion(self, message: str, status_code: int | None) -> str | None:
        """Generate helpful suggestions based on error status code."""
        if status_code == 401:
            return "Check your authentication credentials and ensure GITHUB_TOKEN is set correctly"
        elif status_code == 403:
            return "Check your access permissions. You may not have access to this resource"
        elif status_code == 404:
            return "The requested resource was not found. Check the provider configuration"
        elif status_code == 429:
            return "Rate limit exceeded. The request will be automatically retried"
        elif status_code and 500 <= status_code < 600:
            return "Server error. The request will be automatically retried"
        elif "connection" in message.lower():
            return "Check your network connection and try again"
        return None

    @property
    def is_retryable(self) -> bool:
        """Return whether this error should trigger a retry."""
        return self._is_retryable


class ExecutionError(ConductorError):
    """Raised when workflow execution fails.

    Base class for execution-related errors. More specific execution
    errors inherit from this class.
    """

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
        agent_name: str | None = None,
    ) -> None:
        """Initialize an ExecutionError.

        Args:
            message: The error message describing what went wrong.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
            agent_name: Optional name of the agent where the error occurred.
        """
        self.agent_name = agent_name
        super().__init__(message, suggestion, file_path, line_number)


class UnhandledNodeError(ExecutionError):
    """Raised when a typed node failure has no matching error route."""

    def __init__(
        self,
        *,
        agent_name: str,
        error: ErrorEnvelope,
        output: dict[str, Any],
    ) -> None:
        """Initialize an unhandled typed failure."""
        self.error = error
        self.output = output
        super().__init__(
            f"Step '{agent_name}' raised unhandled error '{error.kind}': {error.message}",
            agent_name=agent_name,
            suggestion=(
                f"Add an on_error route for '{error.kind}' or a catch-all on_error: true route"
            ),
        )


class MaxIterationsError(ExecutionError):
    """Raised when a workflow exceeds its maximum iteration limit.

    This is a safety mechanism to prevent infinite loops in workflows
    with loop-back routing patterns.

    Attributes:
        iterations: The number of iterations that were executed.
        max_iterations: The configured maximum number of iterations.
        agent_history: List of agents that were executed before the limit.
    """

    def __init__(
        self,
        message: str,
        *,
        iterations: int,
        max_iterations: int,
        agent_history: list[str] | None = None,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
    ) -> None:
        """Initialize a MaxIterationsError.

        Args:
            message: The error message describing what went wrong.
            iterations: The number of iterations that were executed.
            max_iterations: The configured maximum number of iterations.
            agent_history: List of agent names executed before the limit.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
        """
        self.iterations = iterations
        self.max_iterations = max_iterations
        self.agent_history = agent_history or []

        # Auto-generate suggestion
        if suggestion is None:
            # Check for obvious loop patterns
            if agent_history and len(agent_history) >= 2:
                recent = agent_history[-5:]
                if len(set(recent)) <= 2:
                    loop_agents = list(set(recent))
                    suggestion = (
                        f"Workflow appears to be looping between agents: "
                        f"{', '.join(loop_agents)}. Check routing conditions to ensure "
                        "the loop can terminate"
                    )
                else:
                    suggestion = (
                        f"Increase workflow.limits.max_iterations "
                        f"(currently {max_iterations}) or check routing logic "
                        "for infinite loops"
                    )
            else:
                suggestion = (
                    f"Increase workflow.limits.max_iterations "
                    f"(currently {max_iterations}) or check routing logic"
                )

        super().__init__(message, suggestion, file_path, line_number)


class TimeoutError(ExecutionError):
    """Raised when a workflow exceeds its timeout limit.

    This is a safety mechanism to prevent workflows from running
    indefinitely.

    Attributes:
        elapsed_seconds: The time elapsed before the timeout.
        timeout_seconds: The configured timeout limit.
        current_agent: The agent that was executing when timeout occurred.
    """

    def __init__(
        self,
        message: str,
        *,
        elapsed_seconds: float,
        timeout_seconds: float,
        current_agent: str | None = None,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
    ) -> None:
        """Initialize a TimeoutError.

        Args:
            message: The error message describing what went wrong.
            elapsed_seconds: The time elapsed before the timeout.
            timeout_seconds: The configured timeout limit.
            current_agent: The agent that was executing when timeout occurred.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
        """
        self.elapsed_seconds = elapsed_seconds
        self.timeout_seconds = timeout_seconds
        self.current_agent = current_agent

        # Auto-generate suggestion
        if suggestion is None:
            suggestion = (
                f"Increase workflow.limits.timeout_seconds (currently {int(timeout_seconds)}s)"
            )
            if current_agent:
                suggestion += f". Timeout occurred while executing agent '{current_agent}'"

        super().__init__(message, suggestion, file_path, line_number)


class AgentTimeoutError(TimeoutError):
    """Raised when an individual agent exceeds its per-agent timeout_seconds limit.

    This is distinct from the workflow-level TimeoutError. An AgentTimeoutError
    is raised when an agent's ``timeout_seconds`` configuration causes a hard
    cancellation via ``asyncio.wait_for()``.

    The timed-out agent name is available via ``current_agent`` (inherited
    from ``TimeoutError``) and ``agent_name`` (inherited from
    ``ExecutionError``). Both are set to the same value for consistency
    with consumers that check either attribute.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        elapsed_seconds: float,
        timeout_seconds: float,
    ) -> None:
        """Initialize an AgentTimeoutError.

        Args:
            agent_name: Name of the agent that timed out.
            elapsed_seconds: The time elapsed before the timeout.
            timeout_seconds: The configured per-agent timeout limit.
        """
        message = f"Agent '{agent_name}' exceeded its timeout ({timeout_seconds}s)"
        suggestion = f"Increase timeout_seconds for agent '{agent_name}' or optimize its execution"
        super().__init__(
            message,
            elapsed_seconds=elapsed_seconds,
            timeout_seconds=timeout_seconds,
            current_agent=agent_name,
            suggestion=suggestion,
        )
        # ExecutionError.__init__ defaults agent_name to None — set it here
        # so both .agent_name and .current_agent resolve to the same value.
        self.agent_name = agent_name


class BudgetExceededError(ExecutionError):
    """Raised when a workflow exceeds its cost budget in enforce mode.

    This is a safety mechanism to prevent runaway spending in agentic
    workflows. Only raised when ``budget_mode`` is ``enforce``.

    Attributes:
        budget_usd: The configured budget limit.
        spent_usd: The actual amount spent when the limit was exceeded.
        current_agent: The agent that was executing when the budget was exceeded.
    """

    def __init__(
        self,
        message: str,
        *,
        budget_usd: float,
        spent_usd: float,
        current_agent: str | None = None,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
    ) -> None:
        """Initialize a BudgetExceededError.

        Args:
            message: The error message describing the overshoot.
            budget_usd: The configured budget limit in USD.
            spent_usd: The cumulative cost when the limit was exceeded.
            current_agent: The agent that was executing when the budget was
                exceeded. Forwarded to ``ExecutionError.agent_name`` so both
                ``.agent_name`` and ``.current_agent`` resolve to the same value.
            suggestion: Optional advice. When omitted, an actionable default is
                generated.
            file_path: Optional path to the workflow file.
            line_number: Optional line number within the workflow file.
        """
        self.budget_usd = budget_usd
        self.spent_usd = spent_usd
        self.current_agent = current_agent

        if suggestion is None:
            suggestion = (
                f"Increase limits.budget_usd (currently ${budget_usd:.2f}) "
                f"or switch to budget_mode: audit to continue without enforcement. "
                f"Resuming this workflow starts a fresh budget window "
                f"(cumulative spend resets to $0)"
            )
            if current_agent:
                suggestion += f". Budget exceeded after agent '{current_agent}'"

        super().__init__(
            message,
            suggestion,
            file_path,
            line_number,
            agent_name=current_agent,
        )
        # ExecutionError.__init__ already stored agent_name; keep current_agent
        # as the canonical attribute used by callers/tests.
        self.current_agent = current_agent


class HumanGateError(ExecutionError):
    """Raised when a human gate encounters an error.

    This includes invalid gate configurations, user cancellation,
    and input validation failures at human gates.
    """

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
        gate_name: str | None = None,
    ) -> None:
        """Initialize a HumanGateError.

        Args:
            message: The error message describing what went wrong.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
            gate_name: Optional name of the human gate.
        """
        self.gate_name = gate_name
        super().__init__(message, suggestion, file_path, line_number)


class WorkflowTerminated(ExecutionError):
    """Raised when a ``type: terminate`` step ends the workflow with ``status: failed``.

    This is an *explicit* termination signal — not an unexpected failure. It carries
    the rendered output, reason, and the name of the terminate step that fired so
    the CLI, event consumers, and dashboard can distinguish it from generic errors.

    The engine's top-level ``except WorkflowTerminated`` handler emits
    ``workflow_failed`` with ``is_explicit: True`` for this exception and
    intentionally skips checkpoint creation. The skip is a property of the
    handler, not of the type — a future refactor that moves checkpointing into
    a nested handler would need to preserve that contract.

    Attributes:
        output: The rendered final output dict (from ``output_template`` if
            provided, else the workflow-level ``output:`` mapping).
        reason: The rendered termination reason (Jinja2-resolved against context).
        terminated_by: Alias for ``agent_name`` — the ``name`` of the terminate
            step that fired. Exposed as a property over ``agent_name`` so the
            two cannot drift apart.
        status: Always the string ``"failed"``. Successful terminations return
            from :meth:`WorkflowEngine.run` cleanly and do not raise, so this
            exception only ever represents the failed-termination branch.
    """

    def __init__(
        self,
        message: str,
        *,
        output: dict[str, Any],
        reason: str,
        terminated_by: str,
        suggestion: str | None = None,
    ) -> None:
        """Initialize a WorkflowTerminated exception.

        Args:
            message: The error message describing the termination (typically the
                rendered ``reason``).
            output: The rendered final output dict for the workflow.
            reason: The rendered termination reason.
            terminated_by: Name of the terminate step that fired. Stored on the
                base class as ``agent_name``; exposed here via the
                :attr:`terminated_by` property so both names point at the same
                underlying value.
            suggestion: Optional advice for resolving the termination.
        """
        self.output = output
        self.reason = reason
        super().__init__(message, suggestion=suggestion, agent_name=terminated_by)

    @property
    def terminated_by(self) -> str:
        """Name of the terminate step that fired (alias for ``agent_name``)."""
        # Stored on the base class to keep one source of truth — both `agent_name`
        # (used by generic ConductorError consumers) and `terminated_by` (used by
        # terminate-specific code paths) MUST agree. A property guarantees that.
        return self.agent_name or ""

    @property
    def status(self) -> str:
        """Always ``"failed"``; success terminations return without raising.

        Kept as an attribute (rather than a constant) so call sites that want
        to log or serialise the termination status have a single API regardless
        of whether the engine introduces additional non-binary statuses in the
        future. Today the value is invariantly ``"failed"``.
        """
        return "failed"


class SubworkflowTerminatedError(ExecutionError):
    """Wraps a child sub-workflow's :class:`WorkflowTerminated` at the parent boundary.

    When a child sub-workflow ends with ``type: terminate, status: failed``,
    its :class:`WorkflowTerminated` is converted to this error before
    propagating to the parent. The conversion serves two goals:

    1. The parent's outer exception handler treats this as a normal
       sub-workflow failure (parent ``workflow_failed`` does not inherit
       ``is_explicit: true``) because the parent author did not opt into
       explicit termination — the child did.
    2. The structured payload the child built (its rendered ``output_template``
       dict, the rendered reason, the terminate step's name) is preserved on
       the wrapper so on_error hooks, debugging surfaces, and the CLI can
       inspect it without walking ``__cause__``.

    Attributes:
        terminated_output: The child's rendered final-output dict (from the
            child's ``output_template:`` or its ``output:`` mapping).
        terminated_reason: The child's rendered termination reason.
        terminated_by: The ``name`` of the terminate step inside the child
            workflow that fired. (Distinct from ``agent_name``, which is the
            parent's ``type: workflow`` agent that invoked the child.)
    """

    def __init__(
        self,
        message: str,
        *,
        terminated_output: dict[str, Any],
        terminated_reason: str,
        terminated_by: str,
        suggestion: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """Initialize a SubworkflowTerminatedError.

        Args:
            message: Human-readable description of the boundary downgrade.
            terminated_output: The child's rendered final-output dict.
            terminated_reason: The child's rendered termination reason.
            terminated_by: Name of the terminate step inside the child
                workflow that fired.
            suggestion: Optional advice for resolving the failure.
            agent_name: Name of the parent's ``type: workflow`` agent that
                invoked the child (set on the base class so generic
                ``ExecutionError`` consumers see the parent's agent name,
                not the child's).
        """
        self.terminated_output = terminated_output
        self.terminated_reason = terminated_reason
        self.terminated_by = terminated_by
        super().__init__(message, suggestion=suggestion, agent_name=agent_name)


class InterruptError(ExecutionError):
    """Raised when the user stops a workflow via the interrupt menu.

    This is distinct from ``KeyboardInterrupt`` (Ctrl+C). An ``InterruptError``
    is a cooperative, user-initiated stop that originates from the interrupt
    handler UI after the user selects "Stop workflow".

    Attributes:
        agent_name: Name of the agent that was active when the interrupt occurred.
    """

    def __init__(
        self,
        message: str = "Workflow stopped by user interrupt",
        *,
        agent_name: str | None = None,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
    ) -> None:
        """Initialize an InterruptError.

        Args:
            message: The error message describing what went wrong.
            agent_name: Name of the agent that was active when interrupted.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
        """
        super().__init__(message, suggestion, file_path, line_number, agent_name=agent_name)


class CheckpointError(ConductorError):
    """Raised when checkpoint operations fail.

    This includes checkpoint file I/O failures, invalid checkpoint format,
    version mismatches, and checkpoint not found errors.
    """

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
        checkpoint_path: str | None = None,
    ) -> None:
        """Initialize a CheckpointError.

        Args:
            message: The error message describing what went wrong.
            suggestion: Optional advice for resolving the error.
            file_path: Optional path to the file where the error occurred.
            line_number: Optional line number where the error occurred.
            checkpoint_path: Optional path to the checkpoint file involved.
        """
        self.checkpoint_path = checkpoint_path
        super().__init__(message, suggestion, file_path, line_number)


class RetryableError(ConductorError):
    """Marker class for errors that should trigger automatic retry.

    This is used to wrap errors that occur during SDK calls and
    should be retried with exponential backoff.
    """

    def __init__(
        self,
        message: str,
        original_error: Exception | None = None,
        attempt: int = 1,
        max_attempts: int = 3,
    ) -> None:
        """Initialize a RetryableError.

        Args:
            message: The error message describing what went wrong.
            original_error: The original exception that was caught.
            attempt: The current attempt number.
            max_attempts: The maximum number of retry attempts.
        """
        self.original_error = original_error
        self.attempt = attempt
        self.max_attempts = max_attempts

        suggestion = f"This error is retryable. Attempt {attempt}/{max_attempts}"
        if attempt >= max_attempts:
            suggestion = f"All {max_attempts} retry attempts have been exhausted"

        super().__init__(message, suggestion)
