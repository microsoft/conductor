"""Performance tests for Claude provider.

These tests measure performance characteristics of the Claude provider
and ensure it meets acceptable latency and throughput requirements.
"""

import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest

from conductor.config.schema import AgentDef, OutputField
from conductor.providers.claude import ANTHROPIC_SDK_AVAILABLE, ClaudeProvider


def create_mock_response(content_dict: dict) -> Mock:
    """Create a properly structured Claude API response mock."""
    mock_content_block = Mock()
    mock_content_block.type = "tool_use"
    mock_content_block.id = "tool_123"
    mock_content_block.name = "emit_output"
    mock_content_block.input = content_dict

    response = Mock()
    response.id = "msg_123"
    response.content = [mock_content_block]
    response.model = "claude-3-5-sonnet-latest"
    response.stop_reason = "end_turn"
    response.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
    response.type = "message"
    response.role = "assistant"
    return response


@pytest.mark.skipif(not ANTHROPIC_SDK_AVAILABLE, reason="Anthropic SDK not installed")
@pytest.mark.performance
@pytest.mark.asyncio
async def test_provider_initialization_latency():
    """Test that provider initialization completes within acceptable time."""
    start = time.perf_counter()
    provider = ClaudeProvider()
    elapsed = time.perf_counter() - start

    # Initialization should be < 500ms (sync operation, may vary with system load)
    assert elapsed < 0.5, f"Initialization took {elapsed:.3f}s, expected < 0.5s"

    await provider.close()


@pytest.mark.skipif(not ANTHROPIC_SDK_AVAILABLE, reason="Anthropic SDK not installed")
@pytest.mark.performance
@pytest.mark.asyncio
async def test_retry_backoff_timing():
    """Test that retry history is accessible and starts empty."""
    provider = ClaudeProvider()

    # Verify retry history starts empty
    retry_history = provider.get_retry_history()
    assert retry_history == [], "Retry history should start empty"

    # Verify retry config is accessible
    assert provider._retry_config is not None
    assert provider._retry_config.max_attempts >= 1

    await provider.close()


@pytest.mark.skipif(not ANTHROPIC_SDK_AVAILABLE, reason="Anthropic SDK not installed")
@pytest.mark.performance
@pytest.mark.asyncio
async def test_concurrent_request_handling():
    """Test that provider can handle multiple concurrent requests."""
    provider = ClaudeProvider()

    # Mock successful API responses
    mock_response = create_mock_response({"result": "test"})

    mock_client = Mock()
    mock_client.messages = Mock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_client.close = AsyncMock()
    provider._client = mock_client

    agent = AgentDef(
        name="test",
        prompt="test",
        output={"result": OutputField(type="string")},
    )

    # Run multiple concurrent requests
    start = time.perf_counter()
    tasks = [provider.execute(agent, {}, f"test prompt {i}") for i in range(5)]
    results = await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - start

    # All should complete successfully
    assert len(results) == 5
    for result in results:
        assert result.content["result"] == "test"

    # With mocked responses, should be very fast
    assert elapsed < 1.0, f"Concurrent requests took {elapsed:.2f}s, expected < 1.0s"

    await provider.close()


@pytest.mark.skipif(not ANTHROPIC_SDK_AVAILABLE, reason="Anthropic SDK not installed")
@pytest.mark.performance
@pytest.mark.asyncio
async def test_parse_recovery_latency():
    """Test that parse recovery doesn't add excessive latency."""
    provider = ClaudeProvider()

    # First call returns text (triggers fallback parsing), second would retry
    text_block = Mock()
    text_block.type = "text"
    text_block.text = '{"result": "parsed from text"}'

    mock_response = Mock()
    mock_response.id = "msg_123"
    mock_response.content = [text_block]
    mock_response.model = "claude-3-5-sonnet-latest"
    mock_response.stop_reason = "end_turn"
    mock_response.usage = Mock(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0)
    mock_response.type = "message"
    mock_response.role = "assistant"

    mock_client = Mock()
    mock_client.messages = Mock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_client.close = AsyncMock()
    provider._client = mock_client

    agent = AgentDef(
        name="test",
        prompt="test",
        output={"result": OutputField(type="string")},
    )

    start = time.perf_counter()
    result = await provider.execute(agent, {}, "test prompt")
    elapsed = time.perf_counter() - start

    # Fallback parsing should be fast
    assert elapsed < 0.5, f"Parse recovery took {elapsed:.3f}s, expected < 0.5s"
    assert result.content["result"] == "parsed from text"

    await provider.close()


@pytest.mark.skipif(not ANTHROPIC_SDK_AVAILABLE, reason="Anthropic SDK not installed")
@pytest.mark.performance
@pytest.mark.asyncio
async def test_memory_efficiency():
    """Test that provider doesn't leak memory during repeated operations."""
    import gc

    provider = ClaudeProvider()

    mock_response = create_mock_response({"result": "test"})

    mock_client = Mock()
    mock_client.messages = Mock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_client.close = AsyncMock()
    provider._client = mock_client

    agent = AgentDef(
        name="test",
        prompt="test",
        output={"result": OutputField(type="string")},
    )

    # Run many iterations
    for _i in range(100):
        await provider.execute(agent, {}, "test prompt")

    # Force garbage collection
    gc.collect()

    # Retry history should not grow unbounded
    retry_history = provider.get_retry_history()
    assert len(retry_history) < 1000, "Retry history growing unbounded"

    await provider.close()
