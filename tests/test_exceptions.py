"""Test that the exceptions module works correctly."""

from conductor.exceptions import (
    ConductorError,
    ConfigurationError,
    ExecutionError,
    HumanGateError,
    MaxIterationsError,
    ProviderError,
    RetryableError,
    TemplateError,
    TimeoutError,
    ValidationError,
)


class TestConductorError:
    """Tests for the base ConductorError class."""

    def test_basic_error_message(self) -> None:
        """Test that basic error message is preserved."""
        error = ConductorError("Something went wrong")
        assert str(error) == "Something went wrong"

    def test_error_with_suggestion(self) -> None:
        """Test that error message includes suggestion when provided."""
        error = ConductorError("Something went wrong", suggestion="Try doing X instead")
        assert "Something went wrong" in str(error)
        assert "💡 Suggestion: Try doing X instead" in str(error)

    def test_suggestion_attribute(self) -> None:
        """Test that suggestion attribute is accessible."""
        error = ConductorError("Error", suggestion="Fix it")
        assert error.suggestion == "Fix it"

    def test_no_suggestion(self) -> None:
        """Test that suggestion is None when not provided."""
        error = ConductorError("Error")
        assert error.suggestion is None

    def test_error_with_file_path(self) -> None:
        """Test that error includes file path when provided."""
        error = ConductorError(
            "Parse error",
            file_path="/path/to/workflow.yaml",
        )
        assert "/path/to/workflow.yaml" in str(error)
        assert "📍 Location: File: /path/to/workflow.yaml" in str(error)

    def test_error_with_line_number(self) -> None:
        """Test that error includes line number when provided."""
        error = ConductorError(
            "Invalid syntax",
            line_number=42,
        )
        assert "Line: 42" in str(error)

    def test_error_with_file_and_line(self) -> None:
        """Test that error includes both file path and line number."""
        error = ConductorError(
            "Missing field",
            file_path="/path/to/file.yaml",
            line_number=15,
        )
        error_str = str(error)
        assert "File: /path/to/file.yaml" in error_str
        assert "Line: 15" in error_str

    def test_error_type_property(self) -> None:
        """Test that error_type returns the class name."""
        error = ConductorError("Test")
        assert error.error_type == "ConductorError"


class TestConfigurationError:
    """Tests for ConfigurationError."""

    def test_inherits_from_conductor_error(self) -> None:
        """Test ConfigurationError inherits from ConductorError."""
        error = ConfigurationError("Bad config")
        assert isinstance(error, ConductorError)
        assert str(error) == "Bad config"

    def test_with_field_path(self) -> None:
        """Test ConfigurationError with field_path."""
        error = ConfigurationError(
            "Invalid value",
            field_path="workflow.limits.max_iterations",
        )
        assert "workflow.limits.max_iterations" in str(error)
        assert "📋 Field:" in str(error)

    def test_auto_generates_entry_point_suggestion(self) -> None:
        """Test auto-generated suggestion for entry_point errors."""
        error = ConfigurationError(
            "entry_point 'missing' not found in agents",
        )
        assert error.suggestion is not None
        assert "agent name" in error.suggestion.lower()

    def test_auto_generates_route_suggestion(self) -> None:
        """Test auto-generated suggestion for route errors."""
        error = ConfigurationError(
            "Agent 'foo' routes to unknown agent 'bar'",
        )
        assert error.suggestion is not None
        assert "route" in error.suggestion.lower()

    def test_custom_suggestion_overrides_auto(self) -> None:
        """Test that custom suggestion overrides auto-generated."""
        error = ConfigurationError(
            "entry_point missing",
            suggestion="Custom suggestion",
        )
        assert error.suggestion == "Custom suggestion"


class TestValidationError:
    """Tests for ValidationError."""

    def test_inherits_from_conductor_error(self) -> None:
        """Test ValidationError inherits from ConductorError."""
        error = ValidationError("Invalid data")
        assert isinstance(error, ConductorError)

    def test_with_field_and_expected_type(self) -> None:
        """Test ValidationError with field and type info."""
        error = ValidationError(
            "Type mismatch",
            field_name="max_iterations",
            expected_type="number",
            actual_value="abc",
        )
        assert error.suggestion is not None
        assert "number" in error.suggestion


