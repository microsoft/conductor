"""Unit tests for idle detection and recovery in CopilotProvider."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import AgentDef
from conductor.exceptions import ProviderError
from conductor.providers.copilot import (
    CopilotProvider,
    IdleRecoveryConfig,
)


def stub_handler(agent: AgentDef, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
    """A simple mock handler that returns stub responses."""
    return {"result": "stub response"}


class TestIdleRecoveryConfig:
    """Tests for IdleRecoveryConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = IdleRecoveryConfig()
        assert config.idle_timeout_seconds == 300.0
        assert config.max_recovery_attempts == 3
        assert "{last_activity}" in config.recovery_prompt

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=60.0,
            max_recovery_attempts=5,
            recovery_prompt="Custom prompt: {last_activity}",
        )
        assert config.idle_timeout_seconds == 60.0
        assert config.max_recovery_attempts == 5
        assert config.recovery_prompt == "Custom prompt: {last_activity}"


class TestBuildRecoveryPrompt:
    """Tests for the _build_recovery_prompt helper method."""

    def test_with_tool_call(self) -> None:
        """Test recovery prompt when last activity was a tool call."""
        provider = CopilotProvider(mock_handler=stub_handler)
        prompt = provider._build_recovery_prompt(
            last_event_type="tool.execution_start",
            last_tool_call="web_search",
        )
        assert "executing tool 'web_search'" in prompt
        assert "gotten stuck" in prompt

    def test_with_event_type_no_tool(self) -> None:
        """Test recovery prompt when last activity was a known event type."""
        provider = CopilotProvider(mock_handler=stub_handler)
        prompt = provider._build_recovery_prompt(
            last_event_type="assistant.reasoning",
            last_tool_call=None,
        )
        assert "reasoning about the problem" in prompt

    def test_with_unknown_event_type(self) -> None:
        """Test recovery prompt with unknown event type."""
        provider = CopilotProvider(mock_handler=stub_handler)
        prompt = provider._build_recovery_prompt(
            last_event_type="unknown.event",
            last_tool_call=None,
        )
        assert "'unknown.event' event" in prompt

    def test_with_no_events(self) -> None:
        """Test recovery prompt when no events were received."""
        provider = CopilotProvider(mock_handler=stub_handler)
        prompt = provider._build_recovery_prompt(
            last_event_type=None,
            last_tool_call=None,
        )
        assert "unknown (no events received)" in prompt

    def test_custom_recovery_prompt_template(self) -> None:
        """Test that custom recovery prompt template is used."""
        config = IdleRecoveryConfig(recovery_prompt="CUSTOM: {last_activity} END")
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )
        prompt = provider._build_recovery_prompt(
            last_event_type="tool.execution_start",
            last_tool_call="calculator",
        )
        assert prompt.startswith("CUSTOM:")
        assert "executing tool 'calculator'" in prompt
        assert prompt.endswith("END")


class TestBuildStuckInfo:
    """Tests for the _build_stuck_info helper method."""

    def test_with_tool_call(self) -> None:
        """Test stuck info when last activity was a tool call."""
        provider = CopilotProvider(mock_handler=stub_handler)
        info = provider._build_stuck_info(
            last_event_type="tool.execution_start",
            last_tool_call="file_read",
        )
        assert "tool 'file_read' was executing" in info

    def test_with_event_type_no_tool(self) -> None:
        """Test stuck info when last activity was an event."""
        provider = CopilotProvider(mock_handler=stub_handler)
        info = provider._build_stuck_info(
            last_event_type="assistant.message",
            last_tool_call=None,
        )
        assert "'assistant.message' event" in info

    def test_with_no_events(self) -> None:
        """Test stuck info when no events were received."""
        provider = CopilotProvider(mock_handler=stub_handler)
        info = provider._build_stuck_info(
            last_event_type=None,
            last_tool_call=None,
        )
        assert "unknown (no events received)" in info


class TestLogRecoveryAttempt:
    """Tests for the _log_recovery_attempt helper method."""

    def test_does_not_raise(self) -> None:
        """Test that logging recovery attempt doesn't raise exceptions."""
        provider = CopilotProvider(mock_handler=stub_handler)
        # Should not raise
        provider._log_recovery_attempt(
            attempt=1,
            last_event_type="tool.execution_start",
            last_tool_call="web_search",
        )

    def test_logs_with_tool_context(self) -> None:
        """Test logging with tool context."""
        provider = CopilotProvider(mock_handler=stub_handler)
        # Should not raise
        provider._log_recovery_attempt(
            attempt=2,
            last_event_type="tool.execution_start",
            last_tool_call="calculator",
        )

    def test_logs_with_event_context(self) -> None:
        """Test logging with event context."""
        provider = CopilotProvider(mock_handler=stub_handler)
        # Should not raise
        provider._log_recovery_attempt(
            attempt=3,
            last_event_type="assistant.reasoning",
            last_tool_call=None,
        )


