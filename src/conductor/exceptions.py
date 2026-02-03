"""Exception hierarchy for Conductor.

This module defines all custom exceptions used throughout the application.
All exceptions inherit from ConductorError and support optional suggestions
to help users resolve issues.
"""

from __future__ import annotations


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
