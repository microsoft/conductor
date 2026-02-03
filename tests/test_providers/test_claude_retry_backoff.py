"""Tests for retry logic backoff and jitter calculation.

Verifies:
- Exponential backoff calculation
- Jitter randomization
- Retry-after header handling
- Max delay capping
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.providers.claude import ClaudeProvider, RetryConfig


class TestClaudeRetryBackoff:
    """Tests for retry backoff and jitter calculations."""

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_calculate_delay_exponential_backoff(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that delay calculation uses exponential backoff."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        config = RetryConfig(base_delay=1.0, max_delay=100.0, jitter=0.0)

        # Exponential backoff: base * 2^(attempt-1)
        # Attempt 1: 1.0 * 2^0 = 1.0
        delay1 = provider._calculate_delay(1, config)
        assert delay1 == 1.0

        # Attempt 2: 1.0 * 2^1 = 2.0
        delay2 = provider._calculate_delay(2, config)
        assert delay2 == 2.0

        # Attempt 3: 1.0 * 2^2 = 4.0
        delay3 = provider._calculate_delay(3, config)
        assert delay3 == 4.0

        # Attempt 4: 1.0 * 2^3 = 8.0
        delay4 = provider._calculate_delay(4, config)
        assert delay4 == 8.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    def test_calculate_delay_max_cap(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that delay is capped at max_delay."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        config = RetryConfig(base_delay=10.0, max_delay=30.0, jitter=0.0)

        # Attempt 1: 10.0 * 2^0 = 10.0 (below max)
        delay1 = provider._calculate_delay(1, config)
        assert delay1 == 10.0

        # Attempt 2: 10.0 * 2^1 = 20.0 (below max)
        delay2 = provider._calculate_delay(2, config)
        assert delay2 == 20.0

        # Attempt 3: 10.0 * 2^2 = 40.0 -> capped to 30.0
        delay3 = provider._calculate_delay(3, config)
        assert delay3 == 30.0

        # Attempt 4: 10.0 * 2^3 = 80.0 -> capped to 30.0
        delay4 = provider._calculate_delay(4, config)
        assert delay4 == 30.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @patch("conductor.providers.claude.random.random")
    def test_calculate_delay_with_jitter(
        self,
        mock_random: Mock,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
    ) -> None:
        """Test that jitter is correctly added to delay."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        # Control random.random() to return predictable values
        mock_random.return_value = 0.5

        provider = ClaudeProvider()
        config = RetryConfig(base_delay=10.0, max_delay=100.0, jitter=0.25)

        # Attempt 1: base=10.0, jitter = 10.0 * 0.25 * 0.5 = 1.25
        # Total: 10.0 + 1.25 = 11.25
        delay1 = provider._calculate_delay(1, config)
        assert delay1 == 11.25

        # Attempt 2: base=20.0, jitter = 20.0 * 0.25 * 0.5 = 2.5
        # Total: 20.0 + 2.5 = 22.5
        delay2 = provider._calculate_delay(2, config)
        assert delay2 == 22.5

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @patch("conductor.providers.claude.random.random")
    def test_jitter_randomization_range(
        self,
        mock_random: Mock,
        mock_anthropic_module: Mock,
        mock_anthropic_class: Mock,
    ) -> None:
        """Test that jitter produces values within expected range."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))
        mock_anthropic_class.return_value = mock_client

        provider = ClaudeProvider()
        config = RetryConfig(base_delay=10.0, max_delay=100.0, jitter=0.25)

        # Test with minimum random value (0.0)
        mock_random.return_value = 0.0
        delay_min = provider._calculate_delay(1, config)
        assert delay_min == 10.0  # No jitter added

        # Test with maximum random value (1.0)
        mock_random.return_value = 1.0
        delay_max = provider._calculate_delay(1, config)
        # Jitter = 10.0 * 0.25 * 1.0 = 2.5
        assert delay_max == 12.5

        # Verify range: [base, base + base*jitter]
        assert 10.0 <= delay_min <= delay_max <= 12.5

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_retry_respects_retry_after_header(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that retry-after header overrides calculated delay."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Create a mock RateLimitError with retry-after header
        class MockRateLimitError(Exception):
            status_code = 429
            response = Mock(headers={"retry-after": "60"})

        mock_error = MockRateLimitError("Rate limit exceeded")

        # First call fails with rate limit, second succeeds
        mock_content_block = Mock()
        mock_content_block.type = "text"
        mock_content_block.text = '{"result": "Success"}'

        mock_response = Mock()
        mock_response.content = [mock_content_block]
        mock_response.usage = Mock(
            input_tokens=10, output_tokens=20, cache_creation_input_tokens=0
        )
        mock_response.model = "claude-3-5-sonnet-latest"
        mock_response.stop_reason = "end_turn"
        mock_response.id = "msg_123"
        mock_response.type = "message"
        mock_response.role = "assistant"

        mock_client.messages.create = AsyncMock(side_effect=[mock_error, mock_response])
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        # Import after patching
        from conductor.providers.claude import ClaudeProvider

        provider = ClaudeProvider(retry_config=RetryConfig(max_attempts=2))

        # Execute - should retry with delay from retry-after header
        agent = AgentDef(
            name="test_agent",
            prompt="Test",
            output={"result": OutputField(type="string")},
        )
        with patch("conductor.providers.claude.asyncio.sleep") as mock_sleep:
            await provider.execute(agent, {}, "Test")

            # Verify sleep was called with retry-after value (60s), not calculated delay
            mock_sleep.assert_called_once()
            # Should be approximately 60 (from header), not exponential backoff
            assert mock_sleep.call_args[0][0] == 60.0

    @patch("conductor.providers.claude.ANTHROPIC_SDK_AVAILABLE", True)
    @patch("conductor.providers.claude.AsyncAnthropic")
    @patch("conductor.providers.claude.anthropic")
    @pytest.mark.asyncio
    async def test_retry_history_includes_delay(
        self, mock_anthropic_module: Mock, mock_anthropic_class: Mock
    ) -> None:
        """Test that retry history records the calculated delay."""
        mock_anthropic_module.__version__ = "0.77.0"
        mock_client = Mock()
        mock_client.models.list = AsyncMock(return_value=Mock(data=[]))

        # Create a retryable error
        class MockAPIStatusError(Exception):
            status_code = 503

        mock_error = MockAPIStatusError("Service unavailable")

        # First call fails, second succeeds
        mock_content_block = Mock()
        mock_content_block.type = "text"
        mock_content_block.text = '{"result": "Success"}'

        mock_response = Mock()
        mock_response.content = [mock_content_block]
        mock_response.usage = Mock(
            input_tokens=10, output_tokens=20, cache_creation_input_tokens=0
        )
        mock_response.model = "claude-3-5-sonnet-latest"
        mock_response.stop_reason = "end_turn"
        mock_response.id = "msg_123"
        mock_response.type = "message"
        mock_response.role = "assistant"

        mock_client.messages.create = AsyncMock(side_effect=[mock_error, mock_response])
        mock_client.close = AsyncMock()
        mock_anthropic_class.return_value = mock_client

        # Import after patching
        from conductor.providers.claude import ClaudeProvider

        retry_cfg = RetryConfig(max_attempts=2, base_delay=2.0, jitter=0.0)
        provider = ClaudeProvider(retry_config=retry_cfg)

        # Execute - should retry once
        agent = AgentDef(
            name="test_agent",
            prompt="Test",
            output={"result": OutputField(type="string")},
        )
        with patch("conductor.providers.claude.asyncio.sleep"):
            await provider.execute(agent, {}, "Test")

        # Check retry history
        history = provider.get_retry_history()
        assert len(history) == 1
        assert "delay" in history[0]
        # With base_delay=2.0, jitter=0.0, first retry should be 2.0s
        assert history[0]["delay"] == 2.0
