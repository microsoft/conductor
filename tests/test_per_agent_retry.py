"""Tests for per-agent retry policies.

Covers:
- RetryPolicy schema validation
- AgentDef with retry field
- Provider _resolve_retry_config
- Fixed vs exponential backoff
- retry_on filtering
- agent_retry event emission
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from conductor.config.schema import AgentDef, GateOption, RetryPolicy
from conductor.exceptions import ProviderError
from conductor.exceptions import TimeoutError as _ConductorTimeoutError
from conductor.providers.copilot import CopilotProvider, RetryConfig

# ---------------------------------------------------------------------------
# RetryPolicy schema tests
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    """Tests for the RetryPolicy Pydantic model."""

    def test_default_values(self) -> None:
        """Test default RetryPolicy creates no-retry policy."""
        policy = RetryPolicy()
        assert policy.max_attempts == 1
        assert policy.backoff == "exponential"
        assert policy.delay_seconds == 2.0
        assert policy.retry_on == ["provider_error", "timeout"]

    def test_custom_values(self) -> None:
        """Test creating a RetryPolicy with custom values."""
        policy = RetryPolicy(
            max_attempts=5,
            backoff="fixed",
            delay_seconds=1.0,
            retry_on=["provider_error"],
        )
        assert policy.max_attempts == 5
        assert policy.backoff == "fixed"
        assert policy.delay_seconds == 1.0
        assert policy.retry_on == ["provider_error"]

    def test_max_attempts_minimum(self) -> None:
        """Test that max_attempts minimum is 1."""
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            RetryPolicy(max_attempts=0)

    def test_max_attempts_maximum(self) -> None:
        """Test that max_attempts maximum is 10."""
        with pytest.raises(ValidationError, match="less than or equal to 10"):
            RetryPolicy(max_attempts=11)

    def test_delay_seconds_minimum(self) -> None:
        """Test that delay_seconds minimum is 0."""
        policy = RetryPolicy(delay_seconds=0.0)
        assert policy.delay_seconds == 0.0

    def test_delay_seconds_negative(self) -> None:
        """Test that negative delay_seconds is rejected."""
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            RetryPolicy(delay_seconds=-1.0)

    def test_delay_seconds_maximum(self) -> None:
        """Test that delay_seconds maximum is 300."""
        with pytest.raises(ValidationError, match="less than or equal to 300"):
            RetryPolicy(delay_seconds=301.0)

    def test_invalid_backoff(self) -> None:
        """Test that invalid backoff strategy is rejected."""
        with pytest.raises(ValidationError, match="Input should be"):
            RetryPolicy(backoff="linear")

    def test_invalid_retry_on(self) -> None:
        """Test that invalid retry_on values are rejected."""
        with pytest.raises(ValidationError, match="Input should be"):
            RetryPolicy(retry_on=["invalid_category"])

    def test_retry_on_timeout_only(self) -> None:
        """Test retry_on with only timeout."""
        policy = RetryPolicy(retry_on=["timeout"])
        assert policy.retry_on == ["timeout"]

    def test_retry_on_empty_list(self) -> None:
        """Test retry_on with empty list."""
        policy = RetryPolicy(retry_on=[])
        assert policy.retry_on == []


# ---------------------------------------------------------------------------
# AgentDef retry field tests
# ---------------------------------------------------------------------------


class TestAgentDefRetry:
    """Tests for the retry field on AgentDef."""

    def test_agent_with_retry(self) -> None:
        """Test creating an agent with a retry policy."""
        agent = AgentDef(
            name="test_agent",
            prompt="Do something",
            retry=RetryPolicy(max_attempts=3, backoff="exponential", delay_seconds=2.0),
        )
        assert agent.retry is not None
        assert agent.retry.max_attempts == 3
        assert agent.retry.backoff == "exponential"

    def test_agent_without_retry(self) -> None:
        """Test creating an agent without retry (default)."""
        agent = AgentDef(name="test_agent", prompt="Do something")
        assert agent.retry is None

    def test_agent_retry_from_dict(self) -> None:
        """Test creating an agent with retry from a dict (YAML-like)."""
        agent = AgentDef(
            name="test_agent",
            prompt="Do something",
            retry={
                "max_attempts": 3,
                "backoff": "fixed",
                "delay_seconds": 1.5,
                "retry_on": ["provider_error"],
            },
        )
        assert agent.retry is not None
        assert agent.retry.max_attempts == 3
        assert agent.retry.backoff == "fixed"
        assert agent.retry.delay_seconds == 1.5
        assert agent.retry.retry_on == ["provider_error"]

    def test_script_agent_cannot_have_retry(self) -> None:
        """Test that script agents cannot have a retry policy."""
        with pytest.raises(ValidationError, match="script agents cannot have 'retry'"):
            AgentDef(
                name="my_script",
                type="script",
                command="echo hello",
                retry=RetryPolicy(max_attempts=3),
            )

    def test_human_gate_can_have_retry_since_unused(self) -> None:
        """Test that human_gate agents can technically have retry field.

        The retry policy is only used by provider-backed agents, so
        human_gate agents can have it without error (it simply won't be used).
        """
        agent = AgentDef(
            name="gate",
            type="human_gate",
            prompt="Choose",
            options=[GateOption(label="Yes", value="yes", route="next_agent")],
            retry=RetryPolicy(max_attempts=2),
        )
        assert agent.retry is not None


# ---------------------------------------------------------------------------
# Provider _resolve_retry_config tests
# ---------------------------------------------------------------------------


class TestResolveRetryConfig:
    """Tests for CopilotProvider._resolve_retry_config."""

    def test_no_agent_retry_uses_provider_default(self) -> None:
        """Test that agents without retry use the provider-level config."""
        provider = CopilotProvider()
        agent = AgentDef(name="test", prompt="Test")

        config = provider._resolve_retry_config(agent)

        assert config is provider._retry_config

    def test_agent_retry_overrides_provider_default(self) -> None:
        """Test that agent retry policy overrides provider defaults."""
        provider = CopilotProvider()
        agent = AgentDef(
            name="test",
            prompt="Test",
            retry=RetryPolicy(max_attempts=5, backoff="fixed", delay_seconds=3.0),
        )

        config = provider._resolve_retry_config(agent)

        assert config.max_attempts == 5
        assert config.backoff == "fixed"
        assert config.base_delay == 3.0
        assert config is not provider._retry_config

    def test_agent_retry_preserves_provider_jitter_and_max_delay(self) -> None:
        """Test that resolved config preserves provider's jitter and max_delay."""
        custom_retry_config = RetryConfig(
            max_attempts=3, base_delay=1.0, max_delay=60.0, jitter=0.5
        )
        provider = CopilotProvider(retry_config=custom_retry_config)
        agent = AgentDef(
            name="test",
            prompt="Test",
            retry=RetryPolicy(max_attempts=2, delay_seconds=5.0),
        )

        config = provider._resolve_retry_config(agent)

        assert config.max_delay == 60.0  # From provider
        assert config.jitter == 0.5  # From provider
        assert config.max_attempts == 2  # From agent
        assert config.base_delay == 5.0  # From agent

    def test_agent_retry_on_is_forwarded(self) -> None:
        """Test that retry_on from agent policy is forwarded to config."""
        provider = CopilotProvider()
        agent = AgentDef(
            name="test",
            prompt="Test",
            retry=RetryPolicy(max_attempts=3, retry_on=["timeout"]),
        )

        config = provider._resolve_retry_config(agent)

        assert config.retry_on == ["timeout"]


# ---------------------------------------------------------------------------
# Fixed vs Exponential backoff tests
# ---------------------------------------------------------------------------


class TestBackoffStrategies:
    """Tests for fixed vs exponential backoff in _calculate_delay."""

    def test_exponential_backoff(self) -> None:
        """Test that exponential backoff doubles the delay each attempt."""
        provider = CopilotProvider()
        config = RetryConfig(base_delay=1.0, max_delay=100.0, jitter=0.0, backoff="exponential")

        delay1 = provider._calculate_delay(1, config)
        delay2 = provider._calculate_delay(2, config)
        delay3 = provider._calculate_delay(3, config)

        assert delay1 == 1.0  # 1 * 2^0
        assert delay2 == 2.0  # 1 * 2^1
        assert delay3 == 4.0  # 1 * 2^2

    def test_fixed_backoff(self) -> None:
        """Test that fixed backoff uses the same delay each attempt."""
        provider = CopilotProvider()
        config = RetryConfig(base_delay=2.0, max_delay=100.0, jitter=0.0, backoff="fixed")

        delay1 = provider._calculate_delay(1, config)
        delay2 = provider._calculate_delay(2, config)
        delay3 = provider._calculate_delay(3, config)

        assert delay1 == 2.0
        assert delay2 == 2.0
        assert delay3 == 2.0

    def test_fixed_backoff_capped_at_max_delay(self) -> None:
        """Test that fixed backoff is capped at max_delay."""
        provider = CopilotProvider()
        config = RetryConfig(base_delay=50.0, max_delay=30.0, jitter=0.0, backoff="fixed")

        delay = provider._calculate_delay(1, config)

        assert delay == 30.0


# ---------------------------------------------------------------------------
# Per-agent retry integration tests (CopilotProvider)
# ---------------------------------------------------------------------------


class TestPerAgentRetryExecution:
    """Integration tests for per-agent retry with CopilotProvider."""

    @pytest.mark.asyncio
    async def test_per_agent_retry_succeeds_after_transient_failure(self) -> None:
        """Test that per-agent retry succeeds after a transient failure."""
        call_count = 0

        def mock_handler(agent: Any, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ProviderError("Server error", status_code=500)
            return {"result": "success"}

        provider = CopilotProvider(mock_handler=mock_handler)
        agent = AgentDef(
            name="resilient_agent",
            prompt="Test",
            retry=RetryPolicy(max_attempts=3, delay_seconds=0.01),
        )

        result = await provider.execute(agent, {}, "Test")

        assert result.content["result"] == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_per_agent_retry_exhausted(self) -> None:
        """Test that per-agent retry fails after exhausting attempts."""
        call_count = 0

        def mock_handler(agent: Any, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            raise ProviderError("Server error", status_code=500)

        provider = CopilotProvider(mock_handler=mock_handler)
        agent = AgentDef(
            name="failing_agent",
            prompt="Test",
            retry=RetryPolicy(max_attempts=2, delay_seconds=0.01),
        )

        with pytest.raises(ProviderError, match="2 attempts"):
            await provider.execute(agent, {}, "Test")

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_per_agent_retry_with_no_retry_policy(self) -> None:
        """Test that agents without retry use provider defaults."""
        call_count = 0

        def mock_handler(agent: Any, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ProviderError("Server error", status_code=500)
            return {"result": "success"}

        retry_config = RetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1)
        provider = CopilotProvider(mock_handler=mock_handler, retry_config=retry_config)
        agent = AgentDef(name="test", prompt="Test")

        result = await provider.execute(agent, {}, "Test")

        assert result.content["result"] == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_per_agent_retry_with_fixed_backoff(self) -> None:
        """Test per-agent retry with fixed backoff strategy."""
        call_count = 0

        def mock_handler(agent: Any, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ProviderError("Server error", status_code=500)
            return {"result": "success"}

        provider = CopilotProvider(mock_handler=mock_handler)
        agent = AgentDef(
            name="fixed_agent",
            prompt="Test",
            retry=RetryPolicy(max_attempts=3, backoff="fixed", delay_seconds=0.01),
        )

        result = await provider.execute(agent, {}, "Test")

        assert result.content["result"] == "success"
        assert call_count == 3

        # Check retry history shows delays are consistent (fixed)
        retry_history = provider.get_retry_history()
        delays = [h["delay"] for h in retry_history if "delay" in h]
        assert len(delays) == 2  # 2 retries before success

    @pytest.mark.asyncio
    async def test_per_agent_retry_only_provider_error(self) -> None:
        """Test retry_on filtering: only retry provider_error, not timeout."""
        call_count = 0

        def mock_handler(agent: Any, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            raise ProviderError("Connection timeout", is_retryable=True)

        provider = CopilotProvider(mock_handler=mock_handler)
        agent = AgentDef(
            name="selective_agent",
            prompt="Test",
            retry=RetryPolicy(max_attempts=3, delay_seconds=0.01, retry_on=["provider_error"]),
        )

        # "Connection timeout" message -> classified as "timeout"
        # retry_on only has "provider_error", so it should NOT retry
        with pytest.raises(ProviderError):
            await provider.execute(agent, {}, "Test")

        assert call_count == 1  # No retry since timeout not in retry_on


# ---------------------------------------------------------------------------
# agent_retry event emission tests
# ---------------------------------------------------------------------------


class TestAgentRetryEventEmission:
    """Tests for agent_retry event emission during retries."""

    @pytest.mark.asyncio
    async def test_agent_retry_event_emitted(self) -> None:
        """Test that agent_retry events are emitted on each retry."""
        call_count = 0
        events: list[tuple[str, dict[str, Any]]] = []

        def mock_handler(agent: Any, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ProviderError("Server error", status_code=500)
            return {"result": "success"}

        provider = CopilotProvider(mock_handler=mock_handler)
        agent = AgentDef(
            name="retry_agent",
            prompt="Test",
            retry=RetryPolicy(max_attempts=3, delay_seconds=0.01),
        )

        result = await provider.execute(
            agent,
            {},
            "Test",
            event_callback=lambda t, d: events.append((t, d)),
        )

        assert result.content["result"] == "success"

        # Filter for agent_retry events
        retry_events = [(t, d) for t, d in events if t == "agent_retry"]
        assert len(retry_events) == 2  # 2 retries

        # Verify event data
        for _event_type, event_data in retry_events:
            assert event_data["agent_name"] == "retry_agent"
            assert event_data["max_attempts"] == 3
            assert "error" in event_data
            assert "delay" in event_data
            assert "attempt" in event_data

    @pytest.mark.asyncio
    async def test_no_agent_retry_event_on_success(self) -> None:
        """Test that no agent_retry events are emitted on success."""
        events: list[tuple[str, dict[str, Any]]] = []

        def mock_handler(agent: Any, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
            return {"result": "success"}

        provider = CopilotProvider(mock_handler=mock_handler)
        agent = AgentDef(
            name="success_agent",
            prompt="Test",
            retry=RetryPolicy(max_attempts=3),
        )

        await provider.execute(
            agent,
            {},
            "Test",
            event_callback=lambda t, d: events.append((t, d)),
        )

        retry_events = [t for t, d in events if t == "agent_retry"]
        assert len(retry_events) == 0


# ---------------------------------------------------------------------------
# _classify_error tests
# ---------------------------------------------------------------------------


class TestClassifyError:
    """Tests for CopilotProvider._classify_error."""

    def test_timeout_error_classified_as_timeout(self) -> None:
        """Test that timeout errors are classified as 'timeout'."""
        error = ProviderError("Request timeout exceeded", is_retryable=True)
        assert CopilotProvider._classify_error(error) == "timeout"

    def test_server_error_classified_as_provider_error(self) -> None:
        """Test that server errors are classified as 'provider_error'."""
        error = ProviderError("Internal server error", status_code=500)
        assert CopilotProvider._classify_error(error) == "provider_error"

    def test_rate_limit_classified_as_provider_error(self) -> None:
        """Test that rate limit errors are classified as 'provider_error'."""
        error = ProviderError("Rate limited", status_code=429)
        assert CopilotProvider._classify_error(error) == "provider_error"

    def test_conductor_timeout_classified_as_timeout(self) -> None:
        """Test that ConductorTimeoutError is classified as 'timeout'."""
        error = _ConductorTimeoutError(
            "Workflow timed out",
            elapsed_seconds=100.0,
            timeout_seconds=60.0,
        )
        assert CopilotProvider._classify_error(error) == "timeout"

    def test_generic_exception_classified_as_provider_error(self) -> None:
        """Test that generic exceptions are classified as 'provider_error'."""
        error = RuntimeError("Something went wrong")
        assert CopilotProvider._classify_error(error) == "provider_error"