class TestTemplateError:
    """Tests for TemplateError."""

    def test_inherits_from_conductor_error(self) -> None:
        """Test TemplateError inherits from ConductorError."""
        error = TemplateError("Template syntax error")
        assert isinstance(error, ConductorError)

    def test_with_undefined_variable(self) -> None:
        """Test TemplateError with undefined variable."""
        error = TemplateError(
            "Undefined variable",
            undefined_variable="missing_var",
        )
        assert error.suggestion is not None
        assert "missing_var" in error.suggestion

    def test_with_syntax_error(self) -> None:
        """Test TemplateError with syntax error."""
        error = TemplateError(
            "Template syntax error: unbalanced braces",
        )
        assert error.suggestion is not None
        assert "syntax" in error.suggestion.lower()


class TestProviderError:
    """Tests for ProviderError."""

    def test_inherits_from_conductor_error(self) -> None:
        """Test ProviderError inherits from ConductorError."""
        error = ProviderError("Provider failed")
        assert isinstance(error, ConductorError)

    def test_401_is_not_retryable(self) -> None:
        """Test that 401 errors are not retryable."""
        error = ProviderError("Unauthorized", status_code=401)
        assert not error.is_retryable
        assert "authentication" in error.suggestion.lower()

    def test_403_is_not_retryable(self) -> None:
        """Test that 403 errors are not retryable."""
        error = ProviderError("Forbidden", status_code=403)
        assert not error.is_retryable
        assert "permission" in error.suggestion.lower()

    def test_404_is_not_retryable(self) -> None:
        """Test that 404 errors are not retryable."""
        error = ProviderError("Not found", status_code=404)
        assert not error.is_retryable

    def test_429_is_retryable(self) -> None:
        """Test that 429 errors are retryable."""
        error = ProviderError("Rate limited", status_code=429)
        assert error.is_retryable
        assert "rate limit" in error.suggestion.lower()

    def test_500_is_retryable(self) -> None:
        """Test that 500 errors are retryable."""
        error = ProviderError("Internal server error", status_code=500)
        assert error.is_retryable

    def test_503_is_retryable(self) -> None:
        """Test that 503 errors are retryable."""
        error = ProviderError("Service unavailable", status_code=503)
        assert error.is_retryable

    def test_connection_error_is_retryable(self) -> None:
        """Test that connection errors are retryable."""
        error = ProviderError("Connection refused")
        assert error.is_retryable

    def test_retryable_override(self) -> None:
        """Test that is_retryable can be overridden."""
        error = ProviderError("Custom", status_code=401, is_retryable=True)
        assert error.is_retryable


class TestSpecificErrors:
    """Tests for specific error types."""

    def test_execution_error(self) -> None:
        """Test ExecutionError inherits from ConductorError."""
        error = ExecutionError("Execution failed")
        assert isinstance(error, ConductorError)

    def test_execution_error_with_agent_name(self) -> None:
        """Test ExecutionError with agent_name."""
        error = ExecutionError(
            "Failed during execution",
            agent_name="answerer",
        )
        assert error.agent_name == "answerer"


