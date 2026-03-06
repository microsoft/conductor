"""Unit tests for idle detection and recovery in CopilotProvider."""

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.config.schema import AgentDef
from conductor.exceptions import ProviderError
from conductor.providers.copilot import (
    _IDLE_IGNORED_EVENTS,
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
        assert config.max_session_seconds == 1800.0
        assert "{last_activity}" in config.recovery_prompt

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=60.0,
            max_recovery_attempts=5,
            max_session_seconds=600.0,
            recovery_prompt="Custom prompt: {last_activity}",
        )
        assert config.idle_timeout_seconds == 60.0
        assert config.max_recovery_attempts == 5
        assert config.max_session_seconds == 600.0
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

    @pytest.mark.asyncio
    async def test_no_recovery_when_events_still_flowing(self) -> None:
        """Test that recovery does NOT fire when events are still flowing.

        This is the core fix for the false-positive idle detection bug:
        if the agent is actively working (tool calls, reasoning) and events
        keep arriving, we should NOT send recovery prompts even if
        session.idle hasn't fired within the timeout window.
        """
        config = IdleRecoveryConfig(
            idle_timeout_seconds=0.1,  # 100ms timeout
            max_recovery_attempts=2,
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()
        mock_session = MagicMock()
        mock_session.send = AsyncMock()

        # Simulate events flowing by continuously updating last_activity_ref
        last_activity_ref: list[Any] = ["tool.execution_start", "bash", time.monotonic()]

        async def simulate_active_session():
            """Simulate an active session by updating the timestamp every 50ms."""
            for _ in range(6):  # 6 * 50ms = 300ms total (3x the idle timeout)
                await asyncio.sleep(0.05)
                last_activity_ref[0] = "tool.execution_complete"
                last_activity_ref[1] = "bash"
                last_activity_ref[2] = time.monotonic()
            # After simulating active work, signal completion
            done.set()

        await asyncio.gather(
            provider._wait_with_idle_detection(
                done=done,
                session=mock_session,
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
            ),
            simulate_active_session(),
        )

        # No recovery messages should have been sent — events were flowing
        assert mock_session.send.call_count == 0

    @pytest.mark.asyncio
    async def test_recovery_counter_resets_between_tasks(self) -> None:
        """Test that recovery attempts reset when new activity is detected.

        Each 'task' (tool call, reasoning step) gets its own budget of
        max_recovery_attempts. If tool call #1 gets stuck and uses recovery
        attempts, then the agent resumes work (events flow), the counter
        resets so the next stuck tool call gets a fresh budget.
        """
        config = IdleRecoveryConfig(
            idle_timeout_seconds=0.05,  # 50ms timeout
            max_recovery_attempts=2,
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()
        mock_session = MagicMock()

        last_activity_ref: list[Any] = ["tool.execution_start", "tool_1", 0.0]
        send_count = [0]

        async def send_side_effect(msg: Any) -> None:
            send_count[0] += 1
            if send_count[0] == 1:
                # After first recovery for tool_1: simulate agent resuming work.
                # A background task provides events for a brief window, which
                # will cause the counter to reset when the next timeout fires.
                async def provide_events() -> None:
                    for _ in range(3):
                        await asyncio.sleep(0.02)
                        last_activity_ref[0] = "tool.execution_complete"
                        last_activity_ref[1] = "tool_1"
                        last_activity_ref[2] = time.monotonic()
                    # Events stop → tool_2 gets stuck
                    last_activity_ref[0] = "tool.execution_start"
                    last_activity_ref[1] = "tool_2"

                asyncio.create_task(provide_events())
            elif send_count[0] == 3:
                # Third recovery overall (1 for tool_1, 2 for tool_2) → done.
                # Schedule with a small delay so it takes effect AFTER
                # the done.clear() that follows session.send() in the method.
                async def finish() -> None:
                    await asyncio.sleep(0.01)
                    done.set()

                asyncio.create_task(finish())

        mock_session.send = AsyncMock(side_effect=send_side_effect)

        await provider._wait_with_idle_detection(
            done=done,
            session=mock_session,
            verbose_enabled=False,
            full_enabled=False,
            last_activity_ref=last_activity_ref,
        )

        # 3 total recovery messages sent. This is impossible without the
        # counter resetting, since max_recovery_attempts=2 would cause a
        # ProviderError on the 3rd attempt without a reset in between.
        assert mock_session.send.call_count == 3


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


class TestIdleIgnoredEvents:
    """Tests for the _IDLE_IGNORED_EVENTS constant and filtering behavior."""

    def test_ignored_events_is_frozenset(self) -> None:
        """Test that _IDLE_IGNORED_EVENTS is an immutable frozenset."""
        assert isinstance(_IDLE_IGNORED_EVENTS, frozenset)

    def test_ignored_events_contains_expected_members(self) -> None:
        """Test that all expected bookkeeping events are in the set."""
        assert "pending_messages.modified" in _IDLE_IGNORED_EVENTS
        assert "session.start" in _IDLE_IGNORED_EVENTS
        assert "session.info" in _IDLE_IGNORED_EVENTS

    def test_real_events_not_in_ignored_set(self) -> None:
        """Test that real agent-work events are NOT in the ignored set."""
        real_events = [
            "assistant.message",
            "assistant.reasoning",
            "tool.execution_start",
            "tool.execution_complete",
            "session.idle",
        ]
        for event in real_events:
            assert event not in _IDLE_IGNORED_EVENTS, f"{event} should not be ignored"


class TestSessionTimeout:
    """Tests for max_session_seconds wall-clock timeout."""

    @pytest.mark.asyncio
    async def test_session_timeout_raises_provider_error(self) -> None:
        """Test that exceeding max_session_seconds raises ProviderError."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=0.05,
            max_recovery_attempts=10,
            max_session_seconds=0.01,  # Very short — will fire quickly
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()  # Never set
        mock_session = MagicMock()
        mock_session.send = AsyncMock()

        last_activity_ref: list[Any] = [None, None, time.monotonic()]

        with pytest.raises(ProviderError) as exc_info:
            await provider._wait_with_idle_detection(
                done=done,
                session=mock_session,
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
            )

        assert "exceeded maximum duration" in str(exc_info.value)
        assert not exc_info.value.is_retryable

    @pytest.mark.asyncio
    async def test_session_timeout_includes_time_since_last_event(self) -> None:
        """Test that the timeout error includes time since last real event."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=0.05,
            max_recovery_attempts=10,
            max_session_seconds=0.01,
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()
        mock_session = MagicMock()
        mock_session.send = AsyncMock()

        last_activity_ref: list[Any] = ["tool.execution_start", "stuck_tool", time.monotonic()]

        with pytest.raises(ProviderError) as exc_info:
            await provider._wait_with_idle_detection(
                done=done,
                session=mock_session,
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
            )

        error_msg = str(exc_info.value)
        assert "stuck_tool" in error_msg
        assert "Last real event" in error_msg
        assert "ago" in error_msg

    @pytest.mark.asyncio
    async def test_session_timeout_fires_even_with_flowing_events(self) -> None:
        """Test that wall-clock timeout fires even when events keep flowing.

        This is the key distinction from idle timeout: even if non-ignored
        events keep resetting the idle clock, the hard cap still fires.
        """
        config = IdleRecoveryConfig(
            idle_timeout_seconds=0.05,  # Short — loop iterates quickly
            max_recovery_attempts=10,
            max_session_seconds=0.15,  # Short wall-clock limit
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()
        mock_session = MagicMock()
        mock_session.send = AsyncMock()

        last_activity_ref: list[Any] = ["assistant.message", None, time.monotonic()]

        # Keep updating the activity timestamp to simulate flowing events
        async def simulate_events() -> None:
            while not done.is_set():
                await asyncio.sleep(0.02)
                last_activity_ref[2] = time.monotonic()

        with pytest.raises(ProviderError) as exc_info:
            await asyncio.gather(
                provider._wait_with_idle_detection(
                    done=done,
                    session=mock_session,
                    verbose_enabled=False,
                    full_enabled=False,
                    last_activity_ref=last_activity_ref,
                ),
                simulate_events(),
            )

        assert "exceeded maximum duration" in str(exc_info.value)
        assert not exc_info.value.is_retryable

    @pytest.mark.asyncio
    async def test_session_completes_before_timeout(self) -> None:
        """Test that sessions completing before max_session_seconds are fine."""
        config = IdleRecoveryConfig(
            idle_timeout_seconds=10.0,
            max_session_seconds=10.0,  # Won't be reached
        )
        provider = CopilotProvider(
            mock_handler=stub_handler,
            idle_recovery_config=config,
        )

        done = asyncio.Event()
        mock_session = MagicMock()

        last_activity_ref: list[Any] = [None, None, time.monotonic()]

        async def complete_quickly() -> None:
            await asyncio.sleep(0.02)
            done.set()

        # Should not raise
        await asyncio.gather(
            provider._wait_with_idle_detection(
                done=done,
                session=mock_session,
                verbose_enabled=False,
                full_enabled=False,
                last_activity_ref=last_activity_ref,
            ),
            complete_quickly(),
        )


class TestStartupRace:
    """Tests for asyncio.Lock in _ensure_client_started."""

    def test_start_lock_exists(self) -> None:
        """Test that the provider has a _start_lock attribute."""
        provider = CopilotProvider(mock_handler=stub_handler)
        assert isinstance(provider._start_lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_concurrent_ensure_started_calls_start_once(self) -> None:
        """Test that concurrent _ensure_client_started calls only start once.

        Simulates the for-each / parallel group race: multiple coroutines
        all call _ensure_client_started() concurrently, but start() should
        only be invoked once.
        """
        provider = CopilotProvider(mock_handler=stub_handler)

        start_call_count = 0

        class MockClient:
            async def start(self_inner) -> None:
                nonlocal start_call_count
                start_call_count += 1
                # Simulate slow startup to widen the race window
                await asyncio.sleep(0.05)

        provider._client = MockClient()
        provider._started = False

        # Stub out _fix_pipe_blocking_mode since we don't have real pipes
        provider._fix_pipe_blocking_mode = lambda: None  # type: ignore[assignment]

        # Launch 5 concurrent calls
        await asyncio.gather(*[provider._ensure_client_started() for _ in range(5)])

        assert start_call_count == 1
        assert provider._started is True

    @pytest.mark.asyncio
    async def test_fix_pipe_blocking_mode_called_once(self) -> None:
        """Test that _fix_pipe_blocking_mode is called exactly once under concurrency."""
        provider = CopilotProvider(mock_handler=stub_handler)

        fix_pipe_count = 0

        class MockClient:
            async def start(self_inner) -> None:
                await asyncio.sleep(0.02)

        def mock_fix_pipe() -> None:
            nonlocal fix_pipe_count
            fix_pipe_count += 1

        provider._client = MockClient()
        provider._started = False
        provider._fix_pipe_blocking_mode = mock_fix_pipe  # type: ignore[assignment]

        await asyncio.gather(*[provider._ensure_client_started() for _ in range(3)])

        assert fix_pipe_count == 1