class TestWaitWithIdleDetection:
    """Tests for the _wait_with_idle_detection method."""

    @pytest.mark.asyncio
    async def test_completes_immediately_when_done_is_set(self) -> None:
        """Test that method completes immediately when done event is already set."""
        provider = CopilotProvider(mock_handler=stub_handler)
        done = asyncio.Event()
        done.set()

        mock_session = MagicMock()
        last_activity_ref = [None, None, 0.0]

        # Should complete immediately without timeout
        await provider._wait_with_idle_detection(
            done=done,
            session=mock_session,
            verbose_enabled=False,
            full_enabled=False,
            last_activity_ref=last_activity_ref,
        )

    @pytest.mark.asyncio
    async def test_timeout_triggers_recovery(self) -> None:
        """Test that timeout triggers a recovery message."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=0.1,  # 100ms timeout
            max_recovery_attempts=5,  # Allow more attempts
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()
        mock_session = MagicMock()
        mock_session.send = AsyncMock()

        # Set done after the first recovery attempt (wait > 1 timeout but < 2 timeouts)
        async def set_done_after_delay():
            await asyncio.sleep(0.15)  # Wait for first recovery (after 100ms timeout)
            done.set()

        last_activity_ref = ["tool.execution_start", "web_search", 0.0]

        # Run both the wait and the delayed done.set()
        await asyncio.gather(
            provider._wait_with_idle_detection(
                done=done,
                session=mock_session,
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
            ),
            set_done_after_delay(),
        )

        # Should have sent at least one recovery message
        assert mock_session.send.call_count >= 1

    @pytest.mark.asyncio
    async def test_max_recovery_attempts_exhausted(self) -> None:
        """Test that ProviderError is raised after max recovery attempts."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=0.05,  # 50ms for more reliable testing
            max_recovery_attempts=2,
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()  # Never set
        mock_session = MagicMock()
        mock_session.send = AsyncMock()

        last_activity_ref = ["tool.execution_start", "slow_tool", 0.0]

        with pytest.raises(ProviderError) as exc_info:
            await provider._wait_with_idle_detection(
                done=done,
                session=mock_session,
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
            )

        assert "stuck after 2 recovery attempts" in str(exc_info.value)
        assert "slow_tool" in str(exc_info.value)
        assert not exc_info.value.is_retryable

    @pytest.mark.asyncio
    async def test_recovery_sends_correct_prompt(self) -> None:
        """Test that recovery sends the correct prompt based on last activity."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=0.1,  # 100ms timeout
            max_recovery_attempts=3,  # Allow more attempts
            recovery_prompt="Continue from {last_activity}",
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()
        mock_session = MagicMock()
        mock_session.send = AsyncMock()

        # Set done after recovery is sent (wait > 1 timeout but < 2 timeouts)
        async def set_done_after_recovery():
            await asyncio.sleep(0.15)  # 150ms to ensure recovery happens after 100ms timeout
            done.set()

        last_activity_ref = ["tool.execution_start", "my_tool", 0.0]

        await asyncio.gather(
            provider._wait_with_idle_detection(
                done=done,
                session=mock_session,
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
            ),
            set_done_after_recovery(),
        )

        # Verify the recovery prompt contains the tool name
        call_args = mock_session.send.call_args_list[0][0][0]
        assert "my_tool" in call_args["prompt"]

    @pytest.mark.asyncio
    async def test_done_event_cleared_after_timeout(self) -> None:
        """Test that done event is cleared after each timeout to allow waiting again."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=0.05,  # 50ms timeout (increased for stability)
            max_recovery_attempts=3,
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()
        mock_session = MagicMock()
        mock_session.send = AsyncMock()

        recovery_count = 0

        async def count_recoveries_and_finish():
            nonlocal recovery_count
            # Wait for 2 recovery attempts, then set done
            while recovery_count < 2:
                await asyncio.sleep(0.07)  # Wait longer than idle timeout
                if mock_session.send.call_count > recovery_count:
                    recovery_count = mock_session.send.call_count
            done.set()

        last_activity_ref = [None, None, 0.0]

        await asyncio.gather(
            provider._wait_with_idle_detection(
                done=done,
                session=mock_session,
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
            ),
            count_recoveries_and_finish(),
        )

        # Should have sent 2 recovery messages before completing
        assert mock_session.send.call_count >= 2


class TestIdleRecoveryIntegration:
    """Integration tests for idle recovery with the full provider."""

    @pytest.mark.asyncio
    async def test_provider_accepts_idle_recovery_config(self) -> None:
        """Test that provider accepts and stores idle recovery config."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=120.0,
            max_recovery_attempts=5,
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )
        assert provider._idle_recovery_config.idle_timeout_seconds == 120.0
        assert provider._idle_recovery_config.max_recovery_attempts == 5

    @pytest.mark.asyncio
    async def test_provider_uses_default_config_when_none(self) -> None:
        """Test that provider uses default config when none provided."""
        provider = CopilotProvider(mock_handler=stub_handler)
        assert provider._idle_recovery_config.idle_timeout_seconds == 300.0
        assert provider._idle_recovery_config.max_recovery_attempts == 3


class TestActivityTracking:
    """Tests for activity tracking in event callbacks."""

    def test_activity_ref_structure(self) -> None:
        """Test the structure of the last_activity_ref list."""
        # The ref is [event_type, tool_call, timestamp]
        ref = [None, None, 0.0]
        assert len(ref) == 3
        assert ref[0] is None  # event_type
        assert ref[1] is None  # tool_call
        assert isinstance(ref[2], float)  # timestamp

    def test_activity_ref_can_be_mutated(self) -> None:
        """Test that activity ref can be mutated from a callback."""
        ref = [None, None, 0.0]

        def simulate_callback():
            ref[0] = "tool.execution_start"
            ref[1] = "web_search"
            ref[2] = 123.456

        simulate_callback()

        assert ref[0] == "tool.execution_start"
        assert ref[1] == "web_search"
        assert ref[2] == 123.456