class TestMaxIterationsError:
    """Tests for MaxIterationsError."""

    def test_inherits_from_execution_error(self) -> None:
        """Test MaxIterationsError inherits from ExecutionError."""
        error = MaxIterationsError(
            "Too many iterations",
            iterations=10,
            max_iterations=10,
        )
        assert isinstance(error, ExecutionError)
        assert isinstance(error, ConductorError)

    def test_attributes_are_set(self) -> None:
        """Test that all attributes are properly set."""
        error = MaxIterationsError(
            "Too many iterations",
            iterations=5,
            max_iterations=10,
            agent_history=["agent1", "agent2", "agent1"],
            suggestion="Increase max_iterations",
        )
        assert error.iterations == 5
        assert error.max_iterations == 10
        assert error.agent_history == ["agent1", "agent2", "agent1"]
        assert error.suggestion == "Increase max_iterations"

    def test_default_agent_history(self) -> None:
        """Test that agent_history defaults to empty list."""
        error = MaxIterationsError(
            "Too many iterations",
            iterations=10,
            max_iterations=10,
        )
        assert error.agent_history == []

    def test_auto_generates_loop_suggestion(self) -> None:
        """Test auto-generated suggestion for obvious loop."""
        error = MaxIterationsError(
            "Too many iterations",
            iterations=10,
            max_iterations=10,
            agent_history=["a", "b", "a", "b", "a", "b"],
        )
        assert "looping" in error.suggestion.lower()


class TestTimeoutError:
    """Tests for TimeoutError."""

    def test_inherits_from_execution_error(self) -> None:
        """Test TimeoutError inherits from ExecutionError."""
        error = TimeoutError(
            "Workflow timed out",
            elapsed_seconds=600.0,
            timeout_seconds=600.0,
        )
        assert isinstance(error, ExecutionError)
        assert isinstance(error, ConductorError)

    def test_attributes_are_set(self) -> None:
        """Test that all attributes are properly set."""
        error = TimeoutError(
            "Workflow timed out",
            elapsed_seconds=300.5,
            timeout_seconds=600.0,
            current_agent="reviewer",
            suggestion="Increase timeout_seconds",
        )
        assert error.elapsed_seconds == 300.5
        assert error.timeout_seconds == 600.0
        assert error.current_agent == "reviewer"
        assert error.suggestion == "Increase timeout_seconds"

    def test_default_current_agent(self) -> None:
        """Test that current_agent defaults to None."""
        error = TimeoutError(
            "Workflow timed out",
            elapsed_seconds=600.0,
            timeout_seconds=600.0,
        )
        assert error.current_agent is None

    def test_auto_generates_suggestion(self) -> None:
        """Test auto-generated suggestion."""
        error = TimeoutError(
            "Workflow timed out",
            elapsed_seconds=600.0,
            timeout_seconds=600.0,
            current_agent="slow_agent",
        )
        assert "timeout_seconds" in error.suggestion
        assert "slow_agent" in error.suggestion


class TestHumanGateError:
    """Tests for HumanGateError."""

    def test_inherits_from_execution_error(self) -> None:
        """Test HumanGateError inherits from ExecutionError."""
        error = HumanGateError("User cancelled")
        assert isinstance(error, ExecutionError)
        assert isinstance(error, ConductorError)

    def test_with_suggestion(self) -> None:
        """Test HumanGateError with suggestion."""
        error = HumanGateError("Invalid option", suggestion="Choose a valid option")
        assert "Invalid option" in str(error)
        assert "💡 Suggestion: Choose a valid option" in str(error)

    def test_with_gate_name(self) -> None:
        """Test HumanGateError with gate_name."""
        error = HumanGateError(
            "Gate failed",
            gate_name="approval_gate",
        )
        assert error.gate_name == "approval_gate"


class TestRetryableError:
    """Tests for RetryableError."""

    def test_basic_creation(self) -> None:
        """Test basic RetryableError creation."""
        error = RetryableError("Temporary failure")
        assert isinstance(error, ConductorError)
        assert error.attempt == 1
        assert error.max_attempts == 3

    def test_with_original_error(self) -> None:
        """Test RetryableError with original error."""
        original = ValueError("Original error")
        error = RetryableError("Wrapped", original_error=original)
        assert error.original_error is original

    def test_suggestion_shows_attempt_info(self) -> None:
        """Test that suggestion shows attempt information."""
        error = RetryableError("Failed", attempt=2, max_attempts=3)
        assert "2/3" in error.suggestion

    def test_suggestion_when_exhausted(self) -> None:
        """Test suggestion when all attempts exhausted."""
        error = RetryableError("Failed", attempt=3, max_attempts=3)
        assert "exhausted" in error.suggestion
